# tests/test_metadata_cli.py
"""Tests for CLI surfaces (fda metadata, fda search, organize flags)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


WORKTREE = Path(__file__).resolve().parent.parent
PYTHON = str(WORKTREE / ".venv" / "bin" / "python")


def _run_cli(*args, env=None, cwd=None):
    base_env = dict(os.environ)
    if env:
        base_env.update(env)
    return subprocess.run(
        [PYTHON, "-m", "fda.cli", *args],
        capture_output=True, text=True, env=base_env,
        cwd=cwd or str(WORKTREE),
    )


class TestFlagMutualExclusion:
    def test_metadata_only_with_no_route_exits_2(self, tmp_path):
        cp = _run_cli(
            "organize", str(tmp_path), "--metadata-only", "--no-route",
        )
        assert cp.returncode == 2
        out = (cp.stderr + cp.stdout).lower()
        assert "mutually exclusive" in out

    def test_metadata_only_with_no_metadata_exits_2(self, tmp_path):
        cp = _run_cli(
            "organize", str(tmp_path), "--metadata-only", "--no-metadata",
        )
        assert cp.returncode == 2
        out = (cp.stderr + cp.stdout).lower()
        assert "mutually exclusive" in out


class TestStandaloneMetadataExit:
    def test_crash_on_missing_dir_exits_1(self, tmp_path):
        # Pointing at a non-directory path should exit 1.
        nonexistent = tmp_path / "does_not_exist"
        cp = _run_cli("metadata", str(nonexistent))
        assert cp.returncode == 1


class TestSearchKoreanDefault:
    def _seed(self, home, sha_prefix="c"):
        """Create a DB at $HOME/.fda/metadata.db with one Korean invoice row."""
        from fda.metadata.store import (
            connect, init_schema, insert_run, upsert_document, upsert_path,
        )
        from fda.metadata.schema import DocumentRow, PathRow
        (home / ".fda").mkdir(parents=True, exist_ok=True)
        conn = connect(home / ".fda" / "metadata.db")
        init_schema(conn)
        insert_run(conn, run_id="r1", target_root="/t",
                   model="claude-sonnet-4-6", fda_version="0.1.0",
                   business_context_sha256=None,
                   started_at="2026-05-13T00:00:00Z")
        upsert_document(conn, DocumentRow(
            sha256=sha_prefix * 64, mime="application/pdf", size_bytes=1,
            language="ko", department="finance", document_type="invoice",
            confidentiality="confidential", summary="청구서",
            keywords_json='{"ko":["청구서"],"en":[]}', confidence=0.9,
            fail_closed_override=False, extract_status="ok",
            sharepoint_url=None, run_id="r1",
            created_at="2026-05-13T00:00:00Z",
            updated_at="2026-05-13T00:00:00Z",
        ))
        upsert_path(conn, PathRow(path_id="f0", sha256=sha_prefix * 64,
                                   path="/t/a.pdf",
                                   mtime="2026-05-13T00:00:00.000000Z",
                                   last_seen_run="r1"))
        conn.close()

    def test_default_output_uses_korean_labels(self, tmp_path):
        self._seed(tmp_path, sha_prefix="c")
        env = {"HOME": str(tmp_path)}
        cp = _run_cli("search", "청구서", env=env)
        assert cp.returncode == 0, f"stderr: {cp.stderr}\nstdout: {cp.stdout}"
        # Default = Korean labels.
        assert "재무" in cp.stdout       # finance → 재무
        assert "대외비" in cp.stdout      # confidential → 대외비 (Task 2 fix)

    def test_english_flag_outputs_codes(self, tmp_path):
        self._seed(tmp_path, sha_prefix="d")
        env = {"HOME": str(tmp_path)}
        cp = _run_cli("search", "청구서", "--english", env=env)
        assert cp.returncode == 0, f"stderr: {cp.stderr}\nstdout: {cp.stdout}"
        assert "finance" in cp.stdout
        assert "confidential" in cp.stdout
