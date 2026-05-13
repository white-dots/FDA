---
status: DRAFT — 2026-05-13
topic: Honesty layer for unextractable files (organize pipeline)
scope: Reader short-circuit + plan_builder quarantine moves + router skip + new report sections; no LLM-prompt changes; no new extractors
---

# Extractor Coverage Honesty Design

The 2026-05-12 mixed-corpus test surfaced a silent failure mode in the
organize pipeline: files whose extension has no registered extractor
(`.doc`, `.xls`, `.ppt`, `.html`) and files whose extractor failed
(image-only PDFs, password-protected `.hwp`, etc.) land in `Misc/` with
a router-generated reason that *sounds* content-based but was produced
from a stub message containing only filename, extension, and size. The
user can't tell from the report that the file was never read.

This spec ships the **honesty layer**: a deterministic pre-routing
short-circuit that diverts unextractable files into two dedicated
quarantine buckets and surfaces them as their own sections in
`routing-report.md`. No LLM call is made for these files; no fabricated
reason is written. Adding extractors for the four legacy formats is
*deliberately* out of scope and becomes a separate follow-up spec per
format (matching the existing per-format sub-project pattern:
`.docx/.xlsx`, `.pptx`, `.csv`, `.hwpx/.hwp`).

## The rule

For each file in the target tree:

1. **Reader runs the extractor first.** Existing behavior; no change.
2. **If `extract_status != "ok"` AND the file is not junk**, reader
   skips the per-file LLM summary call and emits a **quarantine
   catalog entry** (empty `summary` and `type_label`, `extract_status`
   carrying the underlying reason: `"no_extractor"`, `"failed"`, or
   `"tool_missing"`).
3. **Quarantine entries bypass the classifier entirely.** The
   classifier only sees extractable entries.
4. **Plan_builder emits MOVE operations** for quarantine entries to
   one of two buckets, grouped by extension:
   - `extract_status == "no_extractor"` → `_NoExtractor/<ext>/<basename>`
   - `extract_status in ("failed", "tool_missing")` → `_ExtractionFailed/<ext>/<basename>`
   - The `<ext>` segment is `path.suffix.lower().lstrip(".")`, or
     `_no_ext` when the file has no extension.
5. **Router never invokes the destination-router LLM skill for
   quarantine moves.** They have no cloud destination; they are
   local-only bookkeeping.
6. **`routing-report.md` gains two new sections** at the bottom,
   `## 건너뜀 — 추출기 없음 (No Extractor)` and `## 건너뜀 — 추출 실패
   (Extraction Failed)`, each grouped by extension, listing every
   skipped file with its size and the extractor's note. Sections are
   omitted when empty.

## Why short-circuit in the reader (not in the classifier or router)

The signal that "this file has no readable text" originates in the
extractor. Putting the short-circuit immediately after extraction means:

- **No fabricated summaries are ever produced.** Today reader sends a
  stub like `(stub: no_extractor; classify by filename/extension/size)`
  to the file-summarizer LLM, which dutifully produces a plausible-
  sounding `type_label` and `summary` from the stub. Those values then
  propagate through the catalog and influence downstream LLM calls.
  Skipping the LLM call removes both the fabrication and its downstream
  influence in one step.
- **Haiku tokens saved.** One call per unextractable file. Modest per
  file, but consistent across every run on every corpus.
- **The decision lives next to the signal.** No new branching elsewhere
  in the pipeline. Classifier, plan_builder, and router each get one
  small touch that derives quarantine state from existing fields.

Alternative seams considered and rejected during brainstorming:

- **Filter between reader and classifier.** Would still pay for the
  Haiku call and still write fabricated summaries to the catalog
  index. Wasteful and slightly dishonest at rest.
- **Tell the classifier LLM to emit a quarantine grouping.** Delegates
  failure-handling to a non-deterministic model. Wrong place for this
  decision.

## Scope

### In scope (changes in this v1)

1. **`fda/organize/reader.py`** — `_summarize_one` short-circuits the
   backend call when `extraction.status != "ok"`. New `_quarantine_entry`
   helper. New `READER_QUARANTINE` log event.
