---
status: FINAL — 2026-05-12
topic: Cloud destination routing (SharePoint / S3 / RDBMS)
scope: Routing decision only — no uploads, no file moves, no taxonomy changes
---

# Cloud Routing Design

After the organize pipeline produces a tree of category folders on the user's
local disk, each category needs a cloud destination: SharePoint, S3, or RDBMS.
This document specifies how that decision is made and how the result is
reported. **No files are uploaded or moved by this stage.** The output is
purely a recommendation document the user (or a future uploader) can act on.

## Why three destinations

The split is driven by **how each file will be used**, not by cost or
compliance:

- **SharePoint** — the company's shared drive in the cloud. People open files
  here from their laptop, search them, share with coworkers. Best for anything
  an employee might actually read, edit, or look up.
- **S3** — a giant warehouse for boxes. Cheap, holds a lot, but nobody walks in
  to browse. Best for stuff a program will use (a pipeline, a backup script, an
  AI model), or stuff kept "just in case" but rarely touched.
- **RDBMS** — a structured table you can sort, filter, and join with other
  tables. Best for clean tabular data with consistent columns that someone will
  query as data.

## The routing rule

For each category folder, apply these tests in order. The first match wins.

1. **Tabular data someone will query** (clean rows + columns, consistent
   schema — e.g. sales records, transactions, employee lists) → **RDBMS**
2. **Anything a person will open through SharePoint or Teams in the next ~year**
   (read it, search for it, share it, edit it — even if it's a finalized
   read-only PDF) → **SharePoint**
3. **Everything else** (bulk archives, raw scans, OCR inputs, large blobs,
   files only systems consume) → **S3**

### The key insight: "Will a person open this file?"

Mutability ("will employees edit this?") is close but not quite right. A signed
contract PDF will never be edited again, but employees still need to *find*
it, *open* it, and *reference* it from Teams. SharePoint is where people look.
So it belongs there, even though it's read-only.

The better test is **"Will a person open this file through M365 in the next
year?"** Mutability becomes a sub-signal (if yes, version history is a bonus)
rather than the deciding factor.

### Concrete examples

| File | Destination | Why |
|---|---|---|
| Policy documents (.hwp / .docx) | SharePoint | Employees look these up |
| Signed contract PDFs | SharePoint | Read-only, but Legal / Finance need to find them |
| Sales report PowerPoints | SharePoint | People reference past decks |
| Clean `sales_2025.csv` with consistent columns | RDBMS | It's data — you'll want to query it |
| Budget model `.xlsx` with formulas | SharePoint | It's a document that happens to be in Excel |
| 10,000 scanned receipt images | S3 | Bulk, no human opens these one by one |
| Old email backup `.zip` | S3 | Archive, rarely touched |
| Raw OCR output feeding a pipeline | S3 | A program reads it, not a person |

## Granularity

The routing decision is **per category folder**, not per file. After the
organize pipeline groups files into categories (e.g., `Finance/Invoices`,
`Reports/Sales`, `Policies/HR`), each *whole category* gets one destination.

If a category turns out to contain a mix of files where some don't fit the
chosen destination (e.g., a folder routed to SharePoint contains 3 clean CSVs
that would have been RDBMS candidates), the routing report **flags those
files as `misfits`** rather than re-organizing. The user decides whether to
re-run organize with tighter instructions or accept the small misplacement.

## CLI surface

Routing runs as a new stage **inside `fda organize`**, on by default. The user
runs the same command they run today:

```
fda organize <folder>
```

Routing executes after the existing classifier/plan-builder/executor stages
finish writing the category tree to disk. To skip routing (e.g., during
quick taxonomy iteration), pass `--no-route`:

```
fda organize <folder> --no-route
```

There is no separate `fda organize route` subcommand. If a user wants to route
an already-organized tree without re-running the whole pipeline, that is
outside v1 scope.

## Output artifact

Two sidecar files are written to the root of the organized tree:

- `<target>/routing-report.json` — machine-readable, the format a future
  uploader will consume.
