# Organize: `.docx` + `.xlsx` Extractors — Design

**Date:** 2026-05-07
**Branch:** dev_branch
**Predecessor:** structural-sections v1 (PDF + plaintext, English) — shipped 2026-05-07
**Tracker:** `Future Plan - Structural Sections Across Formats.md` (Obsidian, Lion_Chemtech/FDA)

## Goal

Extend the structural-fingerprint signal pipeline beyond PDF/plaintext by adding extractors for `.docx` and `.xlsx`. Each extractor produces `ExtractionResult.text` (drives `verbatim_head` + Haiku summary) and `ExtractionResult.sections` (drives the classifier's structural-fingerprint signal).

For `.xlsx`, also synthesize two purpose-discriminating labels — `"FormulaHeavy"` and `"MergedCells"` — so the fingerprint distinguishes Excel files used as data stores from Excel files used for office computation, which can otherwise share an identical schema fingerprint.

## Non-goals

- `.hwp`, `.pptx`, `.doc`, `.xls`, `.eml`, `.msg` — separate sub-projects.
- `.csv` upgrade beyond the existing generic regex — separate sub-project.
- Korean structural-fingerprint regex coverage in `_sections.py` — corpus-blocked, parked as a separate sub-project (no Korean test corpus exists yet; v1 was tested English-only).
- Skill prompt revisions to `taxonomy-proposer` / `taxonomy-assigner` — they already consume `sections` as opaque `tuple[str, ...]`; new formats supply the same shape.
- Reader, classifier, plan builder, executor, verifier changes — none.

## Architecture

Faithful extension of the v1 format-onboarding pattern: one extractor function per format, registered in the `EXTRACTORS` dict. No new modules, no protocol abstraction, no rewrite of existing extractors.

**Files touched:**
- `pyproject.toml` — add `python-docx` as a required runtime dependency. (`openpyxl>=3.0.0` is already declared.)
- `fda/organize/_extractors.py` — add `_extract_docx`, `_extract_xlsx`; register `.docx` and `.xlsx` in `EXTRACTORS`; add four new xlsx-specific constants. `_PDF_PIPE_CAP_BYTES` stays as-is (subprocess pipe safety, not a contract cap).
- `tests/test_organize_extractors.py` — synthetic in-test fixtures and case coverage for both formats.
- `tests/test_organize_reader.py` — reader passes `sections` through for docx/xlsx (parallel to existing PDF/text coverage).
- `tests/test_organize_constraints.py` — register the four new constants in the CONSTS check.
- `tests/test_organize_pipeline.py` — add a docx + xlsx file to the integration corpus.

**Files not touched:** `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, all skill prompts.

**Text contract:** docx and xlsx extractors do **not** apply their own 64 KiB cap on `text`. Reader owns that contract via `READER_TEXT_CAP_BYTES` (`reader.py:29`) and applies it with byte-boundary-safe truncation plus the `[TRUNCATED at 64KB]` marker. Extractor-side caps in this spec exist only to bound in-process memory while serializing (rows, columns), never as the contract cap.

**openpyxl baseline:** target installed version is `3.0.9`. Two known constraints in this baseline shape the implementation:
- `Workbook` is **not** a context manager — use `try/finally: wb.close()` (or `contextlib.closing`), never `with load_workbook(...) as wb:`.
- `ReadOnlyWorksheet` does **not** expose `merged_cells`. The merged-range count must be done with `read_only=False`.

## `_extract_docx`

```python
def _extract_docx(path: Path) -> ExtractionResult: ...
```

Loader: `docx.Document(path)` (python-docx).

**`sections`** — heading-style paragraphs in document order:
- Iterate `doc.paragraphs`. Capture paragraph text where `paragraph.style.name` matches the regex `^Heading [1-9]$` or equals `"Title"`. Word's default template uses `Title` for the document title — a strong structural signal worth keeping alongside the heading levels.
- Trim whitespace; collapse internal whitespace via `" ".join(s.split())`.
- Apply both length guards from `_sections.py`: `SECTION_HEADER_MIN_CHARS` (3) ≤ `len(label)` ≤ `SECTION_HEADER_MAX_CHARS` (40). Drop labels that fall outside.
- Preserve document order; dedupe via ordered-set pattern (same as `_sections.py`).
- Cap total at `MAX_SECTIONS_PER_FILE` (15).
- No regex fallback over body text. Tables are **not** scanned for heading styles in v1 (Word table cells rarely carry `Heading N` style; risk/value not worth the surface area). If a doc has no heading-styled paragraphs, `sections=()`. v1 limitation: bold-only "headings" and table-cell headings are not detected.

**`text`** — paragraph and table-cell text in document order:
- Iterate `doc.paragraphs` and append non-empty `p.text`.
- Also append non-empty cell text from `doc.tables` (iterate `table.rows`, `row.cells`, `cell.text`). Many real business docs (invoices, forms, reports) carry their content in tables; omitting them silently strips the most informative text.
- Join with `"\n"`. No extractor-side byte cap — Reader applies `READER_TEXT_CAP_BYTES` on the contract side. The serialized string remains in-memory only; for typical office docs this is bounded by disk size of the source.
- Drives `verbatim_head` (first 300 chars seen by classifier) + Haiku summary.
- The `text` and `sections` collections are populated independently; truncation downstream of the extractor does not affect section capture.

**Failure modes:**
- `PackageNotFoundError`, `KeyError`, or other library exceptions → caught by `extract()`'s outer try/except → `status="failed"`, `note=str(e)`, `text=None`, `sections=()`.
- Empty doc (no paragraphs and no tables) → `text=""`, `sections=()`, `status="ok"`.
- Encrypted / password-protected docx → library raises on open → `status="failed"`. We don't try to decrypt.
- No `tool_missing` branch — `python-docx` is a required dep.

## `_extract_xlsx`

```python
def _extract_xlsx(path: Path) -> ExtractionResult: ...
```

**Two workbook loads** are required because openpyxl returns cached values (not formulas) when `data_only=True`:
1. **Pass 1** — `openpyxl.load_workbook(path, data_only=True, read_only=True)` for `text` (cached values) and column-header sections. Streamed; memory-bounded.
2. **Pass 2** — `openpyxl.load_workbook(path, data_only=False, read_only=False)` for formula-cell counting and merged-range counting. **Must be `read_only=False`** because `ReadOnlyWorksheet` in `openpyxl 3.0.9` does not expose `merged_cells`.

Use `try/finally: wb.close()` (or `contextlib.closing(load_workbook(...))`) to release the zip handle. Do **not** use `with load_workbook(...) as wb:` — `Workbook` is not a context manager in the targeted version.

**Cached-value caveat:** `data_only=True` only surfaces formula results that Excel cached at last save. Files written by openpyxl/other libraries without an Excel save round-trip will yield `None` for every formula cell. v1 policy: when a pass-1 cell is `None`, fall back to the pass-2 formula string (`f"={cell.value}"` if `cell.data_type == "f"`) when emitting `text`. This keeps `verbatim_head` informative for spec/template workbooks that were never opened in Excel.

**`sections`** — sheet names + first-row column headers + synthesized purpose labels:
- Compute synthesized labels first so we know how many tail slots to reserve:
  - `"FormulaHeavy"` if `non_empty_cells > 0` **and** `formula_cells / non_empty_cells > _XLSX_FORMULA_DENSITY_THRESHOLD` (0.05). When `non_empty_cells == 0`, the workbook is not formula-heavy by definition. Counts come from pass 2: iterate cells, increment `non_empty_cells` when `cell.value is not None`, and increment `formula_cells` when `cell.data_type == "f"`.
  - `"MergedCells"` if `sum(len(ws.merged_cells.ranges) for ws in wb.worksheets) >= _XLSX_MERGED_CELLS_MIN` (3). Counts come from pass 2 (`read_only=False`).
- Then build schema labels from pass 1 (include hidden sheets — they're structural):
  - For each sheet in `wb.sheetnames`: append `f"Sheet:{name}"` (one entry per sheet — sheet name is structure even when the sheet is empty).
  - Read row 1 via `next(ws.iter_rows(values_only=True, max_row=1), ())`. For each cell value: skip None/empty; `str(v).strip()`; collapse internal whitespace; apply both `SECTION_HEADER_MIN_CHARS` and `SECTION_HEADER_MAX_CHARS` length guards; append as section label.
- Dedupe schema labels via ordered set, then truncate to `MAX_SECTIONS_PER_FILE - len(synthesized)` so the synthesized labels are guaranteed slots in the tail. Concatenate `(schema_labels..., synthesized...)`.

Example output (with two synthesized labels triggering, schema capped at 13):
```
sections = ("Sheet:Orders", "Order ID", "Customer", "Amount",
            "Sheet:Customers", "Email", "Phone",
            "FormulaHeavy", "MergedCells")
```

**`text`** — serialized for `verbatim_head` + Haiku summary:
- For each sheet (pass 1, workbook order):
  - Append `f"Sheet: {name}\n"`.
  - Iterate up to `_XLSX_TEXT_ROWS_PER_SHEET` (20) rows via `iter_rows(values_only=True, max_row=20)`. For each row: take the leftmost `_XLSX_TEXT_COLS_PER_ROW` (32) cells, drop a trailing run of `None`/empty cells (avoids tab-padding sparse rows), then `"\t".join("" if c is None else str(c) for c in row_trimmed)`, then `"\n"`.
  - Blank line between sheets.
- No extractor-side byte cap — Reader applies `READER_TEXT_CAP_BYTES`. The row/column caps above bound in-process memory while building the string.

**Failure modes:**
- `InvalidFileException`, `BadZipFile`, `KeyError` → outer try/except → `status="failed"`.
- Sheet-level exception during iteration → propagates to outer try/except; one bad sheet fails the whole file rather than producing partial state. Acceptable: this matches PDF behavior.
- Workbook with only empty sheets → `text` contains `Sheet: <name>` headers only; `sections` contains `Sheet:<name>` entries plus any triggered synthesized labels; `status="ok"`.
- No `tool_missing` branch.

## Constants

| Name | Value | Module |
|---|---|---|
| `_XLSX_TEXT_ROWS_PER_SHEET` | `20` | `_extractors.py` (new) |
| `_XLSX_TEXT_COLS_PER_ROW` | `32` | `_extractors.py` (new) |
| `_XLSX_FORMULA_DENSITY_THRESHOLD` | `0.05` | `_extractors.py` (new) |
| `_XLSX_MERGED_CELLS_MIN` | `3` | `_extractors.py` (new) |
| `_PDF_PIPE_CAP_BYTES` | `64 * 1024` | `_extractors.py` (existing — unchanged; subprocess pipe safety only) |
| `READER_TEXT_CAP_BYTES` | `64 * 1024` | `reader.py` (existing — owns the 64 KiB text contract for **all** extractors) |
| `MAX_SECTIONS_PER_FILE` | `15` | `_sections.py` (existing — reused as global cap) |
| `SECTION_HEADER_MIN_CHARS` | `3` | `_sections.py` (existing — reused) |
| `SECTION_HEADER_MAX_CHARS` | `40` | `_sections.py` (existing — reused) |

The four new constants must be registered in `tests/test_organize_constraints.py`.

## Skill prompts

Unchanged. The v1 prompts already teach both classifier stages to use `sections` as a structural fingerprint. Synthesized labels and format-native labels flow through the same `tuple[str, ...]` shape. If post-ship classification on docx/xlsx reveals a discriminating axis the prompts can't see, that becomes a v2.1 prompt-tuning task.

## Testing plan

Synthetic fixtures generated in-test via `python-docx` / `openpyxl` (no binary fixtures checked in).

**docx cases** in `tests/test_organize_extractors.py`:
- Heading 1 + Heading 2 mix → labels in document order, deduped
- `Title` style alone → captured
- No headings → `sections=()`; `text` populated
- More than 15 headings → capped at 15
- Empty doc (no paragraphs and no tables) → `text=""`, `sections=()`, `status="ok"`
- Whitespace-only / 2-char heading text → filtered out via `MIN_CHARS`
- 41-char heading text → filtered out via `MAX_CHARS`
- Body content lives in `doc.tables` only (no paragraphs) → `text` includes table cell text; `sections=()`
- Corrupt file (bytes that aren't a docx) → `status="failed"`

**xlsx cases**:
- Single sheet with header row → `("Sheet:<name>", header1, header2, ...)`
- Multi-sheet workbook → schema labels in order, capped to `MAX_SECTIONS_PER_FILE - n_synthesized`, then synthesized labels appended
- Empty cells in row 1 → skipped, others kept
- 41-char column header → filtered out via `MAX_CHARS`
- Sheet with no row-1 content → only `Sheet:<name>` for that sheet
- Hidden sheet → included in sections
- Formula density above threshold → `"FormulaHeavy"` appended
- Formula density at/below threshold → `"FormulaHeavy"` not appended
- Workbook with `non_empty_cells == 0` → `"FormulaHeavy"` not appended (zero-guard)
- Cached value `None` for a formula cell → `text` falls back to formula string from pass 2
- Merged-range count ≥ 3 → `"MergedCells"` appended
- Merged-range count < 3 → `"MergedCells"` not appended
- Wide sheet (50 columns) → `text` row trimmed to `_XLSX_TEXT_COLS_PER_ROW` cells with trailing-empty run dropped
- Encrypted / corrupt → `status="failed"`
- Workbook with only empty sheets → `text` contains `Sheet:` headers; `sections` contains `Sheet:<name>` entries; `status="ok"`

**Reader test** (`tests/test_organize_reader.py`): docx and xlsx files flow through reader; assert `CatalogEntry.sections` is populated and survives extractor failure (sections preserved as `()`, parallel to existing PDF/text coverage).

**Constants test** (`tests/test_organize_constraints.py`): four new constants registered in the CONSTS check.

**Classifier shape-budget test** (`tests/test_organize_classifier.py`): unchanged — payload shape stable; existing assertion that `sections` is included in payload still holds.

**Pipeline integration** (`tests/test_organize_pipeline.py`): add at least one docx and one xlsx to the integration corpus; assert end-to-end moves complete without error.

**Full suite** must pass. New extractor cases approximately double v1's PDF coverage; exact count is not an acceptance criterion — `pytest -x -q` green is.

## Sequencing context

This spec is sub-project 1 of a larger non-PDF format expansion. Subsequent sub-projects (each its own spec → plan → implementation cycle) in expected priority order:

1. **This spec** — `.docx` + `.xlsx`
2. **Korean structural-fingerprint coverage in `_sections.py`** — corpus-blocked; pending real Korean document samples. v1 regex is `[A-Z]`-anchored and will not match Korean headers.
3. **`.hwp`** — pairs with (2). Goes plaintext → regex via `hwp5txt` / `pyhwp`. Without (2), Korean files yield empty `sections`.
4. Remaining backlog (`.pptx`, `.csv` upgrade, `.doc`/`.xls`, `.eml`/`.msg`) — driven by what Lion Chemtech actually uploads.

Tracked in Obsidian: `Future Plan - Structural Sections Across Formats.md`.
