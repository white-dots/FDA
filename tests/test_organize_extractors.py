# tests/test_organize_extractors.py
"""Tests for fda.organize._extractors."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


class TestPlainText:
    def test_txt_file_returns_ok(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.txt"
        p.write_text("hello")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == "hello"

    @pytest.mark.parametrize("ext", [".md", ".csv", ".log", ".json", ".xml"])
    def test_other_text_extensions_route_to_read_text(self, tmp_path, ext):
        from fda.organize import _extractors

        p = tmp_path / f"a{ext}"
        p.write_text("hi")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == "hi"

class TestUnknownExtension:
    def test_returns_no_extractor(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.xyz"
        p.write_bytes(b"\x00\x01")
        r = _extractors.extract(p)
        assert r.status == "no_extractor"
        assert r.text is None
        assert r.sections == ()


class TestPdf:
    def test_happy_path(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF-fake")
        with patch.object(_extractors, "_run_pdftotext", return_value=b"PDF body"):
            with patch("fda.organize._extractors._which", return_value="/usr/bin/pdftotext"):
                r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == "PDF body"

    def test_pdftotext_missing_returns_tool_missing(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF-fake")
        with patch("fda.organize._extractors._which", return_value=None):
            r = _extractors.extract(p)
        assert r.status == "tool_missing"
        assert r.text is None
        assert "pdftotext" in r.note

    def test_pdftotext_exit_nonzero_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF-fake")
        with patch("fda.organize._extractors._which", return_value="/usr/bin/pdftotext"):
            with patch.object(_extractors, "_run_pdftotext",
                              side_effect=RuntimeError("exit 1")):
                r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_pipe_cap_applied_internally(self, tmp_path):
        """The PDF subprocess cap is the extractor's own memory-safety
        measure, separate from Reader's contract."""
        from fda.organize import _extractors

        big = b"x" * (_extractors._PDF_PIPE_CAP_BYTES + 4096)
        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF-fake")
        with patch("fda.organize._extractors._which", return_value="/usr/bin/pdftotext"):
            with patch.object(_extractors, "_run_pdftotext", return_value=big):
                r = _extractors.extract(p)
        assert r.status == "ok"
        # Internal cap means the extractor returns at most _PDF_PIPE_CAP_BYTES
        # of decoded text, not the unbounded subprocess output.
        assert len(r.text) <= _extractors._PDF_PIPE_CAP_BYTES


class TestExtractorFailureIsolation:
    def test_arbitrary_exception_caught(self, tmp_path):
        from fda.organize import _extractors

        def boom(_path):
            raise RuntimeError("bad extractor")

        p = tmp_path / "a.zzz"
        p.write_text("data")
        with patch.dict(_extractors.EXTRACTORS, {".zzz": boom}, clear=False):
            r = _extractors.extract(p)
        assert r.status == "failed"
        assert "bad extractor" in r.note
        assert r.sections == ()


class TestRegistryAdditions:
    def test_register_new_extension_at_runtime(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize.models import ExtractionResult

        def fake_docx(_path):
            return ExtractionResult(text="docx body", status="ok")

        p = tmp_path / "a.docx"
        p.write_bytes(b"\x00")
        with patch.dict(_extractors.EXTRACTORS, {".docx": fake_docx}, clear=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == "docx body"

    def test_unregistered_extension_falls_back(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.docx"
        p.write_bytes(b"\x00")
        # No registration -> no_extractor
        assert ".docx" not in _extractors.EXTRACTORS
        r = _extractors.extract(p)
        assert r.status == "no_extractor"


# ---------------------------------------------------------------------------
# Section extraction is wired into the text-based extractors
# ---------------------------------------------------------------------------


class TestSectionsWiredIntoExtractors:
    def test_read_text_populates_sections(self, tmp_path):
        from fda.organize._extractors import _read_text

        f = tmp_path / "doc.txt"
        f.write_text(
            "Order ID: 10488\n"
            "\n"
            "Shipping Details:\n"
            "ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "Hanna Moos\n"
        )
        result = _read_text(f)
        assert result.status == "ok"
        assert result.sections == ("Shipping Details", "Customer Details")

    def test_read_text_empty_file_yields_empty_sections(self, tmp_path):
        from fda.organize._extractors import _read_text

        f = tmp_path / "empty.txt"
        f.write_text("")
        result = _read_text(f)
        assert result.status == "ok"
        assert result.sections == ()

    @pytest.mark.parametrize(
        "filename, body",
        [
            (
                "data.csv",
                "customer_id,order_date,amount,status\n"
                "1,2024-01-01,100.00,paid\n"
                "2,2024-01-02,200.00,pending\n",
            ),
            (
                "data.json",
                '{"customer_id": 1, "order_date": "2024-01-01", '
                '"amount": 100.0, "status": "paid"}\n',
            ),
            (
                "data.xml",
                "<orders>\n"
                "  <order id=\"1\"><amount>100.00</amount></order>\n"
                "  <order id=\"2\"><amount>200.00</amount></order>\n"
                "</orders>\n",
            ),
        ],
    )
    def test_v1_structured_text_with_no_colon_headers_yields_empty_sections(
        self, tmp_path, filename, body
    ):
        """Pin v1 structured-text behavior: typical CSV/JSON/XML content has
        no colon-only-on-line headers and no ALL-CAPS dividers, so the regex
        helper returns (). Format-native column-header / element-name
        extraction is v2 (see spec's Future format coverage)."""
        from fda.organize._extractors import _read_text

        f = tmp_path / filename
        f.write_text(body)
        result = _read_text(f)
        assert result.status == "ok"
        assert result.sections == ()

    def test_extract_pdf_text_populates_sections(self, tmp_path, monkeypatch):
        """Mock pdftotext to emit a known structural document; assert
        sections are populated from its text."""
        from fda.organize import _extractors
        from fda.organize._extractors import _extract_pdf_text

        # Make _which return a real-looking path so the early bailout
        # doesn't trip; stub _run_pdftotext to return the canned text.
        monkeypatch.setattr(_extractors, "_which", lambda name: "/usr/bin/pdftotext")
        canned = (
            "Order ID: 10488\n"
            "\n"
            "Shipping Details:\n"
            "ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "Hanna Moos\n"
        ).encode("utf-8")
        monkeypatch.setattr(_extractors, "_run_pdftotext", lambda path: canned)

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\n%fake\n")
        result = _extract_pdf_text(f)
        assert result.status == "ok"
        assert result.sections == ("Shipping Details", "Customer Details")

    def test_failed_pdf_extraction_leaves_sections_empty(self, tmp_path, monkeypatch):
        """When pdftotext is missing, ExtractionResult.status is
        'tool_missing' and sections stays ()."""
        from fda.organize import _extractors
        from fda.organize._extractors import _extract_pdf_text

        monkeypatch.setattr(_extractors, "_which", lambda name: None)

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\n%fake\n")
        result = _extract_pdf_text(f)
        assert result.status == "tool_missing"
        assert result.sections == ()
