# Korean Folder Names Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FDA's organize pipeline write Korean folder paths, summaries, and routing reasons for Korean content, while keeping English identity fields (`category_name`, `type_label`, `destination`) stable across languages.

**Architecture:** Mostly a prompt change in three skills (`file-summarizer`, `taxonomy-proposer`, `destination-router`) so the LLM emits language-aware display fields. One small code change updates `_write_md_report` in `fda/organize/router.py` to render the routing report's surrounding labels in Korean. JSON field names stay English everywhere; JSON values split into identity (English) vs display (localized). Existing data classes, executor, planner, and verifier are language-agnostic and do not change.

**Tech Stack:** Python 3.12, pytest, claude-sonnet-4-6 and claude-haiku-4-5 (via skill prompts), markdown.

**Spec:** `docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md`

**Python runtime:** `.venv/bin/python` from the repo root (the path in CLAUDE.md is wrong on this machine — see memory `feedback_fda_python_path`).

**Out of scope (do NOT touch):** `scripts/evaluate_fda_fixture.py`, any CLI `--lang` override, any backfill/migration code, the metadata layer spec, and any of items #2–#7 in `project_organize_test_followups`.

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `fda/organize/skills/file-summarizer/SKILL.md` | Modify (prompt only) | Tell the summarizer LLM to write `summary` in the document's source language. |
| `fda/organize/skills/taxonomy-proposer/SKILL.md` | Modify (prompt only) | Tell the proposer LLM the "Korean wins on any presence" rule and parallel-parent behavior; add one Korean and one English few-shot example. |
| `fda/organize/skills/destination-router/SKILL.md` | Modify (prompt only) | Tell the router LLM to write `reason` in the language matching the category's content. |
| `fda/organize/router.py` (`_write_md_report`, around lines 446–491) | Modify | Render `routing-report.md` labels in Korean. |
| `tests/test_organize_router.py` (`TestReportWriters`, around lines 467–531) | Modify | Update the existing English-label assertions in `test_markdown_writer_emits_per_destination_summary` to expect Korean labels. |
| `tests/test_routing_report.py` | Create | New snapshot test that exercises a mixed Korean/English `RoutingReport` (per the spec). Self-contained — does not depend on helpers in `test_organize_router.py`. |

No schema change. No new CLI flag.

---

## Task 1: Update `file-summarizer/SKILL.md` to emit summaries in source language

**Files:**
- Modify: `fda/organize/skills/file-summarizer/SKILL.md` (the `Rules:` block, around lines 24–30)

The `summary` field is currently described as "one sentence about what this file is and what it's likely used for" with no language guidance. Add an explicit rule that the summary must be written in the document's source language, and update one of the examples to a Korean one so the LLM is grounded.

- [ ] **Step 1: Edit `fda/organize/skills/file-summarizer/SKILL.md` — add the language rule**

Locate the `Rules:` block (immediately after the `Output:` JSON block). It currently reads:

```
Rules:
- Output ONLY the JSON object. No prose, no markdown fences.
- `type_label` is a short tag like `invoice`, `meeting-notes`, `python-source`, `image`, `archive`. Lowercase-with-dashes.
- `summary` is one sentence. Do not exceed 200 characters. Do not include the file path.
- You DO NOT decide whether the file is junk. Junk handling happens elsewhere.
- If the file is impossible to classify (corrupt, empty, opaque), set `type_label` to `unknown` and write a one-sentence summary saying so.
```

Insert a new bullet **immediately after** the `summary` bullet so the rules read:

```
Rules:
- Output ONLY the JSON object. No prose, no markdown fences.
- `type_label` is a short tag like `invoice`, `meeting-notes`, `python-source`, `image`, `archive`. Lowercase-with-dashes.
- `summary` is one sentence. Do not exceed 200 characters. Do not include the file path.
- Write `summary` in the document's source language. If the extracted text is Korean, write the summary in Korean. If English, write it in English. `type_label` stays English lowercase-with-dashes regardless of content language. If no text is available (extraction failed, no extractor, opaque binary), write the summary in English.
- You DO NOT decide whether the file is junk. Junk handling happens elsewhere.
- If the file is impossible to classify (corrupt, empty, opaque), set `type_label` to `unknown` and write a one-sentence summary saying so.
```

- [ ] **Step 2: Replace one English example with a Korean example**

