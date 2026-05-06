# Classifier Verbatim-Head Design

**Date:** 2026-05-06
**Status:** Spec — pending implementation plan
**Predecessor:** `2026-05-06-organize-skill-pipeline-design.md` (the four-stage skill pipeline this builds on)

## Context

The skill-based organize pipeline (Reader → Classifier Stage A → Stage B → PlanBuilder → Executor → Verifier) shipped on 2026-05-06 and produces a complete plan for 100-file runs. Re-running it on `randomized-company-documents-2026-05-05-003` (101 files, ground truth: 30 invoices, 30 purchase orders, 34 shipping orders, 6 monthly stock reports, 1 manifest) now exposes a systematic accuracy failure:

- 28 of 34 true shipping orders were misfiled into `Procurement/Purchase-Orders/`.
- 6 of 34 true shipping orders correctly landed in `Operations/Shipping-And-Fulfillment/`.
- The 30 true POs, 30 invoices, and 6 stock reports were classified correctly.

### Root cause

The three ambiguous classes have asymmetric textual signal in this corpus (and likely in real-world corpora that use the same Northwind-style template family):

| Class | First non-empty line in extracted text |
|---|---|
| True purchase order | Literal `"Purchase Orders"` then a tabular layout (no Shipping/Shipper sections). |
| True invoice | Literal `"Invoice"` then `Order ID / Customer ID / Order Date / Product Details`. |
| **True shipping order** | `"Order ID: <num>"` then nine sections (Shipping Details / Customer / Employee / Shipper / Order Details with Shipped Date / Products / Total). **No type-label phrase anywhere in the text.** |

POs and invoices carry their own name in plaintext. Shipping orders do not — their text has lots of shipping-y signal (Shipping Details, Shipper Details, Shipped Date) but never the literal phrases "shipping order" or "purchase order".

What Reader's Haiku call did with file `0fa84d61b3158eaba46dee96.pdf` (a true shipping order):

- Extracted text begins `"Order ID: 10488\n\nShipping Details:\n..."` and never contains the phrase "purchase order".
- Reader summary it produced: `"Purchase order #10488 for Frankenversand in Munich, Germany, listing two products with a total price of 1560.0."`
- Classifier Stage B saw `"Purchase order #10488"` in the summary, treated it as authoritative, and routed correctly given that summary.

Reader confabulated the type label `"Purchase order"`. Of 34 shipping orders, 6 happened to get summarized with the words `"shipping order"` (a Haiku coin-flip on confabulation direction); the other 28 got `"purchase order"` / `"order"` and landed wrong.

**Single-sentence root cause:** the Reader prompt invites Haiku to assert a document type even when no type label is in the extracted text, and the Classifier never sees the raw text to second-guess that assertion.

## Goal

Recover ≥95% per-class accuracy on the Northwind fixture (and analogously-structured corpora) by:

- **(A)** Tightening the Reader prompt so it never invents a document type.
- **(B)** Adding a small verbatim slice of extracted text to every catalog entry, surfaced to both Classifier stages so they can ground decisions independently of Reader's prose summary.
- **(C)** Making filename-as-signal explicit in the Classifier prompts, with a clear priority: use the basename if it's informative (e.g., `Invoice_10488.pdf`, `Q3_Sales_Report.xlsx`); ignore it when it's a hash, a random ID, or a generic name (`doc1.pdf`, `0fa84d61b3158eaba46dee96.pdf`) and fall back to `verbatim_head` + `summary`.

A, B, and C are complementary: A reduces the rate of misleading prose; B gives the Classifier authoritative signal even when prose drifts; C makes filename signal a deliberate, prioritized input rather than something the model uses or ignores at random.

## Non-goals

- Vision-based reading (rendering PDF pages and sending to a vision model). Out of scope; revisit if option 1 still falls short.
- A structured `type_label_verbatim` field on `CatalogEntry`. Decided against because the verbatim slice already carries this signal and the Classifier can extract it directly without forcing Reader to make a per-file structural extraction decision.
- Taxonomy-aware markers (Stage A outputs literal markers per category that Reader pre-checks). Decided against for now — bigger architectural change; revisit if accuracy still falls short after option 1.
- A tail-of-document slice (footers like "Total Due", "Net Terms"). Land head-only first; add tail later if motivated.
- Multi-pass refinement / verifier-driven re-classification.
- Changes to PlanBuilder, Executor, Verifier, planner.py, prompts.py.

