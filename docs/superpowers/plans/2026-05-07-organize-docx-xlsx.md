# Organize: `.docx` + `.xlsx` Extractors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `.docx` and `.xlsx` text extractors to `fda/organize/_extractors.py` so each format produces an `ExtractionResult` with `text` (drives `verbatim_head` + Haiku summary) and `sections` (drives the classifier's structural-fingerprint signal). For `.xlsx`, also synthesize `"FormulaHeavy"` and `"MergedCells"` purpose-discriminating labels.

**Architecture:** Faithful extension of the v1 format-onboarding pattern — one extractor function per format registered in the `EXTRACTORS` dict. The Reader keeps ownership of the 64 KiB `text` contract cap (`READER_TEXT_CAP_BYTES`), so neither extractor caps `text` itself. xlsx requires two workbook loads because openpyxl's `data_only=True` returns cached values (not formulas), and `ReadOnlyWorksheet` doesn't expose `merged_cells` in the targeted 3.0.9 baseline.

**Tech Stack:** Python 3.12+, `python-docx>=1.0.0` (new runtime dep, tested at 1.2.0), `openpyxl>=3.0.9` (current pin loosens this to `>=3.0.0`; this plan tightens it to match the tested baseline), pytest. No new modules, no protocol abstraction. Spec at `docs/superpowers/specs/2026-05-07-organize-docx-xlsx-design.md`.

**Pretest:** every task ends with running the full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` (CLAUDE.md command). If that path is unavailable on the executing host, fall back to whichever Python 3.12 the test fixture conftest works under (`/opt/anaconda3/bin/python3 -m pytest ...` was verified to work locally).

---

## Task 1: Wire `python-docx` dependency and register four new xlsx constants

**Files:**
- Modify: `pyproject.toml:11-18` (dependencies list)
- Modify: `fda/organize/_extractors.py:31-32` (add four new constants near `_PDF_PIPE_CAP_BYTES`)
- Modify: `tests/test_organize_constraints.py:51-76` (add four entries to `CONSTS`)

- [ ] **Step 1: Write the failing constants test additions**

Edit `tests/test_organize_constraints.py`. In the `CONSTS` dict (around lines 51-76), add five new entries — four xlsx constants plus the previously-missing `SECTION_HEADER_MIN_CHARS` (already defined at `_sections.py:14` as `3`; the existing CONSTS block omits it. The xlsx extractor will import it in Task 2, so register it here):

```python
        "SECTION_HEADER_MIN_CHARS": ("_sections.py", "3"),
        "_XLSX_TEXT_ROWS_PER_SHEET": ("_extractors.py", "20"),
        "_XLSX_TEXT_COLS_PER_ROW": ("_extractors.py", "32"),
        "_XLSX_FORMULA_DENSITY_THRESHOLD": ("_extractors.py", "0.05"),
        "_XLSX_MERGED_CELLS_MIN": ("_extractors.py", "3"),
```

- [ ] **Step 2: Run the constants test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py::TestEachConstantHasOneHome::test_constant_defined_in_owning_module -v`
Expected: FAIL — `_XLSX_TEXT_ROWS_PER_SHEET missing from _extractors.py`.

- [ ] **Step 3: Add the four constants to `_extractors.py`**

Edit `fda/organize/_extractors.py`. Just below the existing `_PDF_TIMEOUT_SECONDS = 10` line (line 32), add:

```python

# xlsx serialization caps (memory-bound the in-process row/col walk; do NOT cap
# `text` — Reader owns the 64 KiB contract cap via READER_TEXT_CAP_BYTES).
_XLSX_TEXT_ROWS_PER_SHEET = 20
_XLSX_TEXT_COLS_PER_ROW = 32
# Synthesized-label thresholds.
_XLSX_FORMULA_DENSITY_THRESHOLD = 0.05
_XLSX_MERGED_CELLS_MIN = 3
```