Locate the `Examples:` block at the bottom of the file. It currently lists three English examples. Replace the second example (`python-source`) with a Korean one so the LLM sees a non-English summary form. The block should read:

```
Examples:
- `{"type_label": "invoice", "summary": "Vendor invoice for May services with line items and totals."}`
- `{"type_label": "visit-report", "summary": "거래처 방문 결과를 기록한 영업팀 보고서로, 일자·담당자·논의 내용이 포함되어 있다."}`
- `{"type_label": "unknown", "summary": "Binary file of unknown format; classify by filename or extension."}`
```

(`python-source` removed; the technical-label-grounding rules already cover it via the "Technical file-type labels … are not affected by this rule" sentence elsewhere in the file.)

- [ ] **Step 3: Run the test suite to confirm no regression**

```bash
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. (Skill-prompt edits don't affect Python-level tests — this is purely a sanity check that nothing else is broken at the moment.)

- [ ] **Step 4: Commit**

```bash
git add fda/organize/skills/file-summarizer/SKILL.md
git commit -m "organize(file-summarizer): summarize in source language

Korean documents now get Korean summaries; English stays English.
type_label is unchanged (English identity field). Falls back to
English when no text is available. Spec:
docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md"
```

---

## Task 2: Update `taxonomy-proposer/SKILL.md` with the language rule and few-shot examples

**Files:**
- Modify: `fda/organize/skills/taxonomy-proposer/SKILL.md` (the "How to use these signals" block and the `Rules:` block)

The proposer LLM emits `category_name`, `subpath`, `description`, and `criteria` per category. The spec dictates:

- `category_name` stays English (stable identifier).
- `description` and `criteria` stay English (the assigner reads them and was prompted in English).
- `subpath` switches language based on category content. **Korean wins on any presence** — any Korean content in a file's `verbatim_head` or `summary` flips the category's subpath to Korean. A category whose every file has empty `verbatim_head` AND no Korean text in `summary` defaults to English.
- Categories are independent — parallel `영업/` and `Sales/` parents are allowed.

- [ ] **Step 1: Edit `fda/organize/skills/taxonomy-proposer/SKILL.md` — add a "Language of subpath" subsection**

Locate the block beginning `How to use these signals when proposing categories:` (around line 24). After the last existing bullet in that block (the one about encoding structural distinctions in `criteria`, ending around line 62), insert a new sub-block. The inserted text must read **exactly**:

```
- **Language of `subpath` — Korean wins on any presence.** Judge each
  category's content language from the `verbatim_head` and `summary` of
  the files that belong to it. If *any* file in the category contains
  Korean (Hangul characters in `verbatim_head` or `summary`), write the
  entire `subpath` in Korean — including the parent segment. Only when
  *every* file in the category is non-Korean does the subpath stay
  English. If every file in the category has empty `verbatim_head` AND
  no Korean text in any `summary` (extraction failed across the
  bucket), the subpath defaults to English. This rule is asymmetric on
  purpose: FDA's primary user audience speaks Korean.

- **`category_name`, `description`, and `criteria` stay English.**
  `category_name` is a stable identifier the downstream assigner joins
  on. `description` and `criteria` are read by the assigner LLM, which
  is prompted in English. Only `subpath` switches language.

- **Categories are independent — parallel parents are fine.** Each
  category emits its own full `subpath`. A mixed corpus may produce
  both `영업/거래처방문보고서` (Korean-content category) and
  `Sales/Purchase-Orders` (English-content category) — that is
  expected. Do not try to unify domains across languages under a single
  parent.
```

- [ ] **Step 2: Add a few-shot examples block**

Locate the `Rules:` block at the end of the file (the bulleted list ending with the `Honor USER_INSTRUCTIONS` rule, around line 97). After that block, insert a new `Examples:` section. The inserted text must read **exactly**:

```
Examples:

A Korean-content category (every file's `verbatim_head` or `summary` contains Hangul):

```json
{
  "category_name": "Client-Visit-Reports",
  "subpath": "영업/거래처방문보고서",
  "description": "Sales-team field reports on client visits.",
  "criteria": "Korean visit-report documents whose verbatim_head or summary identifies them as 거래처 방문 보고서 (sales visit log)."
}
```

An English-content category (no Korean in any file):

```json
{
  "category_name": "Purchase-Orders",
  "subpath": "Sales/Purchase-Orders",
  "description": "Outbound purchase orders sent to suppliers.",
  "criteria": "English purchase-order documents whose verbatim_head begins with the literal phrase \"Purchase Orders\" and includes Order ID, supplier, and line-item sections."
}
```

Note that `category_name`, `description`, and `criteria` are English in both. Only `subpath` switches language.

```

Important formatting note: the inner code fences must use triple backticks. When inserting into the SKILL.md file via the `Edit` tool, write them as triple-backticks in the new_string exactly as shown above. (SKILL.md is plain markdown — the LLM will see the rendered prompt with literal backticks; that's intentional.)

- [ ] **Step 3: Run the test suite**

```bash
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add fda/organize/skills/taxonomy-proposer/SKILL.md
git commit -m "organize(taxonomy-proposer): language-aware subpath

Korean wins on any presence: any Korean content in verbatim_head or
summary flips the category subpath to Korean. category_name,
description, and criteria stay English. Parallel parents (영업/ and
Sales/) are allowed. Spec:
docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md"
```

---

## Task 3: Update `destination-router/SKILL.md` so `reason` matches category content language

**Files:**
- Modify: `fda/organize/skills/destination-router/SKILL.md` (the `Rules:` block, around lines 73–79)

The router LLM emits `destination` (English code: `sharepoint` / `s3` / `rdbms`) and `reason` (free prose). The spec requires `reason` to follow the category's content language.

- [ ] **Step 1: Edit `fda/organize/skills/destination-router/SKILL.md` — add the language rule**

Locate the `Rules:` block immediately after the `OUTPUT FORMAT` JSON example. It currently reads:

```
Rules:
- `destination` MUST be exactly one of `"sharepoint"`, `"s3"`, `"rdbms"`.
- `misfits` is an array; emit `[]` when there are no misfits.
- Each misfit's `path_id` MUST appear in the input `sample`.
- Each misfit's `suggested_destination` MUST be different from the
  category's chosen `destination`.
- Output ONLY the JSON object.
```

Insert a new bullet **before** the `Output ONLY the JSON object.` line so the block reads:

```
Rules:
- `destination` MUST be exactly one of `"sharepoint"`, `"s3"`, `"rdbms"`.
- `misfits` is an array; emit `[]` when there are no misfits.
- Each misfit's `path_id` MUST appear in the input `sample`.
- Each misfit's `suggested_destination` MUST be different from the
  category's chosen `destination`.
- Write `reason` (both the category's and each misfit's) in the
  language matching the category's content. Judge content language
  from the sample's `verbatim_head` and `summary` fields: if any file
  in the sample contains Korean (Hangul), write the reason in Korean;
  otherwise write it in English. `destination` and
  `suggested_destination` stay English codes regardless.
- Output ONLY the JSON object.
```

- [ ] **Step 2: Run the test suite**

```bash
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add fda/organize/skills/destination-router/SKILL.md
git commit -m "organize(destination-router): write reason in content language

reason follows the category's content language (Korean wins on any
Hangul presence in the sample). destination and
suggested_destination stay English codes. Spec:
docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md"
```

---

## Task 4: Render `routing-report.md` labels in Korean (code + tests)

**Files:**
- Modify: `fda/organize/router.py` — `_write_md_report` (around lines 446–491) and the module-level `_PRETTY_DESTINATION` dict (around lines 398–402).
- Modify: `tests/test_organize_router.py` — `TestReportWriters.test_markdown_writer_emits_per_destination_summary` (around lines 519–531).
- Create: `tests/test_routing_report.py` — new snapshot test for the Korean-labels + mixed-categories rendering, per the spec's test plan section.

The report is corpus-root and its audience is the Korean operator, so the surrounding labels are always Korean regardless of any individual category's content language. Category-level `name`, `subpath`, and `reason` are passed through verbatim — they may be Korean, English, or mixed.

### Step-by-step

- [ ] **Step 1: Write a failing test in a NEW file `tests/test_routing_report.py`**

Create the file at `tests/test_routing_report.py` with the contents below verbatim. It is self-contained — it builds a minimal `CatalogEntry` inline rather than depending on the `_entry` helper that lives in `test_organize_router.py`, so it can be run independently.

```python
# tests/test_routing_report.py
"""Snapshot test for _write_md_report (Korean labels, mixed-language categories).

Spec: docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md
"""
from __future__ import annotations