## Design

### 1. Reader prompt tightening (A)

**File:** `fda/organize/skills/file-summarizer/SKILL.md`

**Change:** add explicit rules in the prompt body:

- Do not name a document type unless that exact phrase appears verbatim in the extracted text.
- If no type label is present, describe the document structurally: which sections appear, which key fields are populated, dates, totals. Example wording: `"Order document with shipping, customer, employee, shipper, products, and shipped-date sections, dated 2017-03-27, total 1560.0."`
- Brief note: a downstream classifier sees a separate verbatim slice of the text for grounding; the summary's job is neutral structural description.

Keep the existing length/format constraints. No schema change. Summary remains a free-text string.

### 2. `CatalogEntry.verbatim_head` (B)

**File:** `fda/organize/models.py`

**Change:** add field to the existing `CatalogEntry` dataclass:

```python
verbatim_head: str = ""
```

Default `""` so existing constructors (and any test fixtures that build `CatalogEntry` directly) continue to work unchanged.

### 3. Reader populates `verbatim_head` (B)

**File:** `fda/organize/reader.py`

**Change:** after `_extractors.extract(path)` returns, derive the slice deterministically before kicking off the Haiku summarization:

```python
VERBATIM_HEAD_CHARS = 300

def _verbatim_head(text: str) -> str:
    return text.lstrip()[:VERBATIM_HEAD_CHARS]
```

- Strip leading whitespace so blank PDF preambles don't waste the budget.
- Preserve newlines inside the slice — heading-style first lines (`"Purchase Orders\n"`, `"Invoice\n"`) carry the discriminator.
- Cap at 300 characters. Single named constant in `reader.py`. Enforced single-definition by `tests/test_organize_constraints.py` (existing pattern).
- If `extract_status != "ok"` (extraction failed, file is binary/unsupported, etc.), leave `verbatim_head = ""`.
- The slice is computed in Python only — no extra LLM call, no extra latency, no extra cost.

The summarization call itself is unchanged — Reader still sends the full extracted text to Haiku and stores Haiku's prose response in `summary`. The slice is independent.

### 4. Classifier Stage A sees the slice (B) and uses filename when informative (C)

**Files:**
- `fda/organize/skills/taxonomy-proposer/SKILL.md`
- `fda/organize/classifier.py` (the Stage A payload builder)

**Changes:**

- Stage A sample payload (today: per-entry `{path_id, path, summary, ...}`) gains `verbatim_head`. The existing `path` field is unchanged — it already carries the basename. Approximate cost: ~100 sampled entries × ~300 chars / 4 chars-per-token ≈ 7.5K extra tokens per Stage A call. One call per run. Negligible.
- Prompt update in `taxonomy-proposer/SKILL.md` covering both the verbatim slice and the filename priority:
  - "Each sampled entry has `summary` (Reader's prose), `verbatim_head` (the raw first ~300 chars of the file's extracted text, leading whitespace stripped), and `path` (full path; the last component is the filename)."
  - "**Filename as signal.** When sampling, examine basenames. If many files share an informative naming convention (e.g. `Invoice_*.pdf`, `PO_*.pdf`, `Q3_Sales_Report_*.xlsx`), let that inform what categories to propose. **Ignore basenames that are hashes, random IDs, or generic placeholders** (e.g. `0fa84d61b3158eaba46dee96.pdf`, `doc1.pdf`, `Untitled.pdf`); rely on `verbatim_head` + `summary` for those."
  - "**Verbatim slice as signal.** Use `verbatim_head` to perceive structural diversity in the corpus; if many summaries describe similar 'order documents' but the slices show distinct templates (some begin with `'Purchase Orders'`, others with `'Invoice'`, others with `'Order ID:'` and a Shipping Details section), propose distinct categories accordingly."

### 5. Classifier Stage B sees the slice (B) and uses filename when informative (C)

**Files:**
- `fda/organize/skills/taxonomy-assigner/SKILL.md`
- `fda/organize/classifier.py` (the Stage B per-batch payload builder)

**Changes:**

