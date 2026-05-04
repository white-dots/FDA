# Organize Success Rate — Design

**Date:** 2026-05-04
**Status:** draft (pending Codex review + user approval)
**Owner:** hogyeongkim
**Related notes:** Obsidian `FDA/Future Plan - Organization Success Rate`, `docs/local_metadata_layer_plan.md`

## Problem

`LocalWorkerAgent.organize_files()` produces good groupings but executes them inconsistently. The same folder, run twice, yields different outcomes: some moves complete, others don't, and old source folders are left behind. Root cause: the same Claude tool-use loop is responsible for *deciding* the taxonomy and *calling* `move_file`. When the iteration budget runs thin or the model gets distracted re-inspecting a file, it stops short of executing every move it had in mind. There is no separation between deciding and doing, so non-determinism shows up directly in the filesystem.

## Goals

1. Same target folder + same instructions produces the same set of file moves on every run, given a stable plan.
2. Per-move rationale is captured and surfaced in the journal/UI summary.
3. Old source folders that become empty as a result of the plan are cleaned up.
4. Existing entry points (Telegram `/organize`, web UI, orchestrator) keep working with no signature changes.
5. Same or better Claude API cost per run.

## Non-goals

- Plan persistence in SQLite (lives in a follow-on spec; deliberately excluded to keep scope tight).
- Undo/rollback of an executed plan.
- Re-classification of files in already-organized folders.
- Anything inside git repositories (current safety stays).

## Architecture

A new `fda/organize/` package implements a three-phase pipeline behind a single entry point:

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
       │ executor │  Deterministic apply (happy path).
       │          │  Failures → ONE rescue Claude call → retry/skip/abort.
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

- **Planner has zero side effects.** It cannot move, create, or delete; only read-only tools plus a single terminal `submit_plan(operations, grouping_summary)` tool.
- **Executor doesn't make taxonomy decisions.** It applies what the plan says, in a fixed safe order: `CREATE_DIR` → `MOVE` → `DELETE`. Same plan + same filesystem = same outcome.
- **Rescue is bounded.** At most one Claude call per organize run, given the failures + outcomes-so-far. Tools restricted to: `get_file_info`, `propose_alternative`, `mark_skipped`, `abort`. 5-iteration cap.
- **Verifier is pure observation.** No taxonomy decisions; checks reality matches the plan, surfaces discrepancies, cleans dirs the plan itself emptied.
- **Backwards compatibility.** `LocalWorkerAgent.organize_files()` keeps its signature and adds new methods alongside; Telegram/web/orchestrator paths unchanged.

## Module layout

```
fda/organize/
├── __init__.py     # public API: organize(), apply_plan()
├── models.py       # Plan, Operation, OperationKind, OperationOutcome, PlanResult
├── prompts.py      # PLANNER_SYSTEM_PROMPT, RESCUE_SYSTEM_PROMPT
├── _fs.py          # path validation, git-repo detection, junk-file rules
│                   #   (extracted from local_worker_agent.py — same semantics)
├── planner.py      # build_plan(target, instructions, backend) -> Plan
├── executor.py     # execute_plan(plan, backend) -> list[OperationOutcome]
└── verifier.py     # verify_plan(plan, outcomes, target) -> PlanResult
```

`local_worker_agent.py` re-exports `_fs.py` primitives so existing tests and callers stay intact. This eliminates safety-rule drift between planner-time validation and executor-time enforcement.

## Data models

```python
class OperationKind(str, Enum):
    CREATE_DIR = "create_dir"
    MOVE       = "move"
    DELETE     = "delete"

@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    source: str | None       # None for CREATE_DIR
    destination: str | None  # None for DELETE
    reason: str              # planner's rationale; ≤300 chars

@dataclass(frozen=True)
class Plan:
    target: str              # absolute path of organize target
    instructions: str        # user input, verbatim
    operations: list[Operation]
    grouping_summary: str    # planner's narrative explaining the groupings

@dataclass(frozen=True)
class OperationOutcome:
    operation: Operation
    status: Literal["applied", "rescued", "skipped", "failed"]
    error: str | None = None
    rescue_note: str | None = None  # what rescue did, if applicable

@dataclass(frozen=True)
class PlanResult:
    plan: Plan
    outcomes: list[OperationOutcome]
    leftover_empty_dirs: list[str]   # source dirs cleaned by verifier
    discrepancies: list[str]         # plan said X, filesystem disagrees
    repos_skipped: list[str]         # git repos planner refused to touch
    summary: str                     # rendered narrative for journal/UI
```

Frozen dataclasses prevent a downstream phase from quietly mutating the artifact a previous phase produced.

## Component contracts

### `planner.build_plan(target, instructions, backend) -> Plan`

Runs Claude with read-only tools (`list_directory`, `get_file_info`, `read_file`, `run_command`) and a single terminal `submit_plan` tool. Iteration cap stays at 20 since planning is the only agentic part. System prompt includes today's organization principles plus an explicit "you cannot perform actions; produce a complete plan via `submit_plan`."