- [ ] **Step 4: Update `pyproject.toml` dependencies**

Edit `pyproject.toml`. Add `python-docx>=1.0.0` and tighten the openpyxl floor to `>=3.0.9` to match the tested baseline (the spec at line 38 documents 3.0.9 as the assumed baseline; the existing `>=3.0.0` is looser than what's actually verified). The full block becomes:

```toml
dependencies = [
    "anthropic>=0.45.0",
    "pandas>=1.5.0",
    "openpyxl>=3.0.9",
    "python-docx>=1.0.0",
    "msal>=1.20.0",
    "requests>=2.28.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 4b: Install the editable package so new deps are importable**

The plan's later test steps import `docx` and `openpyxl`. Without an install, Task 2's import-time `from docx import Document` raises `ModuleNotFoundError` instead of producing the expected `no_extractor` failure.

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pip install -e .`
Expected: pip resolves and installs `python-docx` (and `openpyxl` if not already present at >=3.0.9).

- [ ] **Step 5: Run the constants test to verify it passes**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py -v`
Expected: PASS (all entries in `CONSTS` find their owning module + literal).

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green (113 existing tests + 4 newly-passing constants entries).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml fda/organize/_extractors.py tests/test_organize_constraints.py
git commit -m "organize(extractors): declare python-docx dep and reserve xlsx constants"
```

---

## Task 2: `_extract_docx` happy path — heading-styled paragraphs become sections

**Files:**
- Modify: `fda/organize/_extractors.py` (add `_extract_docx`, register `.docx` in `EXTRACTORS`)
- Modify: `tests/test_organize_extractors.py` (new `TestDocx` class with happy-path tests + fixture helper)

- [ ] **Step 1: Write the failing test (Heading 1/2 + Title)**

Append a new class to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestDocxHappyPath -v`
Expected: FAIL — `r.status == "no_extractor"` because `.docx` isn't registered yet.

- [ ] **Step 3: Implement `_extract_docx` and register `.docx`**

Edit `fda/organize/_extractors.py`. Add a new import at the top of the file (after the existing imports, before the constants block):

```python
import re
```

Add a docx heading-style regex constant near the other constants (just below the four xlsx constants from Task 1):

```python
_DOCX_HEADING_RE = re.compile(r"^Heading [1-9]$")
```

Add the `_sections` import alongside the existing `extract_sections_from_text` import. The current line 24 is:

```python
from fda.organize._sections import extract_sections_from_text
```

Replace it with:

```python
from fda.organize._sections import (
    MAX_SECTIONS_PER_FILE,
    SECTION_HEADER_MAX_CHARS,
    SECTION_HEADER_MIN_CHARS,
    extract_sections_from_text,
)
```

Now add the docx extractor. Insert just above the `EXTRACTORS = {...}` dict:

```python
def _extract_docx(path: Path) -> ExtractionResult:
    """Extract text + heading-style sections from a .docx file.

    Sections: paragraphs whose style name is "Title" or matches "Heading [1-9]",
    in document order, deduped, length-guarded, capped at MAX_SECTIONS_PER_FILE.

    Text: every paragraph body plus every table cell body, joined by "\\n".
    No extractor-side byte cap — Reader owns the 64 KiB contract cap.
    """
    from docx import Document

    doc = Document(str(path))
    sections: list[str] = []
    seen: dict[str, None] = {}
    text_parts: list[str] = []

    for p in doc.paragraphs:
        body = p.text
        if body:
            text_parts.append(body)
        if len(sections) >= MAX_SECTIONS_PER_FILE:
            continue
        style_name = getattr(p.style, "name", "") or ""
        if style_name != "Title" and not _DOCX_HEADING_RE.match(style_name):
            continue
        label = " ".join(body.split())
        if not (SECTION_HEADER_MIN_CHARS <= len(label) <= SECTION_HEADER_MAX_CHARS):
            continue
        if label in seen:
            continue
        seen[label] = None
        sections.append(label)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell_text = cell.text
                if cell_text:
                    text_parts.append(cell_text)

    return ExtractionResult(
        text="\n".join(text_parts),
        status="ok",
        sections=tuple(sections),
    )
```

Register `.docx` in the `EXTRACTORS` dict (modify the existing dict to add the entry):

```python
EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _read_text,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
}
```

- [ ] **Step 4: Update the existing `.docx`-not-registered assertion in the same code change**

Once `.docx` is registered, an existing test (`TestRegistryAdditions::test_unregistered_extension_falls_back` at `tests/test_organize_extractors.py:128-136`) will fail because it asserts `".docx" not in _extractors.EXTRACTORS`. Update it now so the suite stays green in one shot:

```python
    def test_unregistered_extension_falls_back(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.zzz"
        p.write_bytes(b"\x00")
        # No registration -> no_extractor
        assert ".zzz" not in _extractors.EXTRACTORS
        r = _extractors.extract(p)
        assert r.status == "no_extractor"
```

- [ ] **Step 5: Run the new tests + full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestDocxHappyPath -v`
Expected: PASS.

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): add _extract_docx (Heading [1-9] + Title)"
```

---

## Task 3: `_extract_docx` edge cases — tables, length guards, dedup, cap, failure

**Files:**
- Modify: `tests/test_organize_extractors.py` (extend `TestDocxHappyPath` and add `TestDocxEdgeCases`)

**Note: regression tests, not red-green TDD.** Task 2's implementation already handles every case in this task — length guards, dedup, MAX_SECTIONS cap, table iteration, and corrupt-file handling are all in the Task 2 code by design. These tests pin that behavior and prevent regressions; they are expected to pass on first run, not fail-then-pass. (Splitting Task 2 into a "minimal" then "expand" pair would be artificial.)

- [ ] **Step 1: Write tests for length guards, dedup, MAX_SECTIONS cap**

Append to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Write tests for table-only docs, empty docs, no-headings docs**

Append:

```python
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
```

- [ ] **Step 3: Write the failure-isolation test**

Append:

```python
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
```

- [ ] **Step 4: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestDocxEdgeCases -v`
Expected: PASS for all seven tests.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors-test): docx length-guard, dedup, cap, table, corrupt"
```

---

## Task 4: `_extract_xlsx` schema sections — sheet names + row-1 column headers

**Files:**
- Modify: `fda/organize/_extractors.py` (add `_extract_xlsx`, register `.xlsx`)
- Modify: `tests/test_organize_extractors.py` (new `TestXlsxSchema` class with fixture helper)

**Prerequisites from Task 2:** `_extractors.py` must already import `MAX_SECTIONS_PER_FILE`, `SECTION_HEADER_MAX_CHARS`, and `SECTION_HEADER_MIN_CHARS` from `fda.organize._sections` (Task 2 Step 3 added this). If executing Task 4 in isolation, ensure those imports are present before adding `_extract_xlsx`.

This task implements pass-1 only (sections + text scaffolding without synthesized labels or formula handling). Tasks 5/6/7 add the rest. Each task ends with green tests against its own subset of behavior.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxSchema -v`
Expected: FAIL — `r.status == "no_extractor"`.

