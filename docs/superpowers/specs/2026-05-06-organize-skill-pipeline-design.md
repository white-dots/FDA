# Organize Pipeline Redesign — Skill-Based Decomposition

**Status:** Design approved 2026-05-06. Ready for implementation planning.

**Supersedes:** the single-loop planner architecture in `docs/superpowers/specs/2026-05-04-organize-success-rate-design.md` (Phase A). The executor and verifier shapes from that spec are unchanged; the planner phase is replaced.

**Rollback marker:** pre-redesign HEAD is commit `53a6cf2` ("fix(claude_backend): don't override API model default with empty string"). All redesign commits land on top.

## Why this exists

The Phase A planner used a single Claude tool-use loop: explore the directory with `list_directory`/`get_file_info`/`read_file`, then submit a complete plan via `submit_plan`. The architecture was sound on paper, but live runs against a 100-file fixture revealed a structural failure mode.

A diagnostic run on `/private/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003/` (101 files across 10 folders) produced this trail:

- 11 `list_directory` calls (root + every folder).
- 52 `read_file` calls (broad sampling across folders).
- **12 consecutive `submit_plan` calls with `operations=[]`**, each rejected by the empty-list check, each retry ~55 seconds apart.
- A 13th call finally landed with **6 operations total** (4 `create_dir` + 2 `move`, both moves from `folder_1`).

Earlier production runs against the same fixture produced "5 files moved into 3 groups" — the same partial-output shape, same root cause.

**Root cause:** the planner's per-turn output token budget. `AnthropicAPIBackend.complete_with_tools` defaults to `max_tokens=4096`. A 100-file plan in JSON is ~10K tokens just for the operations array, plus reasoning preamble the model emits before the tool call. The model can't fit a complete plan in one turn, so it falls back to a "minimal valid" payload (`operations=[]`) repeatedly, then eventually emits a tiny partial plan when generation lands somewhere within the budget.

Two earlier fixes (scaffold-only rejection, empty-list rejection) addressed *symptoms* of this bottleneck — they prevent silent acceptance of bad plans, but they cannot make a bigger plan fit in the output budget. Bumping `max_tokens` is a band-aid that breaks again at ~500–1000 files. Incremental plan construction (`add_to_plan` + `finalize_plan`) was considered and rejected: it solves the token squeeze but keeps the single-conversation context-bloat problem (the planner still carries 500KB of accumulated PDF text in conversation history).

The fix is to decompose the planner into a pipeline of small, single-purpose modules — each one a tight Python function with a narrow LLM prompt + narrow tool surface, calling one another via clean data contracts. Context never accumulates because each LLM call is independent: the file's contents enter, a short summary exits, and the contents are discarded.

## Goals

- A 100-file organize run produces a complete plan and applies it. No 5/100, no 12 empty resubmissions.
- The architecture scales to 1000+ files without further redesign.
- Every run leaves a structured log on disk, default-on, surfaced to the user.
- Cheaper steps run on cheaper models. Per-run Sonnet token spend drops materially.
- Each module is independently testable, mockable, and replaceable.

## Non-goals

- Phase B features (rescue, plan persistence, Telegram `--preview` flag, web UI panel) remain deferred.
- Log retention / rotation policy is out of scope; we just create files in a known location.
- We do not pursue full multi-agent (separate Claude conversations passing JSON over a wall). The skill-based shape gets the same isolation benefits without that complexity.

## Architecture

```
target dir
   │
   ▼
Reader (Python orchestrator)
   ├─ skill: file-summarizer (Haiku 4.5)   ← one call per file, parallel
   ├─ skill: file-summarizer (Haiku 4.5)
   └─ ... (thread pool, ~8 concurrent)
   │
   ▼ Catalog: list[CatalogEntry]
   │
   ▼
Classifier (Python orchestrator)
   └─ skill: document-classifier (Sonnet 4.6)   ← one call, sees only the catalog
   │
   ▼ Groupings: list[Grouping]
   │
   ▼
PlanBuilder (deterministic Python)
   │
   ▼ Plan: list[Operation]
   │
   ▼
Executor (existing, unchanged)
   │
   ▼
Verifier (existing, unchanged)
```

