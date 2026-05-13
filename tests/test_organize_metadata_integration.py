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
