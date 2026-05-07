# Organize: `.pptx` Extractor — Design

**Date:** 2026-05-07
**Branch:** dev_branch
**Predecessor:** `.docx` + `.xlsx` extractors — shipped 2026-05-07
**Tracker:** `Future Plan - Structural Sections Across Formats.md` (Obsidian, Lion_Chemtech/FDA)

## Goal

Extend the structural-fingerprint signal pipeline beyond PDF/plaintext/docx/xlsx by adding an extractor for `.pptx`. The extractor produces `ExtractionResult.text` (drives `verbatim_head` + Haiku summary) and `ExtractionResult.sections` (drives the classifier's structural-fingerprint signal).

For `.pptx`, `sections` is the ordered list of slide titles read from the title placeholder of each slide. No synthesized labels in v1.

## Non-goals

- `.hwp`, `.doc`, `.xls`, `.eml`, `.msg`, `.csv` upgrade — separate sub-projects.
- Korean structural-fingerprint regex coverage in `_sections.py` — corpus-blocked, parked. Not relevant here: pptx slide titles come from the format-native API and are language-agnostic; the `_sections.py` regex is not invoked.
- Synthesized purpose labels for pptx (analogous to xlsx's `FormulaHeavy` / `MergedCells`) — explicitly out for v1. Slide-title text already carries the discriminating signal in the common case. Add later only if Lion Chemtech corpus shows real ambiguity that titles cannot resolve.
- Text-frame fallback for slides without a title placeholder — out for v1. The cleaner v2 path, if measurement shows it is needed, is the "Haiku fallback on empty `sections`" pattern already documented as a possible v2 in `Future Plan - Structural Sections Across Formats.md`.
- PowerPoint Section grouping (the slide-grouping feature) — out for v1. Rarely used in real-world decks; not worth the surface area without measurement.
- Skill prompt revisions to `taxonomy-proposer` / `taxonomy-assigner` — they already consume `sections` as opaque `tuple[str, ...]`. Pptx supplies the same shape. (Pre-existing: the prompts still describe `sections` as coming from a regex pass — already stale for docx/xlsx, will be staler with pptx. Fix in a separate cleanup PR after pptx ships.)
- Reader, classifier, plan builder, executor, verifier changes — none.

## Architecture

Faithful extension of the v1 docx/xlsx format-onboarding pattern: one extractor function per format, registered in the `EXTRACTORS` dict. No new modules, no protocol abstraction, no rewrite of existing extractors.

**Files touched:**
- `pyproject.toml` — add `python-pptx>=1.0.2` as a required runtime dependency. (Older `0.6.x` line predates a `collections.abc` fix that matters under our Python 3.9+ floor.)
- `fda/organize/_extractors.py` — add `_extract_pptx`; register `.pptx` in `EXTRACTORS`; add three new pptx-specific constants. Existing extractors and constants are untouched.
- `tests/test_organize_extractors.py` — synthetic in-test fixtures and case coverage.
- `tests/test_organize_reader.py` — reader passes `sections` through for pptx (parallel to existing PDF/text/docx/xlsx coverage).
- `tests/test_organize_constraints.py` — register the three new constants in the CONSTS check.
- `tests/test_organize_pipeline.py` — add a pptx file to the integration corpus.

**Files not touched:** `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, all skill prompts.

**Text contract:** `_extract_pptx` does **not** apply its own 64 KiB cap on `text`. Reader owns that contract via `READER_TEXT_CAP_BYTES` (`reader.py:29`) and applies byte-boundary-safe truncation plus the `[TRUNCATED at 64KB]` marker. The three new pptx constants exist only to bound in-process memory while serializing (slides, shapes, notes), never as the contract cap.

**python-pptx baseline:** target installed version is `>=1.0.2`. Two known constraints shape the implementation:
- `Presentation` is **not** a context manager and exposes no `close()` — open with `Presentation(str(path))` and let GC release the zip handle. Matches the docx pattern.
- `slide.notes_slide` has a **creation side effect**: accessing it on a slide that has no notes will create a notes slide as a side effect, mutating the deck. Always gate access on `slide.has_notes_slide` first.

## `_extract_pptx`

```python
def _extract_pptx(path: Path) -> ExtractionResult: ...
```

Loader: `pptx.Presentation(str(path))`.

**`sections`** — slide titles in slide order:
- Iterate `presentation.slides`, capped at `_PPTX_SLIDES_MAX`.
- Per slide: read `slide.shapes.title` if present (the placeholder is `None` when the slide layout has no title placeholder, or when the title placeholder exists but holds no text frame).
- Extract the title text via `title.text_frame.text` (or `title.text`, whichever is the supported python-pptx accessor on the targeted version).
- Trim whitespace; collapse internal whitespace via `" ".join(s.split())`.
- Apply both length guards from `_sections.py`: `SECTION_HEADER_MIN_CHARS` (3) ≤ `len(label)` ≤ `SECTION_HEADER_MAX_CHARS` (40). Drop labels that fall outside.
- Preserve slide order; dedupe via ordered-set pattern (parallel to `_sections.py` and `_extract_docx`).
- Cap total at `MAX_SECTIONS_PER_FILE` (15).
- v1 limitation (documented): decks where the title is in a non-placeholder text box, or where the deck uses blank layouts, will yield `sections=()`. No text-frame fallback in v1.

**`text`** — per-slide banner + shape text + speaker notes, in slide order:
- Per slide (still capped at `_PPTX_SLIDES_MAX`):
  - Append `f"Slide {n}: {title_text}\n"` banner where `n` is 1-indexed slide number. If no title (or title fails the length guard / is empty after trim), append `f"Slide {n}:\n"` with no title.
  - Iterate `slide.shapes`, capped at `_PPTX_SHAPES_PER_SLIDE_MAX`. For each shape with `shape.has_text_frame`: append `shape.text_frame.text` if non-empty, then `\n`. (Title placeholder text will appear both in the banner and again as a shape; this duplication is acceptable — the banner makes slide order legible, the shape pass keeps the iteration uniform.)
  - If `slide.has_notes_slide` (gated, no side effect): append `"Notes: "` + `slide.notes_slide.notes_text_frame.text[:_PPTX_NOTES_CHARS_PER_SLIDE_MAX]` + `\n`. Never access `slide.notes_slide` without the `has_notes_slide` guard.
  - Trailing blank line between slides for readability.
- No extractor-side byte cap. Reader applies `READER_TEXT_CAP_BYTES`. The three caps above bound in-process memory while building the string.
- Drives `verbatim_head` (first 300 chars seen by classifier) + Haiku summary.

**Failure modes:**
- `PackageNotFoundError`, `KeyError`, `BadZipFile`, or other library exceptions → caught by the outer `extract()` try/except → `status="failed"`, `note=str(e)`, `text=None`, `sections=()`.
- Empty deck (no slides) → `text=""`, `sections=()`, `status="ok"`.
- Encrypted / password-protected pptx → library raises on open → `status="failed"`. We don't try to decrypt.
- Slide with no title placeholder → no `sections` entry for that slide; banner emits `Slide N:` (no title); shape and notes text still serialized normally.
- Slide-level exception during shape iteration → propagates to outer try/except; one bad slide fails the whole file rather than producing partial state. Acceptable: matches docx behavior.
- No `tool_missing` branch — `python-pptx` is a required dep.

## Constants

| Name | Value | Module | Purpose |
|---|---|---|---|
| `_PPTX_SLIDES_MAX` | `100` | `_extractors.py` (new) | Bounds slide loop. Most decks <50 slides; 100 covers the long tail without unbounded work. |
| `_PPTX_SHAPES_PER_SLIDE_MAX` | `50` | `_extractors.py` (new) | Bounds shape iteration per slide. Real slides rarely exceed ~15 shapes; 50 absorbs templated decks with chrome shapes. |
| `_PPTX_NOTES_CHARS_PER_SLIDE_MAX` | `2000` | `_extractors.py` (new) | Bounds speaker-notes serialization per slide. Most notes are <500 chars; 2000 keeps lecture-style decks readable without runaway. |
| `MAX_SECTIONS_PER_FILE` | `15` | `_sections.py` (existing) | Reused as global cap. |
| `SECTION_HEADER_MIN_CHARS` | `3` | `_sections.py` (existing) | Reused length guard. |
| `SECTION_HEADER_MAX_CHARS` | `40` | `_sections.py` (existing) | Reused length guard. |
| `READER_TEXT_CAP_BYTES` | `64 * 1024` | `reader.py` (existing) | Owns the 64 KiB text contract for **all** extractors — pptx does not duplicate. |

The three new constants must be registered in `tests/test_organize_constraints.py`.

## Skill prompts

Unchanged. The v1 prompts already teach both classifier stages to use `sections` as a structural fingerprint. Slide-title labels flow through the same `tuple[str, ...]` shape used by docx headings and xlsx schema labels. If post-ship classification on pptx reveals a discriminating axis the prompts can't see, that becomes a v2.1 prompt-tuning task.

(Pre-existing aside, not addressed here: the `taxonomy-proposer` / `taxonomy-assigner` SKILL.md docs still describe `sections` as a regex-derived signal. That description is already stale for docx/xlsx and will be staler with pptx. A small documentation cleanup PR — separate from this spec — should refresh those wordings to "format-aware structural fingerprint" once pptx is in.)

## Testing plan

Synthetic fixtures generated in-test via `python-pptx` (no binary fixtures checked in), mirroring how docx/xlsx tests work.

**Cases in `tests/test_organize_extractors.py`:**
- Single slide with title → `sections=(title,)`; `text` contains banner + title.
- Multi-slide deck with titles in slide order → `sections` preserves order, deduped.
- Slide with no title placeholder → no `sections` entry for that slide; banner emits `Slide N:`.
- Slide title that is whitespace-only or 2 chars → filtered out by `MIN_CHARS`.
- Slide title 41 chars → filtered out by `MAX_CHARS`.
- More than 15 titled slides → `sections` capped at 15.
- More than `_PPTX_SLIDES_MAX` slides → slide loop stops at the cap; later slides not in `text`.
- Slide with > `_PPTX_SHAPES_PER_SLIDE_MAX` shapes → shape iteration stops at cap; later shapes not in `text`.
- Speaker notes present → included in `text`, prefixed `Notes: `.
- Speaker notes longer than `_PPTX_NOTES_CHARS_PER_SLIDE_MAX` → truncated to that cap.
- Slide with `has_notes_slide=False` → no `Notes:` line; **regression test**: re-open the saved file with `Presentation(...)` and assert `slide.has_notes_slide` is still `False` (proves we didn't trigger the creation side effect).
- Empty deck (no slides) → `text=""`, `sections=()`, `status="ok"`.
- Corrupt file (bytes that aren't a pptx) → `status="failed"`.

**Reader test** (`tests/test_organize_reader.py`): pptx files flow through Reader; assert `CatalogEntry.sections` is populated and survives extractor failure (sections preserved as `()`, parallel to existing PDF/text/docx/xlsx coverage).

**Constants test** (`tests/test_organize_constraints.py`): three new constants registered in the CONSTS check.

**Classifier shape-budget test** (`tests/test_organize_classifier.py`): unchanged — payload shape stable; existing assertion that `sections` is included in payload still holds.

**Pipeline integration** (`tests/test_organize_pipeline.py`): add at least one pptx to the integration corpus; assert end-to-end moves complete without error.

**Full suite** must pass via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`. Exact count is not an acceptance criterion — green is.

## Sequencing context

This spec is sub-project 2 of the larger non-PDF format expansion. Subsequent sub-projects (each its own spec → plan → implementation cycle) in expected priority order:

1. ✅ `.docx` + `.xlsx` — shipped 2026-05-07.
2. **This spec** — `.pptx`.
3. **Korean structural-fingerprint coverage in `_sections.py`** — corpus-blocked; pending real Korean document samples. Not blocking pptx (slide titles are language-agnostic).
4. **`.hwp`** — pairs with (3). Goes plaintext → regex via `hwp5txt` / `pyhwp`. Without (3), Korean files yield empty `sections`.
5. Remaining backlog (`.csv` upgrade, `.doc`/`.xls`, `.eml`/`.msg`) — driven by what Lion Chemtech actually uploads.

Tracked in Obsidian: `Future Plan - Structural Sections Across Formats.md`.
