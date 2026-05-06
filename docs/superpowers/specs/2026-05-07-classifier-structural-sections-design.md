# Classifier Structural-Sections Design

**Date:** 2026-05-07
**Status:** Spec — pending implementation plan
**Predecessor:** `2026-05-06-classifier-verbatim-head-design.md` (verbatim_head + filename-priority signals); transitively `2026-05-06-organize-skill-pipeline-design.md` (the four-stage pipeline this builds on).

## Context

After the verbatim-head + filename-priority work shipped (commits `253c05a` … `325b1b1`), per-class accuracy on `randomized-company-documents-2026-05-05-003` improved materially. A new residual failure mode now dominates:

**Documents that share topic and prose but differ in field/section structure are merged into the wrong category.**

Concrete example from the user's experiment:

| File | First non-empty content | Discriminator |
|---|---|---|
| Doc A — "purchase order" | `"Purchase Orders"` then `Order ID`, `Order Date`, `Products`, `Total` | Two named fields, one product list. |
| Doc B — "detailed order" | `"Order ID: …"` then `Shipping Details`, `Customer Details`, `Employee`, `Shipper`, `Order Details`, `Products`, `Total` | Six labeled sections plus the products list. |

To Haiku and to the Classifier, both documents look like "order document with widgets totaling $X." Summaries collapse the difference. Filenames are hashes (no signal). Both verbatim_heads contain `Order ID:` early; the discriminating sections (`Shipping Details:`, `Customer Details:`) appear in Doc B at characters > 100 but the Classifier today treats `verbatim_head` as a prose source rather than a structural fingerprint.

### Root cause

All three signals the Classifier currently sees — `summary`, `verbatim_head` prose, filename — describe **what the document is about** (its topic). None of them describe **what fields/sections the document contains** (its structure). When two document types share a topic, the system has no independent channel to disambiguate them.

For business documents specifically, a document type is largely *defined* by its field/section structure: a Purchase Order is a doc with `[PO #, Vendor, Items, Terms]`; a Shipping Order is a doc with `[Order #, Ship-To, Bill-To, Carrier, Items]`. Surfacing that structure as a first-class signal closes the residual gap.

## Goal

Recover ≥95% per-class accuracy on the failure cases the verbatim-head spec couldn't fix — pairs of categories that share topic but differ in structure — by:

- Adding a deterministic, format-specific **structural fingerprint** (`sections: tuple[str, ...]`) to every catalog entry, computed at extraction time without an LLM call.
- Surfacing `sections` to both Classifier stages alongside `summary`, `verbatim_head`, and `path`.
- Teaching both prompts to treat structural divergence as a primary signal that two documents are different types — not a tiebreaker.

The structural channel composes with what already ships (topic via summary/verbatim, identity via filename). When any one channel is uninformative, the others compensate.

## Non-goals

- Vision-based reading (rendering PDF pages to a vision model). Out of scope; revisit if structural sections still fall short.
- Multi-pass refinement that re-classifies ambiguous bins after Stage B.
- Embeddings-assisted clustering or sampling.
- Haiku-derived structural fields (rejected: re-creates the confabulation problem the verbatim-head spec just fixed).
- Larger `verbatim_head` (rejected: model still has to *infer* structure from prose; this spec turns that inference into a list-comparison instead).
- Sections extraction for `.docx`, `.xlsx`, `.doc`, `.xls`, `.hwp`, `.pptx`. Designed-for; not in v1. See the per-format strategies in "Future format coverage" below.
- Changes to PlanBuilder, Executor, Verifier.

## Design

### 1. `ExtractionResult.sections` (new field)

**File:** `fda/organize/models.py`

```python
@dataclass(frozen=True)
class ExtractionResult:
    text: str | None
    status: ExtractStatus
    note: str = ""
    sections: tuple[str, ...] = ()   # NEW — format-specific structural fingerprint
```

Default `()` so existing constructors keep working. Each registered extractor populates `sections` with a list of structural labels native to its format (regex-extracted headers for text; sheet/column names for spreadsheets when those extractors are added later; etc.). Reader passes the value through unchanged.

### 2. `CatalogEntry.sections` (new field)

**File:** `fda/organize/models.py`