- [ ] **Step 3: Implement `_extract_xlsx` (schema-only, scaffolds text)**

Edit `fda/organize/_extractors.py`. Add the extractor just below `_extract_docx`:

```python
def _extract_xlsx(path: Path) -> ExtractionResult:
    """Extract text + sections from an .xlsx workbook.

    Two passes are required: pass 1 (data_only=True, read_only=True) for cached
    values and the row-1 column headers; pass 2 (data_only=False, read_only=False)
    for formula counts and merged-range counts. Workbook is NOT a context manager
    in openpyxl 3.0.9 — close via try/finally.

    Sections layout: schema labels first (Sheet:<name> + row-1 headers, in
    workbook order, deduped, length-guarded, capped to
    MAX_SECTIONS_PER_FILE - len(synthesized)), then synthesized labels in the
    tail.

    Text layout: per sheet, "Sheet: <name>\\n", then up to
    _XLSX_TEXT_ROWS_PER_SHEET rows of \\t-joined cells (left-trimmed to
    _XLSX_TEXT_COLS_PER_ROW), then a blank line. No extractor-side byte cap.
    """
    import openpyxl

    # Pass 2 — formula counts, merged-range counts, formula-string lookup.
    # Filled in Task 6; for this task we just do pass 1 and leave synthesized=[].
    synthesized: list[str] = []

    schema_labels: list[str] = []
    schema_seen: dict[str, None] = {}
    text_parts: list[str] = []

    wb1 = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet_name in wb1.sheetnames:
            ws = wb1[sheet_name]
            sheet_label = f"Sheet:{sheet_name}"
            if sheet_label not in schema_seen:
                schema_seen[sheet_label] = None
                schema_labels.append(sheet_label)
            row1 = next(ws.iter_rows(values_only=True, max_row=1), ())
            for v in row1:
                if v is None:
                    continue
                s = " ".join(str(v).split())
                if not s:
                    continue
                if not (SECTION_HEADER_MIN_CHARS <= len(s) <= SECTION_HEADER_MAX_CHARS):
                    continue
                if s in schema_seen:
                    continue
                schema_seen[s] = None
                schema_labels.append(s)
            text_parts.append(f"Sheet: {sheet_name}\n")
            # Text-row serialization stub — Task 5 fills this in.
            text_parts.append("\n")
    finally:
        wb1.close()

    schema_cap = MAX_SECTIONS_PER_FILE - len(synthesized)
    schema_capped = schema_labels[: max(0, schema_cap)]
    # Final ordered-set pass over `schema_capped + synthesized` so a column
    # header literally named "FormulaHeavy" or "MergedCells" doesn't appear
    # twice when its synthesized counterpart triggers.
    final_seen: dict[str, None] = {}
    sections_list: list[str] = []
    for label in schema_capped + synthesized:
        if label not in final_seen:
            final_seen[label] = None
            sections_list.append(label)
    sections = tuple(sections_list)

    return ExtractionResult(
        text="".join(text_parts),
        status="ok",
        sections=sections,
    )
```

