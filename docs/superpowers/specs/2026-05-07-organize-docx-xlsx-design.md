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
- `pyproject.toml` — add `python-docx` and `openpyxl` as required runtime dependencies.
- `fda/organize/_extractors.py` — add `_extract_docx`, `_extract_xlsx`; register `.docx` and `.xlsx` in `EXTRACTORS`; introduce shared `_TEXT_CAP_BYTES = 64 * 1024` constant (replacing the PDF-specific `_PDF_PIPE_CAP_BYTES`); add three new xlsx-specific constants.
- `tests/test_organize_extractors.py` — synthetic in-test fixtures and case coverage for both formats.
- `tests/test_organize_reader.py` — reader passes `sections` through for docx/xlsx (parallel to existing PDF/text coverage).
- `tests/test_organize_constraints.py` — register the four new constants in the CONSTS check.
- `tests/test_organize_pipeline.py` — add a docx + xlsx file to the integration corpus.

**Files not touched:** `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, all skill prompts.

## `_extract_docx`

```python
def _extract_docx(path: Path) -> ExtractionResult: ...
```

Loader: `docx.Document(path)` (python-docx).

**`sections`** — heading-style paragraphs in document order:
- Iterate `doc.paragraphs`. Capture paragraph text where `paragraph.style.name` matches the regex `^Heading [1-9]$` or equals `"Title"`. Word's default template uses `Title` for the document title — a strong structural signal worth keeping alongside the heading levels.
- Trim whitespace; collapse internal whitespace via `" ".join(s.split())`.
- Skip if the resulting label is empty or shorter than `SECTION_HEADER_MIN_CHARS` (3).
- Preserve document order; dedupe via ordered-set pattern (same as `_sections.py`).
- Cap total at `MAX_SECTIONS_PER_FILE` (15).
- No regex fallback over body text. If a doc has no heading styles, `sections=()`. We accept this limitation; if real-world docx files turn out to use bold-only "headings", that's a v2.1 task.

**`text`** — paragraph text concatenated in document order:
- `"\n".join(p.text for p in doc.paragraphs if p.text)`.
- Truncate at `_TEXT_CAP_BYTES` (64 KiB) by character slice — Python strings are pre-decoded, so no byte-boundary risk.
- Drives `verbatim_head` (first 300 chars seen by classifier) + Haiku summary.
- The `text` and `sections` iterations are independent passes over `doc.paragraphs`. The 64 KiB text cap does not truncate section capture; a doc whose body exceeds 64 KiB still yields its full heading list (subject only to `MAX_SECTIONS_PER_FILE`).

**Failure modes:**
- `PackageNotFoundError`, `KeyError`, or other library exceptions → caught by `extract()`'s outer try/except → `status="failed"`, `note=str(e)`, `text=None`, `sections=()`.
- Empty doc (no paragraphs) → `text=""`, `sections=()`, `status="ok"`.
- Encrypted / password-protected docx → library raises on open → `status="failed"`. We don't try to decrypt.
- No `tool_missing` branch — `python-docx` is a required dep.

## `_extract_xlsx`

```python
def _extract_xlsx(path: Path) -> ExtractionResult: ...
```

**Two workbook loads** are required because openpyxl strips formulas when `data_only=True`:
1. **Pass 1** — `openpyxl.load_workbook(path, data_only=True, read_only=True)` for `text` (computed values) and column-header sections.
2. **Pass 2** — `openpyxl.load_workbook(path, data_only=False, read_only=True)` for formula-cell counting and merged-range counting.

Both loads use `read_only=True` for memory safety on large workbooks. Wrap each in a `with` block to release the underlying zip handle.

**`sections`** — sheet names + first-row column headers + synthesized purpose labels:
- For each sheet in `wb.sheetnames` (pass 1; include hidden sheets — they're structural):
  - Append `f"Sheet:{name}"` always (one entry per sheet — sheet name is structure even when sheet is empty).
  - Read row 1 via `next(ws.iter_rows(values_only=True, max_row=1), ())`. For each cell value: skip None/empty; `str(v).strip()`; collapse internal whitespace; skip if shorter than `SECTION_HEADER_MIN_CHARS`. Append as section label.
- Dedupe via ordered set; cap total at `MAX_SECTIONS_PER_FILE` (15) — a 20-sheet workbook will have its tail truncated.
- After sheet/column labels are accumulated, append synthesized purpose labels (also subject to the global cap):
  - `"FormulaHeavy"` if `formula_cells / non_empty_cells > _XLSX_FORMULA_DENSITY_THRESHOLD` (0.05). Counts come from pass 2: iterate cells, increment `non_empty_cells` for every cell whose value is not None, and increment `formula_cells` when `cell.data_type == "f"`.
  - `"MergedCells"` if `sum(len(ws.merged_cells.ranges) for ws in wb.worksheets) >= _XLSX_MERGED_CELLS_MIN` (3). Counts come from pass 2.

Synthesized labels appear *after* schema labels so they don't crowd out sheet/column names on wide workbooks.

Example output:
```
sections = ("Sheet:Orders", "Order ID", "Customer", "Amount",
            "Sheet:Customers", "Email", "Phone",
            "FormulaHeavy", "MergedCells")