```python
@dataclass(frozen=True)
class CatalogEntry:
    ...
    verbatim_head: str = ""
    sections: tuple[str, ...] = ()   # NEW — copied from ExtractionResult.sections
```

Default `()`. Reader copies the extractor's output into the catalog entry. No truncation on the Reader side; the extractor caps the list (see `MAX_SECTIONS_PER_FILE` below).

### 3. PDF + text extractor populates `sections` via regex

**File:** `fda/organize/_extractors.py`

A small shared helper extracts section labels from any text body:

```python
import re

# Constants — single home; covered by the existing constants-test pattern.
MAX_SECTIONS_PER_FILE = 15
SECTION_HEADER_MIN_CHARS = 3
SECTION_HEADER_MAX_CHARS = 40
SECTION_SCAN_BYTES = 16 * 1024   # only scan first ~16 KB; structure tops most docs

# Pattern A: line that is *just* a section header followed by a colon.
#   "Shipping Details:" / "Bill To:" / "Order Details:"
_HEADER_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Za-z0-9 \-/&]{1,38}[A-Za-z0-9]):[ \t]*$",
    re.MULTILINE,
)

# Pattern B: short ALL-CAPS line that looks like a section divider.
#   "INVOICE" / "TOTAL DUE" / "PURCHASE ORDER"
_ALLCAPS_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Z0-9 \-/&]{2,38}[A-Z0-9])[ \t]*$",
    re.MULTILINE,
)


def _extract_sections_from_text(text: str) -> tuple[str, ...]:
    """Pure function: text → ordered tuple of unique section labels.

    Deterministic, side-effect-free, no LLM. Matches a small bounded set of
    structural patterns: header-with-colon-only-on-line, short ALL-CAPS line.
    Caps at MAX_SECTIONS_PER_FILE.
    """
    if not text:
        return ()
    head = text[:SECTION_SCAN_BYTES]
    seen: dict[str, None] = {}   # ordered set
    for rx in (_HEADER_LINE_RE, _ALLCAPS_LINE_RE):
        for m in rx.finditer(head):
            label = m.group(1).strip()
            if not (SECTION_HEADER_MIN_CHARS <= len(label) <= SECTION_HEADER_MAX_CHARS):
                continue
            # Normalize: collapse whitespace, title-case ALL-CAPS for stable keys.
            label = " ".join(label.split())
            if label.isupper():
                label = label.title()
            seen.setdefault(label, None)
            if len(seen) >= MAX_SECTIONS_PER_FILE:
                return tuple(seen)
    return tuple(seen)
```

Both `_read_text` and `_extract_pdf_text` populate `sections` from their `text` output before returning:

```python
def _read_text(path: Path) -> ExtractionResult:
    ...
    return ExtractionResult(
        text=text, status="ok",
        sections=_extract_sections_from_text(text),
    )

def _extract_pdf_text(path: Path) -> ExtractionResult:
    ...
    return ExtractionResult(
        text=text, status="ok",
        sections=_extract_sections_from_text(text),
    )
```

When `text is None` (extraction failed / no extractor / tool missing), `sections` stays `()`.

### 4. Reader passes `sections` through

**File:** `fda/organize/reader.py`

Reader copies `extraction.sections` into `CatalogEntry.sections` alongside `verbatim_head`. No new logic; the regex runs inside the extractor, not the reader. `_fail_entry` and `_junk_entry` leave `sections=()`.

### 5. Stage A (Taxonomy Proposer) sees `sections`

**Files:**
- `fda/organize/skills/taxonomy-proposer/SKILL.md`
- `fda/organize/classifier.py` (Stage A payload builder)

**Payload change:** each sampled entry already carries `path`, `summary`, `verbatim_head`, etc. Add `sections`. Cost at v1 sample size (≤150 entries × ~15 short labels) ≈ 2 KB extra payload. One call per run.

**Prompt change:** add a new bullet to the existing signal-priority section:

> **Structural fingerprint as signal.** Each entry has `sections` — a list of section/field labels extracted directly from the document (not by an LLM). When sampled documents share similar topics or summaries but their `sections` lists are clearly different in size or content, propose them as **distinct categories**. Two "order documents" with `sections=["Order ID", "Products"]` and `sections=["Order Details", "Shipping Details", "Customer Details", "Employee", "Shipper", "Products"]` are not the same document type — the second has structural fields the first doesn't. Look for this kind of structural diversity in the sample and reflect it in the taxonomy.

