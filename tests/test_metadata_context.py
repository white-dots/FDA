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

    def test_oversize_korean_file_truncates_on_utf8_boundary(self, tmp_path):
        """Korean characters are 3 bytes in UTF-8. Naive byte truncation
        at MAX_BYTES could split a codepoint mid-character. The truncator
        must back off to a valid UTF-8 boundary, leaving a decodable
        string — otherwise the prompt sent to Sonnet would contain a
        broken Korean char and produce confusing output.
        """
        from fda.metadata.context import load_business_context, MAX_BYTES
        path = tmp_path / "bc_ko.md"
        # Construct a Korean string that overflows MAX_BYTES.
        # '한' (U+D55C) is 3 bytes in UTF-8. ceil(MAX_BYTES/3) + 100 chars
        # guarantees overflow.
        n_chars = (MAX_BYTES // 3) + 100
        path.write_text("한" * n_chars, encoding="utf-8")
        result = load_business_context(path)

        assert result.was_truncated is True
        # The truncated text MUST be valid UTF-8 — round-trip through
        # encode/decode and ensure no UnicodeDecodeError.
        encoded = result.text.encode("utf-8")
        assert len(encoded) <= MAX_BYTES
        # Re-decode must succeed (proves we landed on a codepoint boundary).
        round_tripped = encoded.decode("utf-8")
        assert round_tripped == result.text
        # Every char must still be '한'; no partial / replacement chars.
        assert set(result.text) == {"한"}