```

**`text`** — serialized for `verbatim_head` + Haiku summary:
- For each sheet (pass 1, workbook order):
  - Append `f"Sheet: {name}\n"`.
  - Iterate up to `_XLSX_TEXT_ROWS_PER_SHEET` (20) rows via `iter_rows(values_only=True, max_row=20)`. For each row: `"\t".join("" if c is None else str(c) for c in row)`, then `"\n"`.
  - Blank line between sheets.
- Hard stop at `_TEXT_CAP_BYTES` (64 KiB); drop remaining sheets/rows once cap is reached.

**Failure modes:**
- `InvalidFileException`, `BadZipFile`, `KeyError` → outer try/except → `status="failed"`.
- Sheet-level exception during iteration → propagates to outer try/except; one bad sheet fails the whole file rather than producing partial state. Acceptable: this matches PDF behavior.
- Empty workbook (no sheets, rare) → `text=""`, `sections=()`, `status="ok"`.
- No `tool_missing` branch.

## Constants

| Name | Value | Module |
|---|---|---|
| `_TEXT_CAP_BYTES` | `64 * 1024` | `_extractors.py` (new; replaces `_PDF_PIPE_CAP_BYTES` — same value, single home, reused across all extractors) |
| `_XLSX_TEXT_ROWS_PER_SHEET` | `20` | `_extractors.py` (new) |
| `_XLSX_FORMULA_DENSITY_THRESHOLD` | `0.05` | `_extractors.py` (new) |
| `_XLSX_MERGED_CELLS_MIN` | `3` | `_extractors.py` (new) |
| `MAX_SECTIONS_PER_FILE` | `15` | `_sections.py` (existing — reused as global cap) |
| `SECTION_HEADER_MIN_CHARS` | `3` | `_sections.py` (existing — reused) |

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
- Empty doc (no paragraphs) → `text=""`, `sections=()`, `status="ok"`
- Whitespace-only / 2-char heading text → filtered out
- Corrupt file (bytes that aren't a docx) → `status="failed"`

**xlsx cases**:
- Single sheet with header row → `("Sheet:<name>", header1, header2, ...)`
- Multi-sheet workbook → all sheets in order, labels concatenated, capped at 15
- Empty cells in row 1 → skipped, others kept
- Sheet with no row-1 content → only `Sheet:<name>` for that sheet
- Hidden sheet → included in sections
- Formula density above threshold → `"FormulaHeavy"` appended
- Formula density at/below threshold → `"FormulaHeavy"` not appended
- Merged-range count ≥ 3 → `"MergedCells"` appended
- Merged-range count < 3 → `"MergedCells"` not appended
- Encrypted / corrupt → `status="failed"`
- Empty workbook → `text=""`, `sections=()`

**Reader test** (`tests/test_organize_reader.py`): docx and xlsx files flow through reader; assert `CatalogEntry.sections` is populated and survives extractor failure (sections preserved as `()`, parallel to existing PDF/text coverage).

**Constants test** (`tests/test_organize_constraints.py`): four new constants registered in the CONSTS check.

**Classifier shape-budget test** (`tests/test_organize_classifier.py`): unchanged — payload shape stable; existing assertion that `sections` is included in payload still holds.

**Pipeline integration** (`tests/test_organize_pipeline.py`): add at least one docx and one xlsx to the integration corpus; assert end-to-end moves complete without error.

**Full suite** must pass — 113 existing tests + ~12 new tests = ~125 total.

## Sequencing context

This spec is sub-project 1 of a larger non-PDF format expansion. Subsequent sub-projects (each its own spec → plan → implementation cycle) in expected priority order:

1. **This spec** — `.docx` + `.xlsx`
2. **Korean structural-fingerprint coverage in `_sections.py`** — corpus-blocked; pending real Korean document samples. v1 regex is `[A-Z]`-anchored and will not match Korean headers.
3. **`.hwp`** — pairs with (2). Goes plaintext → regex via `hwp5txt` / `pyhwp`. Without (2), Korean files yield empty `sections`.
4. Remaining backlog (`.pptx`, `.csv` upgrade, `.doc`/`.xls`, `.eml`/`.msg`) — driven by what Lion Chemtech actually uploads.

Tracked in Obsidian: `Future Plan - Structural Sections Across Formats.md`.
