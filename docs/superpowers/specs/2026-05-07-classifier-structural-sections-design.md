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
- Haiku-derived structural fields as a primary signal (deferred for v1: re-creates the confabulation problem the verbatim-head spec just fixed). A *hybrid fallback* — regex first, ask Haiku to quote literal section labels from the raw text only on documents where regex returned zero sections — is a defensible v2 candidate; it isolates Haiku to a narrow extraction task ("what labels appear?") rather than an inference task ("what is this document?"). Out of scope for v1 because regex coverage on the target corpus hasn't been measured yet; revisit if manual validation shows >10% of docs returning empty `sections` despite having visible structure.
- Larger `verbatim_head` (deferred for v1: model still has to *infer* structure from prose; this spec turns that inference into a list-comparison instead). Revisit only if `sections` proves insufficient and a richer prose channel is genuinely needed.
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

### 3. New module `fda/organize/_sections.py` for structural extraction

**File:** `fda/organize/_sections.py` (new)

Section extraction is conceptually adjacent to but distinct from the extractor registry's "give me text" responsibility. A separate small module keeps `_extractors.py` focused on extension-dispatch + format conversion, makes the helper trivially reusable for the future `.hwp` extractor (which converts to text and reuses the same logic), and matches the existing pattern (`_fs.py`, `_skills.py`, `_logger.py`).

```python
# fda/organize/_sections.py
"""Deterministic structural-fingerprint extraction from document text.

Pure functions, no LLM, no I/O. Used by text-based extractors (PDF,
plaintext, future .hwp) to populate ExtractionResult.sections.
"""
from __future__ import annotations

import re

# Constants — single home; covered by the constants-test pattern.
MAX_SECTIONS_PER_FILE = 15
SECTION_HEADER_MIN_CHARS = 3
SECTION_HEADER_MAX_CHARS = 40
SECTION_SCAN_CHARS = 16 * 1024   # only scan first ~16 K characters; structure tops most docs

# Pattern A: line that is *just* a section header followed by a colon.
#   "Shipping Details:" / "Bill To:" / "Order Details:"
_HEADER_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Za-z0-9 \-/&]{1,38}[A-Za-z0-9]):[ \t]*$"
)

# Pattern B: short ALL-CAPS line that looks like a section divider.
#   "INVOICE" / "TOTAL DUE" / "PURCHASE ORDER"
_ALLCAPS_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Z0-9 \-/&]{2,38}[A-Z0-9])[ \t]*$"
)


def extract_sections_from_text(text: str) -> tuple[str, ...]:
    """Pure function: text → ordered tuple of unique section labels.

    Walks lines ONCE in source order, trying each pattern per line. This
    preserves intra-document order in mixed-pattern documents (e.g., a
    file whose first matching line is ALL-CAPS and whose second is a
    colon-header). Caps at MAX_SECTIONS_PER_FILE.

    Deterministic, side-effect-free, no LLM.
    """
    if not text:
        return ()
    head = text[:SECTION_SCAN_CHARS]
    seen: dict[str, None] = {}   # ordered set
    for line in head.splitlines():
        for rx in (_HEADER_LINE_RE, _ALLCAPS_LINE_RE):
            m = rx.match(line)
            if not m:
                continue
            label = m.group(1).strip()
            if not (SECTION_HEADER_MIN_CHARS <= len(label) <= SECTION_HEADER_MAX_CHARS):
                continue
            # Normalize: collapse whitespace, title-case ALL-CAPS for stable keys.
            label = " ".join(label.split())
            if label.isupper():
                label = label.title()
            if label not in seen:
                seen[label] = None
                if len(seen) >= MAX_SECTIONS_PER_FILE:
                    return tuple(seen)
            break  # one match per line is enough; don't double-count
    return tuple(seen)
```

Why walk lines once instead of running each regex with `re.MULTILINE` over the whole buffer: source-order preservation. A two-pass approach emits all colon-headers first, then all ALL-CAPS lines, regardless of where they appear in the document. The single-pass version produces the same order the reader sees, which makes diffs deterministic and tests stable.

Both `_read_text` and `_extract_pdf_text` import the helper and populate `sections` before returning:

```python
# In fda/organize/_extractors.py
from fda.organize._sections import extract_sections_from_text

def _read_text(path: Path) -> ExtractionResult:
    ...
    return ExtractionResult(
        text=text, status="ok",
        sections=extract_sections_from_text(text),
    )

def _extract_pdf_text(path: Path) -> ExtractionResult:
    ...
    return ExtractionResult(
        text=text, status="ok",
        sections=extract_sections_from_text(text),
    )
```

When `text is None` (extraction failed / no extractor / tool missing), `sections` stays `()`.

**v1 behavior across registered extensions.** The extractor registry today maps `.txt`, `.md`, `.csv`, `.log`, `.json`, `.xml`, `.pdf` (`_extractors.py:100-107`). All of these go through `_read_text` or `_extract_pdf_text` and therefore receive the same regex treatment in v1. CSV/JSON/XML rarely contain colon-headers or ALL-CAPS dividers, so an empty `sections` tuple for those files is the expected outcome — not a bug. Future format-native extraction (per the Future format coverage table) will swap in a richer extractor for `.csv` and `.xlsx`; until then, the empty-`sections` fallback path through Stage B's prompt covers them correctly.

### 4. Reader passes `sections` through, preserves it on summarizer failure

**File:** `fda/organize/reader.py`

Reader copies `extraction.sections` into `CatalogEntry.sections` alongside `verbatim_head`. No new logic; the regex runs inside the extractor, not the reader. `_junk_entry` leaves `sections=()`.

**`_fail_entry` must accept and preserve `sections`.** When the summarizer Haiku call times out or returns unparseable output, the deterministic structural fingerprint computed in Reader is still valid — losing it on Haiku failure would discard the most reliable signal we have. Mirroring how `verbatim_head` is preserved through summarizer failures today (commit `3c008ba` "preserve verbatim_head on summary failure"), `_fail_entry` gains a `sections` keyword argument and the call sites in `_summarize_one` pass `extraction.sections` through both the timeout and exception paths.

### 5. Stage A (Taxonomy Proposer) sees `sections` and produces structural criteria

**Files:**
- `fda/organize/skills/taxonomy-proposer/SKILL.md`
- `fda/organize/classifier.py` (Stage A payload builder + sampling algorithm)

**Payload change:** each sampled entry already carries `path`, `summary`, `verbatim_head`, etc. Add `sections`. Token estimate at v1 sample size: ~100 entries × ~5 sections (typical) × ~20 chars/section (label + JSON quoting) ≈ 10 KB ≈ 2.5K tokens. One call per run.

**Prompt change.** Two additions to the proposer prompt:

> **Structural fingerprint as signal.** Each entry has `sections` — a list of section/field labels extracted directly from the document (not by an LLM). When sampled documents share similar topics or summaries but their `sections` lists are clearly different in size or content, propose them as **distinct categories**. Two "order documents" with `sections=["Products"]` and `sections=["Shipping Details", "Customer Details", "Employee", "Shipper", "Order Details", "Products"]` are not the same document type — the second has structural fields the first doesn't. Look for this kind of structural diversity in the sample and reflect it in the taxonomy.
>
> **When categories are structurally distinct, encode that in `criteria`.** Stage B has access to the same `sections` lists you do, but it can only compare them to your category criteria — and `criteria` is free prose. When two categories differ structurally, write the discriminating section names directly into `criteria` (e.g., `"criteria": "Documents with Shipping Details, Customer Details, Employee, and Shipper sections in addition to Products."`). Stage B will then have something concrete to match. Categories whose discriminator is purely topical (no structural distinction) need no section names in `criteria`.