2. **`fda/organize/models.py`** — two new module constants
   (`QUARANTINE_NO_EXTRACTOR = "_NoExtractor"`,
   `QUARANTINE_FAILED = "_ExtractionFailed"`) and one new pure helper
   (`quarantine_bucket(entry: CatalogEntry) -> str | None`). New
   `QuarantineEntry` and `QuarantineGroup` dataclasses. `RoutingReport`
   gains a `quarantine: tuple[QuarantineGroup, ...]` field.
3. **`fda/organize/classifier.py`** — filter out entries where
   `quarantine_bucket(e) is not None` before the LLM grouping pass.
   Empty extractable input emits empty `Groupings` (existing code path).
4. **`fda/organize/plan_builder.py`** — `build()` gains a keyword-only
   `quarantine: Sequence[CatalogEntry] = ()` parameter. For each entry,
   emit a MOVE op with destination
   `<target>/<bucket>/<ext>/<basename>` and a synthesized reason
   (`"no extractor registered for .doc"` or the extractor's `note`).
   Existing collision-resolution, path-traversal, and
   `_fs.validate_operation` checks apply unchanged.
5. **`fda/organize/router.py`** — `route()` partitions plan move
   operations into category moves and quarantine moves by destination
   prefix. Quarantine moves are grouped into `QuarantineGroup`s
   (keyed by bucket + extension) and attached to `RoutingReport`. No
   destination-router skill invocation for quarantine moves. New
   `ROUTER_QUARANTINE_GROUP` log event.
6. **`_write_json_report` / `_write_md_report`** — JSON sidecar gains
   a top-level `quarantine` key parallel to `categories`. Markdown
   report appends two new sections (Korean primary, English bucket
   name in parens) when non-empty. Header summary line gains a skip
   count when non-empty.
7. **Tests** — new unit tests per stage (reader, plan_builder, router,
   report writer) plus extensions to the existing organize integration
   fixture. Regression sweep of existing tests that fed `.doc`/`.xls`/
   `.ppt`/`.html` fixtures expecting `Misc/` placement.

### Out of scope (deferred to future specs)

1. **Extractors for `.doc`, `.xls`, `.ppt`, `.html`.** Each becomes
   its own per-format sub-project. The honesty layer is the universal
   scaffolding they build on; once a format ships an extractor, its
   files stop landing in `_NoExtractor/<ext>/` and start flowing into
   real business categories. No coupling between extractor specs and
   this spec beyond the constant names.
2. **Low-confidence file quarantine.** Sibling problem: the LLM saw
   text but couldn't bucket it confidently. Requires its own design
   (confidence signal source, threshold calibration, bucket name like
   `_LowConfidence/`). Reuses this spec's bucket convention and
   short-circuit pattern.
3. **Empty-but-extracted files** (extractor returns
   `status="ok"`, `text=""`). Continue through the normal pipeline.
   Same family as the low-confidence problem; defer.
4. **`summary_failed=True` with `extract_status="ok"`** (LLM call
   timed out or returned bad JSON). Not a quarantine case — the
   classifier still has `verbatim_head` and `sections` to work with.
   Existing failure-summary-threshold behavior preserved.

## Architecture

```
files
  │
  ▼
reader ── extractor ──┬── status == "ok"  ─→ LLM summary ─→ catalog (extractable)
                      │
                      └── status != "ok"  ─→ skip LLM     ─→ catalog (quarantine)
                          (and not is_junk)

extractable ─→ classifier ─→ groupings ──┐
                                         ├──→ plan_builder ─→ executor
quarantine ─────────────────→────────────┘                       │
                                                                 ▼
                                                             router
                                                                 │
                                                                 ▼
                                                  routing-report.{json,md}
                                                  (categories + Skipped sections)
```

## Data model

One new private field on `CatalogEntry`:

```python
quarantine_note: str = ""
```

It carries the extractor's `ExtractionResult.note` for quarantine
entries (e.g., `"password-protected hwp"`, `"pdftotext produced no text
(image-only PDF?)"`) so plan_builder can synthesize a human reason
without re-running the extractor. For non-quarantine entries it stays
empty. The default makes existing test fixtures and call sites
forward-compatible.

The quarantine-bucket discriminator continues to use the existing
`extract_status` field. A pure helper exported from
`fda/organize/models.py` derives the bucket:

```python
def quarantine_bucket(entry: CatalogEntry) -> str | None:
    """Return the quarantine bucket name, or None if the entry is
    processed normally.

    Returns None for:
    - Junk files (handled by the existing DELETE path)
    - Files whose extraction succeeded (extract_status == "ok")

    Returns "_NoExtractor" for files whose extension has no registered
    extractor (extract_status == "no_extractor").

    Returns "_ExtractionFailed" for files whose extractor ran but
    failed (extract_status in {"failed", "tool_missing"}).
    """
    if entry.is_junk:
        return None
    if entry.extract_status == "ok":
        return None
    if entry.extract_status == "no_extractor":
        return QUARANTINE_NO_EXTRACTOR
    return QUARANTINE_FAILED
```

Two new frozen dataclasses live next to `RoutingReport`:

```python
@dataclass(frozen=True)
class QuarantineEntry:
    relative_path: str  # final on-disk path after move, relative to target
    size_bytes: int
    note: str           # synthesized reason (no extractor / extractor note)

@dataclass(frozen=True)
class QuarantineGroup:
    bucket: str         # QUARANTINE_NO_EXTRACTOR | QUARANTINE_FAILED
    ext: str            # "doc", "pdf", "_no_ext", ...
    entries: tuple[QuarantineEntry, ...]
```

`RoutingReport` gains a `quarantine: tuple[QuarantineGroup, ...]` field
with a sane default (`()`).

## Component changes

### Reader (`fda/organize/reader.py`)

In `_summarize_one` (currently line 103), after `extraction =
_extractors.extract(path)`:

```python
size = path.stat().st_size
extraction = _extractors.extract(path)

if extraction.status != "ok":
    return _quarantine_entry(path, size, extraction), "quarantine", ""

head = _verbatim_head(extraction)
user = _build_user_message(path, extraction, size)
# ... existing backend.complete call unchanged ...
```

`_quarantine_entry` constructs a `CatalogEntry` with:
- `path_id=""` (filled by caller after global sort)
- `path=str(path)`, `ext=path.suffix.lower()`, `size_bytes=size`
- `summary=""`, `type_label=""`
- `is_junk=False`
- `summary_failed=False` (extraction failure ≠ summary failure)
- `extract_status=extraction.status`
- `quarantine_note=extraction.note or ""`
- `verbatim_head=""`, `sections=()`

The "quarantine" log kind is handled in the `read()` loop alongside
`done` / `timeout` / `fail` / `deadline`, emitting a single
`READER_QUARANTINE` log event with `path`, `extract_status`, and
`note=extraction.note` (no `elapsed_ms` — no LLM call to measure).

The existing `_build_user_message`'s no-extractor / tool-missing /
failed branches become dead code once this ships. They are deleted in
the same change to keep the module honest.

### Classifier (`fda/organize/classifier.py`)

One filter pass at the top of the function that consumes the catalog
for LLM grouping:

```python
extractable = [e for e in real if quarantine_bucket(e) is None]
# ... existing grouping logic operates on `extractable` ...
```

Where `real` is today's `[e for e in catalog.entries if not e.is_junk]`.
No change to the classifier prompt. The failed-summary threshold check
(line 593-602) continues to compare `summary_failed` over `real`; the
threshold's semantics get cleaner because extractor failures no longer
inflate the count.

The taxonomy-proposer Sonnet skill is unaffected (it receives the
extractable subset; format unchanged).

### Plan builder (`fda/organize/plan_builder.py`)

`build()` signature gains a keyword-only parameter:

```python
def build(
    target_dir: str,
    groupings: Groupings,
    path_by_id: Mapping[str, str],
    junk_paths: Sequence[str],
    *,
    quarantine: Sequence[CatalogEntry] = (),
) -> Plan:
```

For each quarantine entry:

1. Determine bucket via `quarantine_bucket(entry)`.
2. Compute `ext_segment = entry.ext.lstrip(".") or "_no_ext"`.
3. Resolve destination dir via existing `_resolve_destination_dir`
   with subpath `f"{bucket}/{ext_segment}"`. Existing sanitization,
   path-traversal rejection, and target-relative check apply.
4. Resolve basename via existing `_resolve_basename` (collision
   safety with both planned and on-disk files).
5. Synthesize reason:
   - `_NoExtractor` → `f"no extractor registered for {entry.ext}"`
     (use `entry.ext` rather than `<ext_segment>` so the leading dot
     is preserved in the human note even when the subfolder strips it)
   - `_ExtractionFailed` → use `entry.quarantine_note` (populated by
     the reader from `ExtractionResult.note`) when non-empty; fall
     back to `entry.extract_status` (`"failed"` or `"tool_missing"`)
     when the note is empty.
6. Emit a single MOVE `Operation` and feed it through the same
   `_fs.validate_operation` path as category moves.
7. Emit corresponding CREATE_DIR ops for the surviving destination
   dirs (folded into the existing `surviving_dirs` set so a single
   create_dir is emitted per directory).

Empty `groupings.items` plus non-empty `quarantine` is a valid input
and produces a plan whose only operations are quarantine MOVEs +
their CREATE_DIRs. The existing "nothing to do" guard (line 265-269)
considers `quarantine` along with `groupings.items` and `junk_paths`
when deciding whether to raise.

### Router (`fda/organize/router.py`)

After the executor has run, the router walks `plan.operations` and
partitions MOVEs:

```python
def _is_quarantine_dest(destination: str, target: Path) -> bool:
    try:
        rel = Path(destination).relative_to(target)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in (
        QUARANTINE_NO_EXTRACTOR, QUARANTINE_FAILED,
    )
```

Quarantine MOVEs are collected, grouped by `(bucket, ext)`, and
emitted as `QuarantineGroup` records attached to `RoutingReport`. The
existing per-category loop (`for g in groupings.items`) sees no
quarantine entries because the classifier filtered them out upstream.
No call to `_route_one_category` / the destination-router skill for
any quarantine move.

`final_path_by_id` continues to resolve the post-move path for both
category and quarantine moves (executor's MOVE outcomes are walked
uniformly). Quarantine entries' `relative_path` reflects the
post-move on-disk location.

One new log event `ROUTER_QUARANTINE_GROUP` per emitted group, with
`bucket`, `ext`, and `count`.

### Report writer

**JSON sidecar (`_write_json_report`).** `_report_to_dict` gains a
`quarantine` key:

```json
{
  "version": "1.0",
  "generated_at": "...",
  "target_root": "...",
  "categories": [ ... ],
  "quarantine": [
    {
      "bucket": "_NoExtractor",
      "ext": "doc",
      "entries": [
        {
          "relative_path": "_NoExtractor/doc/old-quote.doc",
          "size_bytes": 24576,
          "note": "no extractor registered for .doc"
        }
      ]
    }
  ]
}
```

The `quarantine` key appears even when empty (as `[]`) for
schema stability.

**Markdown report (`_write_md_report`).** When
`report.quarantine` is non-empty, the header summary line gains a
trailing segment:

```
총 카테고리: 7 · 총 파일: 102 · 건너뜀: 15 (추출기 없음 12, 추출 실패 3)
```

After the existing `## 카테고리별 라우팅` section, two new sections
appear in fixed order, each only when the corresponding bucket is
non-empty:

```markdown
## 건너뜀 — 추출기 없음 (No Extractor)

총 12개 파일 · 추출기 등록 시 일반 카테고리로 흐름

### .doc (8개)
- `_NoExtractor/doc/old-quote.doc` (24KB) — no extractor registered for .doc
- `_NoExtractor/doc/contract-v2.doc` (47KB) — no extractor registered for .doc
...

### .xls (4개)
...

## 건너뜀 — 추출 실패 (Extraction Failed)

총 3개 파일 · 파일 자체 문제로 본문을 읽지 못함

### .pdf (2개)
- `_ExtractionFailed/pdf/scan-receipt.pdf` (180KB) — image-only PDF?
...
```

Within each bucket, extensions are listed in alphabetical order;
within each extension, entries are listed by `relative_path`.

Size formatting matches the existing report style (raw bytes for the
JSON sidecar; human-readable in the markdown). The existing
`{size_bytes:,}` style is preserved verbatim in the markdown to keep
the report visually consistent.

## Edge cases

- **Junk wins over quarantine.** `is_junk=True` short-circuits
  `quarantine_bucket()` to `None`. `.DS_Store` and friends keep their
  DELETE path even though they also have `extract_status="no_extractor"`.
- **No extension.** `path.suffix == ""` → subfolder is `_no_ext`. The
  synthesized note becomes `"no extractor registered for ''"`; this is
  an honest description of the situation and acceptable for v1.
- **Empty text with `extract_status="ok"`.** Not quarantined; flows
  through the normal pipeline. Deferred to the same future spec as
  low-confidence quarantine.
- **`summary_failed=True` with `extract_status="ok"`.** Not
  quarantined. The classifier still has `verbatim_head` and `sections`
  to work with; existing behavior preserved.
- **All files in a target are quarantined.** Classifier sees an empty
  extractable set, emits empty `Groupings` (existing path), plan_builder
  emits only quarantine MOVEs + CREATE_DIRs, executor applies, router
  walks the empty `groupings.items` and emits only `QuarantineGroup`s,
  report shows only the Skipped sections. No crash. The
  `READER_FAILED_SUMMARY_THRESHOLD` check (which guards "too many
  classifier failures") is unaffected — quarantine entries are no
  longer counted as classifier failures.
- **Existing `_MISC_CATEGORY_NAMES` and `all_extraction_failed`
  signal** in `router.py` (line 75, 56) become effectively dead for
  non-junk corpora — quarantine takes over the "no usable signal"
  cases upstream. The code stays as defense-in-depth: a classifier
  bug that emits a Misc category for low-content extractable files
  should still short-circuit cleanly.
- **CatalogEntry layout for journal/index.** The journal indexes
  catalog entries with `extract_status` for searchability; quarantine
  entries appear in `index.json` with `summary=""` and
  `type_label=""`. Consumers that filter by truthy `summary` (none
  today, but worth noting) would miss them — they should filter by
  `quarantine_bucket()` instead.

## Testing

### Reader unit tests (`tests/test_organize_reader.py`)

1. Mock `_extractors.extract` to return `status="no_extractor"`.
   Assert: `backend.complete` is **not called**, returned entry has
   `extract_status="no_extractor"`, `summary=""`, `type_label=""`,
   `is_junk=False`, `summary_failed=False`.
2. Same for `status="failed"` and `status="tool_missing"`.
3. Junk file with `status="no_extractor"` → still gets `_junk_entry`
   treatment (`is_junk=True`, `type_label="junk"`); not quarantine.
4. Successful extraction (`status="ok"`) still routes through the
   backend.complete LLM-summary path (regression).
5. `READER_QUARANTINE` log event emitted with `path`,
   `extract_status`, and `note`.

### Plan_builder unit tests (`tests/test_organize_plan_builder.py`)

1. Single `.doc` quarantine entry → single MOVE op to
   `_NoExtractor/doc/old-quote.doc` + matching CREATE_DIR for
   `_NoExtractor/doc/`.
2. Single failed PDF → MOVE to `_ExtractionFailed/pdf/scan.pdf` with
   the extractor's note as reason.
3. No-extension file (`path.suffix == ""`) → `_NoExtractor/_no_ext/`.
4. Collision: two `.doc` files with the same basename (different
   source dirs) → second gets `name (2).doc`.
5. Quarantine MOVE passes `_fs.validate_operation` (target-relative
   destination, no path traversal).
6. Mixed input: groupings + quarantine + junk → all three move/delete
   classes produced; surviving CREATE_DIRs cover all destinations.
7. Empty groupings + non-empty quarantine → plan is valid, contains
   only quarantine MOVEs + CREATE_DIRs (does not raise the "nothing to
   do" guard).
8. Empty groupings + empty quarantine + empty junk → existing
   "nothing to do" guard still raises (regression).

### Router unit tests (`tests/test_organize_router.py`)

1. Plan with mixed category + quarantine MOVEs → destination-router
   skill mock called only for category MOVEs (assert call count).
2. `RoutingReport.quarantine` populated correctly: groups keyed by
   `(bucket, ext)`, entries ordered by `relative_path`, alphabetical
   ext order within each bucket.
3. Plan with only quarantine MOVEs → `RoutingReport.categories == ()`,
   `RoutingReport.quarantine` non-empty.
4. Plan with no quarantine MOVEs → `RoutingReport.quarantine == ()`
   (existing behavior preserved for fully-extractable corpora).

### Report writer unit tests

1. JSON sidecar: top-level `quarantine` key present, even when empty
   (as `[]`); when non-empty, structure matches the documented shape.
2. Markdown: Skipped sections appear when their bucket is non-empty;
   omitted when empty. Header summary line gains the skip count only
   when total quarantine count > 0.
3. Markdown: per-extension `### .<ext> (N개)` subheaders match the
   documented format; per-file lines use the documented format.

### Integration test (extending the organize end-to-end fixture)

1. Mixed corpus: real `.docx` content + unsupported `.doc` + corrupt
   `.pdf` → real category gets `.docx`, `_NoExtractor/doc/` gets
   `.doc`, `_ExtractionFailed/pdf/` gets `.pdf`. Verify on-disk
   layout and report contents.
2. All-quarantine corpus (only `.doc` files) → no crash, report has
   only Skipped sections, executor moves all files into
   `_NoExtractor/doc/`.

### Regression sweep

Grep existing tests for fixtures named or referenced as
`.doc`/`.xls`/`.ppt`/`.html`. Any test that asserted these went to
`Misc/` must be updated to expect `_NoExtractor/<ext>/`. The
2026-05-12 mixed-corpus fixture under
`/private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12/` is
the integration eyeball — rerun after merge to confirm the routing
report no longer fabricates content reasons for the legacy formats.

## Notes for plan-writing

The following are implementation-shape decisions settled here so
plan-writing can proceed without re-litigating:

1. **Plan_builder receives the extractor's `note` via
   `CatalogEntry.quarantine_note`** (populated by the reader from
   `ExtractionResult.note`). Field defaults to `""` for backward
   compatibility with existing tests and call sites.
2. **`QuarantineGroup` ordering in the report.** Within a group,
   entries are sorted by `relative_path`. Across groups, extensions
   are listed alphabetically. Buckets are emitted in fixed order:
   `_NoExtractor` first, `_ExtractionFailed` second. Plan-writing
   should confirm the implementation produces stable output across
   runs (deterministic dict iteration, sorted lists).
3. **Korean note text.** Synthesized notes stay English in v1
   (`"no extractor registered for .doc"`, extractor notes are
   English-only today). Section headers and intro lines are Korean
   per the existing report convention. Per-file notes are
   infrastructure-style surface; the Korean-bucket-names spec
   (2026-05-13) does not cover them.

## Future work

After this spec ships, the following follow-ups become unblocked:

1. **`.html` extractor.** Cheapest of the four (lxml/BeautifulSoup).
   Files stop landing in `_NoExtractor/html/`.
2. **`.doc` extractor.** Subprocess via antiword/wv/textract. Files
   stop landing in `_NoExtractor/doc/`.
3. **`.xls` extractor.** Library via xlrd. Files stop landing in
   `_NoExtractor/xls/`.
4. **`.ppt` extractor.** Hardest — likely `libreoffice --headless`.
   Files stop landing in `_NoExtractor/ppt/`.
5. **Low-confidence quarantine.** Sibling spec. Reuses this spec's
   `_<reason>/` bucket convention and pre-routing short-circuit
   pattern, introducing a third bucket (`_LowConfidence/` or
   `_Ambiguous/`) and a confidence signal whose source is decided in
   that spec's own brainstorm.
6. **Empty-but-extracted file handling.** Same family as
   low-confidence; folded into the same future spec.
