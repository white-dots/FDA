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

        p = tmp_path / "a.zzz"
        p.write_bytes(b"\x00")
        # No registration -> no_extractor
        assert ".zzz" not in _extractors.EXTRACTORS
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


# ---------------------------------------------------------------------------
# .docx — heading styles drive sections; paragraphs+tables drive text
# ---------------------------------------------------------------------------


def _build_docx(path, *, paragraphs=None, table_cells=None):
    """Build a minimal .docx at `path`.

    paragraphs: list of (text, style_name|None). style_name=None uses default.
    table_cells: list of list of strings (rows of cells), or None.
    """
    from docx import Document
    paragraphs = paragraphs or []
    doc = Document()
    for body, style in paragraphs:
        p = doc.add_paragraph(body)
        if style is not None:
            p.style = doc.styles[style]
    if table_cells:
        rows = len(table_cells)
        cols = max((len(r) for r in table_cells), default=0)
        if rows and cols:
            t = doc.add_table(rows=rows, cols=cols)
            for r, row in enumerate(table_cells):
                for c, cell_text in enumerate(row):
                    t.cell(r, c).text = cell_text
    doc.save(str(path))


class TestDocxHappyPath:
    def test_headings_become_sections_in_document_order(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "doc.docx"
        _build_docx(f, paragraphs=[
            ("Quarterly Report", "Title"),
            ("Executive Summary", "Heading 1"),
            ("Findings", "Heading 2"),
            ("body text here", None),
        ])
        r = _extractors.extract(f)
        assert r.status == "ok"
        assert r.sections == ("Quarterly Report", "Executive Summary", "Findings")
        # text contains every paragraph body, joined by newlines.
        assert "Quarterly Report" in r.text
        assert "body text here" in r.text

    def test_title_alone_captured(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "title_only.docx"
        _build_docx(f, paragraphs=[
            ("My Document", "Title"),
            ("body", None),
        ])
        r = _extractors.extract(f)
        assert r.status == "ok"
        assert r.sections == ("My Document",)


class TestDocxEdgeCases:
    def test_two_char_heading_filtered_out(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "short.docx"
        _build_docx(f, paragraphs=[
            ("Hi", "Heading 1"),     # 2 chars, below MIN
            ("Findings", "Heading 1"),
        ])
        r = _extractors.extract(f)
        assert r.sections == ("Findings",)

    def test_whitespace_only_heading_filtered_out(self, tmp_path):
        """Spec line 144: 'Whitespace-only / 2-char heading text → filtered out'.
        Pin the whitespace-only branch separately so a regression that drops
        the `" ".join(body.split())` normalization still gets caught."""
        from fda.organize import _extractors

        f = tmp_path / "ws.docx"
        _build_docx(f, paragraphs=[
            ("   \t  ", "Heading 1"),  # whitespace only -> normalizes to ""
            ("Findings", "Heading 1"),
        ])
        r = _extractors.extract(f)
        assert r.sections == ("Findings",)

    def test_max_chars_applied_after_whitespace_normalization(self, tmp_path):
        """The length guard runs AFTER `" ".join(body.split())`. A heading
        whose raw length exceeds MAX_CHARS but normalizes within bounds must
        be kept; this pins the order of operations."""
        from fda.organize import _extractors

        # Raw length 50, normalized "Quarterly Findings" = 18 chars (well within MAX=40).
        raw = "Quarterly" + (" " * 30) + "Findings"
        assert len(raw) > 40 and len(" ".join(raw.split())) <= 40
        f = tmp_path / "norm.docx"
        _build_docx(f, paragraphs=[
            (raw, "Heading 1"),
        ])
        r = _extractors.extract(f)
        assert r.sections == ("Quarterly Findings",)

    def test_41_char_heading_filtered_out(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "long.docx"
        long_label = "A" + ("b" * 40)  # 41 chars
        _build_docx(f, paragraphs=[
            (long_label, "Heading 1"),
            ("Findings", "Heading 1"),
        ])
        r = _extractors.extract(f)
        assert r.sections == ("Findings",)

    def test_duplicate_headings_deduped_in_first_occurrence_order(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "dup.docx"
        _build_docx(f, paragraphs=[
            ("Findings", "Heading 1"),
            ("Methods", "Heading 1"),
            ("Findings", "Heading 1"),  # repeat — dropped
        ])
        r = _extractors.extract(f)
        assert r.sections == ("Findings", "Methods")

    def test_capped_at_max_sections_per_file(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._sections import MAX_SECTIONS_PER_FILE

        f = tmp_path / "many.docx"
        # 20 unique headings; expect only the first MAX_SECTIONS_PER_FILE retained.
        paras = [(f"Section {i:02d}", "Heading 1") for i in range(20)]
        _build_docx(f, paragraphs=paras)
        r = _extractors.extract(f)
        assert len(r.sections) == MAX_SECTIONS_PER_FILE
        assert r.sections[0] == "Section 00"
        assert r.sections[-1] == f"Section {MAX_SECTIONS_PER_FILE - 1:02d}"

    def test_no_headings_yields_empty_sections(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "plain.docx"
        _build_docx(f, paragraphs=[
            ("just body text, no headings", None),
            ("more body", None),
        ])
        r = _extractors.extract(f)
        assert r.status == "ok"
        assert r.sections == ()
        assert "just body text" in r.text

    def test_empty_doc_yields_empty_text_and_sections(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "empty.docx"
        _build_docx(f)  # no paragraphs, no tables
        r = _extractors.extract(f)
        assert r.status == "ok"
        # python-docx always inserts an empty default paragraph whose `.text`
        # is "". The implementation does `if body: text_parts.append(body)`,
        # so the empty body is dropped and `"\n".join([])` yields "".
        assert r.text == ""
        assert r.sections == ()

    def test_table_cell_text_included_in_text(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "tables.docx"
        _build_docx(
            f,
            paragraphs=[],
            table_cells=[
                ["Invoice No", "Amount"],
                ["INV-001", "$1,234"],
            ],
        )
        r = _extractors.extract(f)
        assert r.status == "ok"
        # No heading-styled paragraphs anywhere in the doc.
        assert r.sections == ()
        # Table cell text was harvested into `text`.
        assert "Invoice No" in r.text
        assert "INV-001" in r.text
        assert "$1,234" in r.text

    def test_corrupt_bytes_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "bad.docx"
        # python-docx requires a valid .docx ZIP package; raw bytes raise
        # PackageNotFoundError, caught by extract()'s outer try/except.
        f.write_bytes(b"this is not a docx file")
        r = _extractors.extract(f)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()