- `<target>/routing-report.md` — human-readable summary, rendered from the
  same data.

Both files are **overwritten** on each `fda organize` run. No history is
retained. (If history is wanted later, it can be added as a follow-up by
writing to `.fda/runs/<timestamp>/` instead.)

### JSON shape

```json
{
  "version": "1.0",
  "generated_at": "2026-05-12T14:30:00Z",
  "target_root": "/absolute/path/to/organized/tree",
  "categories": [
    {
      "name": "Finance/Invoices",
      "subpath": "Finance/Invoices",
      "destination": "sharepoint",
      "reason": "Finance team retrieves these regularly via Teams and M365 search.",
      "low_confidence": false,
      "signals": {
        "file_count": 47,
        "total_size_bytes": 12345678,
        "extension_distribution": {".pdf": 38, ".docx": 9},
        "tabular_schema_consistent": false,
        "all_extraction_failed": false
      },
      "misfits": [
        {
          "path_id": "f042",
          "relative_path": "Finance/Invoices/sales_2025.csv",
          "suggested_destination": "rdbms",
          "reason": "Clean tabular data with consistent schema."
        }
      ]
    }
  ]
}
```

- `destination` ∈ `{"sharepoint", "s3", "rdbms"}`.
- `low_confidence` is `true` when the routing call was made on weak signal
  (see *Edge cases* below). The destination is still chosen; the flag tells
  the user to second-guess.
- `signals` are the structural inputs that were fed to Claude. Recording them
  in the output makes calls reproducible and debuggable.
- `misfits` is an array; empty when nothing is flagged.

### Markdown shape

`routing-report.md` mirrors the JSON in prose form. Per category: name,
destination as a bold header, reason as a sentence, signals as a short list,
and a misfits subsection only if any are flagged. Top-of-file summary lists
counts per destination.

## Decision engine

A new pipeline stage after the classifier. Implemented as a Sonnet skill,
following the existing `taxonomy-proposer` and `taxonomy-assigner` pattern.

**Input per category (sent to Claude):**

- Category metadata: `category_name`, `description`, `criteria`, `subpath`
- File count and total size
- Dominant file-extension distribution (e.g., `{".pdf": 38, ".docx": 9}`)
- Aggregated structural signals: `tabular_schema_consistent` (bool — true
  iff every entry in the category has a consistent column schema, e.g. CSVs
  with matching headers) and `all_extraction_failed` (bool — true iff
  `summary_failed is True` for every `CatalogEntry` in the category).
- A sample of file summaries from inside the category.

**Output per category (returned by the skill):**

- `destination`: one of `sharepoint`, `s3`, `rdbms`
- `reason`: prose explanation
- `misfits`: list of objects `{path_id, suggested_destination, reason}` for
  files that don't fit the chosen destination (may be empty). The router
  resolves each misfit's `relative_path` from the catalog when writing the
  JSON report; the skill itself only returns the three fields above per
  misfit.

Structural signals — including total size — are **inputs to Claude's
judgment**, not hard-coded overrides. Claude decides; the signals inform. No
hard size cap; a 200 GB category of signed contracts can still go to
SharePoint if purpose justifies it, though large size pulls Claude's
recommendation toward S3 in most cases.

## Edge cases

The router applies these defaults *before* calling Claude. When a default
fires, the category is also tagged `low_confidence: true` in the report.

