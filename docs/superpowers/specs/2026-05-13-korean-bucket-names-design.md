---
status: DRAFT — 2026-05-13
topic: Korean folder names for Korean content (organize pipeline)
scope: Prompt changes in 3 skills + routing-report.md template; no schema change, no new CLI flag
---

# Korean Folder Names Design

The 2026-05-12 mixed-corpus test confirmed FDA's content-based classifier
correctly identifies Korean document types (visit reports, hiring requests,
material requests, quality defect reports, production reports). But the
emitted folder names were English-only — `Sales/Client-Visit-Reports/`
holding `거래처 방문 보고서` files. FDA is positioned as a Korean-first
product (Lion Chemtech is the lead customer; ~99% of end users speak
Korean as their primary language). The mismatch is the highest-leverage
UX gap surfaced by the test.

This spec describes a mostly-prompt change: three skill prompts switch
to language-aware output, and one small code change updates the
human-readable routing report. When a category contains any Korean
content, the folder path on disk is written in Korean. JSON *field
names* stay English in every artifact (stable schema); JSON *values*
for display-purpose fields (`subpath`, `summary`, `reason`) localize to
match content; identity-purpose values (`category_name`, `type_label`,
`destination`) stay English. The change covers four touchpoints and
adds one new test.

## The rule

For each category produced by the planner:

1. **Detect language from content.** The taxonomy-proposer LLM already
   sees `summary`, `verbatim_head`, and `sections` for every file. It
   judges the language of the category from those signals.
2. **Korean wins on any presence.** If *any* file in the category has
   Korean content (Hangul in `verbatim_head` or `summary`), the entire
   subpath is written in Korean. Only when *all* files are non-Korean
   does the subpath stay English. This is asymmetric on purpose — FDA
   is a Korean-first product, and the user picked this rule during
   brainstorming.
3. **No-content-signal buckets default English.** If every file in a
   bucket has empty `verbatim_head` (extraction failed or extractor
   missing) AND no Korean text in any `summary`, the subpath defaults
   to English. Same rule applies to `summary` and `reason` values for
   files in this state.
4. **`category_name` is always English.** It is a stable identifier
   downstream code joins on. Only `subpath` switches language.
5. **Each category's `subpath` is independent.** A mixed corpus can
   produce parallel parent folders — e.g., `영업/` for Korean Sales
   categories and `Sales/` for English Sales categories — because each
   category emits its own full `subpath`, and the parent segment
   follows the category's own content language.

## Why prompt-only (not schema change, not code-level detection)

A code-level Korean-ratio detector would force an architecture decision
(where does it run, how does it gate the planner, what threshold).
Schema changes (separate `display_subpath`) would touch the planner,
PlanBuilder, executor, and tests. Both are heavier than the underlying
problem: the planner already sees the content; we just haven't told it
to mirror the content language.

The risk with prompt-only is LLM drift — Sonnet 4.6 emitting an English
subpath for a clearly Korean category. Mitigations:

- Few-shot examples in the prompt show one Korean and one English
  category, grounding the model on the expected output shape.
- The 2026-05-12 fixture is a built-in eyeball test — rerun, see the
  diff, validate.
- If drift appears, a follow-up can add a deterministic post-validator
  (the "Plan B" alternative considered during brainstorming).

## Scope

### In scope (changes in this v1)

1. **`fda/organize/skills/taxonomy-proposer/SKILL.md`** — prompt change.
   Add the rule above plus one Korean and one English few-shot example.
2. **`fda/organize/skills/file-summarizer/SKILL.md`** — prompt change.
   Write `summary` in the document's source language (Korean → Korean,
   English → English). `type_label` stays English lowercase-dashes.
3. **`fda/organize/skills/destination-router/SKILL.md`** — prompt
   change. Write `reason` in the language matching the category's
   content. `destination` stays English (`sharepoint` / `s3` / `rdbms`).
4. **`fda/organize/router.py` `_write_md_report`** — use Korean labels
   in the rendered Markdown report (`## 카테고리별 라우팅`, `대상:`,
   `이유:`, `파일 수:`, etc.). The report is corpus-root, audience is
   the Korean operator; mixed-language content inside the report
   sections is acceptable, but the surrounding labels are always
   Korean.

### Out of scope (v1)

- **`scripts/evaluate_fda_fixture.py`** — produces `_FDA_EVAL.md` and
  `evaluation-report.md`. It is a test tool, not a runtime product
  artifact. Separate decision.
- **CLI `--lang` override flag** — not requested; can be added in a
  follow-up if real-world use surfaces the need.
- **Backfill / migration code** — re-runs simply produce a new
  taxonomy. Files in pre-existing English folders will be moved into
  Korean folders on the next run. This is intentional; FDA's job is to
  organize the current corpus, not preserve prior runs' choices.

## What stays English vs. what localizes

JSON *field names* (the schema keys) are always English everywhere.
JSON *values* split into two classes:

**Identity-purpose values — always English.** These are stable IDs
downstream code joins, greps, or matches on:

- `category_name`
- `type_label`
- `destination`
- `path_id`, `extract_status`, and all other internal field values

**Display-purpose values — localize to match content.** These are
human-readable strings whose audience is the Korean operator:

- `subpath` — the folder path written to disk
- `summary` — the per-file purpose sentence
- `reason` — the router's per-category explanation
- Labels and section headers in `routing-report.md` — always Korean
  (Korean-first product), independent of any individual category's
  language

`routing-report.json` consequently contains a *mix* of English
identity values and localized display values — this is the same
"English codes for identity, Korean labels for display" pattern your
memory rule already established for the future metadata layer.

## Examples

### Before