### Module responsibilities

**Reader** (`fda/organize/reader.py`)
- Walks the target tree once, skipping anything inside a `.git` directory.
- For each file, dispatches a `file-summarizer` skill call to Haiku.
- Junk filenames (`.DS_Store`, `Thumbs.db`, etc.) flagged in the catalog without going to the LLM.
- Per-file failure (timeout, API error, unparseable summary) → entry with `summary_failed=True`, `summary=""`, run continues.
- Concurrency: `concurrent.futures.ThreadPoolExecutor` with 8 workers. I/O-bound; threads sufficient.
- Per-file timeout: 30 s. Total wall-clock cap: 5 min.
- PDF text extraction is delegated to the existing `_extract_pdf_text` helper, which moves from `planner.py` to `reader.py`. The 64 KB byte cap on `pdftotext` stdout stays in place.
- Returns `Catalog` (a tuple of `CatalogEntry`).

**Classifier** (`fda/organize/classifier.py`)
- Single LLM call. Input: the Catalog (with junk entries filtered out before serializing — junk is handled deterministically by PlanBuilder, never by the LLM) plus the user's instructions string. Output: structured JSON.
- Skill output schema enforced via prompt; on JSON parse failure, retry once with a stronger reminder, then raise.
- Returns `Groupings` (a tuple of `Grouping`).
- Never touches the filesystem. The Catalog is its only input source about files.

**PlanBuilder** (`fda/organize/plan_builder.py`)
- Pure deterministic function. Input: `Groupings` + the junk list from the Catalog. Output: `Plan`.
- Produces:
  - One `create_dir` per unique destination directory.
  - One `move` per `(file → destination_dir/filename)`.
  - One `delete` per junk file from the Catalog.
- Validates each op via the existing `_fs.validate_operation` before including it. Drops ops that fail validation, logging the rejection. Raises only if zero valid ops remain *and* there was input (an empty input is a successful no-op run).
- Detects and merges duplicate destination directories.
- Detects and rejects same-file-in-two-groups (Classifier bug); raises `PlanBuilderError` with the conflicting file path.
- Drops ops whose source path no longer exists (race or Classifier hallucination), logging the drop.

**Executor** (`fda/organize/executor.py`) — unchanged.

**Verifier** (`fda/organize/verifier.py`) — unchanged.

### Skill files

Skill files are an internal convention for this codebase: each `SKILL.md` is a markdown file with YAML frontmatter that declares the skill's purpose, model, and (optionally) tools. At runtime, Python loads the file, parses the frontmatter into config, and uses the body as the system prompt for the LLM call. This is a code-organization pattern — there is no server-side skill registry involved. Tests can substitute a fake skill body to exercise edge cases without re-issuing real LLM calls.

Two new skills under `fda/organize/skills/`:

**`file-summarizer/SKILL.md`** — Reader's per-file prompt.
- Input (rendered into the user message): file path, extension, size, raw text or stub.
- Output: a small JSON object `{"type_label": str, "summary": str, "junk": bool}`.
- Model: Haiku 4.5 (`claude-haiku-4-5-20251001`).
- No tools — pure text-in / JSON-out.

**`document-classifier/SKILL.md`** — Classifier's prompt.
- Input: catalog JSON + user instructions.
- Output: groupings JSON `{"groupings": [{"category": str, "destination_dir": str, "files": [str], "reason": str}, ...]}`.
- Model: Sonnet 4.6 (`claude-sonnet-4-6`).
- No tools.

### New data models

In `fda/organize/models.py`:

```python
@dataclass(frozen=True)
class CatalogEntry:
    path: str            # absolute
    ext: str             # lowercase, including the dot
    size_bytes: int
    summary: str         # may be empty if summary_failed
    type_label: str      # short tag from the summarizer
    in_git_repo: bool    # always False in the catalog (skipped at walk time); kept for clarity
    is_junk: bool        # filename matches JUNK_FILES
    summary_failed: bool # True when the summarizer failed for any reason

@dataclass(frozen=True)
class Catalog:
    target: str
    entries: tuple[CatalogEntry, ...]
    git_repos_skipped: tuple[str, ...]

@dataclass(frozen=True)
class Grouping:
    category: str
    destination_dir: str  # absolute path inside target
    files: tuple[str, ...]  # absolute paths
    reason: str

@dataclass(frozen=True)
class Groupings:
    items: tuple[Grouping, ...]
    overall_reason: str
```

`PlanResult` gets one new field: `log_path: str | None = None`.

## Observability

### Default log location

```
~/.fda/logs/organize/<YYYYMMDD-HHMMSS>-<target_basename>.log
```

Example: `~/.fda/logs/organize/20260506-001530-randomized-company-documents-2026-05-05-003.log`.

Cross-platform — `~` expands correctly on macOS, Linux, and Windows under Python's `pathlib`. Timestamp + target basename guarantee no collisions across concurrent runs and let runs sort chronologically.

### Surfacing the path

- The `OrganizeLogger.__init__` emits the log path via `progress_callback("📝 logging to <path>")` immediately after opening the file, so any caller already wired to `progress_callback` (CLI, Telegram, web UI) sees it live.
- `PlanResult.log_path` carries the absolute path back to the caller.
- `LocalWorkerAgent.organize_files()` includes it in the back-compat result dict.
- The orchestrator's journal entry gets a `**Detailed log:** <path>` footer line.

### Format

Plain text, one event per line, grep-friendly:

```
[HH:MM:SS.mmm] EVENT_TYPE key1=value1 key2="value with spaces" ...
```

Strings containing spaces, `=`, or quotes are double-quoted. Long values (file summaries) are truncated to 200 chars in the log line itself; file paths and operation details are never truncated.

### Event vocabulary

| Stage | Events |
|---|---|
| Run lifecycle | `RUN_START target=... instructions="..." log_path=...`, `RUN_END status=... elapsed=...` |
| Reader | `READER_START files=N`, `READER_FILE_DONE path=... summary="..." elapsed_ms=N`, `READER_FILE_FAIL path=... reason=...`, `READER_END ok=N failed=N elapsed=N` |
| Classifier | `CLASSIFIER_START catalog_size=N`, `CLASSIFIER_GROUP category=... files=N reason="..."`, `CLASSIFIER_DONE elapsed_ms=N`, `CLASSIFIER_FAIL reason=... attempt=1` |
| PlanBuilder | `PLAN_BUILD_DONE ops=N create_dirs=N moves=N deletes=N`, then per op: `PLAN_OP idx=0 kind=move src=... dst=... reason="..."` |
| Executor | `EXEC_APPLY idx=0 kind=move src=... dst=... result=applied`, `EXEC_FAIL idx=0 kind=move error=...`, `EXEC_END applied=N failed=N skipped=N` |
| Verifier | `VERIFY_DISCREPANCY desc=...`, `VERIFY_END discrepancies=N empty_dirs_cleaned=N` |

Approximate volume: ~400 lines for a 100-file run, ~500 KB log. Manageable for human review.

### Implementation

Single class in `fda/organize/_logger.py`:

```python
class OrganizeLogger:
    def __init__(self, log_path: Path | None,
                 progress_callback: Callable[[str], None] | None = None): ...
    def log(self, event: str, **fields) -> None: ...   # writes file + forwards to progress_callback
    def close(self) -> None: ...
```

Each pipeline stage takes `logger: OrganizeLogger` as a parameter and emits events. The logger forwards each event to `progress_callback` so the live UI feed stays intact; the file always carries the structured detail.

### Opt-out

- `log_path=None` (default) → use `~/.fda/logs/organize/<ts>-<basename>.log`.
- `log_path=<Path>` → use that exact path.
- `log_path=False` → disable file logging; `progress_callback` still works.