- Stage B per-file payload gains `verbatim_head` alongside the existing `path_id`, `path`, `summary`, etc. The `path` field already carries the basename today. Cost: per-file × all files. For 10K files at 300 chars each ≈ 750K chars ≈ 200K extra tokens distributed across Stage B batches.
- Prompt update in `taxonomy-assigner/SKILL.md` — explicit priority hierarchy:
  - "Each entry has `path` (full path; last component is the filename), `summary` (Reader's prose, may be imprecise about document type), and `verbatim_head` (raw first ~300 chars of the file's extracted text)."
  - "**Signal priority for assignment:**"
    - "**1. Filename, if informative.** If the basename contains words that hint at document type or business purpose (e.g. `Invoice_10488.pdf`, `Q3_Sales_Report.xlsx`, `PO-2024-0042.pdf`), use it as a strong signal. Confirm with `verbatim_head` when possible, but a clearly-named file usually settles the assignment."
    - "**2. If the filename is a hash, a random ID, or generic** (e.g. `0fa84d61b3158eaba46dee96.pdf`, `doc1.pdf`, `Untitled.pdf`, `IMG_4521.jpg`), **ignore it.** Make the decision from `verbatim_head` and `summary` alone."
    - "**3. Within `verbatim_head` + `summary`:** if they disagree about document type, trust `verbatim_head`. Look for literal type labels in the slice (e.g. `'Invoice'`, `'Purchase Orders'`, `'Statement'`, `'Receipt'`) before falling back to prose."
    - "**4. Empty `verbatim_head` (extraction failed):** use `summary` only."
  - "How to recognize an uninformative filename: long hex strings (e.g. 16+ contiguous hex characters), UUID-like patterns, generic placeholders. When in doubt, default to ignoring the filename rather than over-weighting it."

### 6. Tests

**Modified:**

- `tests/test_organize_models.py` — assert `CatalogEntry().verbatim_head == ""` (default).
- `tests/test_organize_reader.py` — fixture file with leading whitespace; assert reader populates `verbatim_head`, strips leading whitespace, caps at 300 chars; on extraction failure assert `verbatim_head == ""`.
- `tests/test_organize_classifier.py` — three regression cases:
  - **Conflict case (B):** entry with `summary == "Purchase order #10488 for Frankenversand"`, `verbatim_head == "Order ID: 10488\n\nShipping Details:\n..."`, and a hash basename (`0fa84d61b3158eaba46dee96.pdf`). Stub taxonomy `[Purchase-Orders, Shipping-And-Fulfillment, Misc]`. Assert the per-file payload contains `verbatim_head` (wire format) and that a fake backend following the prompt returns `Shipping-And-Fulfillment`.
  - **Informative-filename case (C):** entry with basename `Invoice_10488.pdf`, `summary == "Order document for ACME Corp..."`, `verbatim_head == "Order ID: 10488\nCustomer: ACME..."` (no literal "Invoice" in slice). Assert assignment is `Sales-Invoices` — driven purely by filename signal.
  - **Hash-filename ignored case (C):** entry with basename `0fa84d61b3158eaba46dee96.pdf`, `summary == "Stock report for beverages..."`, `verbatim_head == "Monthly Stock Report\nCategory: Beverages..."`. Assert assignment is `Stock-Reports` — model must ignore the hash basename and use `verbatim_head` + `summary`.
- `tests/test_organize_constraints.py` — extend the existing `CONSTS` mapping to include `"VERBATIM_HEAD_CHARS": ("reader.py", "300")` so the named-constant-defined check covers it. Do **not** add `300` to `DISTINCTIVE_LITERALS`: the value `300` is already in `reader.py` as `READER_TOTAL_TIMEOUT_SECONDS = 300` (a seconds timeout, semantically distinct from the chars cap), so a literal-uniqueness check would be ambiguous. The named-constant check is sufficient.

All 113 existing tests must still pass.

### 7. Manual validation (post-merge)

Re-run `python3 scripts/diag_organize.py /tmp/fda-test-sets/randomized-company-documents-2026-05-05-003 --apply` against a fresh fixture copy. Cross-reference the resulting plan with `manifest.csv` ground truth. Acceptance threshold: ≥95% per-class accuracy on PO / Invoice / Shipping / Stock-Report; total ≤5 misclassifications across the 100 documents.

## Architecture impact

```
Reader                          Catalog                         Classifier
─────────                        ───────                         ──────────
extract(path) ─┐
               ├── summary (Haiku) ──┐
               │                     ├── CatalogEntry ──┐
               └── verbatim_head ────┘    {path_id,     │
                   (deterministic           path,       ├── Stage A ─── proposes taxonomy
                    Python; first           summary,    │   (sees path/basename, summary,
                    300 chars after         verbatim_   │    verbatim_head)
                    lstrip)                 head,       │
                                            ...}        └── Stage B ─── assigns each file
                                                            (sees same fields; priority:
                                                             filename → verbatim → summary)
```

