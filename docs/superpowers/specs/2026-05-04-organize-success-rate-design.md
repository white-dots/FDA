# Organize Success Rate — Design

**Date:** 2026-05-04
**Status:** Phase A landed 2026-05-05; Phase B pending separate plan
**Owner:** hogyeongkim
**Related notes:** Obsidian `FDA/Future Plan - Organization Success Rate`, `docs/local_metadata_layer_plan.md`

## Problem

`LocalWorkerAgent.organize_files()` produces good groupings but executes them inconsistently. The same folder, run twice, yields different outcomes: some moves complete, others don't, and old source folders are left behind. Root cause: the same Claude tool-use loop is responsible for *deciding* the taxonomy and *calling* `move_file`. When the iteration budget runs thin or the model gets distracted re-inspecting a file, it stops short of executing every move it had in mind. There is no separation between deciding and doing, so non-determinism shows up directly in the filesystem.

## Goals

1. Same target folder + same instructions + same filesystem state produces the same set of file moves on every run, given a stable plan.
2. Per-move rationale is captured in the structured plan and surfaced in the journal entry.
3. Source folders that become empty as a result of the plan are cleaned up.
4. `LocalWorkerAgent.organize_files()` keeps its current call signature and dict-shaped return; existing callers (Telegram, web, orchestrator) work without modification, with `reason` added to each move dict (additive — unknown keys are ignored by current consumers).
5. Same or better Claude API cost per run on the happy path.

## Non-goals

- **Plan persistence** in SQLite. Plans live in memory for the duration of an `organize()` call (or briefly across `organize_files_preview()` → `organize_files_apply()` within the same Python process). Cross-process / cross-message persistence belongs in a follow-on spec.
- **Undo/rollback** of an executed plan.
- **UI changes** in this spec — no Telegram `--preview` flag, no web-UI toggle. Preview is API-only in Phase A; UI integration is deferred to Phase B.
- Re-classification of files in already-organized folders.
- Anything inside git repositories (current safety stays — and tightens; see below).

## Phasing

This spec defines the full target design but the implementation is delivered in two plans.