### 6. Stage B (Assigner) sees `sections`

**Files:**
- `fda/organize/skills/taxonomy-assigner/SKILL.md`
- `fda/organize/classifier.py` (Stage B per-batch payload builder)

**Payload change:** per-file payload gains `sections`. Cost: ~15 short strings per file. At 10K files ≈ 150K extra tokens distributed across batches. Acceptable.

**Prompt change:** insert a new step in the existing signal-priority hierarchy. The new ordering becomes:

> 1. **Filename, if informative.** (unchanged)
> 2. **If filename is uninformative — ignore it.** (unchanged)
> 3. **Structural fingerprint vs taxonomy criteria.** Compare the file's `sections` list to each category's `criteria`. When two categories are otherwise plausible for a file, the one whose criteria mention sections that match the file's `sections` is the right one. A file with `sections=["Shipping Details", "Customer Details", "Shipper"]` belongs in a category whose criteria describe shipping/fulfillment, not in a generic "Purchase Orders" category — even if both summaries mention an "order."
> 4. **Within `verbatim_head` + `summary`:** (was step 3; renumbered)
> 5. **Empty `verbatim_head` and empty `sections`:** use `summary` only. (was step 4; expanded)

The structural step sits between filename and prose because it is more reliable than prose (deterministic, not Haiku-derived) but less reliable than a clearly informative filename.

### 7. Tests

**Modified:**

- `tests/test_organize_models.py` — assert `ExtractionResult().sections == ()` and `CatalogEntry(...).sections == ()` defaults.
- `tests/test_organize_extractors.py` — five regression cases for `_extract_sections_from_text`:
  - Plain text with `Header:` lines → list contains them in source order.
  - Plain text with ALL-CAPS lines → list contains title-cased versions.
  - Mixed case + duplicates → de-duplicated, source order preserved.
  - More than `MAX_SECTIONS_PER_FILE` candidates → list capped, no overflow.
  - Empty / whitespace-only / unrelated text → returns `()`.
  - Plus: `_read_text` and `_extract_pdf_text` populate `sections` end-to-end on a fixture file.
- `tests/test_organize_reader.py` — fixture file with a known section structure → `CatalogEntry.sections` matches; failed extraction → `sections == ()`.
- `tests/test_organize_classifier.py` — three new regression cases (mirrors the verbatim-head pattern):
  - **Stage B structural-conflict case:** two stub entries — entry A with `summary="order document"`, `verbatim_head="Purchase Orders\nOrder ID..."`, `sections=("Products",)`; entry B with `summary="order document"`, `verbatim_head="Order ID: 10488..."`, `sections=("Shipping Details", "Customer Details", "Employee", "Shipper", "Order Details", "Products")`. Stub taxonomy `[Purchase-Orders, Shipping-Orders, Misc]`. Assert (a) the wire payload contains `sections` for both entries, and (b) a fake backend that follows the prompt routes A → `Purchase-Orders` and B → `Shipping-Orders`. (The discriminator: A has 1 section, B has 6 sections, only `Products` overlaps — the regex `_HEADER_LINE_RE` requires the colon to be the last non-whitespace on the line, so labeled fields like `Order ID: 10488` deliberately don't match. Document title `Purchase Orders` doesn't match either; type-label signal stays in `verbatim_head`.)
  - **Stage B empty-sections fallback:** entry with `verbatim_head=""`, `sections=()`, summary only. Assert assignment uses `summary` (no crash, no spurious structural reasoning).
  - **Stage A structural-diversity case:** sample where 50% of entries have one section shape and 50% have a clearly distinct shape, all with similar summaries. Assert the proposed taxonomy contains at least two distinct categories (not one merged "orders" bucket). Implemented with a fake backend that scans the `sections` field in the payload before deciding.
