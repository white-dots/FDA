# Organize: `.csv` Extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the generic `_read_text` handler for `.csv` with a dedicated `_extract_csv` in `fda/organize/_extractors.py` that produces an `ExtractionResult` with `text` (tab-normalized 20-row × 32-col grid; drives `verbatim_head` + Haiku summary) and `sections` (column headers from row 1 or the first qualifying scan row, with synthesized `"NoHeader"` fallback; drives the classifier's structural-fingerprint signal).

**Architecture:** Faithful extension of the docx/xlsx/pptx format-onboarding pattern — one extractor function in `_extractors.py`, one entry replacing the existing `.csv` mapping in the `EXTRACTORS` dict, no new modules, no protocol abstraction. Reader keeps ownership of the 64 KiB `text` contract cap (`READER_TEXT_CAP_BYTES`), so the extractor does not cap `text` itself; five new `_CSV_*` constants bound in-process memory while reading, sniffing, scanning headers, and serializing, plus one `_CSV_NO_HEADER_LABEL` string literal for the synthesized fallback.

**Tech Stack:** Python 3.12+, stdlib only (`csv`, `io`) — no new runtime dependency. pytest. Spec at `docs/superpowers/specs/2026-05-08-organize-csv-design.md`.

**Pretest:** every task ends with running the full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` (CLAUDE.md command). If that path is unavailable on the executing host, fall back to whichever Python 3.12 the test fixture conftest works under.

**stdlib `csv` API quick reference:**
- `csv.Sniffer().sniff(sample)` returns a `Dialect` (delimiter, quotechar, etc.). Raises `csv.Error` when it can't decide. Fall back to `csv.excel` (comma + double-quote).
- `csv.Sniffer().has_header(sample)` returns a bool heuristic. Can also raise `csv.Error` on degenerate samples; treat as `False`.
- `csv.reader(io.StringIO(text), dialect)` yields lists of cell strings, handling quoting/escaping per dialect. Mid-stream errors (e.g. `"line contains NUL"`) raise `csv.Error`.
- `bytes.decode("utf-8-sig")` strips a leading BOM (`\xef\xbb\xbf`) if present and decodes plain UTF-8 normally otherwise.

**Test Python path:** all `pytest` invocations below assume `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest`. Substitute the equivalent on your host if needed.

---

## Task 1: Reserve six new csv constants

**Files:**
- Modify: `fda/organize/_extractors.py` (add five `_CSV_*` constants and one `_CSV_NO_HEADER_LABEL` string literal after the pptx block)
- Modify: `tests/test_organize_constraints.py` (add six entries to `CONSTS`)

- [ ] **Step 1: Add six entries to the `CONSTS` dict**

Edit `tests/test_organize_constraints.py`. Inside `class TestEachConstantHasOneHome:`, append to the `CONSTS` dict (just below the `_PPTX_NOTES_CHARS_PER_SLIDE_MAX` entry):

```python
        "_CSV_READ_BYTES_MAX": ("_extractors.py", "4 * 1024 * 1024"),
        "_CSV_SNIFF_SAMPLE_CHARS": ("_extractors.py", "8 * 1024"),
        "_CSV_HEADER_SCAN_ROWS": ("_extractors.py", "5"),
        "_CSV_TEXT_ROWS_MAX": ("_extractors.py", "20"),
        "_CSV_TEXT_COLS_PER_ROW": ("_extractors.py", "32"),
        "_CSV_NO_HEADER_LABEL": ("_extractors.py", '"NoHeader"'),
```

- [ ] **Step 2: Run the constants test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py::TestEachConstantHasOneHome::test_constant_defined_in_owning_module -v`
Expected: FAIL — `_CSV_READ_BYTES_MAX missing from _extractors.py`.

- [ ] **Step 3: Add the six constants to `_extractors.py`**

Edit `fda/organize/_extractors.py`. Just below the existing `_PPTX_NOTES_CHARS_PER_SLIDE_MAX = 2000` line (~line 53), add:

```python

# csv extractor caps and fallback label. The five _CSV_* numeric caps
# memory-bound the in-process read/sniff/scan/serialize walk; do NOT cap
# `text` — Reader owns the 64 KiB contract cap via READER_TEXT_CAP_BYTES.
# _CSV_NO_HEADER_LABEL is the synthesized fallback section label appended
# when no usable header is found (parallel to xlsx's "FormulaHeavy").
_CSV_READ_BYTES_MAX = 4 * 1024 * 1024
_CSV_SNIFF_SAMPLE_CHARS = 8 * 1024
_CSV_HEADER_SCAN_ROWS = 5
_CSV_TEXT_ROWS_MAX = 20
_CSV_TEXT_COLS_PER_ROW = 32
_CSV_NO_HEADER_LABEL = "NoHeader"
```