| Situation | Default destination | Calls Claude? |
|---|---|---|
| Category named `Misc` (taxonomy proposer's catch-all) | `s3` | No — short-circuit |
| All files in the category have `summary_failed: true` | `s3` | No — short-circuit |
| Single-file category | (no special handling) | Yes — route normally |
| Empty category (zero files) | (skipped entirely; not included in report) | No |

"Extraction failed" for routing's purposes means **the summarizer produced no
usable summary for Claude**, i.e. `summary_failed is True` on the
`CatalogEntry`. We do not separately check `extract_status`; a file whose
text was extracted but failed to summarize is just as unhelpful to routing
as one whose text was never extracted.

`low_confidence` only marks routing decisions made on weak signal. Confident
calls — including single-file categories where the one file has clear
extraction — remain `low_confidence: false`.

## Sensitivity / confidentiality

**Out of scope.** Routing picks SharePoint / S3 / RDBMS by purpose, not by
sensitivity. HR records, legal contracts, PII, etc. land in default
SharePoint along with everything else SharePoint-bound. Access control to a
restricted SharePoint site is handled separately — either by SharePoint
permissions, or by a future enhancement that adds a `confidential` flag on
each category. Not part of v1.

## Module layout

- `fda/organize/router.py` — pipeline stage entry point. Iterates categories,
  applies edge-case defaults, calls the skill, validates Claude's response,
  writes both report files.
- `fda/organize/skills/destination-router/SKILL.md` — the Sonnet skill prompt
  and response schema. Follows the same shape as
  `fda/organize/skills/taxonomy-proposer/` and
  `fda/organize/skills/taxonomy-assigner/`.
- `tests/test_organize_router.py` — unit tests (see *Validation* below).

The CLI flag `--no-route` is wired through three layers — `fda/cli.py`
parses it, `LocalWorkerAgent.organize_files` forwards it, and the
`organize()` entry point in `fda/organize/__init__.py` skips the router
stage when set. All three signatures gain a `route: bool = True` parameter
(or equivalent). The flag defaults to "routing on"; users opt out, not in.

## Validation

Two layers.

### Layer 1 — Unit tests (automated, every commit)

Synthetic category fixtures + mocked Claude backend (`get_claude_backend()`
mock, following existing FDA convention). These tests protect the **code**,
not Claude's judgment. Coverage:

- Routing-rule output is written to the report correctly for each
  destination.
- `Misc` category short-circuits to `s3` with `low_confidence: true`,
  regardless of mocked Claude reply.
- All-extraction-failed category short-circuits to `s3` with
  `low_confidence: true`.
- Single-file category routes normally (no short-circuit).
- Empty category is skipped, not included in the report.
- Malformed Claude response raises a clean, reported error (does not crash
  the pipeline).
- Misfit list is parsed and propagated to the JSON output.
- JSON and Markdown reports are both written to the target root.
- `--no-route` flag bypasses the stage entirely.

Target ~10–15 tests, milliseconds to run, no live API calls. Pre-commit hook
enforces them along with the rest of the suite.

### Layer 2 — Corpus eyeballing (manual, one-time, before shipping)

Run the actual router stage (real Claude) against the three existing
organized trees: **English v1**, **Korean A**, **Lion Chemtech**. For each,
open `routing-report.md` and read it. Tune the skill prompt or signal set if
a destination call is clearly wrong. No pass/fail metric, no assertion —
this is human judgment confirming Claude's judgment is reasonable on real
data before the feature ships.

Snapshot-style regression tests on real corpora are explicitly **not** in v1
scope. If routing drifts later, that can be added as a follow-up.

## Out of scope (deliberately)

- Actual uploads (SharePoint Graph API, S3 PutObject, RDBMS schema design /
  ingest).
- Credentials, IAM, SharePoint site provisioning.
- Re-organizing files. Mixed categories are reported via `misfits`, not
  split.
- Changes to the existing taxonomy proposer. If routing reveals that
  taxonomies often come out mixed, that's a separate follow-up.
- A standalone `fda organize route <tree>` command.
- Sensitivity / confidentiality branching (no separate restricted SharePoint
  destination).
- Hard size caps.
- Snapshot regression tests on real corpora.
- Folder-structure depth changes to the taxonomy proposer.

## Status & next steps

- **2026-05-12 — FINAL.** All open questions closed. Ready for implementation
  planning via the `writing-plans` skill.
- Implementation order (high level, finalized in the plan): unit-test
  scaffolding → router module + edge-case short-circuits → Sonnet skill →
  CLI wiring (`--no-route`) → JSON + Markdown writers → corpus eyeballing
  pass.