### Failure handling

If `mkdir -p` or file open fails, `OrganizeLogger` emits a single Python `logger.warning(...)`, sets its file handle to `None`, and the run continues with `progress_callback` only. `PlanResult.log_path` is set to `None`. The run never fails because logging failed.

## Migration

### Files added

- `fda/organize/reader.py`
- `fda/organize/classifier.py`
- `fda/organize/plan_builder.py`
- `fda/organize/_logger.py`
- `fda/organize/skills/file-summarizer/SKILL.md`
- `fda/organize/skills/document-classifier/SKILL.md`

### Files modified

- `fda/organize/__init__.py` — `organize()` replaces `planner.build_plan(...)` with `reader.read(...)` → `classifier.classify(...)` → `plan_builder.build(...)`. Adds `log_path` parameter (default `None`, resolved to the auto-path). Initializes `OrganizeLogger` and passes it through every stage.
- `fda/organize/models.py` — adds `CatalogEntry`, `Catalog`, `Grouping`, `Groupings`. Adds `log_path` to `PlanResult`.
- `fda/organize/_fs.py` — removes `validate_plan_shape` (its invariant is now structurally guaranteed by PlanBuilder). Removes the corresponding test class.
- `fda/local_worker_agent.py` — propagates `log_path` through the back-compat result dict.
- `fda/orchestrator.py` — appends `**Detailed log:** <path>` to the journal entry written by `_handle_local_organize_request`.

### Files deleted

- `fda/organize/planner.py` — replaced by reader + classifier + plan_builder.
- `fda/organize/prompts.py` — content moves into the two `SKILL.md` files.
- `tests/test_organize_planner.py` — fully replaced; semantics relocated (see test migration table below).

### Test migration

| Today's test class / method | Migrates to | Rationale |
|---|---|---|
| `TestExecRead` (text / binary stub / PDF / byte cap / no-pdftotext) | `tests/test_organize_reader.py` | Reader owns file reading now. `_extract_pdf_text` and `_BINARY_EXTS` move to `reader.py`. Tests come along verbatim. |
| `TestSubmitPlanShape::test_create_dir_only_plan_is_rejected` | `tests/test_organize_plan_builder.py` | The shape rule is structurally guaranteed by PlanBuilder (no orphan `create_dir`s). New test asserts the invariant directly. |
| `TestSubmitPlanShape::test_delete_only_plan_is_accepted` | `tests/test_organize_plan_builder.py` | Junk-only run stays valid — assert PlanBuilder produces a delete-only Plan when Classifier returns no groupings but Reader flagged junk. |
| `TestSubmitPlanShape::test_create_dir_plus_delete_is_accepted` | dropped | Combination is structurally impossible in the new pipeline (PlanBuilder only emits `create_dir` for destinations of actual moves). |
| `TestSubmitPlanIdempotenceAndEmpty` (3 tests) | dropped | Idempotence was about resubmitting `submit_plan`. There is no `submit_plan`; the LLM doesn't "submit," it classifies. The concept doesn't translate. |
| `TestBuildPlanLongExploration` | `tests/test_organize_pipeline.py` | Reframed: a Catalog of 100+ entries should produce a coherent Groupings + Plan. Pipeline-level test with both LLM stages mocked. |
| `TestBuildPlanHappyPath` | `tests/test_organize_pipeline.py` | End-to-end happy path through reader + classifier + plan_builder, with both LLM calls stubbed. |
| `TestBuildPlanValidationRejection` (2 tests) | dropped | Both tests exercised the all-or-nothing-resubmission contract of `submit_plan`. PlanBuilder doesn't resubmit; it validates per op once and drops failures. The surviving semantics ("bad ops dropped, good ops kept; raises when nothing valid remains") are covered by the new PlanBuilder test cases below, not by porting these. |
| `TestBuildPlanMissedSubmit` | dropped | The "model never called submit_plan" failure mode no longer exists; Classifier always returns a value or raises. |

