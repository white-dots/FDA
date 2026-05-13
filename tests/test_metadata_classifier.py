# tests/test_metadata_classifier.py
"""Tests for fda.metadata.classifier post-process override and single-batch call."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _record_dict(**overrides):
    base = dict(
        department="finance",
        document_type="invoice",
        confidentiality="internal",
        summary="x",
        keywords={"ko": [], "en": ["invoice"]},
        confidence=0.8,
    )
    base.update(overrides)
    return base


class TestFailClosedOverride:
    def test_override_fires_below_threshold(self):
        from fda.metadata.classifier import apply_fail_closed_override
        from fda.metadata.schema import Classification
        c = Classification(**_record_dict(confidence=0.2,
                                          confidentiality="internal"))
        out, fired = apply_fail_closed_override(c)
        assert out.confidentiality == "restricted"
        assert out.fail_closed_override is True
        assert fired is True

    def test_override_does_not_fire_above_threshold(self):
        from fda.metadata.classifier import apply_fail_closed_override
        from fda.metadata.schema import Classification
        c = Classification(**_record_dict(confidence=0.35,
                                          confidentiality="internal"))
        out, fired = apply_fail_closed_override(c)
        assert out.confidentiality == "internal"
        assert out.fail_closed_override is False
        assert fired is False

    def test_override_skipped_when_already_restricted(self):
        from fda.metadata.classifier import apply_fail_closed_override
        from fda.metadata.schema import Classification
        c = Classification(**_record_dict(confidence=0.1,
                                          confidentiality="restricted"))
        out, fired = apply_fail_closed_override(c)
        assert out.confidentiality == "restricted"
        assert out.fail_closed_override is False
        assert fired is False


class TestClassifyBatch:
    def test_single_batch_happy_path(self):
        from fda.metadata.classifier import classify_batch
        records = [_record_dict(), _record_dict(confidence=0.9)]
        backend = MagicMock()
        backend.complete.return_value = json.dumps(records)
        skill = MagicMock(body="prompt", model="claude-sonnet-4-6")
        files = [
            {"path_id": "f000", "ext": ".pdf", "language_hint": "en",
             "summary": "s1", "verbatim_head": "h1"},
            {"path_id": "f001", "ext": ".pdf", "language_hint": "en",
             "summary": "s2", "verbatim_head": "h2"},
        ]
        out = classify_batch(
            files=files, backend=backend, skill=skill, business_context="",
        )
        assert len(out) == 2
        assert out[0].department == "finance"
        assert out[1].confidence == 0.9
        backend.complete.assert_called_once()

    def test_classify_batch_raises_on_invalid_json(self):
        from fda.metadata.classifier import (
            ClassifierResponseError, classify_batch,
        )
        backend = MagicMock()
        backend.complete.return_value = "not json"
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        with pytest.raises(ClassifierResponseError):
            classify_batch(files=[{"path_id": "f000", "ext": ".pdf",
                                   "language_hint": "en", "summary": "s",
                                   "verbatim_head": "h"}],
                           backend=backend, skill=skill,
                           business_context="")

    def test_classify_batch_raises_on_count_mismatch(self):
        from fda.metadata.classifier import (
            ClassifierResponseError, classify_batch,
        )
        backend = MagicMock()
        backend.complete.return_value = json.dumps([_record_dict()])
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        with pytest.raises(ClassifierResponseError, match="count"):
            classify_batch(files=[
                {"path_id": "f000", "ext": ".pdf", "language_hint": "en",
                 "summary": "s", "verbatim_head": "h"},
                {"path_id": "f001", "ext": ".pdf", "language_hint": "en",
                 "summary": "s", "verbatim_head": "h"},
            ], backend=backend, skill=skill, business_context="")