- [ ] **Step 4: Run the constants test to verify it passes**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py -v`
Expected: PASS (all entries in `CONSTS` find their owning module).

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_constraints.py
git commit -m "organize(extractors): reserve csv extractor constants"
```

---

## Task 2: Migrate existing tests that pin `.csv` to `_read_text`

The `.csv` registration is about to switch from `_read_text` to `_extract_csv`. Two existing tests assert the old behavior and will break the moment the registration changes. Update them now (they continue to pass under the current code) so Task 3's registration switch lands clean.

**Note:** `tests/test_organize_extractors.py:171-207` (`test_v1_structured_text_with_no_colon_headers_yields_empty_sections`) calls `_read_text` directly with `data.csv` filename — this is testing the helper, not the dispatch table, so it is **unaffected** and stays as-is.

**Files:**
- Modify: `tests/test_organize_extractors.py:22` (drop `.csv` from the `test_other_text_extensions_route_to_read_text` parametrize list)
- Modify: `tests/test_organize_reader.py:613-625` (`test_empty_when_extraction_returns_no_sections`: switch fixture from `.csv` to `.json`)

- [ ] **Step 1: Drop `.csv` from the read-text parametrize list**

Edit `tests/test_organize_extractors.py:22`. Change:

```python
    @pytest.mark.parametrize("ext", [".md", ".csv", ".log", ".json", ".xml"])
```

to:

```python
    @pytest.mark.parametrize("ext", [".md", ".log", ".json", ".xml"])
```

- [ ] **Step 2: Switch the empty-sections passthrough test from `.csv` to `.json`**

Edit `tests/test_organize_reader.py`. The current test at lines 613–625:

```python
    def test_empty_when_extraction_returns_no_sections(
        self, workspace, fake_backend, logger
    ):
        """A CSV (or any text without colon-headers / ALL-CAPS dividers)
        yields sections=()."""
        from fda.organize import reader

        (workspace / "data.csv").write_text(
            "customer_id,order_date\n1,2024-01-01\n"
        )
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.sections == ()
```

Replace with:

```python
    def test_empty_when_extraction_returns_no_sections(
        self, workspace, fake_backend, logger
    ):
        """A JSON file (or any plaintext without colon-headers /
        ALL-CAPS dividers) yields sections=()."""
        from fda.organize import reader

        (workspace / "data.json").write_text(
            '{"customer_id": 1, "order_date": "2024-01-01"}\n'
        )
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.sections == ()
```

(The `.json` route still goes through `_read_text` → `extract_sections_from_text`, which yields `()` for content with no colon-only-on-line headers and no ALL-CAPS dividers — same intent as the original csv-based assertion.)

- [ ] **Step 3: Run both updated tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPlainText::test_other_text_extensions_route_to_read_text tests/test_organize_reader.py -k empty_when_extraction_returns -v`
Expected: PASS.

- [ ] **Step 4: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_organize_extractors.py tests/test_organize_reader.py
git commit -m "organize(tests): drop csv from read-text routing assertions ahead of csv extractor"
```

---

## Task 3: `_extract_csv` happy path — UTF-8 ASCII csv with row-1 header

Lock in the simplest end-to-end case: a comma-delimited UTF-8 CSV with a clear header row. Register `_extract_csv` in `EXTRACTORS`, replacing `_read_text`.

**Files:**
- Modify: `fda/organize/_extractors.py` (add `import csv`/`import io`, add `_extract_csv`, swap `.csv` registration)
- Modify: `tests/test_organize_extractors.py` (new `TestCsvSections` class with happy-path tests)

- [ ] **Step 1: Write the failing tests (single-row header + multi-row + dedupe)**