- `tests/test_organize_constraints.py` — extend `CONSTS` with three new entries (one per new tunable):
  - `"MAX_SECTIONS_PER_FILE": ("_extractors.py", "15")`
  - `"SECTION_HEADER_MAX_CHARS": ("_extractors.py", "40")`
  - `"SECTION_SCAN_BYTES": ("_extractors.py", "16 * 1024")`
  - `SECTION_HEADER_MIN_CHARS = 3` — value `3` is too generic for a literal-uniqueness check; rely on the named-constant-defined check only (same exception we made for `VERBATIM_HEAD_CHARS = 300`).

All existing tests must still pass.

### 8. Manual validation (post-merge)

Re-run `python3 scripts/diag_organize.py /tmp/fda-test-sets/randomized-company-documents-2026-05-05-003 --apply` against a fresh fixture copy. Cross-reference with `manifest.csv`. Acceptance: the simple-PO vs detailed-shipping-order split that the verbatim-head spec couldn't fix should now resolve to ≥95% per-class accuracy.

If the user has a fixture exercising any other "same-topic-different-structure" pair (Quote vs Invoice, Quarterly vs Annual Report, etc.), include it in the manual run.

## Architecture impact

```
extractor (PDF / text)                Reader                Classifier
─────────────────────                  ──────                ──────────
text + sections ─┐
                 ├── ExtractionResult ──┐
                 │  (text, status,      │
                 │   note, sections)    ├── CatalogEntry ──┐
                 │                      │   { ..., verbatim_head,
                 │                      │     sections } │
                 │                      │                 ├── Stage A — proposes taxonomy
                 │                      │                 │   (sees `sections` in sample;
                 │                      │                 │    splits when sections diverge)
                 │                      │                 │
                 │                      │                 └── Stage B — assigns each file
                 │                      │                     (compares file `sections`
                 │                      │                      to category criteria)
```

The data flow is unchanged in shape. One field appears on `ExtractionResult` and `CatalogEntry`; one helper lives in `_extractors.py`; two prompts gain a structural rule. PlanBuilder, Executor, Verifier, journal, log surfaces — all untouched.

## Constants summary

All in `fda/organize/_extractors.py`, single home, covered by `tests/test_organize_constraints.py::TestEachConstantHasOneHome`:

- `MAX_SECTIONS_PER_FILE = 15`
- `SECTION_HEADER_MIN_CHARS = 3`
- `SECTION_HEADER_MAX_CHARS = 40`
- `SECTION_SCAN_BYTES = 16 * 1024`

## Future format coverage (deferred but designed-for)

The user explicitly asked to record this so it isn't forgotten. The `sections` field is format-agnostic by contract — every extractor populates it with whatever structural fingerprint is native to its format. v1 ships PDF + plaintext only. Each future format adds **one extractor function + one registry entry**. No changes to Reader, Classifier, PlanBuilder, or any prompt.

| Extension | Library / tool | What `sections` should contain | Example |
|---|---|---|---|
| `.docx` | `python-docx` | Paragraph styles `Heading 1`, `Heading 2`, etc., in document order | `["Introduction", "Methodology", "Results", "Conclusion"]` |
| `.xlsx` | `openpyxl` | Sheet names + first-row column headers per sheet, prefix-tagged | `["Sheet:Q3-Sales", "Date", "Product", "Region", "Revenue", "Sheet:Inventory", "SKU", "Quantity"]` |
| `.csv` | stdlib `csv` | First-row column headers (treated as labeled fields) | `["customer_id", "order_date", "amount", "status"]` |
| `.pptx` | `python-pptx` | Slide titles in order | `["Cover", "Q3 Highlights", "Pipeline", "Q&A"]` |
| `.doc` | `libreoffice --headless` → `.docx` → `python-docx` | Same as `.docx` | (same shape) |
| `.xls` | `libreoffice --headless` → `.xlsx` → `openpyxl` | Same as `.xlsx` | (same shape) |
| `.hwp` | `hwp5txt` / `pyhwp` → text → existing regex helper | Reuses `_extract_sections_from_text` on the converted text | (same shape as PDF/text) |
| `.eml` / `.msg` | stdlib `email` / `extract-msg` | Header fields (From, To, Subject, Date) + part labels | `["From", "To", "Subject", "Date", "Attachment:invoice.pdf"]` |

**Registration pattern** (per-format extension to v1):