def test_markdown_writer_renders_korean_labels_and_mixed_categories(tmp_path):
    from fda.organize.models import (
        CatalogEntry, RoutedCategory, RoutingReport,
    )
    from fda.organize.router import _aggregate_signals, _write_md_report

    entry = CatalogEntry(
        path_id="f000",
        path=str(tmp_path / "영업/거래처방문보고서/v.hwp"),
        ext=".hwp",
        size_bytes=1000,
        summary="거래처 방문 결과를 기록한 영업팀 보고서.",
        type_label="visit-report",
        is_junk=False,
        summary_failed=False,
        extract_status="ok",
        sections=(),
    )
    sig = _aggregate_signals([entry])
    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T14:22:00Z",
        target_root=str(tmp_path),
        categories=(
            RoutedCategory(
                name="Client-Visit-Reports",
                subpath="영업/거래처방문보고서",
                destination="sharepoint",
                reason="영업팀이 SharePoint에서 자주 조회하는 거래처 방문 보고서.",
                low_confidence=False, signals=sig, misfits=(),
            ),
            RoutedCategory(
                name="Business-Rates",
                subpath="Finance/Business-Rates",
                destination="sharepoint",
                reason="Finance team reference rates accessed via M365.",
                low_confidence=False, signals=sig, misfits=(),
            ),
        ),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")

    assert "# 라우팅 보고서" in md
    assert "## 카테고리별 라우팅" in md
    assert "대상:" in md
    assert "파일 수:" in md
    assert "이유:" in md
    assert "영업/거래처방문보고서" in md
    assert "영업팀이 SharePoint에서 자주 조회하는 거래처 방문 보고서." in md
    assert "Finance/Business-Rates" in md
    assert "Finance team reference rates accessed via M365." in md
```

- [ ] **Step 2: Run the new test and confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_routing_report.py -v
```

Expected: FAIL — the rendered markdown contains English labels (`# Routing Report`, `**Destination:**`, etc.), not Korean ones.

- [ ] **Step 3: Update `_write_md_report` to render Korean labels**

In `fda/organize/router.py`, replace the body of `_write_md_report` (currently lines 446–491). The new implementation must read **exactly** as below. Pay close attention: the per-destination summary block is removed (replaced by a single header line showing total categories and total files), `## Category: <name>` becomes a single-section `## 카테고리별 라우팅` plus per-category `### <subpath>` blocks, the `**Destination:**` / `**Signals:**` blocks become bullet-list `- 대상:` / `- 파일 수:` / `- 이유:` lines, and the `### Misfits` sub-block becomes `### 불일치 파일` with bullet-list Korean labels.

```python
def _write_md_report(report: RoutingReport, path: Path) -> None:
    total_files = sum(c.signals.file_count for c in report.categories)
    lines: list[str] = []
    lines.append("# 라우팅 보고서")
    lines.append("")
    lines.append(f"생성 시각: {report.generated_at}")
    lines.append(f"대상 루트: {report.target_root}")
    lines.append(
        f"총 카테고리: {len(report.categories)} · 총 파일: {total_files}"
    )
    lines.append("")
    lines.append("## 카테고리별 라우팅")
    lines.append("")
    for c in report.categories:
        lines.append(f"### {c.subpath}")
        lines.append("")
        suffix = " (낮은 신뢰도)" if c.low_confidence else ""
        lines.append(f"- 대상: {c.destination}{suffix}")
        lines.append(f"- 파일 수: {c.signals.file_count}")
        if c.reason:
            lines.append(f"- 이유: {c.reason}")
        ext_str = ", ".join(
            f"{ext} ({n})" for ext, n in c.signals.extension_distribution
        ) or "(없음)"
        lines.append(f"- 확장자 분포: {ext_str}")
        lines.append(f"- 총 용량(바이트): {c.signals.total_size_bytes:,}")
        lines.append(
            f"- 표 형식 일관성: {c.signals.tabular_schema_consistent}"
        )
        lines.append(
            f"- 전체 추출 실패: {c.signals.all_extraction_failed}"
        )
        lines.append("")
        if c.misfits:
            lines.append("### 불일치 파일")
            lines.append("")
            for m in c.misfits:
                lines.append(
                    f"- `{m.relative_path}` → "
                    f"{m.suggested_destination} — {m.reason}"
                )
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

One consequence to apply alongside this replacement:

1. The module-level `_PRETTY_DESTINATION` dict (around lines 398–402) is no longer used by `_write_md_report` (we emit the raw English code `sharepoint`/`s3`/`rdbms` so it stays identity-stable per the spec). Verify it has no other callers with `grep -n _PRETTY_DESTINATION fda/organize/router.py`. If no other usages exist, delete the dict. If it is used elsewhere, leave it.

Do NOT remove the `from collections import Counter` import — it is still used by `_aggregate_signals` at line 53 (`ext_counter: Counter[str] = Counter(e.ext for e in entries)`).

- [ ] **Step 4: Run the new test and confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_organize_router.py::TestReportWriters::test_markdown_writer_renders_korean_labels_and_mixed_categories -v
```

