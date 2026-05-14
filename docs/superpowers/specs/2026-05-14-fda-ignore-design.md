# `.fda-ignore` — Pinning System / Manifest Files at the Target Root

**Date:** 2026-05-14
**Status:** Design (awaiting review)
**Origin:** Organize test follow-ups backlog, item #3 (see `~/Documents/Obsidian Vault/00_Me/02_Side_Hustle/Lion_Chemtech/FDA/FDA Mixed-Corpus Test Findings 2026-05-12.md`)
**Related:** `2026-05-13-extractor-coverage-honesty-design.md` (precedent for quarantine reporting shape)

---

## 1. Problem

During the 2026-05-12 mixed-corpus test, FDA moved `manifest.csv` from the target root into `_Meta/Manifest/`. The evaluator script (`scripts/evaluate_fda_fixture.py`) expects to find `manifest.csv` at the target root and broke. A fragile `rglob` fallback was added afterward to limp through.

The underlying problem generalizes beyond the test fixture:
- Users keep `README.md`, `LICENSE`, `inventory.csv`, project-level configs at the root of folders they hand to FDA. Reorganizing those files is silently destructive.
- FDA already has a similar carve-out for `.git/` (handled in `reader._walk()` and surfaced via `Catalog.git_repos_skipped`). That mechanism needs to generalize.

**Constraint surfaced during brainstorming (2026-05-14):** the user explicitly does not want "ghost folders" — empty or near-empty subfolders left behind because one pinned file lives inside them. Whatever we build must guarantee no ghost folders by construction.

## 2. Goals

1. Allow the user to declare files that FDA must not touch.
2. Cover the recurring evaluator/README/LICENSE cases out of the box, without configuration.
3. Preserve the existing "after FDA runs, the tree is fully organized — no leftover ghost folders" property.
4. Surface pinned files in both the human-readable routing report and the structured log so the user can verify what was protected.

## 3. Non-goals

- Recursive matching (a pattern in `.fda-ignore` only protects files at the target root, not files of the same name nested in subfolders).
- gitignore-style negation (`!pattern`).
- Subdirectory pattern syntax (`subdir/*.csv`).
- Regular expressions.
- Interactive prompts on conflict.
- Preserving OS-junk files (`.DS_Store`, `Thumbs.db`, …) — junk path wins even if the filename is listed in `.fda-ignore` (see §9). (Pinning *does* win over the quarantine path for unextractable files — see §9 again.)

## 4. User-facing behavior

### Default behavior (no `.fda-ignore` file)

A small built-in default list always applies. With no user file in place, FDA leaves these root-level files alone:

```
.fda-ignore       (the file itself, when it exists)
manifest.csv
README.md         (also README.* — README.txt, README.rst, …)
LICENSE           (also LICENSE.*)
```

### With a user-authored `.fda-ignore`

A `.fda-ignore` file at the target root contains one pattern per line. Blank lines and `# comments` are ignored. Patterns are interpreted as **filenames**, not paths, and are matched with stdlib `fnmatch.fnmatch` (supports `*`, `?`, `[abc]`).

**Case-sensitivity:** `fnmatch.fnmatch` normalizes via `os.path.normcase`, which is identity on POSIX (macOS, Linux) and lower-case on Windows. For the macOS/Linux deployment surface, matching is therefore **case-sensitive** — `README.*` will match `README.md` but not `readme.md` or `Readme.MD`. Users wanting to cover variants must list them explicitly (`README.*`, `readme.*`).

User patterns are **additive** with the built-in defaults — there is no way to un-pin a default. (If a default ever becomes wrong for a real user, we revisit the default list rather than adding a negation syntax.)

Example `.fda-ignore`:

```
# files the evaluator script reads
manifest.csv
report-*.txt

# never reorganize these
inventory.csv
NOTES.md
```

### Scope: root-only

A pattern in `.fda-ignore` only matches files **directly inside the target root** (one level deep). A nested file with the same name in a subfolder is organized normally.

Rationale: this is what makes "no ghost folders" automatic. Pinned files always live at the target root; the target root itself is never deleted by the verifier; pinned files anchor in place without keeping any intermediate directory alive.