**Sampling-algorithm change.** The existing stratified sample (extension / top-level dir / leaf dir / failed-summary / largest) doesn't account for structural shape, so a rare structural type can be invisible to Stage A even when it's present in the catalog (Codex review #4). Add a new step before the "fill remaining slots evenly" step:

> Reserve up to 1 slot per **distinct sections-shape signature**, capped at 10 slots total. The signature is the `sections` tuple itself (already deterministic, already capped at 15 entries). De-duplicate against earlier reservation rules. This guarantees Stage A sees at least one exemplar of each structural shape that exists in the catalog, up to the cap.

This is purely additive — no existing reservation rule changes — and the cap (10 slots out of 100 target) leaves the rest of the sample untouched. Constants: `TAXONOMY_SAMPLE_SHAPE_BUDGET = 10` in `classifier.py`, single-home, covered by the same lint test as the other sampling constants.

**Residual blind spot:** when a structural type is present but always merged into a topically-plausible existing category at Stage B (so fallback rate stays low and refinement never fires), the taxonomy will still under-split. v1 accepts this; the manual validation step is the catch-net. v2 candidate: a structural-coverage check that compares the catalog's distinct sections-shapes against the taxonomy's criteria-mentioned sections and triggers refinement if too many shapes go uncovered.

### 6. Stage B (Assigner) sees `sections`

**Files:**
- `fda/organize/skills/taxonomy-assigner/SKILL.md`
- `fda/organize/classifier.py` (Stage B per-batch payload builder)

**Payload change:** per-file payload gains `sections` (already in the per-entry payload spec for Stage A). Token estimate: typical 5 short labels per file × ~20 chars = ~100 chars/file. At 10K files ≈ 1 MB total payload distributed across Stage B batches ≈ 250K tokens. Acceptable. (At the cap of 15 sections per file, the upper bound is ~750K tokens — also acceptable; cost-conscious operators can dial `MAX_SECTIONS_PER_FILE` down.)

**Prompt change:** insert a new step in the existing signal-priority hierarchy and disambiguate the empty-sections fallback by `extract_status`. The new ordering becomes:

> 1. **Filename, if informative.** (unchanged)
> 2. **If filename is uninformative — ignore it.** (unchanged)
> 3. **Structural fingerprint vs taxonomy criteria.** Compare the file's `sections` list to each category's `criteria`. Stage A is instructed to encode discriminating section names directly in `criteria` when categories differ structurally. When two categories are otherwise plausible for a file, the one whose criteria *names* sections that overlap with the file's `sections` is the right one — even if both summaries describe similar topics. Example: a file with `sections=["Shipping Details", "Customer Details", "Shipper"]` belongs in a category whose criteria mention those names, not in a generic "Purchase Orders" category — even if both summaries mention an "order." Use simple substring overlap, not semantic similarity.
> 4. **Within `verbatim_head` + `summary`:** (was step 3; renumbered)
> 5. **Empty-or-uninformative `sections`:** behavior depends on `extract_status`.
>    - `extract_status == "ok"` and `sections == []`: extraction succeeded but the document has no labeled sections (e.g., a free-form email, a flat CSV). Skip step 3 entirely; rely on filename + verbatim_head + summary.
>    - `extract_status != "ok"` (extraction failed, no extractor, tool missing): no structural signal is available — same fallback as above. The empty list does not mean "no structure"; it means "we couldn't tell."
>    - **Always:** an empty `sections` list never excludes a category. Step 3 only ever *prefers* a structurally-matching category over alternatives; it never *rejects* one for lack of overlap.
> 6. **Empty `verbatim_head` AND empty `sections` AND uninformative filename:** use `summary` only. (was step 4 in the old hierarchy.)

The structural step sits between filename and prose because it is more reliable than prose (deterministic, not Haiku-derived) but less reliable than a clearly informative filename. The `extract_status` disambiguation is what keeps "extraction failed" from being silently conflated with "doc legitimately has no sections" — both yield `sections=()` but the prompt treats them identically *only* in the fallback path, never in step 3's preference logic.

**Filename vs sections conflict.** When an informative filename suggests one category but `sections` overlap better with a different category's criteria: filename wins (step 1 still has highest priority), but the model should record the conflict by mapping the file to the filename-suggested category and *not* citing the structural signal as the reason. Real-world example: `Invoice_old_template.pdf` with sections matching a "Quote" category — the user's filename intent overrides the structural drift. This is a deliberate trade-off; if it produces real-world errors, escalate to v2.

### 7. Tests

**Modified:**

- `tests/test_organize_models.py` — default-value tests using valid constructors:
  - `ExtractionResult(text=None, status="ok").sections == ()`
  - `CatalogEntry(path_id="f000", path="/x", ext=".pdf", size_bytes=0, summary="", type_label="", is_junk=False, summary_failed=False, extract_status="ok").sections == ()`
- `tests/test_organize_sections.py` (new file, paired with the new module) — regression cases for `extract_sections_from_text`:
  - Plain text with `Header:`-only-on-line → list contains them in source order.
  - Plain text with ALL-CAPS lines → list contains title-cased versions.
  - **Source-order preservation in mixed-pattern documents:** input where ALL-CAPS line precedes colon-headers → output preserves that order. Pins the single-pass algorithm; a two-pass implementation would fail this test by emitting colon-headers first.
  - Mixed case + duplicates → de-duplicated, source order preserved.
  - More than `MAX_SECTIONS_PER_FILE` candidates → list capped at 15, no overflow.
  - Empty / whitespace-only / unrelated text → returns `()`.
- `tests/test_organize_extractors.py` — end-to-end: `_read_text` and `_extract_pdf_text` populate `sections` on a fixture file. v1-CSV behavior pinned: a CSV with no colon-headers and no ALL-CAPS lines yields `sections=()` (not a bug; future format-native extractor swap covers it).
- `tests/test_organize_reader.py` — three additions:
  - Fixture file with a known section structure → `CatalogEntry.sections` matches.
  - Failed extraction → `sections == ()` (carried straight from `ExtractionResult`).
  - **Summarizer-failure preservation:** Reader's per-file backend mock raises `TimeoutError`; the resulting `CatalogEntry` has `summary_failed=True` AND `sections == ("Shipping Details", ...)` from the successful extraction. Pins the `_fail_entry` `sections` parameter and mirrors the existing `verbatim_head` preservation test.
- `tests/test_organize_classifier.py` — three new regression cases:
  - **Stage B structural-conflict case.** Fixture text is pinned exactly:
    - **Entry A — simple PO,** content (verbatim text fed into `extract_sections_from_text`):
      ```
      Purchase Orders

      Order ID: 10488
      Order Date: 2024-03-15

      Products:
        - Widget A x 5
        - Widget B x 3

      Total: $1560.00
      ```
      Yields `sections=("Products",)`. ("Purchase Orders" doesn't match — title-case standalone, no colon, mixed-case so ALL-CAPS pattern declines. "Order ID: 10488" / "Order Date: 2024-03-15" / "Total: $1560.00" all have content after the colon, so the colon-only-on-line pattern declines. Only "Products:" stands alone.)
    - **Entry B — detailed shipping order,** content:
      ```
      Order ID: 10488

      Shipping Details:
        Frankenversand
        Berliner Platz 43

      Customer Details:
        Name: Hanna Moos

      Employee:
        Janet Leverling

      Shipper:
        Speedy Express

      Order Details:
        Date: 2024-03-15

      Products:
        - Widget A x 5

      Total: $1560.00
      ```
      Yields `sections=("Shipping Details", "Customer Details", "Employee", "Shipper", "Order Details", "Products")` — 6 entries, only `Products` overlaps with A.
    - Stub taxonomy `[Purchase-Orders (criteria mentions just "Products"), Shipping-Orders (criteria mentions "Shipping Details, Customer Details, Employee, Shipper, Order Details"), Misc]`.
    - Assert (a) the wire payload contains `sections` for both entries; (b) a fake backend that follows the prompt's substring-overlap rule routes A → `Purchase-Orders` and B → `Shipping-Orders`.
  - **Stage B empty-sections fallback by `extract_status`:** two stub entries with `verbatim_head=""` and `sections=()`, one with `extract_status="ok"` (legitimately empty), one with `extract_status="failed"`. Assert both fall through to summary-only assignment without crashing or invoking step 3 logic.
  - **Stage A structural-diversity case + sampling-budget check:** synthetic 200-entry catalog with 6 distinct sections-shapes spread thinly (5–10 entries each). Without the new shape-budget rule, the deterministic stratified sample would over-represent the dominant shape and miss the rare ones. Assert (a) the sample reserves at least one entry per distinct shape (up to `TAXONOMY_SAMPLE_SHAPE_BUDGET = 10`); (b) a fake backend that scans `sections` in the payload proposes ≥2 distinct categories.
  - **`_fail_entry` sections preservation** (covered by the reader test above; cross-link only).
- `tests/test_organize_constraints.py` — extend `CONSTS` with the new tunables:
  - `"MAX_SECTIONS_PER_FILE": ("_sections.py", "15")`
  - `"SECTION_HEADER_MAX_CHARS": ("_sections.py", "40")`
  - `"SECTION_SCAN_CHARS": ("_sections.py", "16 * 1024")`
  - `"TAXONOMY_SAMPLE_SHAPE_BUDGET": ("classifier.py", "10")`
  - `SECTION_HEADER_MIN_CHARS = 3` — value `3` is too generic for a literal-uniqueness check; rely on the named-constant-defined check only (same exception we made for `VERBATIM_HEAD_CHARS = 300`). Per Codex review #5, do **not** add `15` or `40` to `DISTINCTIVE_LITERALS` either — they are too generic and risk false-positives elsewhere in the package.

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

The data flow is unchanged in shape. One field appears on `ExtractionResult` and `CatalogEntry`; one new module (`_sections.py`) holds the regex helper; two prompts gain a structural rule; one new sampling-budget constant lands in `classifier.py`. PlanBuilder, Executor, Verifier, journal, log surfaces — all untouched.

## Constants summary

In `fda/organize/_sections.py`, single home, covered by `tests/test_organize_constraints.py::TestEachConstantHasOneHome`:

- `MAX_SECTIONS_PER_FILE = 15`
- `SECTION_HEADER_MIN_CHARS = 3`
- `SECTION_HEADER_MAX_CHARS = 40`
- `SECTION_SCAN_CHARS = 16 * 1024`

In `fda/organize/classifier.py`:

- `TAXONOMY_SAMPLE_SHAPE_BUDGET = 10`

## Future format coverage (deferred but designed-for)

The user explicitly asked to record this so it isn't forgotten. The `sections` field is format-agnostic by contract — every extractor populates it with whatever structural fingerprint is native to its format. v1 ships PDF + plaintext only. Each future format adds **one extractor function + one registry entry**. No changes to Reader, Classifier, PlanBuilder, or any prompt.

| Extension | Library / tool | What `sections` should contain | Example |
|---|---|---|---|
| `.docx` | `python-docx` | Paragraphs styled as `Heading 1`/`Heading 2`/etc. in document order. **Real-world fallback:** many Word docs don't use heading styles at all — implementation must additionally treat short standalone bold paragraphs (≤60 chars, no period at end, surrounded by blank-ish lines) as candidate headers when no styled headings are found. | `["Introduction", "Methodology", "Results", "Conclusion"]` |
| `.xlsx` | `openpyxl` | Sheet names + first **dense** row's column headers per sheet, prefix-tagged. **Real-world fallback:** many business sheets have a banner/title/merged row before the actual header — scan rows from the top until finding the first row where ≥50% of columns hold short string values, treat that as the header row. Skip empty sheets. | `["Sheet:Q3-Sales", "Date", "Product", "Region", "Revenue", "Sheet:Inventory", "SKU", "Quantity"]` |
| `.csv` | stdlib `csv` | First-row column headers (treated as labeled fields). | `["customer_id", "order_date", "amount", "status"]` |
| `.pptx` | `python-pptx` | Slide titles in order; for slides without a title placeholder, the first text frame's first line if ≤60 chars. | `["Cover", "Q3 Highlights", "Pipeline", "Q&A"]` |
| `.doc` | `libreoffice --headless` → `.docx` → `python-docx` | Same as `.docx`, including the bold-fallback rule. | (same shape) |
| `.xls` | `libreoffice --headless` → `.xlsx` → `openpyxl` | Same as `.xlsx`, including the dense-row rule. | (same shape) |
| `.hwp` | `hwp5txt` / `pyhwp` → text → reuse `_sections.extract_sections_from_text` | Same regex output as PDF/plaintext. | (same shape as PDF/text) |
| `.eml` / `.msg` | stdlib `email` / `extract-msg` | Header fields (From, To, Subject, Date) + part labels (`Attachment:<filename>`). | `["From", "To", "Subject", "Date", "Attachment:invoice.pdf"]` |

**Registration pattern** (per-format extension to v1):

```python
# In _extractors.py, when adding e.g. .xlsx:
from fda.organize._sections import MAX_SECTIONS_PER_FILE

def _extract_xlsx(path: Path) -> ExtractionResult:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sections: list[str] = []
    for ws in wb.worksheets:
        sections.append(f"Sheet:{ws.title}")
        # Find first "dense" row: ≥50% of columns hold short string values.
        for row in ws.iter_rows(max_row=10, values_only=True):
            strings = [c for c in row if isinstance(c, str) and 0 < len(c) <= 60]
            if row and len(strings) / max(1, len(row)) >= 0.5:
                sections.extend(s.strip() for s in strings)
                break
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
- **Regex too aggressive at scanning.** `SECTION_SCAN_CHARS = 16384` (characters, not bytes — for non-ASCII text the actual byte size is larger) bounds the scan; even pathological 100-MB plaintext logs cost only the regex on the first ~16 K characters of decoded text.
- **Token cost at scale.** Stage A: ~10 KB of extra payload per run (~2.5K tokens) — one call. Stage B: ~250K tokens distributed across batches at typical 5 sections/file × 10K files (upper bound ~750K tokens at the cap). Modest. If it becomes a concern, drop `MAX_SECTIONS_PER_FILE` to 10 or `SECTION_SCAN_CHARS` to 8192 — both isolated changes.
- **Future-format extractors lie about structure.** Same risk profile as today's `pdftotext` failures. The catalog entry simply gets an empty `sections`; the model falls back to existing signals.

## Backward compatibility

- `ExtractionResult.sections` and `CatalogEntry.sections` default to `()`. Existing tests, fixtures, and constructors keep working.
- Existing classifier prompts continue to work if `sections` is empty — the new prompt rule explicitly handles that fallback.
- No on-disk format changes.
- No changes to public `organize()` / `apply_plan()` API.

## Implementation order (handed to writing-plans)

1. `fda/organize/models.py`: add `sections: tuple[str, ...] = ()` to both `ExtractionResult` and `CatalogEntry`.
2. `tests/test_organize_models.py`: default-value tests for both, using valid constructor calls (Codex review #10c).
3. `fda/organize/_sections.py` (new module): implement `extract_sections_from_text` with the single-pass algorithm + four constants.
4. `tests/test_organize_sections.py` (new): regex behavior including the source-order-preservation test that pins the single-pass algorithm.
5. `fda/organize/_extractors.py`: import from `_sections`; have `_read_text` and `_extract_pdf_text` populate the new field.
6. `tests/test_organize_extractors.py`: end-to-end tests for both extractors; explicit pinning of v1 CSV/JSON/XML behavior (regex runs, empty result is acceptable).
7. `fda/organize/reader.py`: copy `extraction.sections` into `CatalogEntry.sections` in `_summarize_one`; thread `sections` through `_fail_entry` so summarizer failures preserve the deterministic structural fingerprint.
8. `tests/test_organize_reader.py`: assert reader propagates `sections`; assert failed-extraction path leaves `()`; assert summarizer-failure preservation (mirrors the existing `verbatim_head` preservation test).
9. `fda/organize/classifier.py` + `taxonomy-proposer/SKILL.md`: Stage A payload + sampling-shape budget + prompt update (structural fingerprint signal + structural-criteria requirement). Add `TAXONOMY_SAMPLE_SHAPE_BUDGET = 10` constant.
10. `fda/organize/classifier.py` + `taxonomy-assigner/SKILL.md`: Stage B payload + prompt update with the renumbered priority hierarchy and `extract_status`-aware fallback.
11. `tests/test_organize_classifier.py`: four regression cases (Stage B structural-conflict with pinned fixture text, Stage B empty-sections-by-extract_status, Stage A structural-diversity + sampling budget, filename-vs-sections conflict).
12. `tests/test_organize_constraints.py`: extend `CONSTS` with `MAX_SECTIONS_PER_FILE`, `SECTION_HEADER_MAX_CHARS`, `SECTION_SCAN_CHARS` (in `_sections.py`) and `TAXONOMY_SAMPLE_SHAPE_BUDGET` (in `classifier.py`).
13. Manual validation against the Northwind fixture.

Each step is one commit; pre-commit hook enforces the test suite.