Register `.xlsx` in `EXTRACTORS`:

```python
EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _read_text,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
}
```

- [ ] **Step 4: Run TestXlsxSchema to verify it passes**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxSchema -v`
Expected: PASS for all nine tests.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): _extract_xlsx schema sections + Sheet: text banners"
```

---

## Task 5: `_extract_xlsx` text serialization — row/column caps + trailing-empty trim

**Files:**
- Modify: `fda/organize/_extractors.py` (replace the text-row stub inside `_extract_xlsx`)
- Modify: `tests/test_organize_extractors.py` (new `TestXlsxText` class)

- [ ] **Step 1: Write failing tests for text serialization**

Append to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxText -v`
Expected: FAIL — text only contains the `Sheet: <name>\n\n` banners; no row data.

- [ ] **Step 3: Replace the text-row stub in `_extract_xlsx` with full serialization**

Edit `fda/organize/_extractors.py`. Inside `_extract_xlsx`, locate the loop body:

```python
            text_parts.append(f"Sheet: {sheet_name}\n")
            # Text-row serialization stub — Task 5 fills this in.
            text_parts.append("\n")
```

Replace with:

```python
            text_parts.append(f"Sheet: {sheet_name}\n")
            for row in ws.iter_rows(
                values_only=True, max_row=_XLSX_TEXT_ROWS_PER_SHEET
            ):
                row_slice = list(row[:_XLSX_TEXT_COLS_PER_ROW])
                cells_str = ["" if c is None else str(c) for c in row_slice]
                while cells_str and cells_str[-1] == "":
                    cells_str.pop()
                text_parts.append("\t".join(cells_str) + "\n")
            text_parts.append("\n")
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxText -v`
Expected: PASS for all five tests.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): xlsx text serialization with row/col caps"
```

