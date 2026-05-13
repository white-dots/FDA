# tests/test_metadata_retry.py
"""Tests for retry + bisect policy in fda.metadata.classifier."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _record(**kw):
    base = dict(
        department="finance", document_type="invoice",
        confidentiality="internal", summary="x",
        keywords={"ko": [], "en": []}, confidence=0.8,
    )
    base.update(kw)
    return base


def _files(n):
    return [
        {"path_id": f"f{i:03d}", "ext": ".pdf", "language_hint": "en",
         "summary": "s", "verbatim_head": "h"}
        for i in range(n)
    ]


class TestRetryThenBisect:
    def test_first_call_succeeds_no_retry(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        backend.complete.return_value = json.dumps([_record() for _ in range(3)])
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(3), backend=backend, skill=skill,
            business_context="",
        )
        assert len(result.records_by_path_id) == 3
        assert result.batches_retried == 0
        assert result.failed_path_ids == []
        assert backend.complete.call_count == 1

    def test_first_invalid_then_retry_succeeds(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        backend.complete.side_effect = [
            "not json",                                  # attempt 1
            json.dumps([_record() for _ in range(3)]),   # retry succeeds
        ]
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(3), backend=backend, skill=skill,
            business_context="",
        )
        assert len(result.records_by_path_id) == 3
        assert result.batches_retried == 1
        assert backend.complete.call_count == 2

    def test_two_failures_bisect_into_halves(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        # 10 files; both top-level calls fail; bisect into [0..4] + [5..9].
        # Each half succeeds on first attempt of its sub-batch.
        good_half = json.dumps([_record() for _ in range(5)])
        backend = MagicMock()
        backend.complete.side_effect = [
            "not json", "not json",   # top-level + its retry
            good_half,                # left half first attempt
            good_half,                # right half first attempt
        ]
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(10), backend=backend, skill=skill,
            business_context="",
        )
        assert len(result.records_by_path_id) == 10
        # 1 retry counted at the top-level; sub-batches succeeded first try
        assert result.batches_retried == 1
        assert backend.complete.call_count == 4
        assert result.batches_total == 3   # top-level + left half + right half

    def test_single_file_double_failure_marks_failed(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        backend.complete.side_effect = ["bad", "bad"]   # both attempts fail
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(1), backend=backend, skill=skill,
            business_context="",
        )
        assert result.records_by_path_id == {}
        assert result.failed_path_ids == ["f000"]
        assert backend.complete.call_count == 2

    def test_empty_files_returns_empty_result(self):
        """Regression: an empty files list must not infinite-recurse on
        double failure. The guard at the top of _bisect short-circuits.
        """
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=[], backend=backend, skill=skill, business_context="",
        )
        assert result.records_by_path_id == {}
        assert result.failed_path_ids == []
        assert result.batches_total == 0
        assert result.batches_retried == 0
        # No LLM call wasted on an empty batch.
        assert backend.complete.call_count == 0