```
target/
├── Sales/
│   ├── Client-Visit-Reports/      ← 거래처 방문 보고서 (Korean .hwp)
│   ├── Orders-Detailed/            ← English company PDFs
│   └── Purchase-Orders/            ← English company PDFs
├── HR/
│   └── Hiring-Requests/            ← 신규 채용 요청서 (Korean .hwp)
├── Manufacturing/
│   ├── Production-Reports/         ← Korean .hwp
│   └── Quality-Defect-Reports/     ← Korean .hwp
├── Procurement/
│   └── Material-Requests/          ← 원자재 구매 요청서 (Korean .hwp)
├── Finance/
│   ├── Business-Rates/             ← English .xlsx
│   └── Expenses-And-Transactions/  ← English
└── Misc/                           ← mixed extractor-failed
```

### After

```
target/
├── 영업/                             ← Korean-content categories
│   └── 거래처방문보고서/
├── Sales/                           ← English-content categories
│   ├── Orders-Detailed/
│   └── Purchase-Orders/
├── 인사/
│   └── 채용요청/
├── 제조/
│   ├── 생산보고서/
│   └── 품질불량보고서/
├── 구매/
│   └── 원자재구매요청/
├── Finance/                         ← all-English bucket
│   ├── Business-Rates/
│   └── Expenses-And-Transactions/
└── Misc/                            ← no language signal (extraction
                                        failed) → English default. A
                                        Misc bucket with any Korean
                                        leftovers would render as
                                        `기타/`.
```

Note the **parallel `영업/` and `Sales/` parents.** Each category emits
its own full subpath; Korean-content categories produce Korean parent
segments, English-content categories produce English ones. The
proposer is not asked to unify domains across languages.

### One category as JSON (stable English `category_name`, Korean `subpath`)

```json
{
  "category_name": "Client-Visit-Reports",
  "subpath": "영업/거래처방문보고서",
  "description": "Sales-team field reports on client visits",
  "criteria": "..."
}
```

### routing-report.md (after)

```markdown
# 라우팅 보고서

생성 시각: 2026-05-13 14:22 KST
총 카테고리: 10 · 총 파일: 100

## 카테고리별 라우팅

### 영업/거래처방문보고서
- 대상: sharepoint
- 파일 수: 2
- 이유: 영업팀이 SharePoint에서 자주 조회하는 거래처 방문 보고서

### Finance/Business-Rates
- 대상: sharepoint
- 파일 수: 2
- 이유: Finance team reference rates accessed via M365
```

Note the mixed-language paragraph: Korean labels wrap whichever language
the LLM emitted per category. This is intentional.

## Edge cases

- **Korean filenames, English content.** `verbatim_head` is the source
  of truth; the filename is not a language signal. → English subpath.
- **USER_INSTRUCTIONS written in Korean, content in English.** The
  proposer prompt already honors USER_INSTRUCTIONS for categorization
  direction. Language is decided from *content*, not from the
  instructions language, so a Korean instruction over English files
  still produces English subpaths.
- **Bucket where every file failed extraction.** No language signal
  available → English subpath default. These are typically `Misc` or
  similar fallback buckets and don't have real content to mirror.
- **Re-running FDA on a previously organized corpus.** No special
  handling. The planner regenerates the taxonomy from scratch; files
  may move from English folders to Korean folders. Acceptable.

## Tests

1. **`tests/test_routing_report.py` (new) — `_write_md_report`
   snapshot.** Construct a `RoutingReport` containing one Korean
   category (`영업/거래처방문보고서`, Korean `reason`) and one English
   category (`Finance/Business-Rates`, English `reason`). Assert:
   - The rendered Markdown contains `## 카테고리별 라우팅`.
   - The labels `대상:`, `이유:`, `파일 수:` appear in every category
     block.
   - Both subpaths appear verbatim as section titles.
2. **Existing test suite.** No regressions expected. Korean subpaths
   are strings; the executor, planner, and verifier are
   language-agnostic. The pre-commit hook will run the full suite.
3. **Manual eyeball test.** Rerun on the 2026-05-12 fixture
   (`/private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12/`).
   Expected diff: Korean-content buckets switch to Korean subpaths;
   English buckets unchanged; `Misc` unchanged; `routing-report.md`
   shows Korean labels around mixed-language category blocks.

## Implementation order

1. Update `file-summarizer/SKILL.md`. (Smallest, most localized
   change; produces the Korean summaries that feed the proposer.)
2. Update `taxonomy-proposer/SKILL.md` with the rule and few-shot
   examples.
3. Update `destination-router/SKILL.md`.
4. Update `fda/organize/router.py::_write_md_report` and add the new
   snapshot test.
5. Manual eyeball on the 2026-05-12 fixture.

Each step is independently safe to commit. Steps 1–3 are prompt-only.
Step 4 is the only code change.

## Follow-ups (not in this v1)

- Localize `scripts/evaluate_fda_fixture.py` output if the test
  evaluator is intended to be Korean-readable.
- Optional CLI `--lang` flag for English overrides (e.g., shipping
  English-named output to an English-speaking integration partner).
- If LLM drift is observed in real-corpus runs, add a deterministic
  post-validator that checks each category's Korean character ratio
  against its subpath language.

## Related

- Memory: `project_fda_korean_first` (the product constraint this fix
  serves)
- Memory: `project_organize_test_followups` (this is item #1 of 7)
- Findings note: `~/Documents/Obsidian Vault/.../FDA Mixed-Corpus Test
  Findings 2026-05-12.md`
- Test fixture: `/private/tmp/fda-test-sets/randomized-mixed-corpus-
  2026-05-12/`
