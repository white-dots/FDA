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

    @pytest.mark.parametrize("ext", [".md", ".log", ".json", ".xml"])
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


# ---------------------------------------------------------------------------
# .xlsx — sheet names + row-1 headers + synthesized purpose labels
# ---------------------------------------------------------------------------


def _build_xlsx(path, sheets):
    """Build a minimal .xlsx at `path`.

    sheets: list of (name, list-of-row-tuples). First entry replaces the
    default sheet so we don't end up with a stray "Sheet" tab.
    """
    import openpyxl
    wb = openpyxl.Workbook()
    default = wb.active
    if not sheets:
        wb.save(str(path))
        wb.close()
        return
    name, rows = sheets[0]
    default.title = name
    for row in rows:
        default.append(list(row))
    for name, rows in sheets[1:]:
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(list(row))
    wb.save(str(path))
    wb.close()


class TestXlsxSchema:
    def test_single_sheet_with_headers(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "orders.xlsx"
        _build_xlsx(f, [("Orders", [
            ("Order ID", "Customer", "Amount"),
            (1, "ACME", 100),
        ])])
        r = _extractors.extract(f)
        assert r.status == "ok"
        assert r.sections[:4] == ("Sheet:Orders", "Order ID", "Customer", "Amount")

    def test_multi_sheet_workbook(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "multi.xlsx"
        _build_xlsx(f, [
            ("Orders", [("Order ID", "Customer")]),
            ("Customers", [("Email", "Phone")]),
        ])
        r = _extractors.extract(f)
        assert r.status == "ok"
        # Workbook is well under MAX_SECTIONS_PER_FILE; assert the exact tuple
        # so every header (Customer, Phone) is pinned, not just a subset.
        assert r.sections == (
            "Sheet:Orders", "Order ID", "Customer",
            "Sheet:Customers", "Email", "Phone",
        )

    def test_duplicate_headers_across_sheets_deduped_in_first_seen_order(self, tmp_path):
        """Spec line 92: ordered-set dedupe on schema labels. Two sheets
        sharing a column name must only emit it once, in first-seen order."""
        from fda.organize import _extractors

        f = tmp_path / "dup_headers.xlsx"
        _build_xlsx(f, [
            ("S1", [("Customer", "Amount")]),
            ("S2", [("Customer", "Region")]),  # "Customer" repeated
        ])
        r = _extractors.extract(f)
        assert r.sections == (
            "Sheet:S1", "Customer", "Amount",
            "Sheet:S2", "Region",
        )

    def test_empty_cells_in_row1_skipped(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "sparse.xlsx"
        # Use valid-length headers (>= SECTION_HEADER_MIN_CHARS=3); "A"/"C"
        # would be silently dropped by the length guard.
        _build_xlsx(f, [("Sheet1", [("Alpha", None, "Charlie")])])
        r = _extractors.extract(f)
        assert r.sections == ("Sheet:Sheet1", "Alpha", "Charlie")

    def test_short_row1_value_filtered_by_min_chars(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "short_hdr.xlsx"
        _build_xlsx(f, [("Sheet1", [("A", "Customer")])])
        r = _extractors.extract(f)
        # "A" is below SECTION_HEADER_MIN_CHARS (3); dropped.
        assert "A" not in r.sections
        assert "Customer" in r.sections

    def test_long_row1_value_filtered_by_max_chars(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "long_hdr.xlsx"
        long_hdr = "x" * 41  # exceeds SECTION_HEADER_MAX_CHARS
        _build_xlsx(f, [("Sheet1", [(long_hdr, "Customer")])])
        r = _extractors.extract(f)
        assert long_hdr not in r.sections
        assert "Customer" in r.sections

    def test_sheet_with_no_row1_content(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "empty_sheet.xlsx"
        _build_xlsx(f, [("Solo", [])])
        r = _extractors.extract(f)
        assert r.status == "ok"
        assert r.sections == ("Sheet:Solo",)

    def test_hidden_sheet_included(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "hidden.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Visible"
        ws.append(["A", "B"])
        ws2 = wb.create_sheet("Secret")
        ws2.sheet_state = "hidden"
        ws2.append(["X", "Y"])
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        assert "Sheet:Visible" in r.sections
        assert "Sheet:Secret" in r.sections

    def test_corrupt_xlsx_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "bad.xlsx"
        f.write_bytes(b"not an xlsx")
        r = _extractors.extract(f)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_sheet_iteration_failure_fails_whole_file(self, tmp_path, monkeypatch):
        """Spec line 109: 'Sheet-level exception during iteration → propagates
        to outer try/except; one bad sheet fails the whole file rather than
        producing partial state.' Patch openpyxl.load_workbook so its returned
        Workbook's iter_rows raises mid-walk; assert extract() catches it via
        its outer try/except and returns status='failed' with empty sections."""
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "boom.xlsx"
        _build_xlsx(f, [("Orders", [("A", "B"), (1, 2)])])

        original_load = openpyxl.load_workbook

        def boom_iter_rows(*_args, **_kwargs):
            raise RuntimeError("simulated sheet iteration failure")

        def patched_load(path, **kwargs):
            wb = original_load(path, **kwargs)
            for ws in wb.worksheets:
                ws.iter_rows = boom_iter_rows
            return wb

        monkeypatch.setattr(openpyxl, "load_workbook", patched_load)
        r = _extractors.extract(f)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_workbook_with_only_empty_sheets(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "all_empty.xlsx"
        _build_xlsx(f, [("S1", []), ("S2", [])])
        r = _extractors.extract(f)
        assert r.status == "ok"
        assert "Sheet:S1" in r.sections
        assert "Sheet:S2" in r.sections
        # text contains the Sheet: <name> banners even when sheets are empty.
        assert "Sheet: S1" in r.text
        assert "Sheet: S2" in r.text


class TestXlsxText:
    def test_text_contains_tab_joined_rows(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "rows.xlsx"
        _build_xlsx(f, [("Orders", [
            ("Order ID", "Customer", "Amount"),
            (1, "ACME", 100),
            (2, "Globex", 200),
        ])])
        r = _extractors.extract(f)
        assert "Sheet: Orders" in r.text
        assert "Order ID\tCustomer\tAmount" in r.text
        assert "1\tACME\t100" in r.text
        assert "2\tGlobex\t200" in r.text

    def test_text_caps_rows_per_sheet(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "tall.xlsx"
        # 50 rows; cap is 20.
        rows = [(f"row{i}",) for i in range(50)]
        _build_xlsx(f, [("Tall", rows)])
        r = _extractors.extract(f)
        assert "row0" in r.text
        assert "row19" in r.text
        assert "row20" not in r.text

    def test_text_caps_cols_per_row(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "wide.xlsx"
        # 50 columns. _XLSX_TEXT_COLS_PER_ROW is 32.
        wide_row = tuple(f"c{i}" for i in range(50))
        _build_xlsx(f, [("Wide", [wide_row])])
        r = _extractors.extract(f)
        assert "c0" in r.text
        assert "c31" in r.text
        # Anything beyond column 31 (0-indexed) is dropped.
        assert "c32" not in r.text
        assert "c49" not in r.text

    def test_trailing_empty_cells_trimmed(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "trail.xlsx"
        # Row "A,B,None,None,None" — trailing Nones should be trimmed.
        _build_xlsx(f, [("S", [("A", "B", None, None, None)])])
        r = _extractors.extract(f)
        # The serialized row is "A\tB\n", not "A\tB\t\t\t\n".
        # Use \n to make the assertion robust to other rows being empty.
        assert "A\tB\n" in r.text
        # Negative: no run of trailing tabs on this row.
        for line in r.text.splitlines():
            if "A\tB" in line:
                assert not line.endswith("\t")

    def test_blank_line_between_sheets(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "two.xlsx"
        _build_xlsx(f, [
            ("S1", [("a",)]),
            ("S2", [("b",)]),
        ])
        r = _extractors.extract(f)
        # Each sheet's serialization ends with a "\n" separator after the
        # last row, producing a literal blank line ("\n\n") before the next
        # sheet's banner. Pin that — order alone wouldn't catch a regression
        # that drops the trailing "\n".
        assert "\n\nSheet: S2\n" in r.text
        assert r.text.index("Sheet: S1") < r.text.index("Sheet: S2")


class TestXlsxSynthesizedLabels:
    def test_formula_heavy_appended_above_threshold(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "calc.xlsx"
        # 5 non-empty cells, 1 formula → 1/5 = 0.20 > 0.05 → FormulaHeavy.
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Calc"
        ws["A1"] = 1
        ws["A2"] = 2
        ws["A3"] = 3
        ws["A4"] = 4
        ws["A5"] = "=SUM(A1:A4)"
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        assert "FormulaHeavy" in r.sections
        # Tail position: synthesized labels come last.
        assert r.sections[-1] == "FormulaHeavy"

    def test_formula_heavy_not_appended_at_or_below_threshold(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "data.xlsx"
        # 20 non-empty cells, 1 formula → 1/20 = 0.05 NOT > 0.05 → not appended.
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        for i in range(19):
            ws.cell(row=i + 1, column=1, value=i)
        ws.cell(row=20, column=1, value="=SUM(A1:A19)")
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        assert "FormulaHeavy" not in r.sections

    def test_formula_heavy_zero_guard(self, tmp_path):
        from fda.organize import _extractors

        f = tmp_path / "totally_empty.xlsx"
        _build_xlsx(f, [("Empty", [])])
        r = _extractors.extract(f)
        # non_empty_cells == 0 → not formula-heavy by definition.
        assert "FormulaHeavy" not in r.sections

    def test_merged_cells_appended_at_or_above_min(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "merged.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "M"
        ws["A1"] = "x"
        ws.merge_cells("B1:C1")
        ws.merge_cells("B2:C2")
        ws.merge_cells("B3:C3")
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        assert "MergedCells" in r.sections

    def test_merged_cells_not_appended_below_min(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "two_merged.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "M"
        ws["A1"] = "x"
        ws.merge_cells("B1:C1")
        ws.merge_cells("B2:C2")
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        assert "MergedCells" not in r.sections

    def test_synthesized_in_tail_after_schema_cap(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl
        from fda.organize._sections import MAX_SECTIONS_PER_FILE

        f = tmp_path / "wide_with_formulas.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Wide"
        # 20 unique header columns in row 1 — more than the schema cap.
        headers = [f"Header{i:02d}" for i in range(20)]
        ws.append(headers)
        # Trigger FormulaHeavy. With 20 header cells already non-empty, we
        # need formula_count / non_empty > 0.05. Use enough formulas that
        # the ratio comfortably exceeds the threshold even after counting
        # all 20 headers as non-empty: 5 formulas / 25 total = 0.20 > 0.05.
        ws["A2"] = "=SUM(A1)"
        ws["B2"] = "=SUM(B1)"
        ws["C2"] = "=SUM(C1)"
        ws["D2"] = "=SUM(D1)"
        ws["E2"] = "=SUM(E1)"
        # Trigger MergedCells (>= _XLSX_MERGED_CELLS_MIN = 3).
        ws.merge_cells("F2:G2")
        ws.merge_cells("F3:G3")
        ws.merge_cells("F4:G4")
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        # Both synthesized labels triggered → two reserved tail slots.
        assert r.sections[-2:] == ("FormulaHeavy", "MergedCells")
        assert len(r.sections) == MAX_SECTIONS_PER_FILE
        # Schema portion was truncated to MAX_SECTIONS_PER_FILE - 2 = 13.
        schema_portion = r.sections[:-2]
        assert len(schema_portion) == MAX_SECTIONS_PER_FILE - 2


class TestXlsxFormulaFallback:
    def test_cached_none_falls_back_to_formula_string(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "spec.xlsx"
        # openpyxl-saved formulas have cached value None on pass 1.
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Calc"
        ws["A1"] = 10
        ws["A2"] = 20
        ws["A3"] = "=A1+A2"
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        # The formula cell appears in `text` as the formula string, not as
        # an empty cell.
        assert "=A1+A2" in r.text
        # The "=" prefix is single, not double — defensive against
        # implementations that incorrectly do f"={cell.value}" when
        # cell.value already starts with "=".
        assert "==A1+A2" not in r.text

    def test_trailing_empty_trim_does_not_swallow_formula_fallback(self, tmp_path):
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "trail_formula.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "S"
        ws["A1"] = "x"
        ws["B1"] = "=A1"  # cached None on pass 1; substituted from pass 2
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        # The substituted formula is non-empty, so the trailing-trim must
        # leave it in.
        assert "=A1" in r.text

    def test_formula_fallback_aligned_with_sparse_row_layout(self, tmp_path):
        """Pin that pass-1's `enumerate(start=1)` row index matches pass-2's
        `cell.row`/`cell.column` when leading rows are entirely empty.
        Read-only-mode `iter_rows(max_row=N)` returns N rows including
        empty ones, so the index alignment must hold without offset bugs."""
        from fda.organize import _extractors
        import openpyxl

        f = tmp_path / "sparse_formula.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sparse"
        # Leave rows 1-9 entirely empty; only B10 has a formula.
        ws["B10"] = "=1+1"
        wb.save(str(f))
        wb.close()

        r = _extractors.extract(f)
        # The formula string must appear in `text` — i.e., the fallback
        # lookup at (sheet="Sparse", row=10, col=2) succeeds.
        assert "=1+1" in r.text


# ---------------------------------------------------------------------------
# .pptx — slide-title placeholders drive sections; banner+shapes+notes drive text
# ---------------------------------------------------------------------------


def _build_pptx(path, *, slides):
    """Build a minimal .pptx at `path`.

    `slides` is a list of dicts:
      {"layout": int (slide_layouts index, default 0=Title Slide),
       "title": str|None,
       "body_shapes": list[str]|None,
       "notes": str|None}

    `notes` is set ONLY when a string is provided — touching `slide.notes_slide`
    has a creation side effect, so leaving it None must NOT touch it.
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    for spec in slides:
        layout_idx = spec.get("layout", 0)
        slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])
        if spec.get("title") is not None and slide.shapes.title is not None:
            slide.shapes.title.text = spec["title"]
        for body_text in spec.get("body_shapes") or []:
            tx = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
            tx.text_frame.text = body_text
        notes = spec.get("notes")
        if notes is not None:  # only touch notes_slide if explicitly requested
            slide.notes_slide.notes_text_frame.text = notes
    prs.save(str(path))


class TestPptxSections:
    def test_single_slide_with_title_yields_one_section(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.pptx"
        _build_pptx(p, slides=[{"title": "Quarterly Review"}])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("Quarterly Review",)

    def test_multi_slide_titles_preserved_in_order(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "deck.pptx"
        _build_pptx(p, slides=[
            {"title": "Intro"},
            {"title": "Results"},
            {"title": "Next Steps"},
        ])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("Intro", "Results", "Next Steps")

    def test_duplicate_titles_deduped(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "dups.pptx"
        _build_pptx(p, slides=[
            {"title": "Agenda"},
            {"title": "Agenda"},
            {"title": "Wrap"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Agenda", "Wrap")

    def test_slide_without_title_placeholder_contributes_no_section(self, tmp_path):
        """layout 6 (Blank) has no title placeholder; sections should skip it."""
        from fda.organize import _extractors

        p = tmp_path / "blank.pptx"
        _build_pptx(p, slides=[
            {"layout": 0, "title": "Has Title"},
            {"layout": 6, "title": None},
            {"layout": 0, "title": "After Blank"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Has Title", "After Blank")

    def test_two_char_title_filtered_by_min_chars(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "short.pptx"
        _build_pptx(p, slides=[{"title": "OK"}, {"title": "Real Title"}])
        r = _extractors.extract(p)
        assert r.sections == ("Real Title",)
        # Banner must NOT include the guard-failing title (spec: "If title
        # fails the length guard, append `Slide N:\n` with no title").
        assert "Slide 1: OK" not in r.text
        assert "Slide 1:\n" in r.text

    def test_long_title_filtered_by_max_chars(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "long.pptx"
        # 41 chars > SECTION_HEADER_MAX_CHARS (40)
        long_title = "x" * 41
        _build_pptx(p, slides=[
            {"title": long_title},
            {"title": "Kept"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Kept",)
        # Banner must NOT include the guard-failing title.
        # NOTE: the title TEXT may still appear via the shape walk that visits
        # the title placeholder (Task 4 spec: "both are acceptable"), so we
        # only assert the banner-form specifically — mirrors the sibling
        # test_two_char_title_filtered_by_min_chars pattern.
        assert f"Slide 1: {long_title}" not in r.text
        assert "Slide 1:\n" in r.text

    def test_whitespace_only_title_filtered(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "ws.pptx"
        _build_pptx(p, slides=[
            {"title": "   "},
            {"title": "Real"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Real",)

    def test_more_than_max_sections_capped(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._sections import MAX_SECTIONS_PER_FILE

        p = tmp_path / "many.pptx"
        _build_pptx(p, slides=[
            {"title": f"Slide{i:02d}"} for i in range(MAX_SECTIONS_PER_FILE + 5)
        ])
        r = _extractors.extract(p)
        assert len(r.sections) == MAX_SECTIONS_PER_FILE
        assert r.sections[0] == "Slide00"
        assert r.sections[-1] == f"Slide{MAX_SECTIONS_PER_FILE - 1:02d}"

    def test_empty_deck_yields_empty_sections(self, tmp_path):
        from fda.organize import _extractors
        from pptx import Presentation

        p = tmp_path / "empty.pptx"
        Presentation().save(str(p))
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ()
        assert r.text == ""


class TestPptxText:
    def test_banner_includes_slide_index_and_title(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "banner.pptx"
        _build_pptx(p, slides=[
            {"title": "Intro"},
            {"title": "Results"},
        ])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Slide 1: Intro" in r.text
        assert "Slide 2: Results" in r.text

    def test_no_title_banner_has_no_title_suffix(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "blank.pptx"
        _build_pptx(p, slides=[{"layout": 6, "title": None}])
        r = _extractors.extract(p)
        # Banner is exactly "Slide 1:\n" with no title suffix.
        assert "Slide 1:\n" in r.text
        assert "Slide 1: " not in r.text  # no trailing-space title form

    def test_shape_text_included_in_text(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "shapes.pptx"
        _build_pptx(p, slides=[
            {"title": "Header", "body_shapes": ["Bullet point one", "Bullet point two"]},
        ])
        r = _extractors.extract(p)
        assert "Bullet point one" in r.text
        assert "Bullet point two" in r.text

    def test_title_shape_appears_only_in_banner_or_shapes_not_omitted(self, tmp_path):
        """The title appears in the banner, and may also appear when shape
        iteration visits the title placeholder. Both are acceptable per spec
        (the banner makes slide order legible; the shape pass keeps iteration
        uniform). Assert presence, not exact count."""
        from fda.organize import _extractors

        p = tmp_path / "title_only.pptx"
        _build_pptx(p, slides=[{"title": "OnlyTitle"}])
        r = _extractors.extract(p)
        assert "OnlyTitle" in r.text


class TestPptxNotes:
    def test_notes_present_appended_to_text(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "notes.pptx"
        _build_pptx(p, slides=[
            {"title": "T", "notes": "Speaker context here."},
        ])
        r = _extractors.extract(p)
        assert "Notes: Speaker context here." in r.text

    def test_notes_truncated_at_cap(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _PPTX_NOTES_CHARS_PER_SLIDE_MAX

        long_notes = "x" * (_PPTX_NOTES_CHARS_PER_SLIDE_MAX + 200)
        p = tmp_path / "long_notes.pptx"
        _build_pptx(p, slides=[{"title": "T", "notes": long_notes}])
        r = _extractors.extract(p)
        # The full long_notes string should not appear; the truncated prefix should.
        truncated = "Notes: " + ("x" * _PPTX_NOTES_CHARS_PER_SLIDE_MAX)
        assert truncated in r.text
        assert long_notes not in r.text

    def test_no_notes_does_not_access_notes_slide(self, tmp_path, monkeypatch):
        """Critical: extracting from a deck with no notes must NOT touch
        slide.notes_slide at all — accessing it has a creation side effect.

        The extractor does not save the in-memory presentation back to disk,
        so a round-trip read-back wouldn't catch the violation. Instead,
        monkeypatch Slide.notes_slide to raise on access. If the extractor
        accidentally accesses it (i.e., violates the has_notes_slide gate),
        the property will raise and bubble up as status='failed'."""
        from fda.organize import _extractors
        from pptx.slide import Slide

        p = tmp_path / "no_notes.pptx"
        _build_pptx(p, slides=[{"title": "T"}])  # no `notes` key

        def _no_access(self):
            raise AssertionError(
                "extractor accessed slide.notes_slide on a no-notes slide "
                "without gating on has_notes_slide"
            )

        monkeypatch.setattr(Slide, "notes_slide", property(_no_access))

        r = _extractors.extract(p)
        # Status must be ok — the extractor walked the slide without ever
        # touching the patched property. If the extractor accessed it,
        # AssertionError is caught by extract()'s outer try/except and the
        # violation message ends up in r.note.
        assert r.status == "ok", (
            f"extractor accessed notes_slide without guard: r.note={r.note!r}"
        )
        assert "Notes:" not in r.text

    def test_whitespace_only_notes_skipped(self, tmp_path):
        """PowerPoint commonly creates notes slides containing only a stray
        newline; those must NOT produce a spurious 'Notes:' line."""
        from fda.organize import _extractors

        p = tmp_path / "ws_notes.pptx"
        _build_pptx(p, slides=[{"title": "T", "notes": "\n"}])
        r = _extractors.extract(p)
        assert "Notes:" not in r.text

    def test_notes_text_frame_none_skipped(self, tmp_path, monkeypatch):
        """Even when has_notes_slide is True, notes_text_frame can be None
        (notes placeholder removed from the notes-slide layout). The extractor
        must None-check before reading .text."""
        from fda.organize import _extractors
        from pptx import Presentation

        p = tmp_path / "tf_none.pptx"
        _build_pptx(p, slides=[{"title": "T", "notes": "real notes"}])

        # Patch NotesSlide.notes_text_frame to None at the class level so the
        # extractor sees the None case without us having to manipulate XML.
        from pptx.slide import NotesSlide
        monkeypatch.setattr(
            NotesSlide, "notes_text_frame", property(lambda self: None)
        )

        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Notes:" not in r.text


class TestPptxCaps:
    def test_slides_beyond_cap_not_in_text(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _PPTX_SLIDES_MAX

        p = tmp_path / "many_slides.pptx"
        # Build _PPTX_SLIDES_MAX + 3 slides, each with a unique body shape.
        slide_specs = [
            {"title": None, "layout": 6, "body_shapes": [f"BODY_{i}"]}
            for i in range(_PPTX_SLIDES_MAX + 3)
        ]
        _build_pptx(p, slides=slide_specs)

        r = _extractors.extract(p)
        assert r.status == "ok"
        # Slides 1.._PPTX_SLIDES_MAX serialized; slides beyond are not.
        assert f"BODY_{_PPTX_SLIDES_MAX - 1}" in r.text
        assert f"BODY_{_PPTX_SLIDES_MAX}" not in r.text
        assert f"BODY_{_PPTX_SLIDES_MAX + 2}" not in r.text

    def test_shapes_beyond_cap_not_in_text(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _PPTX_SHAPES_PER_SLIDE_MAX

        p = tmp_path / "many_shapes.pptx"
        # One slide with > _PPTX_SHAPES_PER_SLIDE_MAX text shapes.
        bodies = [f"SHAPE_{i}" for i in range(_PPTX_SHAPES_PER_SLIDE_MAX + 5)]
        _build_pptx(p, slides=[{"layout": 6, "title": None, "body_shapes": bodies}])

        r = _extractors.extract(p)
        assert r.status == "ok"
        # The shape iteration index is bounded by _PPTX_SHAPES_PER_SLIDE_MAX,
        # but the shape iterator may include the title placeholder etc., so
        # we cannot assert exactly which late shapes are dropped — just that
        # *some* late shapes are dropped and *some* early shapes are kept.
        kept = sum(1 for i in range(_PPTX_SHAPES_PER_SLIDE_MAX + 5) if f"SHAPE_{i}" in r.text)
        assert kept < _PPTX_SHAPES_PER_SLIDE_MAX + 5
        assert "SHAPE_0" in r.text


class TestPptxFailure:
    def test_corrupt_pptx_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "broken.pptx"
        p.write_bytes(b"not a real pptx")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()


# ---------------------------------------------------------------------------
# .csv — column headers drive sections; tab-normalized grid drives text
# ---------------------------------------------------------------------------


class TestCsvSections:
    def test_simple_ascii_csv_yields_row1_headers(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.csv"
        p.write_text(
            "customer_id,order_date,amount\n"
            "1,2024-01-01,100.00\n"
            "2,2024-01-02,200.00\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("customer_id", "order_date", "amount")

    def test_duplicate_headers_deduped(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "dups.csv"
        # Use 5+-char headers so they pass SECTION_HEADER_MIN_CHARS=3 length
        # guard; "id" (2 chars) would otherwise be dropped before dedupe.
        p.write_text("order,order,name\n1,1,alice\n")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("order", "name")

    def test_text_is_tab_normalized_grid(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "grid.csv"
        p.write_text(
            "customer_id,order_date\n"
            "1,2024-01-01\n"
        )
        r = _extractors.extract(p)
        # delimiter normalized to tab in serialized output
        assert "customer_id\torder_date\n" in r.text
        assert "1\t2024-01-01\n" in r.text


class TestCsvEncoding:
    def test_utf8_bom_stripped_from_first_header_and_text(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "bom.csv"
        # ﻿ is the UTF-8 BOM; utf-8-sig must strip it from the first
        # header label so the section is "name", not "﻿name".
        p.write_bytes("﻿name,age\nalice,30\n".encode("utf-8"))
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age")
        assert "﻿" not in r.text

    def test_utf8_korean_headers(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "ko_utf8.csv"
        p.write_text("이름,나이,도시\n홍길동,30,서울\n", encoding="utf-8")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "이름" in r.sections
        assert "나이" in r.sections
        assert "도시" in r.sections

    def test_cp949_korean_headers_fall_back(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "ko_cp949.csv"
        # Excel-Korean exports default to cp949; bytes do NOT decode under utf-8.
        p.write_bytes("이름,나이,도시\n홍길동,30,서울\n".encode("cp949"))
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "이름" in r.sections
        assert "나이" in r.sections
        assert "도시" in r.sections

    def test_undecodable_bytes_yield_failed_status(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "bad.csv"
        # 0xff sequences that decode under neither utf-8-sig nor cp949.
        p.write_bytes(b"\xff\xfe\xff\xfe\xff\xfe\xff\xfe")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()


class TestCsvDelimiter:
    def test_semicolon_delimited_eu_locale(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "eu.csv"
        # Excel-EU locale exports use ';'. Multiple data rows so the sniffer
        # has signal to pick ';' over ','.
        p.write_text(
            "name;age;city\n"
            "alice;30;Paris\n"
            "bob;25;Berlin\n"
            "carol;35;Madrid\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age", "city")
        # Delimiter normalized to tab in serialized output.
        assert "name\tage\tcity\n" in r.text
        assert ";" not in r.text

    def test_tab_delimited_csv_filename(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "tabbed.csv"
        # Mis-named TSV: content uses tabs, filename ends in .csv.
        p.write_text(
            "name\tage\tcity\n"
            "alice\t30\tParis\n"
            "bob\t25\tBerlin\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age", "city")

    def test_pipe_delimited(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "piped.csv"
        p.write_text(
            "name|age|city\n"
            "alice|30|Paris\n"
            "bob|25|Berlin\n"
            "carol|35|Madrid\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age", "city")

    def test_single_column_yields_no_header_label(self, tmp_path):
        """v1 limitation: single-column CSVs fail rule (1) "≥ 2 non-empty cells"
        in the fallback scan, so they always land as NoHeader. Documented in
        the spec's Non-goals.

        has_header is mocked to False here to keep the test deterministic
        regardless of stdlib heuristic mood — the v1 limitation only applies
        when the fallback scan path runs."""
        from fda.organize import _extractors
        from unittest.mock import patch

        p = tmp_path / "single.csv"
        p.write_text("name\nalice\nbob\ncarol\n")
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("NoHeader",)

    def test_sniff_csv_error_falls_back_to_comma(self, tmp_path):
        """When csv.Sniffer.sniff() raises csv.Error (degenerate sample),
        the implementation must fall back to csv.excel (comma) and parse
        the file as a regular comma CSV — covers the explicit failure mode
        in spec 'Step 2 — sniff the delimiter'."""
        from fda.organize import _extractors
        from unittest.mock import patch

        p = tmp_path / "sniff_fail.csv"
        p.write_text(
            "name,age,city\n"
            "alice,30,Paris\n"
            "bob,25,Berlin\n"
        )
        with patch(
            "csv.Sniffer.sniff",
            side_effect=__import__("csv").Error("could not determine delimiter"),
        ):
            r = _extractors.extract(p)
        assert r.status == "ok"
        # Comma-fallback parsed the headers correctly.
        assert r.sections == ("name", "age", "city")


class TestCsvHeaderDetection:
    def test_preamble_row_rejected_by_cell_count_mismatch(self, tmp_path):
        """Free-form preamble row has different cell count than data rows;
        rule (5) rejects it; row 2 qualifies and becomes the header.

        has_header is mocked False to deterministically exercise the fallback
        scan path — the spec's rule (5) only runs in that branch."""
        from fda.organize import _extractors
        from unittest.mock import patch

        p = tmp_path / "preamble.csv"
        # Row 1 has 1 cell ("Customer Export 2024-Q3"). Rows 2-4 have 5 cells.
        p.write_text(
            "Customer Export 2024-Q3\n"
            "name,age,city,plan,status\n"
            "alice,30,Paris,gold,active\n"
            "bob,25,Berlin,silver,active\n"
            "carol,35,Madrid,gold,churned\n"
        )
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age", "city", "plan", "status")

    def test_multi_row_preamble_skipped(self, tmp_path):
        """Three-line preamble of varying widths followed by a real header
        and data — the scan window finds row 4 as the qualifying header."""
        from fda.organize import _extractors
        from unittest.mock import patch

        p = tmp_path / "multi_preamble.csv"
        p.write_text(
            "Generated 2024-12-01\n"
            "Confidential\n"
            "Source: warehouse\n"
            "name,age,city\n"
            "alice,30,Paris\n"
            "bob,25,Berlin\n"
        )
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age", "city")

    def test_all_numeric_rows_yield_no_header_label(self, tmp_path):
        """Pure-numeric rows fail rule (3) "no pure numbers"; no qualifying
        scan row → NoHeader."""
        from fda.organize import _extractors

        p = tmp_path / "numbers.csv"
        # Force has_header=False heuristic by mocking it: pure-numeric
        # samples can occasionally fool the sniffer, so pin it deterministic.
        from unittest.mock import patch
        p.write_text("1,2,3\n4,5,6\n7,8,9\n")
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("NoHeader",)

    def test_garbage_row1_then_numeric_rows_yields_no_header_label(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "garbage.csv"
        from unittest.mock import patch
        # Row 1 is garbage 1-cell; rows 2+ are numeric. No row qualifies.
        p.write_text("---\n1,2,3\n4,5,6\n")
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("NoHeader",)

    def test_numeric_headers_under_has_header_true_land_normally(self, tmp_path):
        """When has_header=True, row 1 is trusted unconditionally — rule (3)
        "no pure numbers" only applies to the fallback scan. Year-as-header
        layouts (`2024,2025,2026`) must not be filtered."""
        from fda.organize import _extractors

        p = tmp_path / "years.csv"
        from unittest.mock import patch
        p.write_text("2024,2025,2026\n100,200,300\n400,500,600\n")
        with patch("csv.Sniffer.has_header", return_value=True):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("2024", "2025", "2026")

    def test_single_row_file_qualifies_under_rule5_no_following_row(self, tmp_path):
        """Rule (5) accepts a candidate row when there is no following row
        (R is the final row in the file)."""
        from fda.organize import _extractors

        p = tmp_path / "single_row.csv"
        from unittest.mock import patch
        p.write_text("name,age,city\n")
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age", "city")

    def test_duplicate_cells_in_scan_row_rejected(self, tmp_path):
        """Rule (4) rejects rows with duplicate non-empty cells under the
        fallback scan — duplicates don't look like a real header."""
        from fda.organize import _extractors

        p = tmp_path / "dup_scan.csv"
        from unittest.mock import patch
        # Row 1: duplicate cells reject under rule 4. Row 2: distinct
        # column-header-shaped cells qualify.
        p.write_text(
            "name,name,name\n"
            "alpha,beta,gamma\n"
            "1,2,3\n"
            "4,5,6\n"
        )
        with patch("csv.Sniffer.has_header", return_value=False):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("alpha", "beta", "gamma")

    def test_has_header_csv_error_falls_back_to_scan(self, tmp_path):
        """When csv.Sniffer.has_header() raises csv.Error, the implementation
        treats it as False and runs the fallback scan — covers the explicit
        failure mode in spec 'Step 3 — find the header row'."""
        from fda.organize import _extractors
        from unittest.mock import patch

        p = tmp_path / "has_header_fail.csv"
        # Preamble row + valid header row + data so the fallback scan can
        # find row 2 via rule (5) once it runs.
        p.write_text(
            "Customer Export 2024-Q3\n"
            "name,age,city\n"
            "alice,30,Paris\n"
            "bob,25,Berlin\n"
        )
        with patch(
            "csv.Sniffer.has_header",
            side_effect=__import__("csv").Error("could not determine"),
        ):
            r = _extractors.extract(p)
        assert r.status == "ok"
        # Fallback scan ran; row 2 qualified.
        assert r.sections == ("name", "age", "city")


class TestCsvSectionsPostProcessing:
    def test_short_cells_filtered_by_min_chars(self, tmp_path):
        """Cells with stripped length < SECTION_HEADER_MIN_CHARS are dropped.
        When all cells fail, sections fall back to NoHeader."""
        from fda.organize import _extractors
        from unittest.mock import patch

        p = tmp_path / "short.csv"
        # 1- and 2-char cells; trust row 1 via has_header=True so we exercise
        # the length-guard branch on a header row, not the scan rule (2).
        p.write_text("a,b,c,d\n1,2,3,4\n")
        with patch("csv.Sniffer.has_header", return_value=True):
            r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("NoHeader",)

    def test_long_cells_filtered_by_max_chars(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "long.csv"
        # 41 chars > SECTION_HEADER_MAX_CHARS (40)
        long_label = "x" * 41
        p.write_text(f"{long_label},name,age\n1,alice,30\n2,bob,25\n")
        r = _extractors.extract(p)
        assert r.status == "ok"
        # The 41-char label is dropped; survivors land in order.
        assert r.sections == ("name", "age")

    def test_mixed_valid_and_short_cells(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "mixed.csv"
        # `a` (1 char) and `bb` (2 chars) drop; `name` and `email` survive.
        p.write_text(
            "a,name,bb,email\n"
            "1,alice,x,alice@example.com\n"
            "2,bob,y,bob@example.com\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "email")

    def test_more_than_max_distinct_headers_capped(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._sections import MAX_SECTIONS_PER_FILE

        p = tmp_path / "many.csv"
        # MAX + 5 distinct headers, all length-guard valid (4-char names).
        headers = [f"col{i:02d}" for i in range(MAX_SECTIONS_PER_FILE + 5)]
        p.write_text(
            ",".join(headers) + "\n"
            + ",".join(["1"] * len(headers)) + "\n"
            + ",".join(["2"] * len(headers)) + "\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert len(r.sections) == MAX_SECTIONS_PER_FILE
        assert r.sections[0] == "col00"
        assert r.sections[-1] == f"col{MAX_SECTIONS_PER_FILE - 1:02d}"

    def test_header_named_NoHeader_does_not_duplicate(self, tmp_path):
        """If a real header column is literally named "NoHeader", the
        synthesized fallback path is not reached (other columns make sections
        non-empty), and ordered-set dedupe ensures no duplication regardless."""
        from fda.organize import _extractors

        p = tmp_path / "shadow.csv"
        p.write_text(
            "NoHeader,customer_id,amount\n"
            "x,1,100\n"
            "y,2,200\n"
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # "NoHeader" appears at most once.
        assert r.sections.count("NoHeader") <= 1
        # And the other valid headers are present.
        assert "customer_id" in r.sections
        assert "amount" in r.sections


class TestCsvText:
    def test_more_than_max_rows_capped(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _CSV_TEXT_ROWS_MAX

        p = tmp_path / "many_rows.csv"
        # Header + (CAP + 5) data rows.
        body = "name,age\n" + "".join(
            f"row{i:03d},{i}\n" for i in range(_CSV_TEXT_ROWS_MAX + 5)
        )
        p.write_text(body)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # Rows 0 .. _CSV_TEXT_ROWS_MAX-1 in serialized text (header counts as row 0).
        # We expect the header row + (_CSV_TEXT_ROWS_MAX - 1) data rows.
        assert "name\tage\n" in r.text
        assert f"row{_CSV_TEXT_ROWS_MAX - 2:03d}" in r.text  # last included data row
        assert f"row{_CSV_TEXT_ROWS_MAX - 1:03d}" not in r.text  # first dropped
        assert f"row{_CSV_TEXT_ROWS_MAX + 4:03d}" not in r.text  # well past cap

    def test_more_than_max_cols_per_row_capped(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _CSV_TEXT_COLS_PER_ROW

        p = tmp_path / "wide.csv"
        n_cols = _CSV_TEXT_COLS_PER_ROW + 5
        headers = [f"c{i:02d}" for i in range(n_cols)]
        values = [f"v{i:02d}" for i in range(n_cols)]
        p.write_text(",".join(headers) + "\n" + ",".join(values) + "\n")
        r = _extractors.extract(p)
        assert r.status == "ok"
        # Each text row contains _CSV_TEXT_COLS_PER_ROW tab-separated cells.
        first_text_row = r.text.splitlines()[0]
        assert first_text_row.count("\t") == _CSV_TEXT_COLS_PER_ROW - 1
        assert f"c{_CSV_TEXT_COLS_PER_ROW - 1:02d}" in first_text_row
        assert f"c{_CSV_TEXT_COLS_PER_ROW:02d}" not in first_text_row

    def test_quoted_embedded_newline_yields_one_record(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "embedded_nl.csv"
        # cell-A contains an embedded newline; csv.reader yields ONE row.
        p.write_text(
            'description,name\n'
            '"line1\nline2",alice\n'
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # The embedded newline is preserved inside the cell — first text row
        # is "description\tname\n", second is the multi-line cell joined with name.
        assert "description\tname\n" in r.text
        # The "line1\nline2" cell is one record; its content is preserved verbatim.
        assert "line1\nline2" in r.text

    def test_quoted_delimiter_inside_cell_preserved(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "quoted_delim.csv"
        p.write_text(
            'pair,name\n'
            '"a,b",alice\n'
            '"c,d",bob\n'
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # The "a,b" cell remains a single cell; the comma inside survives.
        assert "a,b\talice" in r.text
        assert "c,d\tbob" in r.text

    def test_trailing_comma_preserves_empty_trailing_cell(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "trail.csv"
        # Header with trailing comma → 3 cells, last empty.
        p.write_text("a,b,\n1,2,3\n4,5,6\n")
        r = _extractors.extract(p)
        assert r.status == "ok"
        # First text row has two tab separators (3 cells, last empty).
        first_text_row = r.text.splitlines()[0]
        assert first_text_row.count("\t") == 2
        assert first_text_row.endswith("\t") or first_text_row.endswith("\t ") or first_text_row == "a\tb\t"

    def test_crlf_line_endings_handled(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "crlf.csv"
        # csv.reader handles both CRLF and LF transparently.
        p.write_bytes(b"name,age\r\nalice,30\r\nbob,25\r\n")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age")
        assert "name\tage\n" in r.text
        assert "alice\t30\n" in r.text


class TestCsvEdgeCases:
    def test_zero_byte_file(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "empty.csv"
        p.write_bytes(b"")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == ""
        assert r.sections == ()

    def test_whitespace_only_file_yields_no_header_label(self, tmp_path):
        """A file whose decoded content is only whitespace short-circuits to
        text="" and sections=("NoHeader",) before csv.reader is invoked
        (spec: 'Decoded but parses to zero rows (whitespace-only)')."""
        from fda.organize import _extractors

        p = tmp_path / "ws.csv"
        p.write_text("   \n\n   \n")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == ""
        assert r.sections == ("NoHeader",)

    def test_nul_byte_yields_failed_status(self, tmp_path):
        """csv.reader raises csv.Error('line contains NUL') mid-iteration;
        outer extract() catches → status='failed'."""
        from fda.organize import _extractors

        p = tmp_path / "nul.csv"
        p.write_bytes(b"name,age\nalice,30\n\x00bob,25\n")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_file_larger_than_read_cap_uses_prefix(self, tmp_path):
        """Files > _CSV_READ_BYTES_MAX are silently truncated at the
        extractor; the prefix is processed. No failure."""
        from fda.organize import _extractors
        from fda.organize._extractors import _CSV_READ_BYTES_MAX

        p = tmp_path / "huge.csv"
        # Header in the prefix; pad with trailing data rows past the cap.
        prefix = "name,age\n"
        # Build a body whose total length exceeds _CSV_READ_BYTES_MAX.
        body_row = "alice,30\n"
        n_rows = (_CSV_READ_BYTES_MAX // len(body_row)) + 100
        p.write_text(prefix + body_row * n_rows)
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("name", "age")
        assert "alice\t30\n" in r.text


# ---------------------------------------------------------------------------
# .hwpx — XML in zip (OWPML / TTAK.OT-10.0203)
# ---------------------------------------------------------------------------

# Namespace URIs observed across HWPX revisions. Synthetic fixtures cover
# all three families to exercise local-name matching.
_HWPX_NS_2011 = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_HWPX_NS_2016 = "http://www.hancom.co.kr/hwpml/2016/paragraph"
_HWPX_NS_2021 = "http://www.owpml.org/owpml/2021/paragraph"


def _build_hwpx(
    tmp_path,
    name: str = "doc.hwpx",
    sections: list[list[list[str]]] | None = None,
    namespace: str = _HWPX_NS_2011,
    mimetype: bytes = b"application/hwp+zip",
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """Build a minimal valid .hwpx archive on disk and return its path.

    `sections` is a list of section files; each section file is a list of
    paragraphs; each paragraph is a list of run texts (the <hp:t> contents).
    Default: one section, one paragraph, one run "[발주서]".

    Note: there is no `encrypted=True` parameter — Python's zipfile.writestr
    resets ZipInfo.flag_bits in _open_to_write, so setting flag_bits before
    writestr does not produce an encrypted entry. The encrypted-flag test
    monkeypatches ZipFile.infolist instead.
    """
    import zipfile
    from xml.etree import ElementTree as ET

    if sections is None:
        sections = [[["[발주서]"]]]

    p = tmp_path / name
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # mimetype is conventionally the first member, stored uncompressed.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, mimetype)

        for idx, section in enumerate(sections):
            ET.register_namespace("hp", namespace)
            root = ET.Element(f"{{{namespace}}}sec")
            for para in section:
                p_el = ET.SubElement(root, f"{{{namespace}}}p")
                for run_text in para:
                    run_el = ET.SubElement(p_el, f"{{{namespace}}}run")
                    t_el = ET.SubElement(run_el, f"{{{namespace}}}t")
                    t_el.text = run_text
            xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            zf.writestr(f"Contents/section{idx}.xml", xml_bytes)

        for member_name, member_data in (extra_members or {}).items():
            zf.writestr(member_name, member_data)
    return p


class TestHwpxText:
    def test_single_section_single_paragraph_yields_run_text(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[[["Hello world"]]])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Hello world" in r.text

    def test_korean_bracket_header_picked_up_as_section(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[[["[발주서]"]]])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("발주서",)

    def test_multiple_runs_in_one_paragraph_join_with_empty_string(self, tmp_path):
        """Runs join with "" — HWPX runs are token-level; spaces are explicit."""
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[[["회사 ", "정보:"]]])
        r = _extractors.extract(p)
        assert r.status == "ok"
        # Two adjacent runs concatenate into "회사 정보:" — colon-header regex picks it up.
        assert "회사 정보" in r.sections

    def test_empty_archive_zero_section_files_returns_ok(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == ""
        assert r.sections == ()

    @pytest.mark.parametrize("namespace", [_HWPX_NS_2011, _HWPX_NS_2016, _HWPX_NS_2021])
    def test_namespace_variants_all_yield_text(self, tmp_path, namespace):
        from fda.organize import _extractors

        p = _build_hwpx(
            tmp_path,
            name=f"ns_{hash(namespace) % 1000}.hwpx",
            sections=[[["[제목]"]]],
            namespace=namespace,
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "제목" in r.sections, f"namespace {namespace} did not match local-name"

    def test_multi_paragraph_separated_by_newlines(self, tmp_path):
        """Each <hp:p> ends with a "\\n" so line-oriented section regexes match."""
        from fda.organize import _extractors

        p = _build_hwpx(
            tmp_path,
            sections=[[
                ["[발주서]"],
                ["회사 정보:"],
                ["■ 주의사항"],
            ]],
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # All three Korean banner styles parse — bracket, colon, bullet.
        assert r.sections == ("발주서", "회사 정보", "주의사항")

    def test_multi_section_files_separated_by_newlines(self, tmp_path):
        """Each section file's text ends with a "\\n" before the next section
        joins, so paragraph breaks across HWPX section boundaries do not get
        glued together."""
        from fda.organize import _extractors

        p = _build_hwpx(
            tmp_path,
            sections=[
                [["[발주서]"]],   # section0.xml
                [["회사 정보:"]],  # section1.xml
            ],
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "발주서" in r.sections
        assert "회사 정보" in r.sections


class TestHwpxFailure:
    def test_not_a_zip_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "fake.hwpx"
        p.write_bytes(b"this is not a zip file at all")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_zip_without_mimetype_member_returns_failed(self, tmp_path):
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "no_mt.hwpx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("Contents/section0.xml", b"<root/>")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "OWPML" in r.note or "hwpx" in r.note

    def test_wrong_mimetype_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, mimetype=b"application/zip")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "OWPML" in r.note or "hwpx" in r.note

    def test_mimetype_with_extra_suffix_rejected_exact_match(self, tmp_path):
        """Regression: prefix-only matches like `application/hwp+zip-bad` must
        be rejected. The implementation compares exactly after .strip()."""
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, mimetype=b"application/hwp+zip-bad")
        r = _extractors.extract(p)
        assert r.status == "failed"

    def test_encrypted_entry_returns_failed(self, tmp_path, monkeypatch):
        """Python's zipfile.ZipFile.writestr resets ZipInfo.flag_bits during
        write, so a real encrypted-flag entry cannot be produced via the
        helper. We instead monkeypatch ZipFile.infolist so it returns a
        ZipInfo with flag_bits 0x1 set, simulating the runtime check."""
        from fda.organize import _extractors
        import zipfile

        p = _build_hwpx(tmp_path)

        original_infolist = zipfile.ZipFile.infolist

        def faked_infolist(self):
            infos = original_infolist(self)
            for info in infos:
                if info.filename.startswith("Contents/section"):
                    info.flag_bits |= 0x1
                    break
            return infos

        monkeypatch.setattr(zipfile.ZipFile, "infolist", faked_infolist)
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "encrypted" in r.note


class TestHwpxCaps:
    def test_section_file_count_truncated_at_max(self, tmp_path, monkeypatch):
        """When more than _HWPX_SECTION_FILES_MAX section files exist, the
        extractor truncates at the cap rather than aborting."""
        from fda.organize import _extractors

        # Lower the file-count cap so we can build a small fixture.
        monkeypatch.setattr(_extractors, "_HWPX_SECTION_FILES_MAX", 5)
        # Build cap + 3 section files; only the first 5 should be walked.
        sections = [[[f"section_{i}_marker"]] for i in range(8)]
        p = _build_hwpx(tmp_path, sections=sections)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # First 5 sections present; sections past the cap absent.
        assert "section_0_marker" in r.text
        assert "section_4_marker" in r.text
        assert "section_5_marker" not in r.text
        assert "section_7_marker" not in r.text

    def test_oversized_section_skipped_not_aborted(self, tmp_path, monkeypatch):
        """A section whose ZipInfo.file_size exceeds _HWPX_SECTION_BYTES_MAX
        is skipped (text=""); other sections still produce text."""
        from fda.organize import _extractors

        # Lower the per-section cap so a 4 KiB body trips it; raise the
        # ratio cap so the highly-compressible body doesn't trip the bomb
        # guard first.
        monkeypatch.setattr(_extractors, "_HWPX_SECTION_BYTES_MAX", 1024)
        monkeypatch.setattr(_extractors, "_HWPX_COMPRESSION_RATIO_MAX", 100_000)

        big_body = "x" * 8 * 1024     # ~8 KiB run text > 1 KiB section cap
        p = _build_hwpx(
            tmp_path,
            sections=[
                [[big_body]],         # section0: oversized → skipped
                [["[발주서]"]],       # section1: normal → walked
            ],
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # section0 skipped — its big_body not present.
        assert "x" * 100 not in r.text
        # section1 walked.
        assert r.sections == ("발주서",)

    def test_cumulative_cap_stops_walk(self, tmp_path, monkeypatch):
        """When cumulative file_size exceeds _HWPX_TOTAL_BYTES_MAX before the
        next read, the walk stops with what we have."""
        from fda.organize import _extractors

        # Lower the cumulative cap so a fixture of a few KiB sections trips it.
        # Raise the ratio cap so highly-compressible bodies don't trip the bomb.
        monkeypatch.setattr(_extractors, "_HWPX_TOTAL_BYTES_MAX", 12 * 1024)
        monkeypatch.setattr(_extractors, "_HWPX_SECTION_BYTES_MAX", 8 * 1024)
        monkeypatch.setattr(_extractors, "_HWPX_COMPRESSION_RATIO_MAX", 100_000)

        body = "x" * 5 * 1024  # ~5 KiB body per section
        # Markers are arbitrary ASCII strings checked in r.text — the Korean
        # section regexes are NOT exercised here, this test asserts walk
        # truncation only.
        sections = [
            [["s0_marker_text", body]],
            [["s1_marker_text", body]],
            [["s2_marker_text", body]],   # cumulative > 12 KiB before this read
            [["final_marker_text"]],
        ]
        p = _build_hwpx(tmp_path, sections=sections)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # First two sections walked (text present); third and final stopped.
        assert "s0_marker_text" in r.text
        assert "s1_marker_text" in r.text
        assert "s2_marker_text" not in r.text
        assert "final_marker_text" not in r.text

    def test_compression_ratio_bomb_aborts_archive(self, tmp_path):
        """A single entry whose uncompressed/compressed ratio exceeds
        _HWPX_COMPRESSION_RATIO_MAX is treated as malicious — abort whole."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "bomb.hwpx"
        # Write a "section0.xml" that's highly compressible (1 MiB of "A")
        # so DEFLATE produces a tiny compressed size — ratio in the thousands.
        big_xml = b"<?xml version='1.0'?><root>" + b"A" * (1024 * 1024) + b"</root>"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr("Contents/section0.xml", big_xml)
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "bomb" in r.note or "compress" in r.note


class TestHwpxXmlSafety:
    def test_one_malformed_section_other_section_still_walked(self, tmp_path):
        """Mid-section ParseError → skip that section's text=""; other
        sections still produce text. Status stays "ok"."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "partial.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            # section0: malformed XML.
            zf.writestr("Contents/section0.xml", b"<root><unclosed>")
            # section1: well-formed, contains a Korean header.
            ns = _HWPX_NS_2011
            xml = (
                f'<?xml version="1.0"?>'
                f'<hp:sec xmlns:hp="{ns}">'
                f'<hp:p><hp:run><hp:t>[발주서]</hp:t></hp:run></hp:p>'
                f'</hp:sec>'
            ).encode("utf-8")
            zf.writestr("Contents/section1.xml", xml)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # section0 skipped, section1 walked — its bracket header lands.
        assert r.sections == ("발주서",)

    def test_all_sections_malformed_returns_ok_empty(self, tmp_path):
        """When all section files are unparseable, text="" is acceptable;
        status stays "ok"."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "all_bad.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr("Contents/section0.xml", b"<<not xml>>")
            zf.writestr("Contents/section1.xml", b"</also bad/>")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text.strip() == ""
        assert r.sections == ()

    def test_dtd_payload_aborts_whole_extraction(self, tmp_path):
        """A section file containing a DOCTYPE with an inline entity
        declaration is rejected by defusedxml's EntitiesForbidden (which
        fires before DTDForbidden under the default forbid_dtd=False) —
        abort the whole extraction with status="failed"."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "dtd.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr(
                "Contents/section0.xml",
                b'<?xml version="1.0"?>'
                b'<!DOCTYPE foo [<!ENTITY x "hello">]>'
                b"<root>&x;</root>",
            )
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None

    def test_billion_laughs_payload_aborts_whole_extraction(self, tmp_path):
        """A section file with nested entity expansion (billion laughs) is
        rejected by defusedxml's EntitiesForbidden — abort."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "lol.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr(
                "Contents/section0.xml",
                b'<?xml version="1.0"?>'
                b'<!DOCTYPE lolz ['
                b'<!ENTITY lol "lol">'
                b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">'
                b']>'
                b"<lolz>&lol2;</lolz>",
            )
        r = _extractors.extract(p)
        assert r.status == "failed"