Append to `tests/test_organize_extractors.py`:

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvSections -v`
Expected: FAIL — `r.sections` is empty (current `_read_text` regex doesn't match comma-headed lines), and `r.text` contains commas not tabs.

- [ ] **Step 3: Add stdlib imports**

Edit `fda/organize/_extractors.py`. In the import block (lines 17-22), insert `import csv`, `import io`, and `import itertools` so the alphabetic order becomes:

```python
import csv
import io
import itertools
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable
```

- [ ] **Step 4: Implement `_extract_csv`**

Edit `fda/organize/_extractors.py`. Add the new function below `_extract_pptx` (after the `return ExtractionResult(...)` at the end of `_extract_pptx`, around line 369):

```python
def _extract_csv(path: Path) -> ExtractionResult:
    """Extract text + column-header sections from a .csv file.

    Sections: row-1 column headers under csv.Sniffer.has_header(), or the
    first row in the first _CSV_HEADER_SCAN_ROWS that satisfies all five
    header-shape rules when has_header() is False. Length-guarded, deduped,
    capped at MAX_SECTIONS_PER_FILE. When no usable header is found,
    sections=(_CSV_NO_HEADER_LABEL,).

    Text: up to _CSV_TEXT_ROWS_MAX physical rows from the start, each
    truncated to _CSV_TEXT_COLS_PER_ROW cells, "\\t"-joined with a trailing
    "\\n". Sniffed delimiter is normalized to tab so the classifier sees a
    uniform shape across .csv and .xlsx. No banner. No extractor-side byte
    cap — Reader owns the 64 KiB contract cap.

    Encoding: utf-8-sig (BOM-tolerant) → cp949 (Excel-Korean exports).
    Both decode failures → status="failed".
    """
    with path.open("rb") as f:
        raw = f.read(_CSV_READ_BYTES_MAX)

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("cp949")
        except UnicodeDecodeError as e:
            return ExtractionResult(text=None, status="failed", note=str(e))

    if not decoded:
        return ExtractionResult(text="", status="ok", sections=())
    if not decoded.strip():
        # Whitespace-only decoded content: csv.reader would yield rows of
        # whitespace cells, but spec requires text="" and sections=("NoHeader",).
        return ExtractionResult(
            text="", status="ok", sections=(_CSV_NO_HEADER_LABEL,)
        )

    sample = decoded[:_CSV_SNIFF_SAMPLE_CHARS]
    try:
        # Restrict candidate delimiters so the sniffer cannot pick a stray
        # alphabetic byte (e.g. "n" from "name") on short single-column samples.
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = False

    # Bounded two-pass iteration. Pass 1: read up to _CSV_HEADER_SCAN_ROWS + 1
    # rows for header detection (the +1 is rule 5's lookahead). Pass 2:
    # re-iterate from the start and read up to _CSV_TEXT_ROWS_MAX rows for
    # serialization. A malformed row past those windows does not affect
    # extraction — only mid-window corruption propagates as status="failed".
    header_iter = csv.reader(io.StringIO(decoded), dialect)
    scan_window = list(itertools.islice(header_iter, _CSV_HEADER_SCAN_ROWS + 1))

    header_cells: list[str] | None = None
    if has_header and scan_window:
        header_cells = scan_window[0]
    elif scan_window:
        scan_limit = min(_CSV_HEADER_SCAN_ROWS, len(scan_window))
        for i in range(scan_limit):
            r = scan_window[i]
            non_empty = [c for c in r if c and c.strip()]
            if len(non_empty) < 2:
                continue
            normalized = [" ".join(c.split()) for c in non_empty]
            if not all(
                SECTION_HEADER_MIN_CHARS <= len(s) <= SECTION_HEADER_MAX_CHARS
                for s in normalized
            ):
                continue
            if any(_is_pure_number(s) for s in normalized):
                continue
            if len(set(normalized)) != len(normalized):
                continue
            # rule 5: cell count matches the next row, OR R is the final row.
            # The scan_window size is _CSV_HEADER_SCAN_ROWS + 1, so for i in
            # 0..scan_limit-1 the lookahead scan_window[i+1] is in-bounds
            # whenever the file has more rows than the scan window. When
            # i + 1 == len(scan_window), the file had ≤ scan+1 rows total and
            # R is the final row — accept it (no cell-count constraint).
            if i + 1 < len(scan_window) and len(r) != len(scan_window[i + 1]):
                continue
            header_cells = r
            break

    if header_cells is None:
        sections = (_CSV_NO_HEADER_LABEL,) if scan_window else ()
    else:
        labels: list[str] = []
        seen: dict[str, None] = {}
        for cell in header_cells:
            if cell is None:
                continue
            label = " ".join(str(cell).split())
            if not label:
                continue
            if not (SECTION_HEADER_MIN_CHARS <= len(label) <= SECTION_HEADER_MAX_CHARS):
                continue
            if label in seen:
                continue
            seen[label] = None
            labels.append(label)
            if len(labels) >= MAX_SECTIONS_PER_FILE:
                break
        sections = tuple(labels) if labels else (_CSV_NO_HEADER_LABEL,)

    text_iter = csv.reader(io.StringIO(decoded), dialect)
    text_parts: list[str] = []
    for row in itertools.islice(text_iter, _CSV_TEXT_ROWS_MAX):
        cells = [str(c) if c is not None else "" for c in row[:_CSV_TEXT_COLS_PER_ROW]]
        text_parts.append("\t".join(cells) + "\n")

    return ExtractionResult(
        text="".join(text_parts),
        status="ok",
        sections=sections,
    )