### What "pinned" means operationally

A pinned file:

- Is not read by the extractor.
- Does not trigger the per-file Haiku summary call.
- Never appears in the plan as a source or destination.
- Stays at its exact original path after the run completes.

### Reporting

Pinned files appear in two places:

1. `routing-report.md` — a Korean-primary section after the quarantine sections:

   ```markdown
   ## 고정됨 — .fda-ignore (2)
   - `manifest.csv`
   - `README.md`
   ```

   We use `고정됨` ("pinned/fixed") rather than `보존됨` ("preserved") because the feature semantics are about pinning a file in place (the user's mental model is "pin this filename"). Keeping the verb consistent across spec, code, and tests prevents drift.

2. Structured log — a `READER_PINNED` event per pinned file plus a `pinned=N` field in `READER_START` / `READER_END`.

The JSON sidecar always emits a `"pinned": []` key (even when empty) for schema stability — same convention as `quarantine` from follow-up #2.

## 5. Architecture

`.fda-ignore` is a **reader-stage filter**, mirroring the existing `git_repos_skipped` mechanism.

```
target/                                    reader.read(target)
├── .fda-ignore       ──┐                    │
├── manifest.csv      ──┤                    ├── _walk(target) → files, git_skipped
├── README.md         ──┤                    │
├── 9a3f.xlsx           │                    ├── _fda_ignore.load_patterns(target)
├── b827.pdf            │                    │       → defaults ∪ user patterns
└── subfolder/          │                    │
    └── inner.csv       │                    ├── partition root files:
                        │                    │     pinned ← matches at depth=0
                        │                    │     to_summarize ← everything else
                        │                    │
                        │                    ├── ThreadPoolExecutor: summarize to_summarize
                        │                    │       (pinned files skipped entirely)
                        │                    │
                        │                    └── return Catalog(
                        │                            entries=..., (no pinned files)
                        │                            git_repos_skipped=...,
                        │                            files_pinned=tuple(pinned),
                        │                          )
```

Downstream stages (classifier, plan_builder, executor, verifier) are unmodified. Because pinned files never appear in `path_by_id`, no operation references them. The verifier's empty-dir cleanup is already root-safe — `verifier.py:104` unconditionally skips `target_resolved` when computing rmdir candidates — and pinned files only ever live at the root (root-only scope), so they cannot create ghost subfolders either. The router gets a new field on `RoutingReport` plus a new section in the markdown writer; nothing else changes downstream.

There is one small change inside `fda/organize/__init__.py`: the `_translate_catalog_for_stage5` helper reconstructs `Catalog` and currently passes `target`, `entries`, and `git_repos_skipped`. It must now also forward `files_pinned` so the field is not silently dropped before the metadata stage. The metadata stage itself iterates `catalog.entries` and does not need pinning awareness — propagating the field is a hygiene fix, not a functional dependency.

## 6. Components and code changes

### 6.1 New module — `fda/organize/_fda_ignore.py`

```python
"""Root-only file-pinning via .fda-ignore.

Reader stage utility: decide which root-level files FDA must leave untouched
during organization. Patterns combine built-in defaults with the user's
optional target/.fda-ignore file (additive only — no negations).
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BUILTIN_DEFAULTS: frozenset[str] = frozenset({
    ".fda-ignore",
    "manifest.csv",
    "README.md",
    "README.*",
    "LICENSE",
    "LICENSE.*",
})


def load_patterns(target: Path) -> tuple[str, ...]:
    """Return defaults ∪ user patterns. Order: defaults first, then user.

    Reads `target/.fda-ignore` if present. Strips `# comments` and blank
    lines. Unreadable file (permission error, decode error) → defaults only,
    with a warning to the logger. Never raises.
    """
    user: list[str] = []
    ignore_file = target / ".fda-ignore"
    if ignore_file.is_file():
        try:
            for raw in ignore_file.read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].strip()
                if line:
                    user.append(line)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(".fda-ignore unreadable at %s: %s", ignore_file, e)
    return tuple(sorted(BUILTIN_DEFAULTS)) + tuple(user)


def is_pinned(filename: str, patterns: tuple[str, ...]) -> bool:
    """True iff `filename` matches any of `patterns` via fnmatch.fnmatch.

    Caller is responsible for restricting to root-depth files.
    """
    return any(fnmatch.fnmatch(filename, pat) for pat in patterns)
```

### 6.2 Model changes — `fda/organize/models.py`

Two dataclasses gain a new field:

```python
@dataclass(frozen=True)
class Catalog:
    target: str
    entries: tuple[CatalogEntry, ...]
    git_repos_skipped: tuple[str, ...]
    files_pinned: tuple[str, ...] = ()   # absolute paths, sorted


@dataclass(frozen=True)
class RoutingReport:
    version: str
    generated_at: str
    target_root: str
    categories: tuple[RoutedCategory, ...]
    quarantine: tuple[QuarantineGroup, ...] = ()
    files_pinned: tuple[str, ...] = ()   # relative paths, sorted
```

Default values `()` keep existing test fixtures and call sites working unchanged. `Catalog.files_pinned` stores absolute paths (consistent with `git_repos_skipped`); `RoutingReport.files_pinned` stores paths relative to `target_root` (consistent with `QuarantineEntry.relative_path`) so the rendered report and JSON sidecar are stable across moves of the target.

### 6.3 Reader change — `fda/organize/reader.py`

In `read()`, the existing flow is:

```python
files, skipped = _walk(target)
junks = [p for p in files if _fs.is_junk_file(p)]
real  = [p for p in files if not _fs.is_junk_file(p)]
```

Pinning is inserted **after junk filtering** so that OS-junk filenames listed in `.fda-ignore` still get DELETEd (the desired behavior in §9):

```python
files, skipped = _walk(target)
junks = [p for p in files if _fs.is_junk_file(p)]
real  = [p for p in files if not _fs.is_junk_file(p)]

# NEW — root-only pin via .fda-ignore
patterns = _fda_ignore.load_patterns(target)
pinned_set = {
    p for p in real
    if p.parent == target and _fda_ignore.is_pinned(p.name, patterns)
}
real = [p for p in real if p not in pinned_set]
pinned = tuple(sorted(str(p) for p in pinned_set))
```

Order rationale:
- Junk filtering first → junk files in `.fda-ignore` (e.g. user typed `.DS_Store`) still hit the DELETE path. Pinning never preserves junk.
- Pinning before extraction → pinned files skip the extractor *and* skip quarantine. Pinning wins over the quarantine path for unextractable files.

Log changes:
- `READER_START` gains `pinned=len(pinned)`
- New event per pinned file: `READER_PINNED path=<abs>`
- `READER_END` gains `pinned=<count>`

Return `Catalog(..., files_pinned=pinned)`.

### 6.4 Router change — `fda/organize/router.py`

`route()` already constructs a `RoutingReport` from `catalog`, `plan`, and routed categories. The construction call (currently at `router.py:453`) gets one new field:

```python
report = RoutingReport(
    version="1.0",
    generated_at=_now_iso(),
    target_root=str(target_path),
    categories=tuple(routed),
    quarantine=quarantine_groups,
    files_pinned=tuple(sorted(
        str(Path(p).relative_to(target_path))
        for p in catalog.files_pinned
    )),
)
```

Two additions to the report writers (both take `RoutingReport`, not `Catalog` directly):

- **Markdown:** `_write_md_report` emits a `## 고정됨 — .fda-ignore (N)` section listing `report.files_pinned`. Section ordering: after the two quarantine sections (`## 건너뜀 — 추출기 없음`, `## 건너뜀 — 추출 실패`), before the trailing newline. The section is omitted entirely when `files_pinned` is empty.
- **JSON:** `_report_to_dict` always emits `"pinned": [...]` (whatever `report.files_pinned` holds — empty list when no pinned files) so consumers can rely on the key existing.

The router does NOT need a new parameter — `files_pinned` flows in via the existing `catalog` argument.

### 6.5 Small change to `fda/organize/__init__.py`

`_translate_catalog_for_stage5` currently reconstructs the catalog without `files_pinned`:

```python
return Catalog(
    target=catalog.target,
    entries=translated_entries,
    git_repos_skipped=catalog.git_repos_skipped,
    files_pinned=catalog.files_pinned,   # NEW — forward the field
)
```

The metadata stage doesn't use `files_pinned`, but propagating the field avoids silent data loss across the stage boundary.

### 6.6 No changes to

- `classifier` — operates on catalog entries; pinned files aren't entries.
- `plan_builder` — operates on `path_by_id`; pinned files aren't in it.
- `executor` — operates on plan operations; pinned files generate none.
- `verifier` — already root-safe; pinned files at the root are never rmdir candidates.
- `fda/metadata/` — iterates `catalog.entries`; pinned files are absent, so they are naturally excluded from metadata classification.

## 7. Data flow summary

```
_walk(target)
    → all_files, git_repos_skipped

load_patterns(target)
    → defaults ∪ user patterns

partition all_files
    → pinned (depth=0, matches a pattern)
    → to_summarize (everything else)

summarize to_summarize (Haiku, one per file)
    → CatalogEntry per file

Catalog(
    entries=summarized,
    git_repos_skipped=...,
    files_pinned=pinned,
)
    → classifier → groupings (pinned never appear)
    → plan_builder → plan (pinned never referenced)
    → executor → outcomes (pinned never moved/deleted)
    → verifier → result (root unconditionally excluded from rmdir;
                        subfolder rmdir unaffected — root-only scope
                        means pinned files never live in a subfolder)
    → _translate_catalog_for_stage5 → forwards files_pinned to metadata stage
    → router → builds RoutingReport with relative-path files_pinned;
               routing-report.md gets a 고정됨 section
```

## 8. Tests

### Unit — `tests/test_organize_fda_ignore.py` (new)

- `load_patterns_returns_defaults_when_no_file`
- `load_patterns_merges_user_patterns_with_defaults`
- `load_patterns_strips_comments_and_blank_lines`
- `load_patterns_falls_back_to_defaults_on_unreadable_file`
- `load_patterns_falls_back_to_defaults_on_decode_error`
- `is_pinned_matches_exact_filename`
- `is_pinned_matches_wildcard`
- `is_pinned_no_match`

### Reader — `tests/test_organize_reader.py` (extend)

- `reader_pins_default_filenames_at_root`
- `reader_pins_user_patterns_at_root`
- `reader_does_not_pin_same_name_in_subfolder`
- `reader_pinned_files_skip_extractor_and_summary` (asserts the mock backend is never called for pinned files)
- `reader_emits_pinned_events_in_log`
- `reader_reader_start_and_end_carry_pinned_count` — assert `pinned=N` appears as a structured field on both `READER_START` and `READER_END` events
- `reader_catalog_files_pinned_is_sorted`
- `reader_junk_filename_in_ignore_is_still_deleted` — `.DS_Store` listed in `.fda-ignore` still goes to the DELETE path; not in `files_pinned`
- `reader_fda_ignore_is_directory_falls_back_to_defaults` — `target/.fda-ignore/` (directory, not file) → `load_patterns()` returns defaults only; no crash
- `reader_fda_ignore_is_symlink_is_followed` — symlinked `.fda-ignore` pointing at a regular file is read and honored
- `reader_symlinked_root_file_not_pinned` — a symlink at the root whose name matches `manifest.csv` is skipped by `_walk` (existing behavior) and therefore never appears in `files_pinned`; document this as a known limitation
- `reader_git_repo_target_yields_empty_pinned` — when target itself contains `.git/`, `_walk` returns early; `files_pinned=()` regardless of `.fda-ignore` contents

### End-to-end — `tests/test_organize_pipeline.py` (extend)

- `organize_leaves_manifest_at_root` — place `manifest.csv` at the root of a tmp tree with other organizable files; run `organize()`; assert the manifest is still at its original absolute path post-run.
- `organize_no_ghost_folders_when_pinned` — pinned root files do not cause any non-root folder to survive empty-dir cleanup that would otherwise be deleted.

### Router — `tests/test_organize_router.py` (extend)

- `router_emits_고정됨_section_when_pinned` — assert the Korean section header (`## 고정됨 — .fda-ignore (N)`) and bullet list match the spec.
- `router_omits_고정됨_section_when_none` — section absent when `files_pinned=()`.
- `router_json_pinned_key_always_present` — JSON sidecar has `"pinned"` key whether populated or empty.
- `router_routing_report_files_pinned_are_relative_paths` — `RoutingReport.files_pinned` holds paths relative to `target_root`, sorted.

### Quarantine interaction

- `pinned_takes_precedence_over_quarantine` — if a pinned filename pattern also matches an unextractable extension, the file is pinned (not quarantined). Pinning happens before extraction; quarantine never sees it.

## 9. Edge cases and invariants

| Case | Behavior |
|------|----------|
| No `.fda-ignore` file | Defaults apply. |
| Empty `.fda-ignore` (only blank/comments) | Defaults only. |
| `.fda-ignore` unreadable | Defaults only, warning logged. Never aborts the run. |
| `.fda-ignore` not UTF-8 | Defaults only, warning logged. |
| `.fda-ignore` is a directory, not a file | `load_patterns()` returns defaults only (the `is_file()` guard short-circuits). The directory's *contents* are still walked by `_walk()` and organized like any other folder — name-collision is the user's mistake. |
| `.fda-ignore` is a symlink to a regular file | Followed (Path.is_file follows symlinks); patterns are read normally. |
| Pattern matches no file | Silent no-op. No warning, no failure. |
| Filename case differs from pattern | `fnmatch` is case-sensitive on POSIX → no match. Users must list variants explicitly. |
| Pinned name lives in subfolder too | Subfolder copy organized normally (root-only). |
| Symlinked file at the root matching a pin pattern | `_walk()` skips root symlinks (existing behavior) → the file never enters `files`, cannot be pinned. Known limitation; documented but not addressed in this design. |
| Junk file name listed in `.fda-ignore` (e.g. `.DS_Store`) | Junk path wins — file is DELETEd. Pinning does not preserve junk. (Pinning happens *after* junk filtering — see §6.3.) |
| Pinned name is also unextractable | Pinning wins — file is not extracted, not quarantined, not moved. (Pinning happens *before* extraction — see §6.3.) |
| Target itself is a git repo | `_walk()` early-returns with `skipped=[target]` and `files=[]`. `load_patterns()` may still read `target/.fda-ignore`, but the pinning loop iterates an empty `real` list → `files_pinned=()`. No harmful interaction. |
| Two patterns match the same file | Single match — file is in `files_pinned` once (set semantics in the partition). |
| `.fda-ignore` itself | Pinned by default (in `BUILTIN_DEFAULTS`). |
| `.fda-ignore` modified mid-run | The file is read once at the start of `reader.read()`. Concurrent edits produce a stale snapshot for the in-flight run. Consistent with the existing single-writer-per-target assumption documented in `_fs.py`. |

## 10. Backwards compatibility

- `Catalog.files_pinned` is a new field with a default value, so existing test fixtures and any caller that constructs `Catalog` positionally without it remain valid.
- Existing runs against trees without `.fda-ignore` and without root-level `manifest.csv` / `README.md` / `LICENSE` see no behavior change.
- Existing runs against trees with a root-level `README.md` will now have it pinned (this is the intended fix). Document in the changelog/memory.

## 11. Open questions

None at design time — all decisions locked through brainstorming Q&A on 2026-05-14. Codex review (2026-05-14) surfaced 11 valid findings, all incorporated into the spec above (contradictions fixed, edge cases added, test list extended, `RoutingReport` field added, `_translate_catalog_for_stage5` forwarding noted, Korean label changed from `보존됨` → `고정됨` for semantic alignment with "pinned").

## 12. Future work (deferred — explicitly out of scope here)

- gitignore-style negation (`!pattern`)
- Subdirectory patterns (`subdir/*.csv`)
- A `--no-defaults` CLI flag
- Per-extension default rules
- Pinning that survives across runs at deeper target roots (would require persistent per-folder state)