The data flow shape is unchanged. One field (`verbatim_head`) is added to `CatalogEntry`. Three prompts (`file-summarizer`, `taxonomy-proposer`, `taxonomy-assigner`) are updated. The existing `path` field is reused — no new filename field needed. No changes to PlanBuilder, Executor, Verifier, or any orchestration code.

## Risks and mitigations

- **Token cost on large corpora.** 10K files × 300 chars ≈ 200K extra tokens spread across Stage B batches. Acceptable. If it becomes a budget concern at 100K-file scale, drop `VERBATIM_HEAD_CHARS` to 200 or 150 — first-line discriminators are usually <80 chars.
- **Slice misses signal that's later in the document.** A 300-char head won't capture footers ("Total Due", "Net Terms", "Bill To" address blocks). Out of scope; deferred. Most type discriminators in practice are at the top.
- **Extracted text starts with garbage** (e.g. malformed PDF where pdftotext yields binary noise before the content). The slice will be garbage too, and the prompt fallback ("empty or noisy → trust summary") covers this. Reader still produces a useful prose summary independently.
- **Classifier overcorrects** — trusts a misleading slice over a correct summary. Mitigation: prompt instructs to use the slice for *type label* signal specifically, not as a wholesale summary replacement. Stage B regression test pins the expected behavior on the canonical conflict case.
- **Reader prompt fix (A) doesn't fully prevent confabulation.** Mitigation: B carries the load. Even if A fails 100% of the time and Haiku continues to invent type labels, B alone should fix the bug as long as the Classifier follows the "trust verbatim" instruction.
- **Filename signal (C) over-trusted on misleading basenames.** Real corpora sometimes have lying filenames — e.g., `Invoice_old_template_donotuse.pdf` is actually a draft, or `Q3.pdf` is actually Q4. Mitigation: prompt instructs to *confirm filename signal with `verbatim_head` when possible*, not to use it blindly. The hash-filename test case pins the "ignore uninformative basename" behavior; if real-world misleading basenames become a problem, add a "verbatim must not contradict filename" check in a future iteration.
- **Hash detection in the prompt is heuristic, not deterministic.** The model decides what counts as "uninformative" — it could miscall a real word like `report.pdf` as generic, or miss a less-obvious hash format. Acceptable risk for v1: the priority hierarchy explicitly defaults to ignoring the filename when in doubt, so misjudgments fall back to the (correct) `verbatim_head` + `summary` path.

## Backward compatibility

- `CatalogEntry.verbatim_head` defaults to `""`. Existing tests, fixtures, and any external constructors (none known in production code) keep working.
- Existing classifier prompts continue to work if `verbatim_head` is empty (the prompt explicitly handles that case).
- No on-disk format changes (no journal/state schema impact). The catalog is in-memory only.
- No changes to public API of `organize()` or `apply_plan()`.

## Implementation order (handed to writing-plans)

1. `fda/organize/models.py`: add `verbatim_head` field with default `""`.
2. `tests/test_organize_models.py`: default-value test.
3. `fda/organize/reader.py`: implement `_verbatim_head`, populate field, define `VERBATIM_HEAD_CHARS = 300`.
4. `tests/test_organize_reader.py`: leading-whitespace strip, cap, extraction-failure cases.
5. `fda/organize/skills/file-summarizer/SKILL.md`: prompt rewrite (A).
6. `fda/organize/classifier.py` + `taxonomy-proposer/SKILL.md`: Stage A payload (+ `verbatim_head`) + prompt update covering both verbatim slice and filename-signal priority (B + C).
7. `fda/organize/classifier.py` + `taxonomy-assigner/SKILL.md`: Stage B payload (+ `verbatim_head`) + prompt update with the explicit signal-priority hierarchy `filename → verbatim → summary` (B + C).
8. `tests/test_organize_classifier.py`: three regression cases (conflict, informative-filename, hash-filename).
9. `tests/test_organize_constraints.py`: add `VERBATIM_HEAD_CHARS` to the `CONSTS` named-constant check.
10. Manual validation against the Northwind fixture (hash-named) and a follow-up smoke run with informative basenames if a fixture is available.

Each step is one commit; pre-commit hook enforces the test suite.