Expected: PASS.

- [ ] **Step 5: Update the existing English-label test in `tests/test_organize_router.py`**

The existing `test_markdown_writer_emits_per_destination_summary` method (around lines 519–531) asserts English labels like `# Routing Report`, `SharePoint: 1`, `low confidence`. Those labels no longer exist. Replace the entire method — both signature and body — with the version below. The method is renamed (the old "per_destination_summary" name no longer matches the new layout) and its assertions now match the Korean labels.

Open `tests/test_organize_router.py`. Locate the method definition starting with `def test_markdown_writer_emits_per_destination_summary(self, tmp_path):` (around line 519). Replace everything from that `def` line through the closing `assert "low confidence" in md.lower()` line (around line 531, inclusive) with this single block (preserve the 4-space indentation — it's a method on `TestReportWriters`):

```python
    def test_markdown_writer_emits_korean_labels_with_misfit_and_low_confidence(self, tmp_path):
        from fda.organize.router import _write_md_report
        report = self._sample_report(tmp_path)
        out = tmp_path / "routing-report.md"
        _write_md_report(report, out)
        md = out.read_text(encoding="utf-8")
        assert "# 라우팅 보고서" in md
        assert "## 카테고리별 라우팅" in md
        assert "대상:" in md
        assert "파일 수:" in md
        assert "총 카테고리: 2" in md
        assert "Finance/Invoices" in md
        assert "sales.csv" in md
        assert "낮은 신뢰도" in md
```

- [ ] **Step 6: Run the full router test class plus the new snapshot file**

```bash
.venv/bin/python -m pytest tests/test_organize_router.py::TestReportWriters tests/test_routing_report.py -v
```

Expected: all four methods pass (`test_json_writer_emits_expected_shape`, `test_markdown_writer_emits_korean_labels_with_misfit_and_low_confidence`, `test_route_writes_both_sidecars` in `TestReportWriters`, plus `test_markdown_writer_renders_korean_labels_and_mixed_categories` in the new file).

- [ ] **Step 7: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: every test passes. Particularly watch for unexpected regressions in `test_organize_pipeline.py` and `test_organize_router.py::TestRoute*`.

- [ ] **Step 8: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): Korean labels in routing-report.md

