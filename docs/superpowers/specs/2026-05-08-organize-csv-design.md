# Organize: `.csv` Extractor — Design

**Date:** 2026-05-08
**Branch:** dev_branch
**Predecessor:** `.pptx` extractor — shipped 2026-05-08 (commit `352b72c`)
**Tracker:** `Future Plan - Structural Sections Across Formats.md` (Obsidian, Lion_Chemtech/FDA)

## Goal

Replace the current generic `_read_text` handler for `.csv` with a dedicated `_extract_csv` that produces structural `sections` (column headers) and a serialized `text` view that mirrors xlsx, so the classifier sees a uniform shape across tabular formats. Today `.csv` flows through `_read_text` — Korean files corrupt under `errors="replace"`, the plaintext heading regex in `_sections.py` rarely matches `id,name,email`, and `text` is the raw bytes. The new extractor handles encoding, delimiter detection, header detection, length-guarded sections, and a tab-normalized text grid.

`sections` for `.csv` is the ordered list of row-1 (or first-detected-header-row) column labels, with one synthesized fallback label `"NoHeader"` when no usable header is found.

## Non-goals

- `.tsv` as a separate registered extension — add later if the corpus shows mis-named TSV files surviving `csv.Sniffer`.
- Synthesized purpose labels beyond `"NoHeader"` (e.g. `Wide`, `LongTable`, `NumericHeavy`) — corpus-blocked, parked. Add later only if Lion Chemtech corpus shows real ambiguity that headers cannot resolve.
- Any encoding beyond UTF-8 (BOM-tolerant) and CP949. Other Korean encodings (`euc-kr` proper, `iso-2022-kr`) are subsumed by CP949 in practice for files Excel produces; UTF-16 / Latin-1 are out.
- Any CSV dialect quoting/escaping fixes — accept whatever `csv.Sniffer` decides; let `csv.reader` handle `quotechar`/`escapechar` per the sniffed dialect.
- Type inference, column-statistics, schema validation — out. Only the headers are structural; serialized cells are strings.
- Reader, classifier, plan_builder, executor, verifier, prompts, models — no changes.
- Skill prompt revisions — same rationale as pptx; prompts already consume `sections` as opaque `tuple[str, ...]`.
- Korean structural-fingerprint regex coverage in `_sections.py` — separate sub-project; not invoked by `_extract_csv` because column headers come from the format-native `csv.reader`, not the regex.

## Architecture

Faithful extension of the docx/xlsx/pptx format-onboarding pattern: one extractor function per format, registered in the `EXTRACTORS` dict. No new modules, no protocol abstraction, no rewrite of existing extractors.

**Files touched:**
- `fda/organize/_extractors.py` — add `_extract_csv`; replace `".csv": _read_text` with `".csv": _extract_csv` in `EXTRACTORS`; add five new csv-specific constants and one synthesized-label string. Existing extractors are untouched.
- `tests/test_organize_extractors.py` — synthetic in-test fixtures and case coverage.
- `tests/test_organize_reader.py` — Reader passes `sections` through for csv (parallel to existing PDF/text/docx/xlsx/pptx coverage).
- `tests/test_organize_constraints.py` — register the five new constants and the `_CSV_NO_HEADER_LABEL` string in the CONSTS check.
- `tests/test_organize_pipeline.py` — add a csv file to the integration corpus.

