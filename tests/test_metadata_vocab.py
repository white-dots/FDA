# tests/test_metadata_vocab.py
"""Tests for fda.metadata.vocab."""
from __future__ import annotations

import pytest


class TestVocab:
    def test_department_codes_are_stable_lowercase_english(self):
        from fda.metadata.vocab import DEPARTMENTS
        assert "sales" in DEPARTMENTS
        assert "finance" in DEPARTMENTS
        assert "hr" in DEPARTMENTS
        assert "production" in DEPARTMENTS
        assert "rd" in DEPARTMENTS
        assert "legal" in DEPARTMENTS
        assert "operations" in DEPARTMENTS
        assert "marketing" in DEPARTMENTS
        assert "executive" in DEPARTMENTS
        assert "unknown" in DEPARTMENTS

    def test_document_type_codes(self):
        from fda.metadata.vocab import DOCUMENT_TYPES
        assert {"invoice", "contract", "report", "proposal", "memo",
                "policy", "presentation", "spreadsheet", "image", "data",
                "archive", "correspondence", "unknown"}.issubset(set(DOCUMENT_TYPES))

    def test_confidentiality_codes(self):
        from fda.metadata.vocab import CONFIDENTIALITY
        assert set(CONFIDENTIALITY) == {"public", "internal", "confidential",
                                         "restricted"}

    def test_korean_labels_resolve_for_every_department_code(self):
        from fda.metadata.vocab import DEPARTMENTS, ko_label_for_department
        for code in DEPARTMENTS:
            label = ko_label_for_department(code)
            assert isinstance(label, str) and len(label) > 0

    def test_korean_label_for_unknown_code_falls_back_to_english(self):
        from fda.metadata.vocab import ko_label_for_department
        assert ko_label_for_department("not-a-real-code") == "not-a-real-code"
