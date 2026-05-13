# tests/test_metadata_schema.py
"""Tests for fda.metadata.schema Pydantic models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _valid_kwargs():
    return dict(
        department="finance",
        document_type="invoice",
        confidentiality="confidential",
        summary="2025년 3분기 매출 청구서",
        keywords={"ko": ["청구서", "2025"], "en": ["invoice", "2025"]},
        confidence=0.82,
    )


class TestClassification:
    def test_accepts_valid_record(self):
        from fda.metadata.schema import Classification
        c = Classification(**_valid_kwargs())
        assert c.department == "finance"
        assert c.fail_closed_override is False

    def test_rejects_unknown_department(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["department"] = "not-a-real-department"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_unknown_document_type(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["document_type"] = "novel"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_unknown_confidentiality(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["confidentiality"] = "super-secret"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_keywords_over_eight_per_language(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["keywords"] = {"ko": [f"k{i}" for i in range(9)], "en": []}
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_unknown_keys_strict_mode(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["extra_field"] = "nope"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_confidence_must_be_in_range(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["confidence"] = 1.5
        with pytest.raises(ValidationError):
            Classification(**kw)