---

## Task 6: `_extract_xlsx` synthesized labels — `FormulaHeavy` + `MergedCells`

**Files:**
- Modify: `fda/organize/_extractors.py` (add pass-2 block to `_extract_xlsx`)
- Modify: `tests/test_organize_extractors.py` (new `TestXlsxSynthesizedLabels` class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxSynthesizedLabels -v`
Expected: FAIL — `synthesized` is currently always `[]`.

- [ ] **Step 3: Implement pass 2**

Edit `fda/organize/_extractors.py`. Inside `_extract_xlsx`, replace the placeholder line:

```python
    # Pass 2 — formula counts, merged-range counts, formula-string lookup.
    # Filled in Task 6; for this task we just do pass 1 and leave synthesized=[].
    synthesized: list[str] = []
```

with:

```python
    # Pass 2 — formula counts, merged-range counts. ReadOnlyWorksheet does
    # NOT expose merged_cells in openpyxl 3.0.9, so this pass uses the
    # default read_only=False.
    non_empty_cells = 0
    formula_cells = 0
    merged_count = 0
    wb2 = openpyxl.load_workbook(path, data_only=False, read_only=False)
    try:
        for ws in wb2.worksheets:
            merged_count += len(ws.merged_cells.ranges)
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    non_empty_cells += 1
                    if cell.data_type == "f":
                        formula_cells += 1
    finally:
        wb2.close()

    synthesized: list[str] = []
    if (
        non_empty_cells > 0
        and formula_cells / non_empty_cells > _XLSX_FORMULA_DENSITY_THRESHOLD
    ):
        synthesized.append("FormulaHeavy")
    if merged_count >= _XLSX_MERGED_CELLS_MIN:
        synthesized.append("MergedCells")
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxSynthesizedLabels -v`
Expected: PASS for all six tests.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): xlsx synthesized labels FormulaHeavy + MergedCells"
```

---

## Task 7: `_extract_xlsx` cached-`None` formula fallback in `text`

**Files:**
- Modify: `fda/organize/_extractors.py` (collect formula strings during pass 2; substitute during pass-1 row serialization)
- Modify: `tests/test_organize_extractors.py` (new `TestXlsxFormulaFallback` class)

Why this matters: openpyxl-saved formulas have `None` cached values until Excel reopens and saves the file. Without fallback, every spec/template workbook would surface formula cells as empty in `verbatim_head` and the Haiku summary.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxFormulaFallback -v`
Expected: FAIL — formula strings are absent from `r.text` because pass 1 sees `None` and substitutes `""`.

- [ ] **Step 3: Implement formula-string capture in pass 2 + substitution in pass 1**

Edit `fda/organize/_extractors.py`. Inside `_extract_xlsx`, the pass-2 block currently looks like (from Task 6):

```python
    non_empty_cells = 0
    formula_cells = 0
    merged_count = 0
    wb2 = openpyxl.load_workbook(path, data_only=False, read_only=False)
    try:
        for ws in wb2.worksheets:
            merged_count += len(ws.merged_cells.ranges)
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    non_empty_cells += 1
                    if cell.data_type == "f":
                        formula_cells += 1
    finally:
        wb2.close()
```

Replace with the formula-string-capturing version:

```python
    non_empty_cells = 0
    formula_cells = 0
    merged_count = 0
    formulas_by_addr: dict[tuple[str, int, int], str] = {}
    wb2 = openpyxl.load_workbook(path, data_only=False, read_only=False)
    try:
        for ws in wb2.worksheets:
            merged_count += len(ws.merged_cells.ranges)
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    non_empty_cells += 1
                    if cell.data_type == "f":
                        formula_cells += 1
                        # openpyxl returns formula values that already start
                        # with "=" (e.g., "=A2*10"). Defensive prefix only if
                        # missing — never f"={cell.value}" blindly, which
                        # would double-prefix and produce "==A2*10".
                        raw = str(cell.value)
                        formula_str = raw if raw.startswith("=") else f"={raw}"
                        formulas_by_addr[(ws.title, cell.row, cell.column)] = (
                            formula_str
                        )
    finally:
        wb2.close()
```

Now substitute the cached `None` during pass 1's row serialization. Inside the pass-1 sheet loop, the row body currently looks like:

```python
            text_parts.append(f"Sheet: {sheet_name}\n")
            for row in ws.iter_rows(
                values_only=True, max_row=_XLSX_TEXT_ROWS_PER_SHEET
            ):
                row_slice = list(row[:_XLSX_TEXT_COLS_PER_ROW])
                cells_str = ["" if c is None else str(c) for c in row_slice]
                while cells_str and cells_str[-1] == "":
                    cells_str.pop()
                text_parts.append("\t".join(cells_str) + "\n")
            text_parts.append("\n")
```

Replace with the formula-aware version (note we now need the row index, so use `enumerate`):

```python
            text_parts.append(f"Sheet: {sheet_name}\n")
            for r_idx, row in enumerate(
                ws.iter_rows(values_only=True, max_row=_XLSX_TEXT_ROWS_PER_SHEET),
                start=1,
            ):
                row_slice = list(row[:_XLSX_TEXT_COLS_PER_ROW])
                cells_str: list[str] = []
                for c_idx, value in enumerate(row_slice, start=1):
                    if value is None:
                        formula = formulas_by_addr.get(
                            (sheet_name, r_idx, c_idx)
                        )
                        cells_str.append(formula if formula is not None else "")
                    else:
                        cells_str.append(str(value))
                while cells_str and cells_str[-1] == "":
                    cells_str.pop()
                text_parts.append("\t".join(cells_str) + "\n")
            text_parts.append("\n")
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestXlsxFormulaFallback -v`
Expected: PASS for both tests.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): xlsx cached-None formula fallback in text"
```

---

## Task 8: Reader propagation + pipeline integration corpus

**Files:**
- Modify: `tests/test_organize_reader.py` (add docx + xlsx propagation tests)
- Modify: `tests/test_organize_pipeline.py` (extend the workspace fixture)

**Note: integration regression tests, not red-green TDD.** Reader already copies `extraction.sections` into `CatalogEntry.sections` at `reader.py:172` (shipped with v1). These tests pin that the new formats flow through unchanged; they are expected to pass on first run. Without them, a future regression in Reader's propagation could silently drop sections only for the new formats.

- [ ] **Step 1: Write the reader propagation tests for docx + xlsx**

Append to `tests/test_organize_reader.py` (inside `TestSectionsPropagation` if you prefer; I'll show as a new sibling class for clarity):

```python
class TestSectionsPropagationDocxXlsx:
    def test_docx_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        from fda.organize import reader
        # Reuse the docx fixture helper from extractors tests via direct call.
        from docx import Document

        f = workspace / "doc.docx"
        d = Document()
        p = d.add_paragraph("My Title")
        p.style = d.styles["Title"]
        p2 = d.add_paragraph("Findings")
        p2.style = d.styles["Heading 1"]
        d.add_paragraph("body content")
        d.save(str(f))

        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("doc.docx"))
        assert e.extract_status == "ok"
        assert e.sections == ("My Title", "Findings")
        assert e.verbatim_head.startswith("My Title")

    def test_xlsx_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        from fda.organize import reader
        import openpyxl

        f = workspace / "wb.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Orders"
        ws.append(["Order ID", "Customer"])
        ws.append([1, "ACME"])
        wb.save(str(f))
        wb.close()

        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("wb.xlsx"))
        assert e.extract_status == "ok"
        assert "Sheet:Orders" in e.sections
        assert "Order ID" in e.sections
        assert "Customer" in e.sections

    def test_docx_failed_extraction_yields_empty_sections_in_catalog(
        self, workspace, fake_backend, logger
    ):
        """Parallel to existing PDF/text coverage: when an extractor fails,
        the catalog entry's sections is preserved as ()."""
        from fda.organize import reader

        f = workspace / "broken.docx"
        f.write_bytes(b"not a real docx")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("broken.docx"))
        assert e.extract_status == "failed"
        assert e.sections == ()

    def test_xlsx_failed_extraction_yields_empty_sections_in_catalog(
        self, workspace, fake_backend, logger
    ):
        """Spec line 166: reader failure preservation applies to both formats.
        Pin xlsx alongside docx."""
        from fda.organize import reader

        f = workspace / "broken.xlsx"
        f.write_bytes(b"not a real xlsx")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("broken.xlsx"))
        assert e.extract_status == "failed"
        assert e.sections == ()
```

- [ ] **Step 2: Run the propagation tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx -v`
Expected: PASS — Reader already passes `extraction.sections` through to `CatalogEntry.sections` (`reader.py:172`); these tests just pin the new formats.

- [ ] **Step 3: Extend the pipeline integration corpus**

Edit `tests/test_organize_pipeline.py:16-23`. The current fixture is:

```python
@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root
```

Replace with:

```python
@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")

    # Minimal .docx (one Title paragraph) so the integration corpus exercises
    # the new format end-to-end.
    from docx import Document
    docx_path = root / "report.docx"
    d = Document()
    p = d.add_paragraph("Report Title")
    p.style = d.styles["Title"]
    d.save(str(docx_path))

    # Minimal .xlsx (one sheet, one header row).
    import openpyxl
    xlsx_path = root / "data.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Order ID", "Customer"])
    wb.save(str(xlsx_path))
    wb.close()

    return root