**Files not touched:** `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, all skill prompts, `pyproject.toml`.

**No new dependency.** Implementation uses only Python stdlib `csv` and `io`.

**Text contract:** `_extract_csv` does **not** apply its own 64 KiB cap on `text`. Reader owns that contract via `READER_TEXT_CAP_BYTES` (`reader.py:29`) and applies byte-boundary-safe truncation plus the `[TRUNCATED at 64KB]` marker. The new constants exist only to bound in-process memory while reading, sniffing, scanning headers, and serializing — never as the contract cap.

## `_extract_csv`

```python
def _extract_csv(path: Path) -> ExtractionResult: ...
```

### Step 1 — read raw bytes, decode

- Read up to `_CSV_READ_BYTES_MAX` bytes from `path`. Larger files are read up to the cap and processed against that prefix; the prefix is sufficient for header detection and 20-row serialization. (Out-of-cap content is silently dropped at the extractor; it never reaches Reader.)
- Decode attempt 1: `bytes.decode("utf-8-sig")` (strict). The `utf-8-sig` codec strips a leading BOM if present and decodes plain UTF-8 normally otherwise.
- On `UnicodeDecodeError`, attempt 2: `bytes.decode("cp949")` (strict). Covers Excel-Korean exports.
- On both failing → `ExtractionResult(text=None, status="failed", note=str(e), sections=())`.

### Step 2 — sniff the delimiter

- Take the first `_CSV_SNIFF_SAMPLE_CHARS` characters of the decoded string as a sample.
- `dialect = csv.Sniffer().sniff(sample)`. On `csv.Error` → `dialect = csv.excel` (comma, default quoting).
- Known sniff() failure modes accepted: single-column files (no delimiter to detect), short-first-line preambles biasing detection, samples where commas appear mostly inside quoted text. In all cases the comma fallback is acceptable — the design's correctness does not depend on perfect delimiter detection.

### Step 3 — find the header row

- `has_header = csv.Sniffer().has_header(sample)`. On `csv.Error` → `False`.
- Reset to a fresh `csv.reader(io.StringIO(decoded_text), dialect)` for header scanning.
- If `has_header` is `True`: row 1 is the header.
- If `has_header` is `False`: scan rows 1 through `_CSV_HEADER_SCAN_ROWS` (5). Pick the first row R where **all five** of these hold:
  1. R has ≥ 2 non-empty cells.
  2. Every non-empty cell in R passes the length guard (`SECTION_HEADER_MIN_CHARS` ≤ stripped-and-collapsed length ≤ `SECTION_HEADER_MAX_CHARS`).
  3. No cell in R parses as a pure number (`int(cell)` or `float(cell)` succeeds without raising).
  4. All non-empty cells in R are distinct (case-sensitive comparison after strip-and-collapse).
  5. R's total cell count equals the cell count of the row immediately following R, OR R is the final row in the file.
- If no row in the scan window qualifies → no header found.

Rule (5) is the preamble defense: a free-form preamble row almost always has a different cell count than the data rows that follow it.

### Step 4 — build `sections`

- If a header row was found:
  - For each cell in the header row: strip outer whitespace, collapse internal whitespace via `" ".join(s.split())`.
  - Apply the length guard `SECTION_HEADER_MIN_CHARS` (3) ≤ len ≤ `SECTION_HEADER_MAX_CHARS` (40). Drop cells outside the range.
  - Ordered-set dedupe (preserve first occurrence).
  - Cap at `MAX_SECTIONS_PER_FILE` (15).
  - If the resulting list is non-empty: `sections = tuple(labels)`.
  - If the resulting list is empty (every cell failed the length guard): `sections = (_CSV_NO_HEADER_LABEL,)`.
- If no header row was found: `sections = (_CSV_NO_HEADER_LABEL,)`.

`_CSV_NO_HEADER_LABEL = "NoHeader"` is a module-level string literal in `_extractors.py`. It is appended directly — the length guard is irrelevant to it (the literal is project-controlled, and 8 chars happens to satisfy the guard anyway). This mirrors how `FormulaHeavy` and `MergedCells` are appended in `_extract_xlsx` outside the column-header pipeline.

**Cases that produce `sections = (_CSV_NO_HEADER_LABEL,)`:**
- `has_header()` returns `False` AND no scan row qualifies under rules (1)–(5).
- `has_header()` returns `True` but row 1 produces zero usable labels after the length guard.
- File decodes to ≥ 1 byte but parses to zero rows (whitespace-only, single newline, only-quotes etc.).
- Single-column file (under rule (1)'s "≥ 2 non-empty cells" requirement — accepted v1 limitation).
- All header-candidate cells fail the length guard.

**Explicit exception:** zero-byte file → `text=""`, `sections=()`, `status="ok"`. Nothing to assess; no fallback label.

### Step 5 — build `text`

- Re-iterate from the start with a fresh `csv.reader(io.StringIO(decoded_text), dialect)`.
- Take up to `_CSV_TEXT_ROWS_MAX` (20) physical rows total from the start of the file. No special header treatment — matches xlsx's per-sheet 20-row cap (which also includes the header row in the 20).
- Per row: take up to `_CSV_TEXT_COLS_PER_ROW` (32) cells, `\t`-join them with `"\t".join(cells)`, append `"\n"`.
- No banner. CSV has no analogue to `Sheet:` or `Slide N:`.
- No extractor-side byte cap. Reader applies `READER_TEXT_CAP_BYTES`.
- Drives `verbatim_head` (first 300 chars seen by classifier) + Haiku summary.

The sniffed delimiter is normalized to tab in the serialized output, so the classifier sees a uniform shape across `.csv`, `.xlsx`, and (future) `.tsv`.

## Failure modes

- File unreadable (OS error on read) → `status="failed"`, `note=str(e)`.
- Both UTF-8-sig and CP949 decode fail → `status="failed"`, `note=str(e)`.
- Empty file (zero bytes) → `text=""`, `sections=()`, `status="ok"`.
- Decoded but parses to zero rows (whitespace-only) → `text=""`, `sections=("NoHeader",)`, `status="ok"`.
- `csv.Sniffer.sniff()` raises → comma-default dialect; continue.
- `csv.Sniffer.has_header()` raises → treat as `False`; fall through to scan.
- `csv.reader` raises mid-iteration (e.g. NUL byte → `csv.Error("line contains NUL")`) → propagates to outer `extract()` try/except → `status="failed"`. Matches docx/xlsx/pptx behavior.
- `_CSV_NO_HEADER_LABEL` already present as a literal column header → ordered-set dedupe handles it; `sections` will not contain `"NoHeader"` twice.

## Constants

| Name | Value | Module | Purpose |
|---|---|---|---|
| `_CSV_READ_BYTES_MAX` | `4 * 1024 * 1024` | `_extractors.py` (new) | Bounds in-process memory for `path.read_bytes()`. 4 MiB covers all realistic header/header-fallback/20-row cases; larger files are sampled against the prefix. |
| `_CSV_SNIFF_SAMPLE_CHARS` | `8 * 1024` | `_extractors.py` (new) | Character-count sample handed to `csv.Sniffer.sniff()` and `has_header()`. Applied to the already-decoded string. Standard ~8 K; weak for very long preambles, accepted limitation. |
| `_CSV_HEADER_SCAN_ROWS` | `5` | `_extractors.py` (new) | Number of rows scanned in the `has_header()`-False fallback. Conservative; metadata-heavy exports with > 5 preamble rows accepted limitation. |
| `_CSV_TEXT_ROWS_MAX` | `20` | `_extractors.py` (new) | Caps physical rows in serialized `text`. Matches xlsx's 20-row-per-sheet cap. |
| `_CSV_TEXT_COLS_PER_ROW` | `32` | `_extractors.py` (new) | Caps cells per serialized row. Matches xlsx's 32-col cap. |
| `_CSV_NO_HEADER_LABEL` | `"NoHeader"` | `_extractors.py` (new) | Synthesized fallback section label when no usable header is found. Single-source-of-truth string literal. |
| `MAX_SECTIONS_PER_FILE` | `15` | `_sections.py` (existing) | Reused as global cap. |
| `SECTION_HEADER_MIN_CHARS` | `3` | `_sections.py` (existing) | Reused length guard. |
| `SECTION_HEADER_MAX_CHARS` | `40` | `_sections.py` (existing) | Reused length guard. |
| `READER_TEXT_CAP_BYTES` | `64 * 1024` | `reader.py` (existing) | Owns the 64 KiB text contract for **all** extractors — csv does not duplicate. |

The five new `_CSV_*` constants and the `_CSV_NO_HEADER_LABEL` string must be registered in `tests/test_organize_constraints.py`.

## Skill prompts

Unchanged. The v1 prompts already teach both classifier stages to use `sections` as a structural fingerprint. CSV column-header labels flow through the same `tuple[str, ...]` shape used by docx headings, xlsx schema labels, and pptx slide titles. The `"NoHeader"` synthesized label is opaque to the prompts; it acts as a structural "signal absent" indicator that the classifier can correlate with filename and verbatim_head.

(Pre-existing aside, not addressed here: the `taxonomy-proposer` / `taxonomy-assigner` SKILL.md docs still describe `sections` as a regex-derived signal. That description is already stale for docx/xlsx/pptx and will be staler with csv. The same separate documentation cleanup PR called out in the pptx spec applies.)

## Testing plan

Synthetic fixtures generated in-test by writing bytes/strings to `tmp_path` files (no binary fixtures checked in), mirroring how docx/xlsx/pptx tests work.

**Cases in `tests/test_organize_extractors.py`:**

*Happy path & encoding:*
- Comma CSV with clean ASCII headers → headers in `sections` in order, header + 1 data row in `text` `\t`-joined.
- UTF-8 CSV with Korean headers → headers decoded correctly, no `﻿` artifact.
- UTF-8-with-BOM CSV → BOM stripped from first header label and from `text`.
- CP949-encoded CSV with Korean headers → falls back to CP949 decode, headers land in `sections`.
- Bytes that decode under neither UTF-8-sig nor CP949 → `status="failed"`.

*Delimiter sniffing:*
- Semicolon-delimited (Excel-EU locale) `.csv` → sniffed as `;`, headers land normally, `text` is `\t`-joined (delimiter normalized).
- Tab-delimited mis-named `.csv` (i.e. content is TSV) → sniffed as `\t`, headers land normally.
- Pipe-delimited `.csv` → sniffed as `|`, headers land normally.
- Single-column file (no delimiter present) → sniff falls back to comma; `csv.reader` yields 1-cell rows; header rule (1) rejects → `sections=("NoHeader",)`. **Documents v1 limitation.**

*Header detection:*
- `has_header()=True` happy path → row 1 in `sections`.
- `has_header()=False`, row 1 is numeric data → no scan row qualifies (rule 3 rejects) → `sections=("NoHeader",)`.
- Preamble row ("Customer Export 2024-Q3,,,") + headers on row 2 + data rows of width 5 → row 1 rejected by rule (5) (cell count differs from row 2); row 2 qualifies → row 2 headers in `sections`.
- Multi-row preamble (3 lines) + headers on row 4 + data → row 4 qualifies; rows 1–3 rejected.
- Garbage row 1 + numeric rows 2+ → no row qualifies (rule 3) → `sections=("NoHeader",)`.
- Two-column preamble row that happens to have the same cell count as data rows → rule (5) cannot reject; relies on rules (1)–(4); accepted false-positive risk in v1, document as known limitation.
- Header row where every cell is < 3 chars (e.g. `a,b,c,d`) → length guard drops all → `sections=("NoHeader",)`.
- Header row mixing valid and invalid cells (e.g. `a,name,bb,email`) → only `name`, `email` survive length guard; `sections=("name","email")`.
- Numeric-looking but valid headers (`2024,2025,2026`) under `has_header()=True` → land in `sections` (length guard passes; rule 3 only applies to fallback scan, not to trusted row 1).
- Single-row file whose only row passes rules (1)–(4) and rule (5)'s "no following row" branch → headers in `sections`.

*Length guard, dedupe, caps:*
- Header > 40 chars → dropped by `SECTION_HEADER_MAX_CHARS`.
- Header < 3 chars → dropped by `SECTION_HEADER_MIN_CHARS`.
- Duplicate headers (`id,id,name`) → ordered-set dedupe; `sections=("id","name")`.
- > 15 distinct headers → cap at 15.
- Header literally named `"NoHeader"` plus other valid headers → `"NoHeader"` appears once in `sections`, not duplicated.

*Text serialization:*
- > 20 rows → `text` capped at 20 physical rows.
- > 32 columns → each `text` row trimmed to 32 cells.
- Quoted embedded newline (`"line1\nline2",b,c`) → `csv.reader` yields one record; serialized as one `text` row.
- Quoted delimiter inside cell (`"a,b",c`) → cell preserved as-is, not split.
- Trailing comma (`a,b,`) → trailing empty cell preserved in `text`.
- Mixed CRLF / LF line endings → `csv.reader` handles both; `text` rows match logical-record boundaries.

*Edge cases:*
- Zero-byte file → `text=""`, `sections=()`, `status="ok"`.
- Whitespace-only file (e.g. `"\n\n\n"`) → `text=""`, `sections=("NoHeader",)`, `status="ok"`.
- File contains a NUL byte → `csv.reader` raises `csv.Error("line contains NUL")` → outer `extract()` catches → `status="failed"`.
- File larger than `_CSV_READ_BYTES_MAX` → only the prefix is processed; no error; `text` reflects the prefix.

**Reader test** (`tests/test_organize_reader.py`): csv files flow through Reader; assert `CatalogEntry.sections` is populated and survives extractor failure (sections preserved as `()`, parallel to existing PDF/text/docx/xlsx/pptx coverage).

**Constants test** (`tests/test_organize_constraints.py`): five new `_CSV_*` constants and `_CSV_NO_HEADER_LABEL` registered in the CONSTS check.

**Classifier shape-budget test** (`tests/test_organize_classifier.py`): unchanged — payload shape stable; existing assertion that `sections` is included in payload still holds.

**Pipeline integration** (`tests/test_organize_pipeline.py`): add at least one csv to the integration corpus; assert end-to-end moves complete without error.

**Full suite** must pass via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`. Exact count is not an acceptance criterion — green is.

## Sequencing context

This spec is sub-project 3 of the larger non-PDF format expansion. Position in the rollout:

1. ✅ `.docx` + `.xlsx` — shipped 2026-05-07.
2. ✅ `.pptx` — shipped 2026-05-08 (commit `352b72c`).
3. **This spec** — `.csv`.
4. **Korean structural-fingerprint coverage in `_sections.py`** — corpus-blocked; pending real Korean document samples. Not blocking csv (column headers come from `csv.reader`, language-agnostic).
5. **`.hwp`** — pairs with (4). Goes plaintext → regex via `hwp5txt` / `pyhwp`. Without (4), Korean files yield empty `sections`.
6. Remaining backlog (`.tsv` registration, `.doc`/`.xls`, `.eml`/`.msg`, synthesized labels for csv) — driven by what Lion Chemtech actually uploads.