**Phase A (this spec's primary deliverable):** planner + deterministic executor (no rescue) + verifier + back-compat wrapper for `LocalWorkerAgent.organize_files()` + API-level `organize_files_preview()` / `organize_files_apply()`. No UI changes. Ships the determinism win immediately. Failed operations stay `failed`; idempotent re-run is the recovery story.

**Phase B (follow-on plan):** Claude rescue call for failed operations + Telegram `--preview` confirmation flow + web UI preview panel. Requires cross-message plan storage (out of scope for Phase A) and is informed by real Phase-A rescue-rate measurements.

The user selected the hybrid execution path ("deterministic + Claude rescue") during brainstorming. Phase A ships the deterministic half; Phase B adds rescue once we know how often it actually fires in production.

## Architecture

```
target_path, instructions, preview?
            │
            ▼
       ┌─────────┐
       │ planner │   Claude with READ-ONLY tools.
       │         │   Emits Plan (operations + reasons).
       └─────────┘
            │
            │  if preview=True → return Plan, stop.
            ▼
       ┌──────────┐
       │ executor │  Phase A: deterministic apply only.
       │          │  Phase B: failures → ONE Claude rescue call.
       └──────────┘
            │
            ▼
       ┌──────────┐
       │ verifier │  Re-list target, diff against Plan.
       │          │  Records discrepancies + opportunistic empty-dir cleanup.
       └──────────┘
            │
            ▼
       PlanResult  →  back-compat dict for organize_files() callers
```

### Invariants

- **Planner has zero side effects.** Tools are `list_directory`, `get_file_info`, `read_file`, plus the terminal `submit_plan(operations, grouping_summary)` tool. **No `run_command`** — it executes arbitrary shell and is not safely read-only.
- **Executor doesn't make taxonomy decisions.** It applies what the plan says, in a fixed safe order: `CREATE_DIR` → `MOVE` → `DELETE`. Same plan + same filesystem = same outcome.
- **Rescue (Phase B only) is bounded.** At most one Claude call per organize run, capped at 5 iterations, with a tiny tool surface. Operation indexes refer to the original `Plan.operations` list (stable across the executor's reordering).
- **Verifier is pure observation.** No taxonomy decisions; checks reality matches the plan, surfaces discrepancies, cleans dirs the plan itself emptied.
- **Single safety source.** All path validation, git-repo detection, and junk-only delete rules live in `_fs.py`. Both planner-time validation (inside `submit_plan`) and executor-time enforcement use the same primitives.
- **No shared mutable state in the package.** All per-run state is passed through function arguments. Removes today's latent concurrency bug where a second `organize_files()` call on the shared `LocalWorkerAgent` would clobber `self._current_project`, `self._organize_moves`, etc.
- **Back-compat surface.** `LocalWorkerAgent.organize_files()` keeps its signature and dict shape. Each `moves` dict gains a `reason` field (additive). The dict gains optional `discrepancies` and `leftover_empty_dirs` arrays.

## Deliberate semantic tightening

Two safety rules are intentionally stricter than today; called out so we don't claim "same semantics":

1. **Moves into git repos are blocked.** Today only the source side is checked (`_orgtool_move_file` only blocks moves *from* inside a repo). The new `_fs.py` blocks both directions: any operation whose source OR destination resolves inside a `.git`-bearing tree is rejected at planner-validation time and at executor time.
2. **Planner validation is all-or-nothing.** A `submit_plan` call with any invalid operation is rejected entirely; the planner gets a tool-result error listing every offending op and can fix and resubmit. Only a fully-valid `submit_plan` signals the loop to terminate. No partial acceptance.

## Module layout

```
fda/organize/
├── __init__.py     # public API: organize(), apply_plan()
├── models.py       # Plan, Operation, OperationKind, OperationOutcome, PlanResult
├── prompts.py      # PLANNER_SYSTEM_PROMPT (Phase A); RESCUE_SYSTEM_PROMPT (Phase B)
├── _fs.py          # path validation, git-repo detection (both directions),
│                   #   junk-file rules, deterministic apply primitives
├── planner.py      # build_plan(target, instructions, *, backend) -> Plan
├── executor.py     # execute_plan(plan, *, backend=None) -> list[OperationOutcome]
└── verifier.py     # verify_plan(plan, outcomes, target) -> PlanResult
```

`local_worker_agent.py` re-exports `_fs.py` primitives so existing tests that touch `_is_inside_git_repo`, `_JUNK_FILES`, etc. via `LocalWorkerAgent` keep working.

**Backend injection.** Every function that needs Claude takes a `backend` keyword argument; default is `get_claude_backend()` resolved lazily. Tests pass a stub directly. This avoids today's pattern of patching `fda.local_worker_agent.get_claude_backend`, which would not reach a new package without explicit support.

## Data models

```python
class OperationKind(str, Enum):
    CREATE_DIR = "create_dir"
    MOVE       = "move"
    DELETE     = "delete"

@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    source: str | None       # absolute path; None for CREATE_DIR
    destination: str | None  # absolute path; None for DELETE
    reason: str              # planner's rationale; ≤300 chars

@dataclass(frozen=True)
class Plan:
    target: str              # absolute path of organize target
    instructions: str        # user input, verbatim
    operations: tuple[Operation, ...]
    grouping_summary: str    # planner's narrative explaining the groupings

@dataclass(frozen=True)
class OperationOutcome:
    operation_index: int     # index into Plan.operations (stable across reordering)
    operation: Operation
    status: Literal["applied", "rescued", "skipped", "failed"]
    error: str | None = None
    rescue_note: str | None = None  # Phase B only

@dataclass(frozen=True)
class PlanResult:
    plan: Plan
    outcomes: tuple[OperationOutcome, ...]
    leftover_empty_dirs: tuple[str, ...]   # source dirs cleaned by verifier
    discrepancies: tuple[str, ...]         # plan said X, filesystem disagrees
    repos_skipped: tuple[str, ...]         # git repos planner refused to touch
    summary: str                           # rendered narrative for journal/UI
```

**Path format.** All `Operation.source` / `destination` are absolute paths. The planner is instructed to emit absolute paths under `Plan.target`. Validation rejects any operation whose source or destination resolves outside `Plan.target` (or, for moves, into a git repo on either side). `Plan.operations` is a `tuple` to preserve immutability across phases.

Frozen dataclasses + tuples prevent a downstream phase from quietly mutating the artifact a previous phase produced.

## Component contracts

### `planner.build_plan(target, instructions, *, backend) -> Plan`

Runs Claude with read-only tools and a single terminal `submit_plan` tool. Iteration cap stays at 20 since planning is the only agentic part. System prompt includes today's organization principles plus an explicit "you cannot perform actions; produce a complete plan via `submit_plan`."

`submit_plan` schema (no change from previous draft):

```python
{
    "name": "submit_plan",
    "description": "Submit the final organization plan. Call exactly once when planning is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"enum": ["create_dir", "move", "delete"]},
                        "source": {"type": "string"},        # absolute path
                        "destination": {"type": "string"},   # absolute path
                        "reason": {"type": "string"},        # ≤300 chars
                    },
                    "required": ["kind", "reason"],
                },
            },
            "grouping_summary": {"type": "string"},
        },
        "required": ["operations", "grouping_summary"],
    },
}
```

**Submission detection.** The `submit_plan` tool executor sets a closure-local `_submitted: bool` flag and stores the validated `Plan`. After `complete_with_tools()` returns (which does NOT raise on iteration cap — it forces a final no-tools turn), the planner checks the flag. If `False`, raise `PlannerDidNotSubmitError`. If `True`, return the stored `Plan`.

**Validation (all-or-nothing).** On `submit_plan` call, every operation is checked through `_fs.py`:
- path is absolute and resolves inside `Plan.target`,
- source (if present) does not resolve inside a git repo,
- destination (if present) does not resolve inside a git repo,
- destination (for MOVE) does not already exist,
- DELETE target is in the junk allowlist or is an empty file.

If any op fails, the tool result is an error message naming every offending op; the planner can fix and resubmit. Only a fully-valid submission flips the `_submitted` flag.

### `executor.execute_plan(plan, *, backend=None) -> tuple[OperationOutcome, ...]`

Phase A: deterministic only.

Reorder operations into groups: all `CREATE_DIR` first (in original order), then all `MOVE`, then all `DELETE`. Each `OperationOutcome.operation_index` records the original index in `plan.operations`, so callers can map outcomes back to the user-visible plan order regardless of execution order.

For each operation:
- Re-validate through `_fs.py` (handles races: a directory that wasn't a git repo at plan-time may have become one).
- Apply via the matching `_fs.py` primitive (`_fs.create_dir`, `_fs.move`, `_fs.delete`).
- On success: `OperationOutcome(status="applied")`.
- On expected failure (validation rejection, destination collision, permission error): `OperationOutcome(status="failed", error=str(e))`. Loop continues.
- On unexpected exception (e.g., filesystem unmounted): re-raise; the orchestrator wrapper converts to `{"success": False, "error": ...}`.

Phase B (deferred): if `backend` is provided AND any outcomes are `failed`, make ONE rescue Claude call. Tools available to rescue:
- `get_file_info(path)` — re-inspect a failure.
- `propose_alternative(failed_operation_index, new_destination)` — replace the destination of a failed `MOVE` operation; original source is reused; same `_fs` validation applies; parent dirs of `new_destination` are created implicitly (matches today's `shutil.move` behavior). Returns the new outcome status.
- `mark_skipped(failed_operation_index, reason)` — flip status from `failed` to `skipped`; record `reason` in `rescue_note`.
- `abort(reason)` — stop rescue; remaining failures stay `failed`; record `reason` on rescue's overall summary.

Operation indexes in rescue tools refer to `plan.operations` (the original index), not the reordered execution position.

### `verifier.verify_plan(plan, outcomes, target) -> PlanResult`

Re-lists the target tree once. For each `applied`/`rescued` outcome, checks the filesystem matches the operation's intent. Mismatches → `discrepancies` (each entry is a human-readable string). Then walks source parents of all `MOVE` operations; any directory that is now empty AND existed before the plan ran AND is inside `Plan.target` AND is not a git repo gets `rmdir`'d and recorded in `leftover_empty_dirs`. Renders a human-readable `summary`:

```
Organized {N} files into {M} groups:

📁 Projects/Lion-Chemtech/
   - proposal-v2.docx — grouped with Lion Chemtech client materials
   - meeting-notes.md — same project, related context

📁 Documents/Receipts/
   - costco-2026-04.pdf — receipt by date

⚠ 1 operation didn't complete:
   - move "weird-binary.bin" → "Archives/" (failed: destination already exists)

🧹 Cleaned up 2 empty source folders.
```

### `organize(target, instructions, *, preview=False, backend=None, progress_callback=None) -> Plan | PlanResult`

Public orchestrator in `fda/organize/__init__.py`:

1. `_fs.validate_target(target)` — raises `ValueError` if outside allowed roots or not a directory.
2. `plan = planner.build_plan(target, instructions, backend=backend)`.
3. If `preview`, return `plan`.
4. `outcomes = executor.execute_plan(plan, backend=backend)`.
5. `result = verifier.verify_plan(plan, outcomes, target)`.
6. Return `result`.

`apply_plan(plan, *, backend=None, progress_callback=None) -> PlanResult` is a separate public function for the preview-then-apply flow (caller holds the `Plan` in memory between calls).

**`progress_callback` event contract.** The callback receives prefixed strings:
- `"planner: <free-form>"` — planner narration (one per Claude turn).
- `"executor: applied N/M"` — incremented per applied operation.
- `"executor: failed N/M (<error>)"` — per failure.
- `"verifier: <free-form>"` — verifier status.
- `"rescue: <free-form>"` — Phase B only.

If `progress_callback` is `None`, all events are silently dropped. Telegram (which today doesn't pass a callback) gets no behavior change.

## Concurrency

The new package has no module-level or class-level mutable state. All per-run state is local to the `organize()` call. This means:

- Two concurrent `organize(target_a)` and `organize(target_b)` calls do not interfere.
- The `LocalWorkerAgent.organize_files()` wrapper no longer needs to set `self._current_project`, `self._organize_moves`, etc. (Today's instance-field mutation is a latent bug under the multi-bot orchestrator that owns one `LocalWorkerAgent`; this design fixes it incidentally.)
- Filesystem races (a file disappearing between planning and execution) are handled by the executor's per-op re-validation; affected operations end up as `failed` outcomes with the race reason.

We do NOT add cross-process file locks. The expected failure rate from concurrent organize calls on the same folder is low (you'd have to be running two bots against one Downloads folder simultaneously) and the executor's per-op semantics handle it gracefully.

## Wire-up to existing entry points

Phase A:

- **`LocalWorkerAgent.organize_files(target_path, instructions="", progress_callback=None)`** — calls `organize(target=target_path, instructions=instructions, backend=self._backend, progress_callback=progress_callback)` with `preview=False`. Translates `PlanResult` → today's dict shape:

  ```python
  {
      "success": bool,            # True iff all outcomes applied/rescued/skipped AND no discrepancies
      "summary": result.summary,
      "moves": [
          {"from": op.source, "to": op.destination, "reason": op.reason}
          for outcome in result.outcomes
          if outcome.operation.kind == MOVE and outcome.status in ("applied", "rescued")
      ],
      "deletions": [...],         # same shape, with reason
      "dirs_created": [...],      # paths only
      "repos_skipped": list(result.repos_skipped),
      "discrepancies": list(result.discrepancies),         # NEW (additive)
      "leftover_empty_dirs": list(result.leftover_empty_dirs),  # NEW (additive)
  }
  ```

  On failure path (planner error, validation error, unexpected exception): `{"success": False, "error": str(e)}`, matching today's behavior exactly.

- **`LocalWorkerAgent.organize_files_preview(target_path, instructions="", progress_callback=None) -> Plan`** — new method; returns the structured `Plan`. Does NOT execute anything. For programmatic callers (MCP server, tests, future UI work).

- **`LocalWorkerAgent.organize_files_apply(plan, progress_callback=None) -> dict`** — new method; takes a previously-generated `Plan`, calls `apply_plan()`, returns the same dict shape as `organize_files()`.

Telegram, Discord, Slack, web setup UI: **no code changes in Phase A.** Their existing call to `organize_files(target_path, instructions)` keeps working. The new `_preview` / `_apply` methods are not surfaced to bots; that's Phase B.

## Error handling

| Where | What can go wrong | Behavior |
|---|---|---|
| `_fs.validate_target` | path outside allowed roots, not a directory | raises `ValueError`; wrapper catches → `{"success": False, "error": ...}` (matches today) |
| Planner Claude call | timeout, API error | wrapper returns `{"success": False, "error": ...}` |
| Planner output | `submit_plan` never successfully called | raises `PlannerDidNotSubmitError`; wrapper translates to `{"success": False, "error": ...}` |
| Planner output | `submit_plan` validation fails (touches git repo, bad delete, non-absolute path, outside target) | error returned to Claude; can fix and resubmit |
| Executor — single op | filesystem error, permission denied, destination exists | per-op outcome `failed` with error string; loop continues |
| Executor — unexpected exception | filesystem unmounted, OS-level error | re-raised; wrapper catches → `{"success": False, "error": ...}` |
| Executor (Phase B) — rescue Claude call | timeout, API error | log warning; unrescued failures stay `failed`; verifier still runs |
| Verifier | filesystem race | record as discrepancy; don't crash |
| Verifier — empty-dir cleanup | rmdir fails | log debug; omit from `leftover_empty_dirs`; don't crash |

**Task status mapping (orchestrator).** `_handle_local_organize_request` already calls `state.update_task(task_id, status="completed" if result.get("success") else "blocked")`. With the new design:

- `result["success"] == True` iff all outcomes are `applied`, `rescued`, or `skipped` AND `result["discrepancies"]` is empty.
- Any `failed` outcome OR any non-empty `discrepancies` → `success=False`; task moves to `blocked`.
- A journal entry is **always** written, regardless of success — partial completion is part of the audit trail, not a reason to skip the journal.

**Atomicity.** The executor is not transactional. Each op is independent; partial completion produces an honest `PlanResult`.

**Idempotency.** Re-running `apply_plan(plan)` on a partially-applied plan is safe:
- `CREATE_DIR` is no-op if the directory already exists.
- `MOVE` is `skipped` (not `failed`) if the source is already gone AND the destination already exists with the same name and same content (size + mtime check).
- `DELETE` is no-op if the file is gone.

This makes "re-run after a transient failure" a safe user action.

**Crash recovery.** A crash mid-executor leaves no `PlanResult` and no journal entry. The recovery story is:
1. Re-run `organize(same target, same instructions)`. The planner regenerates a plan against the now-partially-organized filesystem. Idempotent operations and updated state mean the new plan only contains the moves still needed. No persistence required.

This is acceptable for Phase A. If we later want crash-resilient resumption, plan persistence becomes a separate spec.

## Journal entry shape

The orchestrator (`_handle_local_organize_request`) is updated to write the following journal content (additive to today's structure):

```markdown
## Target
`/Users/.../Downloads`

## Instructions
sort by client

## Plan Summary
{result.plan.grouping_summary}

## Files Moved (12 of 14 planned)
- `proposal.docx` → `Projects/Lion-Chemtech/proposal.docx` — Lion Chemtech client material
- `costco-receipt.pdf` → `Documents/Receipts/costco-receipt.pdf` — receipt grouped by type
... (truncated at 50)

## Directories Created
- `Projects/Lion-Chemtech/`
- `Documents/Receipts/`

## Couldn't Complete (2)
- `weird-binary.bin` → `Archives/` (destination already exists)
- `locked.docx` → `Documents/` (permission denied)

## Empty Folders Cleaned
- `Projects/old-stuff/`

## Discrepancies
(none)

## Junk Deleted
- `.DS_Store`

## Git Repos Skipped
- `Projects/MyApp/`
```

The journal `summary` line:
`[LOCAL] File organization: {brief} (12/14 moves, 1 deletion, 1 cleanup)`

This is structurally identical to today's journal entry, with new sections added for `Plan Summary`, per-move `reason`, `Couldn't Complete`, `Empty Folders Cleaned`, and `Discrepancies`. Existing journal-search consumers continue to find entries by their existing tags (`worker`, `local`, `file-organization`).

## Testing

New file: `tests/test_organize.py`. Existing safety primitive tests in `test_local_worker.py` continue to exercise `_fs.py` via `LocalWorkerAgent` re-exports.

**Per-component unit tests** using `tmp_path` and a stub Claude backend:

- **`test_models.py`** — dataclass invariants, frozenness, tuple-vs-list conversions at boundaries.
- **`test_fs.py`** — path validation; git-repo detection (source side); git-repo detection (destination side, NEW behavior); junk-file allowlist; empty-file delete; absolute-path enforcement; "destination already exists" rejection.
- **`test_planner.py`**:
  - happy path: stub backend returns a `submit_plan` tool call → planner returns matching `Plan`.
  - planner tries to delete a non-junk file → all-or-nothing rejection, retries successfully.
  - planner targets a git repo (source or destination) → rejected.
  - planner emits a relative path → rejected.
  - planner never calls `submit_plan` (loop hits cap, falls through to no-tools summary) → `PlannerDidNotSubmitError`.
  - planner calls `submit_plan` with one bad op → entire submission rejected; tool result lists every offender.
- **`test_executor.py`** (Phase A):
  - happy path: 5 ops on tmp filesystem, all `applied`; `operation_index` matches plan order.
  - move with destination collision → `failed` with error string.
  - move from inside a git repo (planner missed it; race) → `_fs` rejects, `failed`.
  - re-running same plan → `CREATE_DIR` no-op, `MOVE` `skipped` (already at destination), `DELETE` no-op.
  - executor-level unexpected exception (mock filesystem error) → re-raised.
- **`test_executor_rescue.py`** (Phase B):
  - rescue stub re-routes a destination collision via `propose_alternative` → status flips to `rescued`.
  - rescue stub `mark_skipped` → status flips to `skipped` with `rescue_note`.
  - rescue stub `abort` → remaining failures stay `failed`.
  - rescue Claude timeout → log + unrescued failures stay `failed`; verifier still runs.
- **`test_verifier.py`**:
  - all ops applied, filesystem matches → no discrepancies.
  - applied move, file disappeared → discrepancy recorded.
  - source parent emptied by plan → cleaned, recorded in `leftover_empty_dirs`.
  - source parent emptied but contains other files plan didn't touch → NOT cleaned.
  - source parent that is a git repo → NOT cleaned.
  - summary rendering: groups by destination, includes reasons, lists "Couldn't do" tail.

**Integration test:** `test_organize_pipeline.py` — full `organize()` against tmp folder with stub backend that returns a fixed Plan. Asserts final `PlanResult` shape, journal-friendly summary, and dict translation in `LocalWorkerAgent.organize_files()`.

**Backwards-compatibility tests** in `test_local_worker.py`:
- `LocalWorkerAgent.organize_files()` returns a dict with all existing keys (`success`, `summary`, `moves`, `deletions`, `dirs_created`, `repos_skipped`).
- Each move dict contains `from`, `to`, AND new `reason` field.
- New `discrepancies` and `leftover_empty_dirs` fields are present (possibly empty arrays).
- Concurrent `organize_files()` calls on different targets do not corrupt each other (regression test for today's instance-field bug).

**Test infrastructure:** `conftest.py` gains a `stub_claude_backend` fixture that takes a script of tool-call responses and replays them deterministically. Backend is passed via the new `backend=` keyword, not patched into the module.

**Manual smoke test (not automated):** before merging Phase A, run new `organize_files()` against `~/Downloads` and a copy of a messy `~/Desktop`; compare plan vs actual moves; force a destination collision to confirm the failure outcome path; verify journal entry includes `reason` per move.

## Cost analysis

Both old and new use `claude-sonnet-4-20250514` ($3/M input, $15/M output). Per-run estimate for a typical ~30-file folder.

### Today (single agentic loop)

| | tokens | $ |
|---|---|---|
| Input  | ~45,000 | $0.135 |
| Output | ~4,500  | $0.068 |
| **Total** | | **~$0.20 / run** |

### Phase A (planner + deterministic executor + verifier)

- **Planner (Claude):** ~$0.18 (similar token volume; no execution turns; heavier final `submit_plan` output).
- **Executor (deterministic):** $0.
- **Verifier (deterministic):** $0.

| Scenario | Cost |
|---|---|
| Today | ~$0.20 |
| Phase A | **~$0.18** |

Phase A is roughly cost-neutral on the happy path and materially cheaper on previously-failure-prone runs (no re-run needed for the same input).

### Phase B (adds rescue)

- **Rescue (Claude, only on failures):** ~$0.03 when triggered (5-iter cap, small payload).

| Scenario | Cost |
|---|---|
| Phase B, no rescue needed | ~$0.18 |
| Phase B, rescue triggered | ~$0.21 |
| Phase B, expected average (assumed 30% rescue rate) | **~$0.19** |

The 30% rescue-rate guess is unmeasured. Phase A logs failure counts; Phase B's design will be refined with that data.

**Optional follow-on:** prompt caching on the planner system prompt would cut planner input cost ~50% on repeat runs. Treat as a separate small task once the new pipeline is stable.

## Open questions / out of scope

- **Plan persistence in SQLite** — needed for Telegram preview-then-confirm; deferred to a follow-on spec alongside Phase B UI work.
- **Rescue rate measurement** — Phase A logs `failed` outcome counts so Phase B can be designed against real data, not the 30% guess.
- **Empty-dir cleanup policy** — Phase A only cleans dirs the plan itself emptied, and only if they aren't git repos. Cleaning user-created empty dirs unrelated to the plan is intentionally out of scope.
- **MCP server** (`fda/mcp_server.py`) — currently exposes the orchestrator; the new `organize_files_preview` / `organize_files_apply` could be exposed there in Phase A as a low-risk way to use preview from Claude Code sessions. Not a blocker; can land in either phase.
- **Concurrency lock** — current design tolerates concurrent runs on different targets and degrades gracefully on the same target. If real-world failures show this isn't enough, add a per-target file lock in Phase B.

## Implementation order

### Phase A (this spec's primary plan)

1. `models.py` — Plan, Operation, PlanResult dataclasses + tests.
2. `_fs.py` — extract path validation, git-repo detection (BOTH directions, NEW), junk-file rules, deterministic apply primitives. Keep `LocalWorkerAgent` re-exports for back-compat with existing tests.
3. `prompts.py` + `planner.py` — read-only tools, `submit_plan` with all-or-nothing validation, `PlannerDidNotSubmitError` via flag-after-return mechanism. Stub-backend tests.
4. `executor.py` happy path (no rescue). Per-op re-validation. `operation_index` tracking. Idempotent re-run semantics.
5. `verifier.py` — filesystem diff, empty-dir cleanup (with git-repo guard), summary rendering.
6. `organize()` and `apply_plan()` in `__init__.py`. `progress_callback` event contract.
7. Wire into `LocalWorkerAgent.organize_files()` with back-compat dict translation including `reason` per move. Add `organize_files_preview` / `organize_files_apply` methods.
8. Update `_handle_local_organize_request` journal entry to include `Plan Summary`, per-move `reason`, `Couldn't Complete`, `Empty Folders Cleaned`, `Discrepancies` sections.
9. Update task-status mapping: `success=False` triggers `blocked`; journal always written.
10. Manual smoke test on `~/Downloads`. Land Phase A.

### Phase B (separate follow-on spec/plan)

11. Add rescue Claude call to `executor.py` with the four rescue tools.
12. Add plan persistence (SQLite `organize_plans` table) — this is what unlocks Telegram preview.
13. Add Telegram `--preview` flag with confirmation flow.
14. Add web UI preview panel.
15. Refine rescue prompts based on Phase A's logged failure data.