def _is_pure_number(s: str) -> bool:
    """True iff s parses as int or float (no exception). Used by the
    has_header-False fallback scan to reject all-numeric rows."""
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        return False
```

- [ ] **Step 5: Swap `.csv` in `EXTRACTORS`**

Edit `fda/organize/_extractors.py`. In the `EXTRACTORS` dict (around line 372-383), change `".csv": _read_text,` to `".csv": _extract_csv,`. The full block becomes:

```python
EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _extract_csv,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
}
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvSections -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): _extract_csv happy path + register"
```

---

## Task 4: Encoding fallback — UTF-8 BOM, CP949, decode failure

**Note on TDD discipline:** Task 3 already coded the decode chain `utf-8-sig → cp949 → failed`. This task locks the contract in regression tests. Tests are expected to PASS the first time they run. If any fails, the Task 3 implementation deviated from spec — fix the implementation, do not loosen the test.

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestCsvEncoding` class)

- [ ] **Step 1: Append encoding tests**

```python
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
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvEncoding -v`
Expected: PASS (4 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): csv encoding fallback (utf-8-sig BOM, cp949, decode failure)"
```

---

## Task 5: Delimiter sniffing — semicolon, tab, pipe, single-column

**Note on TDD discipline:** Task 3 already wired `csv.Sniffer().sniff()` with `csv.excel` fallback. This task locks delimiter handling in regression tests, including the v1 single-column NoHeader limitation.

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestCsvDelimiter` class)

- [ ] **Step 1: Append delimiter tests**

```python
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
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvDelimiter -v`
Expected: PASS (5 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): csv delimiter sniff (semicolon/tab/pipe + single-column NoHeader)"
```

---

## Task 6: Header detection — `has_header()=False` fallback scan rules

The fallback scan in Task 3's implementation enforces all five rules (≥ 2 non-empty cells; length guard; no pure numbers; all distinct; cell count matches next row OR R is final). This task locks each rule via dedicated regression tests, and the all-rules-fail → NoHeader case.

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestCsvHeaderDetection` class)

- [ ] **Step 1: Append fallback-scan tests**

```python
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
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvHeaderDetection -v`
Expected: PASS (8 tests). If a test fails, the Task 3 implementation deviated from a rule — fix the implementation.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): csv has_header-False fallback scan with 5 rules"
```

---

## Task 7: Sections post-processing — length guard, dedupe, cap, all-fail-NoHeader

The length-guard / dedupe / cap pipeline runs over the cells of whichever row is selected as the header (whether by has_header=True or by the scan). This task locks the post-processing contract.

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestCsvSectionsPostProcessing` class)

- [ ] **Step 1: Append post-processing tests**

```python
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
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvSectionsPostProcessing -v`
Expected: PASS (5 tests). If any fails, the Task 3 implementation deviated from the post-processing contract — fix the implementation.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): csv sections length guard, dedupe, cap, NoHeader fallback"
```

---

## Task 8: Text serialization — caps, embedded newlines, quoted delimiters, line endings

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestCsvText` class)

- [ ] **Step 1: Append text-serialization tests**

```python
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
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvText -v`
Expected: PASS (6 tests). If any fails, fix the Task 3 implementation.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): csv text serialization (caps, quoted newlines/delims, CRLF)"
```

---

## Task 9: Edge cases — zero-byte, whitespace-only, NUL byte, oversized file

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestCsvEdgeCases` class)

- [ ] **Step 1: Append edge-case tests**