`submit_plan` schema:

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
                        "source": {"type": "string"},
                        "destination": {"type": "string"},
                        "reason": {"type": "string"},
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

The tool executor short-circuits on `submit_plan`: parses and validates each operation against `_fs` rules (rejects ops targeting git repos; rejects deletes outside the junk allowlist). **Validation is all-or-nothing**: if any operation is rejected, the entire submission is rejected and a tool-result error lists every offending op so the planner can fix and resubmit. Only a fully-valid `submit_plan` signals the loop to terminate. If the iteration cap is reached without a successful submission, raise `PlannerDidNotSubmitError`.

### `executor.execute_plan(plan, backend) -> list[OperationOutcome]`

Applies operations in fixed order (`CREATE_DIR` → `MOVE` → `DELETE`). Each op routed through `_fs.py` safety primitives. Failures collected, not raised. After the happy-path pass, if any failures remain, ONE rescue Claude call:

- Inputs: list of failed operations + outcomes-so-far + system prompt.
- Tool surface: `get_file_info(path)`, `propose_alternative(failed_index, new_destination)`, `mark_skipped(failed_index, reason)`, `abort(reason)`.
- 5-iteration cap.
- Outcomes returned by rescue update the original outcome list (status flips from `failed` to `rescued` or `skipped`).

### `verifier.verify_plan(plan, outcomes, target) -> PlanResult`

Re-lists the target tree once. For each `applied`/`rescued` outcome, checks the filesystem matches the operation's intent. Mismatches → `discrepancies`. Then walks source parents of all `MOVE` operations; any directory that is now empty AND existed before the plan ran gets `rmdir`'d and recorded in `leftover_empty_dirs`. Renders a human-readable `summary` (group by destination, list reasons, then a "Couldn't do" tail for failures).

### `organize(target, instructions, preview=False, progress_callback=None) -> Plan | PlanResult`

Public orchestrator in `fda/organize/__init__.py`:

1. `_fs.validate_target(target)` — raises `ValueError` like today.
2. `plan = planner.build_plan(...)`.
3. If `preview`, return `plan`.
4. `outcomes = executor.execute_plan(plan, backend)`.
5. `result = verifier.verify_plan(plan, outcomes, target)`.
6. Return `result`.

`apply_plan(plan, progress_callback=None) -> PlanResult` is a separate public function for the preview-then-apply flow.

## Wire-up to existing entry points

`LocalWorkerAgent` gains two new methods alongside the unchanged one:

- `organize_files(target_path, instructions="", progress_callback=None)` — calls `organize(...)` with `preview=False`. Translates `PlanResult` → today's dict shape (`success`, `summary`, `moves`, `deletions`, `dirs_created`, `repos_skipped`) plus a new optional `discrepancies` field. Telegram and web UI consumers unchanged.
- `organize_files_preview(target_path, instructions="", progress_callback=None) -> Plan` — new method for the preview flow. Returns the structured `Plan` for rendering.
- `organize_files_apply(plan, progress_callback=None) -> PlanResult` — new method that wraps `apply_plan`.

Telegram `/organize` gains a `--preview` flag that calls `organize_files_preview`, replies with the rendered plan, and waits for a follow-up confirmation before calling `organize_files_apply`. The web UI exposes a "Preview first" toggle.

## Error handling

| Where | What can go wrong | Behavior |
|---|---|---|
| `validate_target` | path outside allowed roots, not a directory | raise `ValueError` (same as today) |
| Planner Claude call | timeout, API error | wrapper returns `{"success": False, "error": ...}` |
| Planner output | `submit_plan` never called within iteration cap | raise `PlannerDidNotSubmitError` → wrapper translates |
| Planner output | `submit_plan` validation fails (touches git repo, bad delete) | error returned to Claude; can fix and resubmit |
| Executor — single op | filesystem error, permission denied, destination exists | per-op outcome `failed` with error string; loop continues |
| Executor — rescue Claude call | timeout, API error | log warning; unrescued failures stay `failed`; verifier still runs |
| Verifier | filesystem race | record as discrepancy; don't crash |
| Verifier — empty-dir cleanup | rmdir fails | log debug, omit from `leftover_empty_dirs`, don't crash |

**Atomicity:** the executor is not transactional. Each op is independent; partial completion produces an honest `PlanResult`. The verifier + journal entry give the user a complete record of what changed.

**Idempotency:** re-running `apply_plan(plan)` on a partially-applied plan is safe. `CREATE_DIR` is no-op if the directory exists; `MOVE` is `skipped` if the source is gone and the destination already exists with the same name; `DELETE` is no-op if the file is gone. This makes "re-run after a transient failure" a safe user action.

