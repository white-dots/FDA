# tests/test_metadata_context.py
"""Tests for fda.metadata.context (business_context.md loader)."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestLoadContext:
    def test_missing_file_returns_empty_text_and_none_sha(self, tmp_path):
        from fda.metadata.context import load_business_context
        path = tmp_path / "does_not_exist.md"
        result = load_business_context(path)
        assert result.text == ""
        assert result.sha256 is None
        assert result.was_truncated is False
        assert "no business_context.md" in (result.info_message or "").lower()

    def test_normal_file_loads_full_content(self, tmp_path):
        from fda.metadata.context import load_business_context
        path = tmp_path / "bc.md"
        path.write_text("## Departments\n- 영업 (sales)\n", encoding="utf-8")
        result = load_business_context(path)
        assert "영업" in result.text
        assert result.sha256 is not None
        assert len(result.sha256) == 64
        assert result.was_truncated is False

    def test_oversize_file_truncates_at_50kb_and_warns(self, tmp_path):
        from fda.metadata.context import load_business_context, MAX_BYTES
        path = tmp_path / "bc.md"
        big = "x" * (MAX_BYTES + 1000)
        path.write_text(big, encoding="utf-8")
        result = load_business_context(path)
        assert result.was_truncated is True
        assert len(result.text.encode("utf-8")) <= MAX_BYTES
        assert result.sha256 is not None
        assert "truncated" in (result.info_message or "").lower()