```python
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
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestCsvEdgeCases -v`
Expected: PASS (4 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): csv edge cases (zero-byte, whitespace, NUL, oversize)"
```

---

## Task 10: Reader integration — `CatalogEntry.sections` populated for `.csv`

**Files:**
- Modify: `tests/test_organize_reader.py:694` (extend `TestSectionsPropagationDocxXlsx` with parallel csv cases — note: the class name is historical; keep it)

The exact reader API from the existing docx/xlsx/pptx tests is:
- `reader.read(workspace, backend=fake_backend, logger=logger)` returns a catalog
- `catalog.entries` is a list; each entry has `.path` (string), `.sections` (tuple), `.extract_status`, `.verbatim_head`
- `e.path.endswith("foo.csv")` is the matcher convention
- `workspace`, `fake_backend`, `logger` are pytest fixtures already defined in the file

- [ ] **Step 1: Add two csv tests inside `class TestSectionsPropagationDocxXlsx`**

Open `tests/test_organize_reader.py`, find `class TestSectionsPropagationDocxXlsx` (around line 694), and append these two methods inside it (immediately after the existing `test_pptx_failed_extraction_yields_empty_sections_in_catalog` method):

```python
    def test_csv_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        from fda.organize import reader

        f = workspace / "data.csv"
        f.write_text(
            "customer_id,order_date,amount\n"
            "1,2024-01-01,100.00\n"
            "2,2024-01-02,200.00\n"
        )

        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("data.csv"))
        assert e.extract_status == "ok"
        assert e.sections == ("customer_id", "order_date", "amount")
        # Verbatim head reflects the tab-normalized grid (delimiter normalized).
        assert e.verbatim_head.startswith("customer_id\torder_date\tamount")

    def test_csv_failed_extraction_yields_empty_sections_in_catalog(
        self, workspace, fake_backend, logger
    ):
        """Parallel to docx/xlsx/pptx: when the csv extractor fails (e.g. NUL
        byte triggers csv.Error mid-iteration), the catalog entry's sections
        is preserved as ()."""
        from fda.organize import reader

        f = workspace / "broken.csv"
        # NUL byte forces csv.reader to raise csv.Error('line contains NUL').
        f.write_bytes(b"name,age\nalice,30\n\x00bob,25\n")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("broken.csv"))
        assert e.extract_status == "failed"
        assert e.sections == ()
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx::test_csv_sections_flow_through_reader tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx::test_csv_failed_extraction_yields_empty_sections_in_catalog -v`
Expected: PASS (2 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_reader.py
git commit -m "organize(reader): csv sections preserved through reader (passthrough + failure)"
```

---

## Task 11: Pipeline integration — add `.csv` to the integration corpus

**Files:**
- Modify: `tests/test_organize_pipeline.py:17-51` (extend the `workspace` fixture with one csv)
- Modify: `tests/test_organize_pipeline.py` (`test_full_pipeline` and `test_preview_returns_plan_without_executing` assertions)

- [ ] **Step 1: Extend the `workspace` fixture with a csv**

Open `tests/test_organize_pipeline.py`. In the `workspace` fixture (lines 17-51), immediately **after** the pptx block ending at `prs.save(str(pptx_path))` (line 49), and **before** `return root`, add:

```python

    # Minimal .csv (one header row, one data row).
    csv_path = root / "data.csv"
    csv_path.write_text("customer_id,order_date\n1,2024-01-01\n")
```

- [ ] **Step 2: Extend `test_full_pipeline` to assert the csv is moved**

In `class TestOrganize`, method `test_full_pipeline` (around line 98), after the existing `assert (workspace / "Texts" / "deck.pptx").exists()` line (line 116), add:

```python
        assert (workspace / "Texts" / "data.csv").exists()
```

- [ ] **Step 3: Extend `test_preview_returns_plan_without_executing` to assert csv survives preview**

In the same class, method `test_preview_returns_plan_without_executing` (around line 120), after the existing `assert (workspace / "deck.pptx").exists()` line (line 134), add:

```python
        assert (workspace / "data.csv").exists()
```

- [ ] **Step 4: Run the pipeline test**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -v`
Expected: PASS — `test_full_pipeline` and `test_preview_returns_plan_without_executing` both pass; the csv flows through the pipeline alongside docx/xlsx/pptx.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_organize_pipeline.py
git commit -m "organize(integration): csv flows through reader and pipeline"
```

---

## Done condition

- `fda/organize/_extractors.py` defines five new `_CSV_*` constants, the `_CSV_NO_HEADER_LABEL` string, the `_extract_csv` function, and the `_is_pure_number` helper. `EXTRACTORS` registers `.csv` against `_extract_csv` (replacing `_read_text`).
- All test classes added in Tasks 3–11 are green.
- Two pre-existing tests updated in Task 2 (`test_other_text_extensions_route_to_read_text` parametrize; `test_empty_when_extraction_returns_no_sections` fixture switch) remain green.
- Full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`.
- Eleven commits on `dev_branch`, each tied to one task.

After completion, the Obsidian "Future Plan - Structural Sections Across Formats" note should be updated by the user to reflect `.csv` shipped (parallel to the docx/xlsx update on 2026-05-07 and the pptx update on 2026-05-08). That update is **not** an implementation step — it's the user's record-keeping after merge.
