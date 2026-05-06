# tests/test_organize_reader.py
"""Tests for fda.organize.reader."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _summarizer_response(type_label: str = "doc", summary: str = "x"):
    """Return a `complete()` response payload that looks like our skill output.

    Uses json.dumps so the result is valid JSON (repr() would emit single
    quotes which json.loads rejects).
    """
    import json
    return json.dumps({"type_label": type_label, "summary": summary})


@pytest.fixture
def fake_backend():
    """Backend whose .complete returns a JSON summary payload."""
    backend = MagicMock()
    backend.complete.return_value = _summarizer_response()
    return backend


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def logger(tmp_path):
    from fda.organize._logger import OrganizeLogger
    return OrganizeLogger(log_path=tmp_path / "r.log", target_basename="ws")


class TestPathIdAssignment:
    def test_ids_assigned_in_path_sorted_order(self, workspace, fake_backend, logger):
        from fda.organize import reader

        (workspace / "b.txt").write_text("b")
        (workspace / "a.txt").write_text("a")
        (workspace / "c.txt").write_text("c")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        ids_paths = [(e.path_id, Path(e.path).name) for e in catalog.entries]
        assert ids_paths == [("f000", "a.txt"), ("f001", "b.txt"), ("f002", "c.txt")]
        # Happy-path assertion: at least one entry has summary_failed=False
        # so we know the JSON parsing path actually succeeded.
        assert any(not e.summary_failed for e in catalog.entries), (
            "expected at least one successful summary on happy path"
        )
        assert all(e.summary == "x" for e in catalog.entries if not e.summary_failed)

    def test_two_runs_same_tree_produce_same_ids(self, workspace, fake_backend, logger):
        from fda.organize import reader

        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("b")
        c1 = reader.read(workspace, backend=fake_backend, logger=logger)
        c2 = reader.read(workspace, backend=fake_backend, logger=logger)
        assert [e.path_id for e in c1.entries] == [e.path_id for e in c2.entries]
        assert [e.path for e in c1.entries] == [e.path for e in c2.entries]


class TestJunkFlagged:
    def test_ds_store_marked_is_junk_no_llm_call(self, workspace, fake_backend, logger):
        from fda.organize import reader

        (workspace / ".DS_Store").write_bytes(b"\x00")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        junk = [e for e in catalog.entries if e.is_junk]
        assert [Path(e.path).name for e in junk] == [".DS_Store"]
        # Backend was called only for the non-junk file
        called_paths = [
            call.kwargs.get("messages", [{}])[0].get("content", "")
            for call in fake_backend.complete.call_args_list
        ]
        assert all(".DS_Store" not in c for c in called_paths)
        assert fake_backend.complete.call_count == 1


class TestGitWorktreeSkipped:
    def test_repo_root_and_internals_excluded(self, workspace, fake_backend, logger):
        from fda.organize import reader

        repo = workspace / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "main.py").write_text("# git-tracked")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        names = [Path(e.path).name for e in catalog.entries]
        assert "main.py" not in names
        assert "a.txt" in names
        assert str(repo) in catalog.git_repos_skipped


class TestSymlinkSkipped:
    def test_file_and_dir_symlinks_excluded(self, workspace, fake_backend, logger):
        from fda.organize import reader

        outside = workspace.parent / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("leak")
        (workspace / "a.txt").write_text("a")
        link_file = workspace / "link.txt"
        link_dir = workspace / "linkdir"
        try:
            os.symlink(outside / "leak.txt", link_file)
            os.symlink(outside, link_dir)
        except OSError:
            pytest.skip("symlinks unsupported on this platform")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        names = [Path(e.path).name for e in catalog.entries]
        assert "leak.txt" not in names
        assert "link.txt" not in names
        assert "a.txt" in names


class TestTextCap:
    def test_64k_truncation_marker_in_prompt(self, workspace, fake_backend, logger):
        from fda.organize import reader

        # 2 MB text file
        big = "z" * (2 * 1024 * 1024)
        (workspace / "big.txt").write_text(big)
        reader.read(workspace, backend=fake_backend, logger=logger)
        # The first .complete call's user message should contain
        # the [TRUNCATED at 64KB] marker.
        first = fake_backend.complete.call_args_list[0]
        user = first.kwargs.get("messages")[0]["content"]
        assert "[TRUNCATED at 64KB]" in user
        # The text content sent must be <= 64KB + small overhead
        assert len(user) < 80 * 1024


class TestPerFileFailure:
    def test_backend_error_records_failed_summary(self, workspace, logger):
        from fda.organize import reader

        backend = MagicMock()
        backend.complete.side_effect = RuntimeError("boom")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=backend, logger=logger)
        e = catalog.entries[0]
        assert e.summary_failed is True
        assert e.summary == ""

    def test_unparseable_response_records_failed(self, workspace, logger):
        from fda.organize import reader

        backend = MagicMock()
        backend.complete.return_value = "not-json"
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=backend, logger=logger)
        e = catalog.entries[0]
        assert e.summary_failed is True


class TestPerFileTimeout:
    def test_backend_timeout_marks_failed(self, workspace, logger):
        """Per-file timeout enforced at the backend HTTP layer."""
        from fda.organize import reader

        backend = MagicMock()
        backend.complete.side_effect = TimeoutError("backend HTTP timeout")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=backend, logger=logger)
        e = catalog.entries[0]
        assert e.summary_failed is True


class TestDeadline:
    def test_total_cap_marks_unstarted_failed(self, workspace, logger, monkeypatch):
        from fda.organize import reader

        # 12 files. With a deadline of 0.2s, the backend mock honors `timeout`
        # so calls dispatched late get a 0-ish timeout and raise immediately.
        for i in range(12):
            (workspace / f"f{i}.txt").write_text("x")
        monkeypatch.setattr(reader, "READER_TOTAL_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(reader, "READER_WORKER_COUNT", 2)

        backend = MagicMock()

        def slow(*, timeout=None, **kwargs):
            # Simulate a 0.5s HTTP call honoring the request timeout.
            if timeout is None or timeout >= 0.5:
                time.sleep(0.5)
                return _summarizer_response()
            time.sleep(max(0.0, timeout))
            raise TimeoutError("HTTP request deadline exceeded")

        backend.complete.side_effect = slow
        catalog = reader.read(workspace, backend=backend, logger=logger)
        failed = [e for e in catalog.entries if e.summary_failed]
        assert len(failed) >= 1, "expected at least one entry to fail by deadline"


class TestExtractorRegistryUsed:
    def test_register_runtime_extension(self, workspace, fake_backend, logger):
        from fda.organize import reader, _extractors
        from fda.organize.models import ExtractionResult

        (workspace / "a.foo").write_bytes(b"binary")

        def fake(_path):
            return ExtractionResult(text="hello-foo", status="ok")

        with patch.dict(_extractors.EXTRACTORS, {".foo": fake}, clear=False):
            reader.read(workspace, backend=fake_backend, logger=logger)
        first = fake_backend.complete.call_args_list[0]
        assert "hello-foo" in first.kwargs.get("messages")[0]["content"]


class TestNoDenylistShortCircuit:
    def test_register_for_exe_works(self, workspace, fake_backend, logger):
        """A format previously in _BINARY_EXTS is no longer denylisted."""
        from fda.organize import reader, _extractors
        from fda.organize.models import ExtractionResult

        (workspace / "a.exe").write_bytes(b"\x00\x01")

        def fake(_path):
            return ExtractionResult(text="exe-text", status="ok")

        with patch.dict(_extractors.EXTRACTORS, {".exe": fake}, clear=False):
            reader.read(workspace, backend=fake_backend, logger=logger)
        first = fake_backend.complete.call_args_list[0]
        assert "exe-text" in first.kwargs.get("messages")[0]["content"]


class TestNoExtractorStub:
    def test_unknown_extension_sends_stub(self, workspace, fake_backend, logger):
        from fda.organize import reader

        (workspace / "a.zzz").write_bytes(b"\x00")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.extract_status == "no_extractor"
        # Backend was called and given a stub describing the file.
        first = fake_backend.complete.call_args_list[0]
        msg = first.kwargs.get("messages")[0]["content"]
        assert "no_extractor" in msg or "binary" in msg or "stub" in msg


class TestSkillLoaderUnit:
    def test_load_skill_parses_frontmatter(self, tmp_path):
        from fda.organize import _skills

        skill_dir = tmp_path / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo\n"
            "description: a demo\n"
            "model: claude-haiku-4-5-20251001\n"
            "---\n"
            "Body here.\n"
        )
        cfg = _skills.load_skill(skill_dir)
        assert cfg.name == "demo"
        assert cfg.model == "claude-haiku-4-5-20251001"
        assert "Body here." in cfg.body

    def test_missing_model_raises(self, tmp_path):
        from fda.organize import _skills

        skill_dir = tmp_path / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")
        with pytest.raises(ValueError):
            _skills.load_skill(skill_dir)
