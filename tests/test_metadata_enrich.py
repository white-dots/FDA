# tests/test_metadata_enrich.py
"""Tests for fda.metadata.enrich."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestSha256:
    def test_hash_is_deterministic(self, tmp_path):
        from fda.metadata.enrich import sha256_of
        p = tmp_path / "a.txt"
        p.write_bytes(b"hello world")
        h1 = sha256_of(p)
        h2 = sha256_of(p)
        assert h1 == h2
        assert len(h1) == 64
        assert all(c in "0123456789abcdef" for c in h1)

    def test_different_content_yields_different_hash(self, tmp_path):
        from fda.metadata.enrich import sha256_of
        a = tmp_path / "a.txt"; a.write_bytes(b"AAA")
        b = tmp_path / "b.txt"; b.write_bytes(b"BBB")
        assert sha256_of(a) != sha256_of(b)


class TestMime:
    def test_detects_pdf_by_extension(self, tmp_path):
        from fda.metadata.enrich import mime_of
        p = tmp_path / "doc.pdf"; p.write_bytes(b"%PDF-1.4\n")
        assert mime_of(p) == "application/pdf"

    def test_detects_text_for_unknown_extension(self, tmp_path):
        from fda.metadata.enrich import mime_of
        p = tmp_path / "doc.unknownext"; p.write_text("hello", encoding="utf-8")
        mime = mime_of(p)
        assert isinstance(mime, str) and mime != ""


class TestLanguage:
    def test_detects_korean_when_hangul_present(self):
        from fda.metadata.enrich import language_of
        assert language_of("안녕하세요 매출 보고서입니다") == "ko"

    def test_detects_english_when_no_hangul(self):
        from fda.metadata.enrich import language_of
        assert language_of("This is an invoice for Q3 2025.") == "en"

    def test_detects_korean_for_mixed_text_with_any_hangul(self):
        from fda.metadata.enrich import language_of
        # "Korean wins" — any hangul flips to ko in the simple heuristic.
        assert language_of("Invoice 청구서") == "ko"

    def test_empty_text_returns_unknown(self):
        from fda.metadata.enrich import language_of
        assert language_of("") == "unknown"


class TestMtime:
    def test_returns_iso_8601_utc(self, tmp_path):
        from fda.metadata.enrich import mtime_iso
        p = tmp_path / "a.txt"; p.write_text("x")
        s = mtime_iso(p)
        # Format: "2026-05-13T..." ending with "Z"
        assert s.endswith("Z")
        assert "T" in s
        assert s[:4].isdigit()
