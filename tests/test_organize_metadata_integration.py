# tests/test_organize_metadata_integration.py
"""Stage-5 hook integration tests.

These exercise `_run_metadata_stage()` directly, not the full
organize() pipeline — we want to verify the hook's defensive
try/except + progress_callback wiring, not re-test stages 1-4.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _empty_catalog(target):
    from fda.organize.models import Catalog
    return Catalog(target=str(target), entries=(), git_repos_skipped=())


class TestStageHook:
    def test_metadata_crash_does_not_propagate(self, tmp_path):
        """Hook swallows exceptions; caller (organize()) keeps going."""
        from fda.organize import _run_metadata_stage
        olog = MagicMock()
        captured: list[str] = []
        with patch("fda.metadata.run", side_effect=RuntimeError("boom")):
            # Must not raise.
            _run_metadata_stage(
                target_path=tmp_path, catalog=_empty_catalog(tmp_path),
                backend=MagicMock(), olog=olog,
                progress_callback=captured.append,
            )
        # User saw a warning line.
        assert any("metadata stage failed" in m for m in captured)
        # Structured log captured the fatal.
        olog.log.assert_any_call("METADATA_FAIL_FATAL", error="boom")

    def test_happy_path_surfaces_counts(self, tmp_path):
        from fda.metadata.schema import RunReport
        from fda.organize import _run_metadata_stage
        captured: list[str] = []
        fake = RunReport(
            run_id="r-test", files_seen=3, files_classified=2,
            files_failed=1, batches_total=1, batches_retried=0,
        )
        with patch("fda.metadata.run", return_value=fake):
            _run_metadata_stage(
                target_path=tmp_path, catalog=_empty_catalog(tmp_path),
                backend=MagicMock(), olog=MagicMock(),
                progress_callback=captured.append,
            )
        joined = " ".join(captured)
        assert "metadata: 2/3" in joined
        assert "1 failed" in joined

    def test_progress_callback_none_is_safe(self, tmp_path):
        """No progress_callback wired? Hook still runs without crashing."""
        from fda.metadata.schema import RunReport
        from fda.organize import _run_metadata_stage
        fake = RunReport(run_id="r", files_seen=1, files_classified=1,
                          files_failed=0, batches_total=1, batches_retried=0)
        with patch("fda.metadata.run", return_value=fake):
            _run_metadata_stage(
                target_path=tmp_path, catalog=_empty_catalog(tmp_path),
                backend=MagicMock(), olog=MagicMock(),
                progress_callback=None,
            )


class TestCatalogPathTranslation:
    """Stage 5 must see POST-move paths. The catalog from stage 1 records
    original (pre-move) paths; if stage 5 reads those, enrich.sha256_of()
    hits ENOENT. Regression test for 2026-05-13 corpus eyeball bug.
    """

    def _make_entry(self, path: str, path_id: str = "f000", is_junk: bool = False):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id=path_id, path=path, ext=".pdf",
            size_bytes=100, summary="x", type_label="pdf",
            is_junk=is_junk, summary_failed=False, extract_status="ok",
        )

    def _make_move_outcome(self, src: str, dst: str, idx: int = 0,
                           status: str = "applied"):
        from fda.organize.models import (
            Operation, OperationKind, OperationOutcome,
        )
        op = Operation(kind=OperationKind.MOVE, source=src,
                       destination=dst, reason="x")
        return OperationOutcome(
            operation_index=idx, operation=op, status=status,
        )

    def test_applied_move_updates_entry_path(self):
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import Catalog
        src = "/root/folder_1/file.pdf"
        dst = "/root/Invoices/file.pdf"
        catalog = Catalog(target="/root",
                          entries=(self._make_entry(src),),
                          git_repos_skipped=())
        out = _translate_catalog_for_stage5(
            catalog, [self._make_move_outcome(src, dst)],
        )
        assert out.entries[0].path == dst

    def test_rescued_move_also_updates_path(self):
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import Catalog
        src = "/root/folder_1/file.pdf"
        dst = "/root/Invoices/file.pdf"
        catalog = Catalog(target="/root",
                          entries=(self._make_entry(src),),
                          git_repos_skipped=())
        out = _translate_catalog_for_stage5(
            catalog, [self._make_move_outcome(src, dst, status="rescued")],
        )
        assert out.entries[0].path == dst

    def test_failed_move_leaves_path_alone(self):
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import Catalog
        src = "/root/folder_1/file.pdf"
        dst = "/root/Invoices/file.pdf"
        catalog = Catalog(target="/root",
                          entries=(self._make_entry(src),),
                          git_repos_skipped=())
        out = _translate_catalog_for_stage5(
            catalog, [self._make_move_outcome(src, dst, status="failed")],
        )
        assert out.entries[0].path == src

    def test_skipped_move_leaves_path_alone(self):
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import Catalog
        src = "/root/folder_1/file.pdf"
        dst = "/root/Invoices/file.pdf"
        catalog = Catalog(target="/root",
                          entries=(self._make_entry(src),),
                          git_repos_skipped=())
        out = _translate_catalog_for_stage5(
            catalog, [self._make_move_outcome(src, dst, status="skipped")],
        )
        assert out.entries[0].path == src

    def test_no_matching_move_leaves_path_alone(self):
        """Entry not referenced by any move (e.g. unmoved file) keeps its path."""
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import Catalog
        catalog = Catalog(target="/root",
                          entries=(self._make_entry("/root/unmoved.pdf"),),
                          git_repos_skipped=())
        out = _translate_catalog_for_stage5(catalog, [])
        assert out.entries[0].path == "/root/unmoved.pdf"

    def test_preserves_target_and_repos_skipped(self):
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import Catalog
        catalog = Catalog(target="/root",
                          entries=(self._make_entry("/root/a.pdf"),),
                          git_repos_skipped=("/root/.git",))
        out = _translate_catalog_for_stage5(catalog, [])
        assert out.target == "/root"
        assert out.git_repos_skipped == ("/root/.git",)

    def test_create_dir_outcome_ignored(self):
        """Only MOVE outcomes update paths; CREATE_DIR is irrelevant."""
        from fda.organize import _translate_catalog_for_stage5
        from fda.organize.models import (
            Catalog, Operation, OperationKind, OperationOutcome,
        )
        catalog = Catalog(target="/root",
                          entries=(self._make_entry("/root/a.pdf"),),
                          git_repos_skipped=())
        op = Operation(kind=OperationKind.CREATE_DIR, source=None,
                       destination="/root/Invoices", reason="x")
        out = _translate_catalog_for_stage5(
            catalog,
            [OperationOutcome(operation_index=0, operation=op,
                              status="applied")],
        )
        assert out.entries[0].path == "/root/a.pdf"