```

The existing `test_full_pipeline` assertion checks both `.txt` files end up under `Texts/`. Update to also check the new files survive the pipeline. Inside `test_full_pipeline` after the existing assertions, add:

```python
        # New format files were also categorized (the scripted backend
        # assigns every entry to "Texts", so they all land in the same dir).
        assert (workspace / "Texts" / "report.docx").exists()
        assert (workspace / "Texts" / "data.xlsx").exists()
```

Inside `test_preview_returns_plan_without_executing`, add after the existing assertions:

```python
        assert (workspace / "report.docx").exists()
        assert (workspace / "data.xlsx").exists()
```

(Preview must NOT move files — these assertions guard against accidental execution.)

- [ ] **Step 4: Run the pipeline tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_organize_reader.py tests/test_organize_pipeline.py
git commit -m "organize(integration): docx + xlsx flow through reader and pipeline"
```

---

## Acceptance criteria

- All eight tasks committed in order on `dev_branch`.
- `pytest -x -q` green throughout each task's final step (no skipped or xfail-tagged tests added).
- `_PDF_PIPE_CAP_BYTES` unchanged (subprocess pipe safety; not the contract cap).
- New runtime dependency: `python-docx>=1.0.0`. The `openpyxl` floor is tightened from `>=3.0.0` to `>=3.0.9` to match the tested baseline.
- No edits to `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, or any skill prompt.
- Constants test passes with the four new xlsx constants AND the previously-missing `SECTION_HEADER_MIN_CHARS` registered.
- Spec test cases enumerated in `2026-05-07-organize-docx-xlsx-design.md` (Testing plan section, lines 138-164) all map to a test method in this plan.

## Spec coverage cross-reference

| Spec test bullet | Plan test |
|---|---|
| docx: Heading 1 + Heading 2 mix → labels in document order, deduped | Task 2 `test_headings_become_sections_in_document_order` + Task 3 `test_duplicate_headings_deduped_in_first_occurrence_order` |
| docx: `Title` style alone → captured | Task 2 `test_title_alone_captured` |
| docx: No headings → `sections=()`; `text` populated | Task 3 `test_no_headings_yields_empty_sections` |
| docx: More than 15 headings → capped at 15 | Task 3 `test_capped_at_max_sections_per_file` |
| docx: Empty doc → `text=""`, `sections=()`, `status="ok"` | Task 3 `test_empty_doc_yields_empty_text_and_sections` |
| docx: Whitespace-only / 2-char heading text → filtered out | Task 3 `test_two_char_heading_filtered_out` + `test_whitespace_only_heading_filtered_out` |
| docx: 41-char heading text → filtered out | Task 3 `test_41_char_heading_filtered_out` + `test_max_chars_applied_after_whitespace_normalization` (order-of-ops pin) |
| docx: Body content lives in `doc.tables` only → `text` includes it | Task 3 `test_table_cell_text_included_in_text` |
| docx: Corrupt file → `status="failed"` | Task 3 `test_corrupt_bytes_returns_failed` |
| xlsx: Single sheet with header row | Task 4 `test_single_sheet_with_headers` |
| xlsx: Multi-sheet workbook → schema labels in order | Task 4 `test_multi_sheet_workbook` + Task 6 `test_synthesized_in_tail_after_schema_cap` (cap interaction) |
| xlsx: Empty cells in row 1 skipped | Task 4 `test_empty_cells_in_row1_skipped` |
| xlsx: 41-char column header filtered | Task 4 `test_long_row1_value_filtered_by_max_chars` |
| xlsx: Sheet with no row-1 content → only `Sheet:<name>` | Task 4 `test_sheet_with_no_row1_content` |
| xlsx: Hidden sheet → included | Task 4 `test_hidden_sheet_included` |
| xlsx: Ordered-set dedupe across sheets | Task 4 `test_duplicate_headers_across_sheets_deduped_in_first_seen_order` |
| xlsx: Sheet-level iteration failure → whole-file failure | Task 4 `test_sheet_iteration_failure_fails_whole_file` |
| xlsx: Formula density above threshold → `FormulaHeavy` | Task 6 `test_formula_heavy_appended_above_threshold` |
| xlsx: Formula density at/below threshold → not appended | Task 6 `test_formula_heavy_not_appended_at_or_below_threshold` |
| xlsx: `non_empty_cells == 0` zero-guard | Task 6 `test_formula_heavy_zero_guard` |
| xlsx: Cached `None` formula → fallback to formula string | Task 7 `test_cached_none_falls_back_to_formula_string` + `test_trailing_empty_trim_does_not_swallow_formula_fallback` + `test_formula_fallback_aligned_with_sparse_row_layout` |
| xlsx: Merged-range count ≥ 3 → `MergedCells` | Task 6 `test_merged_cells_appended_at_or_above_min` |
| xlsx: Merged-range count < 3 → not appended | Task 6 `test_merged_cells_not_appended_below_min` |
| xlsx: Wide sheet → row trimmed to `_XLSX_TEXT_COLS_PER_ROW` cells with trailing-empty drop | Task 5 `test_text_caps_cols_per_row` + `test_trailing_empty_cells_trimmed` |
| xlsx: Encrypted / corrupt → `status="failed"` | Task 4 `test_corrupt_xlsx_returns_failed` |
| xlsx: Workbook with only empty sheets | Task 4 `test_workbook_with_only_empty_sheets` |
| Reader docx/xlsx propagation (success + failure) | Task 8 `TestSectionsPropagationDocxXlsx` (incl. `test_xlsx_failed_extraction_yields_empty_sections_in_catalog`) |
| Pipeline integration with docx + xlsx | Task 8 fixture extension + assertions |
| Constants registered in CONSTS | Task 1 (4 xlsx + `SECTION_HEADER_MIN_CHARS`) |
| Classifier shape-budget unchanged | No new test required (existing test covers `sections` payload shape regardless of source format) |