Report header and per-category labels (대상, 파일 수, 이유, 확장자 분포,
총 용량, 표 형식 일관성, 전체 추출 실패, 낮은 신뢰도, 불일치 파일)
render in Korean. Destination code (sharepoint/s3/rdbms) stays English
for identity stability. Category subpath and reason are passed through
verbatim so mixed Korean/English corpora render correctly. Spec:
docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md"
```

---

## Task 5: Manual eyeball test on the 2026-05-12 fixture

**Files:** none — this is a runtime verification.

The four code/prompt changes above are now committed. Rerun the organize pipeline on the 2026-05-12 mixed-corpus fixture and inspect the output by eye. This is the test the spec explicitly relies on; it is non-automated on purpose because the LLM outputs are not deterministic.

- [ ] **Step 1: Copy the fixture to a throwaway scratch directory**

`scripts/diag_organize.py` organizes a target directory **in place** — it does not accept a separate source. To preserve the original fixture, copy it first:

```bash
rm -rf /private/tmp/fda-eyeball-2026-05-13
cp -R /private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12 /private/tmp/fda-eyeball-2026-05-13
ls /private/tmp/fda-eyeball-2026-05-13 | head
```

If `/private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12` is missing, stop and tell the user — the eyeball test cannot proceed without it. Do not regenerate or alter the fixture.

- [ ] **Step 2: Run the organize pipeline (with router) against the scratch copy**

From the repo root, run the diagnostic organize script with `--apply` (the cloud router runs by default when `route=True`, which is the default in `fda.organize.organize`):

```bash
.venv/bin/python scripts/diag_organize.py /private/tmp/fda-eyeball-2026-05-13 --apply
```

The script writes a timestamped log to `/tmp/fda-organize-diag-<timestamp>.log` and prints its path on completion. If the script raises or the log shows `ROUTER` errors, stop and surface them rather than declaring the eyeball test green.

- [ ] **Step 3: Inspect the resulting folder tree**

```bash
find /private/tmp/fda-eyeball-2026-05-13 -maxdepth 3 -type d | sort
```

Expected (qualitative — the LLM is not deterministic, so exact names may vary):

- At least one Korean parent folder exists (e.g. `영업/`, `인사/`, `제조/`, `구매/`).
- English parents still exist for English-only buckets (e.g. `Finance/`).
- `Misc/` (or its Korean equivalent `기타/` if any Korean leftovers landed in it) is present as the fallback bucket.
- No folder has been duplicated under both Korean and English parents for the same category — each category emits its own subpath, but a single category appears under one parent only.

- [ ] **Step 4: Inspect `routing-report.md`**

```bash
.venv/bin/python -c "from pathlib import Path; print(Path('/private/tmp/fda-eyeball-2026-05-13/routing-report.md').read_text(encoding='utf-8'))" | head -80
```

Expected:

- Header reads `# 라우팅 보고서`.
- Section header reads `## 카테고리별 라우팅`.
- Each category block uses Korean bullet labels (`- 대상:`, `- 파일 수:`, `- 이유:`).
- `대상:` values are English codes (`sharepoint`, `s3`, `rdbms`).
- `이유:` text is Korean for Korean-content categories, English for English-content categories.

- [ ] **Step 5: Inspect `routing-report.json` for the identity/display split**

```bash
.venv/bin/python -c "
import json
from pathlib import Path
data = json.loads(Path('/private/tmp/fda-eyeball-2026-05-13/routing-report.json').read_text(encoding='utf-8'))
for c in data['categories'][:3]:
    print({'name': c['name'], 'subpath': c['subpath'], 'destination': c['destination'], 'reason': c['reason'][:60]})
"
```

Expected:

- `name` (the `category_name`) is English everywhere.
- `destination` is one of `sharepoint`, `s3`, `rdbms`.
- For Korean-content categories: `subpath` is Korean; `reason` is Korean.
- For English-content categories: `subpath` is English; `reason` is English.

- [ ] **Step 6: Write a short eyeball-result note**

If anything in steps 3–5 fails or looks wrong, stop and surface it to the user rather than declaring success. Otherwise, write a 5–10 line note to `~/Documents/Obsidian Vault/00_Me/02_Side_Hustle/Lion_Chemtech/FDA/FDA Korean Bucket Names Eyeball 2026-05-13.md` summarizing: which Korean parents appeared, which English parents remained, where any unexpected English subpath landed under a Korean-content category (LLM drift indicator), and whether `routing-report.md` looks right. This artifact backs the follow-up decision in spec section "Follow-ups" about whether a post-validator is needed.

- [ ] **Step 7: Update the memory backlog**

Mark item #1 in `~/.claude/projects/-Users-hogyeongkim-Desktop-Projects-FDA-FDA/memory/project_organize_test_followups.md` as done (date 2026-05-13), preserve items #2–#7 verbatim. Update the corresponding line in `MEMORY.md` so it reads something like `#1 (Korean bucket names) shipped 2026-05-13; #2–#7 pending`.

- [ ] **Step 8: Do NOT commit the eyeball note or any fixture output**

The note lives in the Obsidian vault (outside the repo). The fixture output under `/private/tmp/` is throwaway. Nothing in this task creates files inside the repo. Skip the `git add` / `git commit` step for Task 5.

---

## Verification summary

After Task 4, the automated suite must be all-green:

```bash
.venv/bin/python -m pytest tests/ -x -q --tb=short
```

After Task 5, the qualitative eyeball must show:

- Korean folder names for Korean-content categories.
- English folder names for English-content categories.
- `routing-report.md` rendered with Korean surrounding labels.
- `routing-report.json` with English `name` / `destination` and language-matched `subpath` / `reason`.

If any of these fails, do not declare the feature shipped — surface the gap to the user.