```python
# In _extractors.py, when adding e.g. .xlsx:
def _extract_xlsx(path: Path) -> ExtractionResult:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sections = []
    for ws in wb.worksheets:
        sections.append(f"Sheet:{ws.title}")
        first_row = next(ws.iter_rows(max_row=1, values_only=True), ())
        for cell in first_row:
            if cell and isinstance(cell, str):
                sections.append(cell.strip())
    sections = tuple(dict.fromkeys(sections))[:MAX_SECTIONS_PER_FILE]
    text = ...   # whatever text representation the existing pipeline expects
    return ExtractionResult(text=text, status="ok", sections=sections)

EXTRACTORS[".xlsx"] = _extract_xlsx
```

Each format's `sections` extraction is best-effort: if the underlying library or binary isn't installed, the extractor returns `ExtractionResult(text=None, status="tool_missing", note=..., sections=())` and the catalog entry has `sections=()` — same fallback as today's missing-`pdftotext` case. The format never becomes a hard install dependency.

## Risks and mitigations

- **Regex over-matches.** A line like `Note:` or `Re:` could be picked up as a section. Mitigation: the prompt instructs the model to weigh `sections` against `summary` and category criteria, not to trust structure alone. If over-match becomes a frequent problem, tighten the regex (require ≥2 words, or require non-content-line context). Cheap to iterate.
- **Regex under-matches.** PDFs where headers don't follow the colon/ALL-CAPS conventions (e.g., headers in a distinct font but not punctuated) won't match. Mitigation: this regression is no worse than today; `summary` and `verbatim_head` continue to do their job. The structural channel is additive, never subtractive.
- **Garbage extraction → garbage sections.** If `pdftotext` produces noise, the regex will match noise. Mitigation: noise rarely matches the `[A-Z]…:$` pattern, so empirically the list stays short or empty. Worst case: a few junk labels per file, which the model can ignore by cross-referencing `summary`.
- **Regex too aggressive at scanning.** `SECTION_SCAN_BYTES = 16 KB` bounds the scan; even pathological 100-MB plaintext logs cost only the regex on the first 16 KB.
- **Token cost at scale.** ~150 KB of additional payload at 10K files. Modest. If it ever becomes a concern, drop `MAX_SECTIONS_PER_FILE` to 10 or `SECTION_SCAN_BYTES` to 8 KB — both isolated changes.
- **Future-format extractors lie about structure.** Same risk profile as today's `pdftotext` failures. The catalog entry simply gets an empty `sections`; the model falls back to existing signals.

## Backward compatibility

- `ExtractionResult.sections` and `CatalogEntry.sections` default to `()`. Existing tests, fixtures, and constructors keep working.
- Existing classifier prompts continue to work if `sections` is empty — the new prompt rule explicitly handles that fallback.
- No on-disk format changes.
- No changes to public `organize()` / `apply_plan()` API.

## Implementation order (handed to writing-plans)

1. `fda/organize/models.py`: add `sections: tuple[str, ...] = ()` to both `ExtractionResult` and `CatalogEntry`.
2. `tests/test_organize_models.py`: default-value tests for both.
3. `fda/organize/_extractors.py`: implement `_extract_sections_from_text` + four constants; have `_read_text` and `_extract_pdf_text` populate `sections`.
4. `tests/test_organize_extractors.py`: regex behavior + end-to-end-via-extractor tests.
5. `fda/organize/reader.py`: copy `extraction.sections` into `CatalogEntry.sections` in `_summarize_one`.
6. `tests/test_organize_reader.py`: assert reader propagates `sections`; assert failed-extraction path leaves `()`.
7. `fda/organize/classifier.py` + `taxonomy-proposer/SKILL.md`: Stage A payload + prompt update.
8. `fda/organize/classifier.py` + `taxonomy-assigner/SKILL.md`: Stage B payload + prompt update with the renumbered priority hierarchy.
9. `tests/test_organize_classifier.py`: three regression cases (Stage B conflict, Stage B empty-sections, Stage A diversity).
10. `tests/test_organize_constraints.py`: extend `CONSTS` with the three checkable new constants.
11. Manual validation against the Northwind fixture.

Each step is one commit; pre-commit hook enforces the test suite.