### New test files

`tests/test_organize_reader.py`
- Catalog populated for a mixed dir (text + PDF + binary + junk).
- Per-file failure → `summary_failed=True`, run continues.
- Junk file (`.DS_Store`) flagged in catalog without going to the LLM.
- Files inside `.git` dirs skipped at walk time (never sent to summarizer).
- Parallelism: thread pool runs N files concurrently, results aggregated in deterministic order.
- Migrated `_extract_pdf_text` test cases (text / binary stub / PDF / byte cap / no-pdftotext).
- Per-file timeout exercised (mock a hanging summarizer).

`tests/test_organize_classifier.py`
- Happy path: catalog → groupings JSON parsed correctly.
- Malformed JSON → retry once with stronger system message, then raise.
- User instructions honored (e.g., "by month" produces month-based categories — assert via mock).
- Empty catalog → empty groupings, no failure.
- Junk-only catalog → empty groupings (junk handled by PlanBuilder, not Classifier).

`tests/test_organize_plan_builder.py`
- Groupings → ops (one `create_dir` per unique dest, one `move` per file).
- Two groupings with same destination → merged into one `create_dir`.
- Same file in two groupings → raises `PlanBuilderError` naming the conflicting file.
- File in groupings that doesn't exist on disk → op dropped, drop logged.
- Junk-only catalog → delete-only Plan.
- Empty groupings + empty junk → empty Plan (status=success, nothing to do).
- Per-op `validate_operation` invoked → bad ops dropped + logged; raises only if zero valid ops remain and there was input.

`tests/test_organize_logger.py`
- Default path used when `log_path=None`.
- Custom path used when supplied.
- `log_path=False` disables file logging entirely.
- File creation failure (mocked permission error) → run continues, warning logged, `PlanResult.log_path == None`.
- Event format: timestamp + tag + key=value, with strings containing spaces correctly quoted.

### Tests unchanged

- `tests/test_organize_executor.py`
- `tests/test_organize_verifier.py`
- `tests/test_organize_fs.py` (drop the `validate_plan_shape` cases when that helper goes away)
- `tests/test_organize_models.py` (extend with cases for the new dataclasses)
- `tests/test_local_worker.py` — minor: only the result-shape assertions need updating to include `log_path`.

## Implementation order

Each step lands as a small commit, each with green tests independently. Land in this order so dependencies flow forward:

1. `_logger.py` + `tests/test_organize_logger.py`. No other deps.
2. `models.py` updates: add `CatalogEntry` / `Catalog` / `Grouping` / `Groupings`; add `PlanResult.log_path`. Extend `tests/test_organize_models.py`.
3. `plan_builder.py` + `tests/test_organize_plan_builder.py`. Deterministic — easy to land first among the LLM-adjacent code.
4. `reader.py` + `skills/file-summarizer/SKILL.md` + `tests/test_organize_reader.py`. LLM mocked.
5. `classifier.py` + `skills/document-classifier/SKILL.md` + `tests/test_organize_classifier.py`. LLM mocked.
6. Wire in `__init__.py`. Delete `planner.py` and `prompts.py`. Migrate / drop `tests/test_organize_planner.py` cases. Delete the old test file.
7. `_fs.py` cleanup (remove `validate_plan_shape` and its tests).
8. `local_worker_agent.py` + `orchestrator.py` for log-path surfacing. Update `tests/test_local_worker.py` result-shape assertion.
9. Manual end-to-end on a fresh randomized fixture per the test fixture runbook. Spot-check the log file. Confirm full plan applied (not 5/100).

## Out of scope

- Log retention / rotation / pruning policy.
- A separate JSONL output format.
- Streaming responses from the Classifier (one-shot is sufficient at the catalog scale).
- Concurrency for the Classifier (single call by design).
- Multi-target organize runs in a single invocation.
- Phase B features (rescue, plan persistence, Telegram preview flag, web UI panel).