**Single safety source:** `_fs.py` is the only place path validation, git-repo detection, and junk-only delete rules live. Both planner-time validation (inside `submit_plan`) and executor-time enforcement go through it. No way for the planner to approve something the executor would refuse.

## Testing

New file: `tests/test_organize.py`. Existing safety primitive tests in `test_local_worker.py` continue to exercise `_fs.py` via `LocalWorkerAgent` re-exports.

**Per-component unit tests** using `tmp_path` and a stub Claude backend:

- **`test_models.py`** — dataclass invariants, frozenness.
- **`test_planner.py`**:
  - happy path: stub backend returns a `submit_plan` tool call → planner returns matching `Plan`.
  - planner tries to delete a non-junk file → rejected, retries successfully.
  - planner targets a git repo → rejected.
  - planner never calls `submit_plan` → `PlannerDidNotSubmitError`.
- **`test_executor.py`**:
  - happy path: 5 ops on tmp filesystem, all `applied`.
  - move with destination collision → `failed`, rescue stub re-routes → `rescued`.
  - move from inside a git repo (planner missed it) → `_fs` rejects, `failed`.
  - rescue Claude timeout → unrescued ops stay `failed`, pipeline continues.
  - re-running same plan → idempotent.
- **`test_verifier.py`**:
  - all ops applied, filesystem matches → no discrepancies.
  - applied move, file disappeared → discrepancy recorded.
  - source parent emptied by plan → cleaned, recorded in `leftover_empty_dirs`.
  - source parent emptied but contains other files plan didn't touch → NOT cleaned.
  - summary rendering: groups by destination, includes reasons, lists "Couldn't do" tail.

**Integration test:** `test_organize_pipeline.py` — full `organize()` against tmp folder with stub backend that returns a fixed Plan. Asserts final `PlanResult` shape, journal-friendly summary, and dict translation in `LocalWorkerAgent.organize_files()`.

**Backwards-compatibility tests** in `test_local_worker.py`:
- `LocalWorkerAgent.organize_files()` returns a dict with all existing keys plus the new `discrepancies` field.

**Test infrastructure:** `conftest.py` gains a `stub_claude_backend` fixture that takes a script of tool-call responses and replays them deterministically.

**Manual smoke test (not automated):** before merging, run new organize against `~/Downloads` and a copy of a messy `~/Desktop`; compare plan vs actual moves; force a destination collision to confirm rescue triggers cleanly.

## Cost analysis

Both old and new use `claude-sonnet-4-20250514` ($3/M input, $15/M output). Numbers below are per-run for a typical ~30-file folder.

### Today (single agentic loop)

| | tokens | $ |
|---|---|---|
| Input  | ~45,000 | $0.135 |
| Output | ~4,500  | $0.068 |
| **Total** | | **~$0.20 / run** |

### New pipeline

- **Planner (Claude):** ~$0.18 (similar tokens; no execution turns; heavier final `submit_plan` output).
- **Executor (deterministic):** $0.
- **Rescue (Claude, only on failures):** ~$0.03 when triggered (5-iter cap, small payload).
- **Verifier (deterministic):** $0.

| Scenario | Cost |
|---|---|
| Today | ~$0.20 |
| New, no rescue needed | ~$0.18 |
| New, rescue triggered (~30% of runs assumed) | ~$0.21 |
| New, expected average | ~$0.19 |

**Real savings on failure-prone runs:** today, when the loop runs out mid-execution, you re-run → ~$0.40. The new pipeline is deterministic per plan, so re-runs aren't needed for the same input. Expected savings on previously-failure-prone folders: 30–40%.

**Optional follow-on (not in this spec):** prompt caching on the planner system prompt would cut planner input cost ~50% on repeat runs. Treat as a separate small task once the new pipeline is stable.

## Open questions / out of scope

- **Plan persistence:** storing plans in SQLite (with status, re-run, undo) is genuinely valuable but belongs in a follow-on spec to keep this one shippable.
- **Rescue rate measurement:** the 30% guess is unmeasured. Once deployed, the executor should log rescue rate so we can replace the guess with real data.
- **Empty-dir cleanup policy:** current scope only cleans dirs the plan itself emptied. Cleaning user-created empty dirs unrelated to the plan is intentionally out of scope.
- **Preview confirmation UX on Telegram:** `--preview` requires a follow-up message to confirm. Exact wording / timeout is a small UX decision deferred to implementation.

## Implementation order (for the follow-on plan)

1. `models.py` + `_fs.py` (extract from `local_worker_agent.py`, add tests).
2. `prompts.py` + `planner.py` (with stub-backend tests).
3. `executor.py` happy path (no rescue yet).
4. `verifier.py` + summary rendering.
5. Wire into `LocalWorkerAgent.organize_files()` with back-compat dict translation; ensure existing tests pass.
6. `executor.py` rescue path.
7. `organize_files_preview` / `organize_files_apply` methods + Telegram `--preview` flag + web UI toggle.
8. Manual smoke test on `~/Downloads`.
