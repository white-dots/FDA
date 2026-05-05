# Organize Success Rate — Phase A Implementation Plan

**Status:** Implemented 2026-05-05.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single Claude tool-use loop in `LocalWorkerAgent.organize_files()` with a deterministic three-phase pipeline (planner → executor → verifier) so the same target folder + instructions yields the same set of file moves on every run, with per-move rationale captured for the journal.

**Architecture:** New `fda/organize/` package. Planner is the only Claude call; it emits a structured `Plan`. Executor applies the plan deterministically (no Claude in Phase A). Verifier diffs the filesystem against the plan and cleans empty source folders. `LocalWorkerAgent.organize_files()` becomes a thin wrapper that translates `PlanResult` to today's dict shape. Existing Telegram/Discord/Slack/web call sites are unchanged. Backend is injected via keyword arguments so tests can stub without monkey-patching.

**Tech Stack:** Python 3.12, dataclasses, pytest with `tmp_path`, `unittest.mock.MagicMock` for the Claude backend, project's existing `fda.claude_backend.get_claude_backend` and `fda.config.LOCAL_WORKER_PROJECTS`.

**Spec reference:** `docs/superpowers/specs/2026-05-04-organize-success-rate-design.md` (Phase A scope only — rescue, plan persistence, Telegram `--preview`, and web UI panel are deferred to a separate Phase B plan).

**Rollback marker:** Pre-Phase-A HEAD is commit `76cb960` ("docs: Phase A implementation plan for organize success-rate redesign"). All Phase A code commits land on top of it. To revert Phase A wholesale on an unshared branch: `git reset --hard 76cb960`. To revert non-destructively (e.g., after the work has been pushed or merged): `git revert 76cb960..HEAD`. Optional one-time tag: `git tag pre-phase-a 76cb960`.

**Test runner:** `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` (per project CLAUDE.md). The pre-commit hook runs the same command — commits are blocked on test failures.

**File structure (created/modified):**

| File | Status | Purpose |
|---|---|---|
| `fda/organize/__init__.py` | create | public API: `organize()`, `apply_plan()` |
| `fda/organize/models.py` | create | `Plan`, `Operation`, `OperationKind`, `OperationOutcome`, `PlanResult` |
| `fda/organize/_fs.py` | create | path validation, git-repo detection (both directions), junk rules, deterministic apply primitives |
| `fda/organize/prompts.py` | create | `PLANNER_SYSTEM_PROMPT` |
| `fda/organize/planner.py` | create | `build_plan()` + `PlannerDidNotSubmitError` |
| `fda/organize/executor.py` | create | `execute_plan()` (happy path; no rescue in Phase A) |
| `fda/organize/verifier.py` | create | `verify_plan()` + summary rendering |
| `tests/test_organize_models.py` | create | model dataclass tests |
| `tests/test_organize_fs.py` | create | `_fs` validation + apply primitive tests |
| `tests/test_organize_planner.py` | create | planner happy path + rejection + missed-submit tests |
| `tests/test_organize_executor.py` | create | executor happy path + race + idempotency tests |
| `tests/test_organize_verifier.py` | create | verifier diff + empty-dir cleanup + summary tests |
| `tests/test_organize_pipeline.py` | create | full `organize()` integration test |
| `tests/conftest.py` | modify | add `stub_claude_backend` fixture |
| `fda/local_worker_agent.py` | modify | `organize_files()` becomes wrapper; add `organize_files_preview` and `organize_files_apply`; remove instance-state mutation |
| `fda/orchestrator.py` | modify | `_handle_local_organize_request` task-status mapping + extended journal entry |
| `tests/test_local_worker.py` | modify | back-compat dict shape test + concurrency regression test |

---

## Task 1: Data models

**Files:**
- Create: `fda/organize/__init__.py` (empty for now), `fda/organize/models.py`, `tests/test_organize_models.py`

- [ ] **Step 1.1: Create empty package marker**

```bash
mkdir -p fda/organize
touch fda/organize/__init__.py
```

- [ ] **Step 1.2: Write failing model tests**

Create `tests/test_organize_models.py`:

```python
"""Tests for fda.organize.models — dataclass invariants."""

import pytest
from dataclasses import FrozenInstanceError

from fda.organize.models import (
    OperationKind,
    Operation,
    Plan,
    OperationOutcome,
    PlanResult,
)


def _make_op(kind=OperationKind.MOVE, source="/t/a.txt", destination="/t/b/a.txt"):
    return Operation(kind=kind, source=source, destination=destination, reason="test")


class TestOperationKind:
    def test_values_are_lowercase_strings(self):
        assert OperationKind.CREATE_DIR.value == "create_dir"
        assert OperationKind.MOVE.value == "move"
        assert OperationKind.DELETE.value == "delete"


class TestOperation:
    def test_construct(self):
        op = _make_op()
        assert op.kind == OperationKind.MOVE
        assert op.source == "/t/a.txt"
        assert op.destination == "/t/b/a.txt"
        assert op.reason == "test"

    def test_frozen(self):
        op = _make_op()
        with pytest.raises(FrozenInstanceError):
            op.reason = "changed"  # type: ignore[misc]


class TestPlan:
    def test_construct_with_tuple_operations(self):
        plan = Plan(
            target="/t",
            instructions="sort",
            operations=(_make_op(),),
            grouping_summary="one move",
        )
        assert isinstance(plan.operations, tuple)
        assert len(plan.operations) == 1

    def test_frozen(self):
        plan = Plan(target="/t", instructions="", operations=(), grouping_summary="")
        with pytest.raises(FrozenInstanceError):
            plan.target = "/x"  # type: ignore[misc]


class TestOperationOutcome:
    def test_defaults(self):
        op = _make_op()
        outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
        assert outcome.error is None
        assert outcome.rescue_note is None

    def test_status_values(self):
        op = _make_op()
        for status in ("applied", "rescued", "skipped", "failed"):
            OperationOutcome(operation_index=0, operation=op, status=status)


class TestPlanResult:
    def test_construct(self):
        plan = Plan(target="/t", instructions="", operations=(), grouping_summary="")
        result = PlanResult(
            plan=plan,
            outcomes=(),
            leftover_empty_dirs=(),
            discrepancies=(),
            repos_skipped=(),
            summary="nothing to do",
        )
        assert result.summary == "nothing to do"
        assert isinstance(result.outcomes, tuple)
        assert isinstance(result.leftover_empty_dirs, tuple)
```

- [ ] **Step 1.3: Run tests, confirm they fail with import error**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_models.py -x -q --tb=short
```

Expected: `ModuleNotFoundError: No module named 'fda.organize.models'`.

- [ ] **Step 1.4: Implement models**

Create `fda/organize/models.py`:

```python
"""Immutable data models for the organize pipeline.

Plans flow planner -> executor -> verifier; immutability prevents a
downstream phase from quietly mutating an artifact a previous phase
produced.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class OperationKind(str, Enum):
    CREATE_DIR = "create_dir"
    MOVE = "move"
    DELETE = "delete"


@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    source: str | None
    destination: str | None
    reason: str


@dataclass(frozen=True)
class Plan:
    target: str
    instructions: str
    operations: tuple[Operation, ...]
    grouping_summary: str


OutcomeStatus = Literal["applied", "rescued", "skipped", "failed"]


@dataclass(frozen=True)
class OperationOutcome:
    operation_index: int
    operation: Operation
    status: OutcomeStatus
    error: str | None = None
    rescue_note: str | None = None


@dataclass(frozen=True)
class PlanResult:
    plan: Plan
    outcomes: tuple[OperationOutcome, ...]
    leftover_empty_dirs: tuple[str, ...]
    discrepancies: tuple[str, ...]
    repos_skipped: tuple[str, ...]
    summary: str
```

- [ ] **Step 1.5: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_models.py -x -q --tb=short
```

Expected: all model tests pass; full suite still green.

- [ ] **Step 1.6: Commit**

```bash
git add fda/organize/__init__.py fda/organize/models.py tests/test_organize_models.py
git commit -m "feat(organize): add Plan/Operation/PlanResult dataclasses"
```

---

## Task 2: `_fs.py` — validation primitives

**Files:**
- Create: `fda/organize/_fs.py`, `tests/test_organize_fs.py`

- [ ] **Step 2.1: Write failing tests for path validation + git-repo detection**

Create `tests/test_organize_fs.py`:

```python
"""Tests for fda.organize._fs — validation primitives + apply functions."""

import pytest
from pathlib import Path

from fda.organize import _fs
from fda.organize.models import Operation, OperationKind


@pytest.fixture
def workspace(tmp_path):
    """Build a workspace with a regular file, a git repo, and a junk file."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / ".DS_Store").write_bytes(b"\x00")

    repo = root / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "tracked.py").write_text("# tracked")

    (root / "subdir").mkdir()
    return root


class TestValidateTarget:
    def test_inside_allowed_root(self, workspace):
        result = _fs.validate_target(str(workspace), [workspace.parent])
        assert result == workspace.resolve()

    def test_outside_allowed_root_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not in allowed"):
            _fs.validate_target("/etc", [tmp_path])

    def test_not_a_directory_raises(self, workspace):
        f = workspace / "a.txt"
        with pytest.raises(ValueError, match="not a directory"):
            _fs.validate_target(str(f), [workspace.parent])


class TestIsInsideGitRepo:
    def test_file_in_repo(self, workspace):
        assert _fs.is_inside_git_repo(workspace / "myrepo" / "tracked.py") is True

    def test_file_outside_repo(self, workspace):
        assert _fs.is_inside_git_repo(workspace / "a.txt") is False

    def test_nonexistent_path_uses_parent(self, workspace):
        # parent of a not-yet-created file inside a repo
        path = workspace / "myrepo" / "would_be_here.txt"
        assert _fs.is_inside_git_repo(path) is True


class TestIsJunkFile:
    def test_known_junk(self, workspace):
        assert _fs.is_junk_file(workspace / ".DS_Store") is True

    def test_regular_file(self, workspace):
        assert _fs.is_junk_file(workspace / "a.txt") is False


class TestIsEmptyFile:
    def test_empty(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert _fs.is_empty_file(f) is True

    def test_nonempty(self, tmp_path):
        f = tmp_path / "full.txt"
        f.write_text("hi")
        assert _fs.is_empty_file(f) is False


class TestValidateOperation:
    def test_create_dir_inside_target_ok(self, workspace):
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(workspace / "new"),
            reason="r",
        )
        _fs.validate_operation(op, workspace)  # no raise

    def test_path_must_be_absolute(self, workspace):
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination="new",
            reason="r",
        )
        with pytest.raises(ValueError, match="absolute"):
            _fs.validate_operation(op, workspace)

    def test_destination_outside_target_rejected(self, workspace, tmp_path):
        outside = tmp_path / "elsewhere"
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(outside),
            reason="r",
        )
        with pytest.raises(ValueError, match="outside target"):
            _fs.validate_operation(op, workspace)

    def test_move_source_in_git_repo_rejected(self, workspace):
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "myrepo" / "tracked.py"),
            destination=str(workspace / "moved.py"),
            reason="r",
        )
        with pytest.raises(ValueError, match="git repository"):
            _fs.validate_operation(op, workspace)

    def test_move_destination_in_git_repo_rejected(self, workspace):
        # NEW behavior: today's code only blocks the source side.
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "a.txt"),
            destination=str(workspace / "myrepo" / "a.txt"),
            reason="r",
        )
        with pytest.raises(ValueError, match="git repository"):
            _fs.validate_operation(op, workspace)

    def test_delete_non_junk_rejected(self, workspace):
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(workspace / "a.txt"),
            destination=None,
            reason="r",
        )
        with pytest.raises(ValueError, match="junk|empty"):
            _fs.validate_operation(op, workspace)

    def test_delete_empty_file_ok(self, workspace):
        empty = workspace / "empty.txt"
        empty.write_text("")
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(empty),
            destination=None,
            reason="r",
        )
        _fs.validate_operation(op, workspace)  # no raise

    def test_delete_junk_file_ok(self, workspace):
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(workspace / ".DS_Store"),
            destination=None,
            reason="r",
        )
        _fs.validate_operation(op, workspace)  # no raise
```

- [ ] **Step 2.2: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_fs.py -x -q --tb=short
```

Expected: `ImportError` or `AttributeError` for `_fs` symbols.

- [ ] **Step 2.3: Implement validation primitives**

Create `fda/organize/_fs.py`:

```python
"""Filesystem primitives for the organize pipeline.

Single source of truth for path validation, git-repo detection (both
source and destination), junk-file rules, and the deterministic apply
operations used by the executor.
"""

from pathlib import Path

from fda.organize.models import Operation, OperationKind


JUNK_FILES: frozenset[str] = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini",
    ".Spotlight-V100", ".Trashes", ".fseventsd",
})


def validate_target(path: str, allowed_roots: list[Path]) -> Path:
    """Resolve `path` and confirm it is inside one of `allowed_roots`.

    Returns the resolved Path. Raises ValueError if outside the
    allowed list or not an existing directory.
    """
    resolved = Path(path).expanduser().resolve()
    inside = any(
        resolved == root or resolved.is_relative_to(root)
        for root in (Path(r).resolve() for r in allowed_roots)
    )
    if not inside:
        raise ValueError(
            f"Target path not in allowed list: {resolved}"
        )
    if not resolved.is_dir():
        raise ValueError(f"Target is not a directory: {resolved}")
    return resolved


def is_inside_git_repo(path: Path) -> bool:
    """Walk up from `path` looking for a `.git` directory.

    Works for non-existent paths (uses the deepest existing ancestor).
    """
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_file():
        current = current.parent
    while current != current.parent:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def is_junk_file(path: Path) -> bool:
    return path.name in JUNK_FILES


def is_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


def _require_absolute(label: str, value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {value!r}")
    return p


def _require_inside_target(label: str, p: Path, target: Path) -> None:
    if not (p == target or p.is_relative_to(target)):
        raise ValueError(f"{label} is outside target {target}: {p}")


def validate_operation(op: Operation, target: Path) -> None:
    """Validate a single Operation against `_fs` rules.

    Raises ValueError with a human-readable message on rejection.
    """
    if op.kind == OperationKind.CREATE_DIR:
        if op.destination is None:
            raise ValueError("CREATE_DIR requires destination")
        dest = _require_absolute("destination", op.destination)
        _require_inside_target("destination", dest, target)
        if is_inside_git_repo(dest):
            raise ValueError(f"destination is inside a git repository: {dest}")
        return

    if op.kind == OperationKind.MOVE:
        if op.source is None or op.destination is None:
            raise ValueError("MOVE requires both source and destination")
        src = _require_absolute("source", op.source)
        dest = _require_absolute("destination", op.destination)
        _require_inside_target("source", src, target)
        _require_inside_target("destination", dest, target)
        if is_inside_git_repo(src):
            raise ValueError(f"source is inside a git repository: {src}")
        if is_inside_git_repo(dest):
            raise ValueError(f"destination is inside a git repository: {dest}")
        return

    if op.kind == OperationKind.DELETE:
        if op.source is None:
            raise ValueError("DELETE requires source")
        src = _require_absolute("source", op.source)
        _require_inside_target("source", src, target)
        if is_inside_git_repo(src):
            raise ValueError(f"source is inside a git repository: {src}")
        if not (is_junk_file(src) or is_empty_file(src)):
            raise ValueError(
                f"DELETE only allowed for junk or empty files: {src.name}"
            )
        return

    raise ValueError(f"Unknown operation kind: {op.kind}")
```

- [ ] **Step 2.4: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_fs.py -x -q --tb=short
```

Expected: all `_fs` validation tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add fda/organize/_fs.py tests/test_organize_fs.py
git commit -m "feat(organize): add _fs validation primitives (block git repos both directions)"
```

---

## Task 3: `_fs.py` — apply primitives

**Files:**
- Modify: `fda/organize/_fs.py`, `tests/test_organize_fs.py`

- [ ] **Step 3.1: Append failing tests for apply primitives**

Append to `tests/test_organize_fs.py`:

```python
class TestApplyCreateDir:
    def test_creates_dir(self, workspace):
        dest = workspace / "new" / "nested"
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(dest),
            reason="r",
        )
        _fs.apply_create_dir(op, workspace)
        assert dest.is_dir()

    def test_existing_dir_is_idempotent(self, workspace):
        dest = workspace / "subdir"
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(dest),
            reason="r",
        )
        _fs.apply_create_dir(op, workspace)  # no raise
        assert dest.is_dir()


class TestApplyMove:
    def test_moves_file(self, workspace):
        src = workspace / "a.txt"
        dest = workspace / "subdir" / "a.txt"
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest),
            reason="r",
        )
        _fs.apply_move(op, workspace)
        assert not src.exists()
        assert dest.read_text() == "hello"

    def test_creates_parent_dirs(self, workspace):
        src = workspace / "a.txt"
        dest = workspace / "deeply" / "nested" / "a.txt"
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest),
            reason="r",
        )
        _fs.apply_move(op, workspace)
        assert dest.read_text() == "hello"

    def test_destination_collision_raises(self, workspace):
        src = workspace / "a.txt"
        dest = workspace / "subdir" / "a.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("existing")
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest),
            reason="r",
        )
        with pytest.raises(FileExistsError):
            _fs.apply_move(op, workspace)
        # Source untouched
        assert src.read_text() == "hello"

    def test_idempotent_when_already_moved_returns_skipped(self, workspace):
        # Source gone, destination has same-name file -> return "skipped".
        src = workspace / "a.txt"
        dest = workspace / "subdir" / "a.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("hello")
        src.unlink()
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest),
            reason="r",
        )
        # apply_move returns a status string: "applied" or "skipped".
        assert _fs.apply_move(op, workspace) == "skipped"
        assert dest.read_text() == "hello"

    def test_returns_applied_on_first_move(self, workspace):
        src = workspace / "a.txt"
        dest = workspace / "subdir" / "a.txt"
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest),
            reason="r",
        )
        assert _fs.apply_move(op, workspace) == "applied"


class TestApplyDelete:
    def test_deletes_junk(self, workspace):
        target = workspace / ".DS_Store"
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(target),
            destination=None,
            reason="r",
        )
        _fs.apply_delete(op, workspace)
        assert not target.exists()

    def test_idempotent_when_already_gone(self, workspace):
        target = workspace / "ghost.txt"
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(target),
            destination=None,
            reason="r",
        )
        _fs.apply_delete(op, workspace)  # no raise
```

- [ ] **Step 3.2: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_fs.py -x -q --tb=short
```

Expected: `AttributeError: module 'fda.organize._fs' has no attribute 'apply_create_dir'`.

- [ ] **Step 3.3: Implement apply primitives**

Append to `fda/organize/_fs.py`:

```python
import shutil


def apply_create_dir(op: Operation, target: Path) -> None:
    """Idempotent mkdir -p. Re-validates before applying."""
    validate_operation(op, target)
    Path(op.destination).mkdir(parents=True, exist_ok=True)


def apply_move(op: Operation, target: Path) -> str:
    """Move source -> destination.

    Returns "applied" on a fresh move, "skipped" if source is already
    missing AND destination already holds a file with the same name
    (idempotent re-run). Raises FileExistsError on collision (source
    still present), FileNotFoundError if neither source nor destination
    is present, OSError on permission errors.
    """
    validate_operation(op, target)
    src = Path(op.source)
    dest = Path(op.destination)

    if not src.exists():
        if dest.exists() and dest.is_file():
            return "skipped"
        raise FileNotFoundError(f"source missing: {src}")

    if dest.exists():
        raise FileExistsError(f"destination already exists: {dest}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return "applied"


def apply_delete(op: Operation, target: Path) -> None:
    """Delete source if it exists; no-op if already gone."""
    validate_operation(op, target)
    src = Path(op.source)
    if not src.exists():
        return
    src.unlink()
```

- [ ] **Step 3.4: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_fs.py -x -q --tb=short
```

Expected: all `_fs` tests (validation + apply) pass.

- [ ] **Step 3.5: Commit**

```bash
git add fda/organize/_fs.py tests/test_organize_fs.py
git commit -m "feat(organize): add _fs apply primitives (create_dir, move, delete)"
```

---

## Task 4: Planner — prompts, `submit_plan` validation, `PlannerDidNotSubmitError`

**Files:**
- Create: `fda/organize/prompts.py`, `fda/organize/planner.py`, `tests/test_organize_planner.py`
- Modify: `tests/conftest.py` (add `stub_claude_backend` fixture)

- [ ] **Step 4.1: Add stub backend fixture to conftest**

Append to `tests/conftest.py`:

```python
# ---------------------------------------------------------------------------
# Stub Claude backend for organize planner tests
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_claude_backend():
    """Build a backend whose complete_with_tools replays scripted tool calls.

    Usage:
        backend = stub_claude_backend(
            [("submit_plan", {"operations": [...], "grouping_summary": "..."})],
        )
        # Each positional arg is one iteration's list of (tool_name, tool_input).

    The stub calls the tool_executor for each scripted tool call in order
    and returns "" as final assistant text. Iteration cap and timeout
    arguments from the real backend are ignored.
    """
    from unittest.mock import MagicMock

    def _build(*iterations):
        backend = MagicMock()

        def fake_complete_with_tools(*, tool_executor, **_kwargs):
            for iteration in iterations:
                for name, tinput in iteration:
                    tool_executor(name, tinput)
            return ""

        backend.complete_with_tools.side_effect = fake_complete_with_tools
        return backend

    return _build
```

- [ ] **Step 4.2: Write failing planner tests**

Create `tests/test_organize_planner.py`:

```python
"""Tests for fda.organize.planner — happy path, validation rejection, missed submit."""

import pytest
from pathlib import Path

from fda.organize import planner
from fda.organize.models import OperationKind


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / "b.txt").write_text("world")
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return root


def _valid_submit(workspace):
    return ("submit_plan", {
        "operations": [
            {
                "kind": "create_dir",
                "destination": str(workspace / "Texts"),
                "reason": "group text files",
            },
            {
                "kind": "move",
                "source": str(workspace / "a.txt"),
                "destination": str(workspace / "Texts" / "a.txt"),
                "reason": "text file",
            },
        ],
        "grouping_summary": "grouped texts",
    })


class TestBuildPlanHappyPath:
    def test_returns_plan_when_submit_called(self, workspace, stub_claude_backend):
        backend = stub_claude_backend([_valid_submit(workspace)])
        plan = planner.build_plan(workspace, "sort", backend=backend)
        assert plan.target == str(workspace)
        assert plan.instructions == "sort"
        assert plan.grouping_summary == "grouped texts"
        assert len(plan.operations) == 2
        assert plan.operations[0].kind == OperationKind.CREATE_DIR
        assert plan.operations[1].kind == OperationKind.MOVE
        assert plan.operations[1].reason == "text file"


class TestBuildPlanValidationRejection:
    def test_invalid_then_valid_submission_succeeds(self, workspace, stub_claude_backend):
        bad = ("submit_plan", {
            "operations": [
                {
                    "kind": "delete",
                    "source": str(workspace / "a.txt"),  # not junk, not empty
                    "reason": "want to delete",
                },
            ],
            "grouping_summary": "bad",
        })
        good = _valid_submit(workspace)
        backend = stub_claude_backend([bad], [good])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert len(plan.operations) == 2  # the second submission was accepted

    def test_all_or_nothing_on_partial_invalid(self, workspace, stub_claude_backend):
        # One valid op + one invalid op in the same submission -> entire
        # submission rejected; planner can resubmit.
        mixed = ("submit_plan", {
            "operations": [
                {
                    "kind": "create_dir",
                    "destination": str(workspace / "Texts"),
                    "reason": "ok",
                },
                {
                    "kind": "move",
                    "source": str(workspace / "a.txt"),
                    "destination": str(workspace / "repo" / "a.txt"),  # into git repo
                    "reason": "bad",
                },
            ],
            "grouping_summary": "mixed",
        })
        good = _valid_submit(workspace)
        backend = stub_claude_backend([mixed], [good])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert plan.grouping_summary == "grouped texts"


class TestBuildPlanMissedSubmit:
    def test_raises_when_submit_never_called(self, workspace, stub_claude_backend):
        # Stub does nothing; the planner's _submitted flag stays False.
        backend = stub_claude_backend([])  # zero iterations
        with pytest.raises(planner.PlannerDidNotSubmitError):
            planner.build_plan(workspace, "", backend=backend)
```

- [ ] **Step 4.3: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_planner.py -x -q --tb=short
```

Expected: `ModuleNotFoundError: No module named 'fda.organize.planner'`.

- [ ] **Step 4.4: Implement prompts.py**

Create `fda/organize/prompts.py`:

```python
"""System prompts for the organize pipeline."""

PLANNER_SYSTEM_PROMPT = """You are the file organization planner for FDA.
Your job is to scan a target directory, understand what each file is and how
files relate to each other, and produce a complete organization PLAN.

You have READ-ONLY tools:
- list_directory: see what files and folders exist (with size and modified date)
- get_file_info: detailed metadata (size, dates, MIME, git-repo status)
- read_file: read a text file's contents to understand its purpose

You produce the plan via:
- submit_plan(operations, grouping_summary): submit the final plan as a list
  of operations. You CANNOT perform any moves, deletes, or directory creates
  yourself. Another component executes your plan.

OPERATIONS:
- {"kind": "create_dir", "destination": "<absolute path>", "reason": "..."}
- {"kind": "move", "source": "<absolute path>", "destination": "<absolute path>", "reason": "..."}
- {"kind": "delete", "source": "<absolute path>", "reason": "..."}

RULES:
- All paths must be ABSOLUTE and inside the target directory.
- NEVER touch a file or directory inside a git repository (any directory
  containing a .git folder is a repo — leave it alone, on both source and
  destination sides).
- DELETE is only allowed for known junk files (.DS_Store, Thumbs.db,
  desktop.ini) or files that are completely empty.
- Every operation must include a short `reason` (≤300 chars) explaining
  why this file belongs in that group. The reasons appear in the user's
  journal, so write them for a human reader.
- submit_plan validation is ALL-OR-NOTHING: if any operation is rejected,
  the entire submission is rejected and you must fix and resubmit.

ORGANIZATION PRINCIPLES:
- Group by purpose/project first, then by type within groups.
- Keep small, self-contained projects together.
- Common top-level folders: Projects/, Documents/, Archives/, Scripts/.
- Preserve the user's filenames; do not rename.
- When in doubt, leave a file where it is (don't include it in any move).

After exploring, call submit_plan exactly once with a valid plan.
"""
```

- [ ] **Step 4.5: Implement planner.py**

Create `fda/organize/planner.py`:

```python
"""Planner phase: runs Claude with read-only tools and a terminal submit_plan
tool. Produces a validated Plan; never performs side effects."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from fda.organize import _fs
from fda.organize.models import Operation, OperationKind, Plan
from fda.organize.prompts import PLANNER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


PLANNER_MAX_ITERATIONS = 20


class PlannerDidNotSubmitError(Exception):
    """Raised when the planner loop ends without a successful submit_plan."""


_PLANNER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_directory",
        "description": "List entries in a directory with size and modified date.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_file_info",
        "description": "Get size, dates, MIME type, and git-repo status for a path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file's contents (use sparingly).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "submit_plan",
        "description": (
            "Submit the final organization plan. Call exactly once with a "
            "complete, validated set of operations. Validation is all-or-nothing."
        ),
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
    },
]


def _parse_operations(raw_ops: list[dict[str, Any]]) -> list[Operation]:
    parsed: list[Operation] = []
    for raw in raw_ops:
        kind = OperationKind(raw["kind"])
        parsed.append(Operation(
            kind=kind,
            source=raw.get("source"),
            destination=raw.get("destination"),
            reason=raw.get("reason", ""),
        ))
    return parsed


def build_plan(
    target: Path,
    instructions: str,
    *,
    backend,
    progress_callback: Callable[[str], None] | None = None,
) -> Plan:
    """Run the planner Claude loop and return a validated Plan.

    Raises PlannerDidNotSubmitError if the loop ends without a successful
    submit_plan call (e.g., iteration cap reached without submission).
    """
    state: dict[str, Any] = {"submitted": False, "plan": None}

    def _emit(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(f"planner: {msg}")
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)

    def tool_executor(name: str, tinput: dict[str, Any]) -> str:
        if name == "list_directory":
            return _exec_list(target, tinput)
        if name == "get_file_info":
            return _exec_info(target, tinput)
        if name == "read_file":
            return _exec_read(target, tinput)
        if name == "submit_plan":
            return _exec_submit(target, tinput, state, instructions, _emit)
        return f"unknown tool: {name}"

    _emit(f"starting on {target}")
    backend.complete_with_tools(
        system=PLANNER_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"TARGET DIRECTORY: {target}\n\n"
                f"INSTRUCTIONS: {instructions or '(organize the files in this directory)'}\n\n"
                "Explore with the read-only tools, then call submit_plan exactly "
                "once with a complete, valid plan."
            ),
        }],
        tools=_PLANNER_TOOLS,
        tool_executor=tool_executor,
        max_iterations=PLANNER_MAX_ITERATIONS,
    )

    if not state["submitted"]:
        raise PlannerDidNotSubmitError(
            "Planner finished without successfully calling submit_plan"
        )

    return state["plan"]


# ---- read-only tool executors ------------------------------------------------

def _exec_list(target: Path, tinput: dict[str, Any]) -> str:
    rel = tinput.get("path", ".")
    full = (target / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    if not (full == target or full.is_relative_to(target)):
        return "error: path outside target"
    if not full.is_dir():
        return f"error: not a directory: {rel}"
    lines: list[str] = []
    for entry in sorted(full.iterdir()):
        if entry.name.startswith(".") and entry.name != ".git":
            continue
        suffix = "/" if entry.is_dir() else ""
        try:
            size = entry.stat().st_size
            lines.append(f"{entry.name}{suffix}\t{size} bytes")
        except OSError:
            lines.append(f"{entry.name}{suffix}\t(stat failed)")
    return "\n".join(lines) or "(empty)"


def _exec_info(target: Path, tinput: dict[str, Any]) -> str:
    p = Path(tinput.get("path", ""))
    if not p.is_absolute():
        p = (target / p).resolve()
    if not (p == target or p.is_relative_to(target)):
        return "error: path outside target"
    if not p.exists():
        return "error: not found"
    try:
        st = p.stat()
    except OSError as e:
        return f"error: {e}"
    info = {
        "name": p.name,
        "type": "directory" if p.is_dir() else "file",
        "size_bytes": st.st_size,
        "in_git_repo": _fs.is_inside_git_repo(p),
    }
    if p.is_dir():
        info["is_git_repo"] = (p / ".git").exists()
    return json.dumps(info)


def _exec_read(target: Path, tinput: dict[str, Any]) -> str:
    p = Path(tinput.get("path", ""))
    if not p.is_absolute():
        p = (target / p).resolve()
    if not (p == target or p.is_relative_to(target)):
        return "error: path outside target"
    if not p.is_file():
        return "error: not a file"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"error: {e}"
    return text[:10000]


# ---- submit_plan executor ----------------------------------------------------

def _exec_submit(
    target: Path,
    tinput: dict[str, Any],
    state: dict[str, Any],
    instructions: str,
    emit: Callable[[str], None],
) -> str:
    raw_ops = tinput.get("operations", [])
    grouping = tinput.get("grouping_summary", "")
    try:
        operations = _parse_operations(raw_ops)
    except (KeyError, ValueError) as e:
        return f"error: malformed operation: {e}"

    rejections: list[str] = []
    for idx, op in enumerate(operations):
        try:
            _fs.validate_operation(op, target)
        except ValueError as e:
            rejections.append(f"  [{idx}] {op.kind.value}: {e}")

    if rejections:
        emit(f"submit_plan rejected: {len(rejections)} invalid op(s)")
        return (
            "submit_plan rejected (validation is all-or-nothing). "
            "Fix every rejected op and resubmit:\n" + "\n".join(rejections)
        )

    state["submitted"] = True
    state["plan"] = Plan(
        target=str(target),
        instructions=instructions,
        operations=tuple(operations),
        grouping_summary=grouping,
    )
    emit(f"submit_plan accepted: {len(operations)} ops")
    return f"plan accepted ({len(operations)} operations)"
```

- [ ] **Step 4.6: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_planner.py -x -q --tb=short
```

Expected: planner happy path + rejection retry + missed submit tests pass.

- [ ] **Step 4.7: Commit**

```bash
git add fda/organize/prompts.py fda/organize/planner.py tests/test_organize_planner.py tests/conftest.py
git commit -m "feat(organize): add planner with submit_plan validation"
```

---

## Task 5: Executor (happy path only)

**Files:**
- Create: `fda/organize/executor.py`, `tests/test_organize_executor.py`

- [ ] **Step 5.1: Write failing executor tests**

Create `tests/test_organize_executor.py`:

```python
"""Tests for fda.organize.executor — Phase A happy path (no rescue)."""

import pytest
from pathlib import Path

from fda.organize import executor
from fda.organize.models import Operation, OperationKind, Plan


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


def _plan(workspace, *operations):
    return Plan(
        target=str(workspace),
        instructions="",
        operations=tuple(operations),
        grouping_summary="",
    )


def _move(workspace, src_name, dest_rel, reason="r"):
    return Operation(
        kind=OperationKind.MOVE,
        source=str(workspace / src_name),
        destination=str(workspace / dest_rel),
        reason=reason,
    )


def _create_dir(workspace, dest_rel, reason="r"):
    return Operation(
        kind=OperationKind.CREATE_DIR,
        source=None,
        destination=str(workspace / dest_rel),
        reason=reason,
    )


def _delete(workspace, src_name, reason="r"):
    return Operation(
        kind=OperationKind.DELETE,
        source=str(workspace / src_name),
        destination=None,
        reason=reason,
    )


class TestHappyPath:
    def test_all_ops_apply(self, workspace):
        plan = _plan(
            workspace,
            _create_dir(workspace, "Texts"),
            _move(workspace, "a.txt", "Texts/a.txt"),
            _move(workspace, "b.txt", "Texts/b.txt"),
            _delete(workspace, ".DS_Store"),
        )
        outcomes = executor.execute_plan(plan)
        assert all(o.status == "applied" for o in outcomes)
        assert (workspace / "Texts" / "a.txt").exists()
        assert (workspace / "Texts" / "b.txt").exists()
        assert not (workspace / ".DS_Store").exists()

    def test_outcome_indexes_match_plan_order(self, workspace):
        # Plan order: move, create_dir, delete (NOT execution order)
        plan = _plan(
            workspace,
            _move(workspace, "a.txt", "Texts/a.txt"),  # index 0
            _create_dir(workspace, "Texts"),           # index 1
            _delete(workspace, ".DS_Store"),           # index 2
        )
        outcomes = executor.execute_plan(plan)
        # Outcomes are returned in the original plan order, regardless of
        # the executor's internal CREATE_DIR -> MOVE -> DELETE reordering.
        assert [o.operation_index for o in outcomes] == [0, 1, 2]
        assert outcomes[0].operation.kind == OperationKind.MOVE
        assert outcomes[1].operation.kind == OperationKind.CREATE_DIR


class TestFailureSemantics:
    def test_destination_collision_records_failed(self, workspace):
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("collision")
        plan = _plan(workspace, _move(workspace, "a.txt", "Texts/a.txt"))
        outcomes = executor.execute_plan(plan)
        assert outcomes[0].status == "failed"
        assert "exists" in outcomes[0].error.lower()
        # Source untouched on failure
        assert (workspace / "a.txt").read_text() == "aaa"

    def test_filesystem_race_into_git_repo(self, workspace):
        # Simulate: planner planned a move, but a git repo appeared at the
        # destination's parent before execution.
        (workspace / "subdir").mkdir()
        plan = _plan(workspace, _move(workspace, "a.txt", "subdir/a.txt"))
        # Convert subdir into a repo after planning, before executing.
        (workspace / "subdir" / ".git").mkdir()
        outcomes = executor.execute_plan(plan)
        assert outcomes[0].status == "failed"
        assert "git" in outcomes[0].error.lower()


class TestIdempotency:
    def test_rerun_same_plan(self, workspace):
        plan = _plan(
            workspace,
            _create_dir(workspace, "Texts"),
            _move(workspace, "a.txt", "Texts/a.txt"),
            _delete(workspace, ".DS_Store"),
        )
        first = executor.execute_plan(plan)
        assert all(o.status == "applied" for o in first)
        second = executor.execute_plan(plan)
        # Second run: create_dir is no-op ("applied"), move is "skipped"
        # (source gone, destination present), delete is no-op ("applied").
        statuses = {o.operation.kind: o.status for o in second}
        assert statuses[OperationKind.CREATE_DIR] == "applied"
        assert statuses[OperationKind.MOVE] == "skipped"
        assert statuses[OperationKind.DELETE] == "applied"
```

- [ ] **Step 5.2: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_executor.py -x -q --tb=short
```

Expected: `ModuleNotFoundError: No module named 'fda.organize.executor'`.

- [ ] **Step 5.3: Implement executor**

Create `fda/organize/executor.py`:

```python
"""Executor phase: deterministically apply a Plan.

Phase A: happy path only — failures are recorded as `failed` outcomes.
Phase B will add a Claude rescue call for failures.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fda.organize import _fs
from fda.organize.models import (
    Operation,
    OperationKind,
    OperationOutcome,
    Plan,
)

logger = logging.getLogger(__name__)


def execute_plan(
    plan: Plan,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[OperationOutcome, ...]:
    """Apply `plan` to the filesystem deterministically.

    Operations are executed in CREATE_DIR -> MOVE -> DELETE order for
    safety, but each OperationOutcome.operation_index records the original
    index in `plan.operations` so callers can map outcomes back to plan
    order. Returned outcomes are sorted by operation_index.
    """
    target = Path(plan.target)
    indexed = list(enumerate(plan.operations))

    def order_key(item: tuple[int, Operation]) -> int:
        kind = item[1].kind
        if kind == OperationKind.CREATE_DIR:
            return 0
        if kind == OperationKind.MOVE:
            return 1
        return 2

    sorted_for_exec = sorted(indexed, key=order_key)
    outcomes_by_index: dict[int, OperationOutcome] = {}
    total = len(plan.operations)
    applied = 0

    def _emit(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(f"executor: {msg}")
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)

    for original_index, op in sorted_for_exec:
        try:
            status = _apply_one(op, target)
        except (ValueError, FileExistsError, FileNotFoundError, OSError) as e:
            outcomes_by_index[original_index] = OperationOutcome(
                operation_index=original_index,
                operation=op,
                status="failed",
                error=str(e),
            )
            _emit(f"failed {applied}/{total} ({e})")
            continue
        applied += 1
        outcomes_by_index[original_index] = OperationOutcome(
            operation_index=original_index,
            operation=op,
            status=status,
        )
        _emit(f"{status} {applied}/{total}")

    return tuple(outcomes_by_index[i] for i in range(total))


def _apply_one(op: Operation, target: Path) -> str:
    """Apply a single operation. Returns its outcome status string
    ("applied" or "skipped") or raises a filesystem-level exception."""
    if op.kind == OperationKind.CREATE_DIR:
        _fs.apply_create_dir(op, target)
        return "applied"
    if op.kind == OperationKind.MOVE:
        return _fs.apply_move(op, target)
    if op.kind == OperationKind.DELETE:
        _fs.apply_delete(op, target)
        return "applied"
    raise ValueError(f"unknown operation kind: {op.kind}")
```

- [ ] **Step 5.4: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_executor.py -x -q --tb=short
```

Expected: all executor tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add fda/organize/executor.py tests/test_organize_executor.py
git commit -m "feat(organize): add deterministic executor (Phase A, no rescue)"
```

---

## Task 6: Verifier — diff, empty-dir cleanup, summary rendering

**Files:**
- Create: `fda/organize/verifier.py`, `tests/test_organize_verifier.py`

- [ ] **Step 6.1: Write failing verifier tests**

Create `tests/test_organize_verifier.py`:

```python
"""Tests for fda.organize.verifier — diff + empty-dir cleanup + summary."""

import pytest
from pathlib import Path

from fda.organize import verifier
from fda.organize.models import (
    Operation,
    OperationKind,
    OperationOutcome,
    Plan,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _plan(workspace, *ops, summary=""):
    return Plan(
        target=str(workspace),
        instructions="",
        operations=tuple(ops),
        grouping_summary=summary,
    )


def _outcome(idx, op, status="applied", error=None):
    return OperationOutcome(
        operation_index=idx, operation=op, status=status, error=error,
    )


class TestNoDiscrepancies:
    def test_all_applied_filesystem_matches(self, workspace):
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("a")
        op_dir = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(workspace / "Texts"),
            reason="r",
        )
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "old" / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_dir, op_move)
        outcomes = (_outcome(0, op_dir), _outcome(1, op_move))
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert result.discrepancies == ()


class TestDiscrepancies:
    def test_applied_move_but_destination_missing(self, workspace):
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        # Marked applied but the file doesn't exist anywhere.
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert any("a.txt" in d for d in result.discrepancies)


class TestEmptyDirCleanup:
    def test_source_parent_emptied_by_plan_is_cleaned(self, workspace):
        old = workspace / "old"
        old.mkdir()
        # File was already moved out before verifier sees it.
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(old / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("a")
        plan = _plan(workspace, op_move)
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert str(old) in result.leftover_empty_dirs
        assert not old.exists()

    def test_source_parent_with_unrelated_files_not_cleaned(self, workspace):
        old = workspace / "old"
        old.mkdir()
        (old / "unrelated.txt").write_text("keep me")
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(old / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert str(old) not in result.leftover_empty_dirs
        assert old.exists()

    def test_source_parent_in_git_repo_not_cleaned(self, workspace):
        repo = workspace / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        sub = repo / "sub"
        sub.mkdir()
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(sub / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        # Even if verifier wouldn't normally see this op (planner would
        # reject it), guard the cleanup loop against it anyway.
        outcomes = (_outcome(0, op_move, status="failed", error="git repo"),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert str(sub) not in result.leftover_empty_dirs
        assert sub.exists()


class TestSummaryRendering:
    def test_summary_includes_grouping_and_reasons(self, workspace):
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("a")
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "old" / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="grouped with other text files",
        )
        plan = _plan(workspace, op_move, summary="Two groups")
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert "grouped with other text files" in result.summary
        assert "a.txt" in result.summary

    def test_summary_lists_failures(self, workspace):
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        outcomes = (_outcome(0, op_move, status="failed", error="permission denied"),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert "Couldn't" in result.summary or "couldn't" in result.summary.lower()
        assert "permission denied" in result.summary
```

- [ ] **Step 6.2: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_verifier.py -x -q --tb=short
```

Expected: `ModuleNotFoundError: No module named 'fda.organize.verifier'`.

- [ ] **Step 6.3: Implement verifier**

Create `fda/organize/verifier.py`:

```python
"""Verifier phase: diff filesystem against the executed plan, clean empty
source directories the plan itself emptied, and render a human-readable
summary for the journal/UI."""

from __future__ import annotations

import logging
from pathlib import Path
from collections import defaultdict

from fda.organize import _fs
from fda.organize.models import (
    Operation,
    OperationKind,
    OperationOutcome,
    Plan,
    PlanResult,
)

logger = logging.getLogger(__name__)


def verify_plan(
    plan: Plan,
    outcomes: tuple[OperationOutcome, ...],
    target: Path,
) -> PlanResult:
    discrepancies = _find_discrepancies(outcomes)
    leftover_empty_dirs = _clean_empty_source_dirs(plan, outcomes, target)
    summary = _render_summary(plan, outcomes, leftover_empty_dirs)
    return PlanResult(
        plan=plan,
        outcomes=outcomes,
        leftover_empty_dirs=tuple(leftover_empty_dirs),
        discrepancies=tuple(discrepancies),
        repos_skipped=(),  # populated when planner can report this; left empty for Phase A
        summary=summary,
    )


def _find_discrepancies(outcomes: tuple[OperationOutcome, ...]) -> list[str]:
    """Check that filesystem reality matches every outcome the executor
    considers complete (applied, rescued, or skipped via idempotent re-run)."""
    issues: list[str] = []
    for outcome in outcomes:
        if outcome.status not in ("applied", "rescued", "skipped"):
            continue
        op = outcome.operation
        if op.kind == OperationKind.CREATE_DIR:
            if not Path(op.destination).is_dir():
                issues.append(f"create_dir marked applied but missing: {op.destination}")
        elif op.kind == OperationKind.MOVE:
            if not Path(op.destination).exists():
                issues.append(
                    f"move marked applied but destination missing: "
                    f"{Path(op.source).name} -> {op.destination}"
                )
        elif op.kind == OperationKind.DELETE:
            if Path(op.source).exists():
                issues.append(f"delete marked applied but file still present: {op.source}")
    return issues


def _clean_empty_source_dirs(
    plan: Plan,
    outcomes: tuple[OperationOutcome, ...],
    target: Path,
) -> list[str]:
    """Remove directories that the plan itself emptied.

    Walks the source-parents of every MOVE op (whether applied or not),
    and rmdirs any that are now empty AND inside `target` AND not a
    git repo. Each removed dir is recorded.
    """
    candidates: set[Path] = set()
    for op in plan.operations:
        if op.kind != OperationKind.MOVE or op.source is None:
            continue
        parent = Path(op.source).parent
        if parent == target:
            continue
        candidates.add(parent)

    cleaned: list[str] = []
    for parent in sorted(candidates, key=lambda p: -len(p.parts)):
        if not parent.exists() or not parent.is_dir():
            continue
        if not parent.is_relative_to(target):
            continue
        if _fs.is_inside_git_repo(parent) or (parent / ".git").exists():
            continue
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
                cleaned.append(str(parent))
        except OSError as e:
            logger.debug("could not rmdir %s: %s", parent, e)
    return cleaned


def _render_summary(
    plan: Plan,
    outcomes: tuple[OperationOutcome, ...],
    leftover_empty_dirs: list[str],
) -> str:
    moves_by_dest: dict[str, list[OperationOutcome]] = defaultdict(list)
    deletes: list[OperationOutcome] = []
    failures: list[OperationOutcome] = []

    for outcome in outcomes:
        op = outcome.operation
        if outcome.status == "failed":
            failures.append(outcome)
            continue
        if op.kind == OperationKind.MOVE:
            dest_dir = str(Path(op.destination).parent)
            moves_by_dest[dest_dir].append(outcome)
        elif op.kind == OperationKind.DELETE:
            deletes.append(outcome)

    lines: list[str] = []
    if plan.grouping_summary:
        lines.append(plan.grouping_summary)
        lines.append("")

    total_moves = sum(len(v) for v in moves_by_dest.values())
    if total_moves:
        lines.append(
            f"Organized {total_moves} files into {len(moves_by_dest)} groups:"
        )
        lines.append("")
        for dest_dir in sorted(moves_by_dest):
            lines.append(f"📁 {dest_dir}/")
            for outcome in moves_by_dest[dest_dir]:
                op = outcome.operation
                fname = Path(op.source).name
                lines.append(f"   - {fname} — {op.reason}")
            lines.append("")

    if deletes:
        lines.append(f"Deleted {len(deletes)} junk file(s):")
        for outcome in deletes:
            lines.append(f"   - {Path(outcome.operation.source).name}")
        lines.append("")

    if leftover_empty_dirs:
        lines.append(f"Cleaned {len(leftover_empty_dirs)} empty source folder(s):")
        for d in leftover_empty_dirs:
            lines.append(f"   - {d}")
        lines.append("")

    if failures:
        lines.append(f"Couldn't complete {len(failures)} operation(s):")
        for outcome in failures:
            op = outcome.operation
            label = op.source or op.destination or "?"
            lines.append(f"   - {Path(label).name}: {outcome.error}")
        lines.append("")

    return "\n".join(lines).strip() or "Nothing to organize."
```

- [ ] **Step 6.4: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_verifier.py -x -q --tb=short
```

Expected: all verifier tests pass.

- [ ] **Step 6.5: Commit**

```bash
git add fda/organize/verifier.py tests/test_organize_verifier.py
git commit -m "feat(organize): add verifier (diff + empty-dir cleanup + summary)"
```

---

## Task 7: Public API — `organize()` and `apply_plan()`

**Files:**
- Modify: `fda/organize/__init__.py`
- Create: `tests/test_organize_pipeline.py`

- [ ] **Step 7.1: Write failing pipeline tests**

Create `tests/test_organize_pipeline.py`:

```python
"""Integration tests for fda.organize.organize() — full planner+executor+verifier."""

import pytest
from pathlib import Path

from fda.organize import organize, apply_plan
from fda.organize.models import Plan, PlanResult


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


def _planner_script(workspace):
    return [("submit_plan", {
        "operations": [
            {
                "kind": "create_dir",
                "destination": str(workspace / "Texts"),
                "reason": "group text files",
            },
            {
                "kind": "move",
                "source": str(workspace / "a.txt"),
                "destination": str(workspace / "Texts" / "a.txt"),
                "reason": "text file",
            },
            {
                "kind": "move",
                "source": str(workspace / "b.txt"),
                "destination": str(workspace / "Texts" / "b.txt"),
                "reason": "text file",
            },
            {
                "kind": "delete",
                "source": str(workspace / ".DS_Store"),
                "reason": "macOS junk",
            },
        ],
        "grouping_summary": "All texts grouped",
    })]


class TestOrganize:
    def test_full_pipeline_executes_plan(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        result = organize(
            str(workspace),
            "sort",
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert isinstance(result, PlanResult)
        assert (workspace / "Texts" / "a.txt").exists()
        assert (workspace / "Texts" / "b.txt").exists()
        assert not (workspace / ".DS_Store").exists()
        assert "All texts grouped" in result.summary
        assert "text file" in result.summary

    def test_preview_returns_plan_without_executing(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        plan = organize(
            str(workspace),
            "",
            preview=True,
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert isinstance(plan, Plan)
        # Filesystem unchanged
        assert (workspace / "a.txt").exists()
        assert (workspace / ".DS_Store").exists()

    def test_invalid_target_raises(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        with pytest.raises(ValueError, match="not in allowed"):
            organize(
                "/etc",
                "",
                backend=backend,
                allowed_roots=[workspace.parent],
            )

    def test_progress_callback_receives_phase_prefixed_events(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        events: list[str] = []
        organize(
            str(workspace),
            "",
            backend=backend,
            allowed_roots=[workspace.parent],
            progress_callback=events.append,
        )
        prefixes = {e.split(":", 1)[0] for e in events}
        assert "planner" in prefixes
        assert "executor" in prefixes


class TestApplyPlan:
    def test_apply_after_preview(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        plan = organize(
            str(workspace),
            "",
            preview=True,
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        # Filesystem still untouched
        assert (workspace / "a.txt").exists()

        result = apply_plan(plan)
        assert isinstance(result, PlanResult)
        assert (workspace / "Texts" / "a.txt").exists()
        assert not (workspace / "a.txt").exists()
```

- [ ] **Step 7.2: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -x -q --tb=short
```

Expected: `ImportError: cannot import name 'organize' from 'fda.organize'`.

- [ ] **Step 7.3: Implement `__init__.py`**

Replace the empty `fda/organize/__init__.py` with:

```python
"""Public API for the organize pipeline.

Phase A:
- organize(target, instructions, *, preview=False, backend=None, allowed_roots=None,
           progress_callback=None) -> Plan | PlanResult
- apply_plan(plan, *, progress_callback=None) -> PlanResult

Phase B (deferred): Claude rescue, plan persistence, UI integration.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fda.organize import _fs, executor, planner, verifier
from fda.organize.models import (
    Operation,
    OperationKind,
    OperationOutcome,
    Plan,
    PlanResult,
)

logger = logging.getLogger(__name__)


def organize(
    target: str,
    instructions: str = "",
    *,
    preview: bool = False,
    backend=None,
    allowed_roots: list[Path] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Plan | PlanResult:
    """Plan and (unless preview) execute organization for `target`.

    Raises ValueError if `target` is outside `allowed_roots` or not a
    directory. Raises planner.PlannerDidNotSubmitError if the planner
    fails to submit a valid plan within its iteration cap.
    """
    if backend is None:
        from fda.claude_backend import get_claude_backend
        backend = get_claude_backend()

    if allowed_roots is None:
        from fda.config import LOCAL_WORKER_PROJECTS
        allowed_roots = [Path(p) for p in LOCAL_WORKER_PROJECTS]

    target_path = _fs.validate_target(target, allowed_roots)
    plan = planner.build_plan(
        target_path,
        instructions,
        backend=backend,
        progress_callback=progress_callback,
    )
    if preview:
        return plan
    return apply_plan(plan, progress_callback=progress_callback)


def apply_plan(
    plan: Plan,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> PlanResult:
    """Execute a previously-generated Plan and verify the result."""
    target_path = Path(plan.target)
    outcomes = executor.execute_plan(plan, progress_callback=progress_callback)
    if progress_callback:
        try:
            progress_callback("verifier: starting")
        except Exception:
            logger.debug("progress_callback raised", exc_info=True)
    return verifier.verify_plan(plan, outcomes, target_path)


__all__ = [
    "organize",
    "apply_plan",
    "Plan",
    "Operation",
    "OperationKind",
    "OperationOutcome",
    "PlanResult",
]
```

- [ ] **Step 7.4: Run tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -x -q --tb=short
```

Expected: integration tests pass.

- [ ] **Step 7.5: Commit**

```bash
git add fda/organize/__init__.py tests/test_organize_pipeline.py
git commit -m "feat(organize): add organize() and apply_plan() public API"
```

---

## Task 8: `LocalWorkerAgent` wrapper rewrite + new preview/apply methods

**Files:**
- Modify: `fda/local_worker_agent.py` (rewrite `organize_files`, add `organize_files_preview`, `organize_files_apply`, remove organize-related instance state)
- Modify: `tests/test_local_worker.py` (back-compat tests, concurrency regression test)

- [ ] **Step 8.1: Write failing back-compat tests**

Append to `tests/test_local_worker.py`:

```python
class TestOrganizeFilesBackCompat:
    """Phase A: organize_files() returns the same dict shape as today,
    plus per-move `reason`, `discrepancies`, and `leftover_empty_dirs`."""

    def test_dict_shape_preserved(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Operation, OperationKind, Plan, PlanResult, OperationOutcome

        op = Operation(
            kind=OperationKind.MOVE,
            source=str(local_worker_dir / "readme.txt"),
            destination=str(local_worker_dir / "Texts" / "readme.txt"),
            reason="text file",
        )
        plan = Plan(
            target=str(local_worker_dir),
            instructions="",
            operations=(op,),
            grouping_summary="g",
        )
        outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
        fake_result = PlanResult(
            plan=plan,
            outcomes=(outcome,),
            leftover_empty_dirs=(),
            discrepancies=(),
            repos_skipped=(),
            summary="rendered summary",
        )

        def fake_organize(target, instructions, **kwargs):
            return fake_result

        monkeypatch.setattr("fda.organize.organize", fake_organize)

        result = local_worker.organize_files(str(local_worker_dir), "")
        assert result["success"] is True
        assert "summary" in result
        assert "moves" in result
        assert "deletions" in result
        assert "dirs_created" in result
        assert "repos_skipped" in result
        assert "discrepancies" in result
        assert "leftover_empty_dirs" in result
        # Per-move reason is included
        assert result["moves"][0]["reason"] == "text file"
        assert result["moves"][0]["from"].endswith("readme.txt")
        assert result["moves"][0]["to"].endswith("Texts/readme.txt")

    def test_success_false_when_any_outcome_failed(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Operation, OperationKind, Plan, PlanResult, OperationOutcome

        op = Operation(
            kind=OperationKind.MOVE,
            source=str(local_worker_dir / "readme.txt"),
            destination=str(local_worker_dir / "Texts" / "readme.txt"),
            reason="r",
        )
        plan = Plan(target=str(local_worker_dir), instructions="", operations=(op,), grouping_summary="")
        outcome = OperationOutcome(
            operation_index=0, operation=op, status="failed", error="permission denied",
        )
        fake_result = PlanResult(
            plan=plan, outcomes=(outcome,), leftover_empty_dirs=(),
            discrepancies=(), repos_skipped=(), summary="s",
        )
        monkeypatch.setattr("fda.organize.organize", lambda t, i, **kw: fake_result)

        result = local_worker.organize_files(str(local_worker_dir), "")
        assert result["success"] is False

    def test_success_false_when_discrepancies(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Plan, PlanResult

        plan = Plan(target=str(local_worker_dir), instructions="", operations=(), grouping_summary="")
        fake_result = PlanResult(
            plan=plan, outcomes=(), leftover_empty_dirs=(),
            discrepancies=("a.txt missing",), repos_skipped=(), summary="s",
        )
        monkeypatch.setattr("fda.organize.organize", lambda t, i, **kw: fake_result)

        result = local_worker.organize_files(str(local_worker_dir), "")
        assert result["success"] is False

    def test_validation_error_returns_dict_not_raises(self, local_worker):
        # Path outside allowed roots must come back as dict, not raise.
        result = local_worker.organize_files("/etc", "")
        assert result["success"] is False
        assert "error" in result


class TestOrganizeFilesPreviewAndApply:
    def test_preview_returns_plan(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Plan

        fake_plan = Plan(target=str(local_worker_dir), instructions="", operations=(), grouping_summary="g")
        monkeypatch.setattr("fda.organize.organize", lambda t, i, **kw: fake_plan)

        result = local_worker.organize_files_preview(str(local_worker_dir), "")
        assert isinstance(result, Plan)
        assert result.grouping_summary == "g"

    def test_apply_returns_dict(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Plan, PlanResult

        plan = Plan(target=str(local_worker_dir), instructions="", operations=(), grouping_summary="")
        fake_result = PlanResult(
            plan=plan, outcomes=(), leftover_empty_dirs=(),
            discrepancies=(), repos_skipped=(), summary="done",
        )
        monkeypatch.setattr("fda.organize.apply_plan", lambda p, **kw: fake_result)

        result = local_worker.organize_files_apply(plan)
        assert result["success"] is True
        assert result["summary"] == "done"


class TestOrganizeConcurrencyRegression:
    """Two concurrent organize_files() calls on different targets must not
    corrupt each other. Today's instance-field mutation made this unsafe;
    Phase A removes the shared mutable state."""

    def test_two_targets_dont_share_state(self, local_worker, tmp_path, monkeypatch):
        from fda.organize.models import Operation, OperationKind, Plan, PlanResult, OperationOutcome

        ws_a = tmp_path / "a"
        ws_a.mkdir()
        (ws_a / "x.txt").write_text("x")
        ws_b = tmp_path / "b"
        ws_b.mkdir()
        (ws_b / "y.txt").write_text("y")

        # Reconfigure the worker's allowed projects.
        from pathlib import Path
        local_worker.projects = [Path(tmp_path)]

        captured_targets: list[str] = []

        def fake_organize(target, instructions, **kwargs):
            captured_targets.append(target)
            t = Path(target)
            op = Operation(
                kind=OperationKind.MOVE,
                source=str(t / list(t.iterdir())[0].name),
                destination=str(t / "Sorted" / list(t.iterdir())[0].name),
                reason="r",
            )
            plan = Plan(target=str(t), instructions=instructions, operations=(op,), grouping_summary="")
            outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
            return PlanResult(
                plan=plan, outcomes=(outcome,), leftover_empty_dirs=(),
                discrepancies=(), repos_skipped=(), summary="s",
            )

        monkeypatch.setattr("fda.organize.organize", fake_organize)

        # Sequential calls (the regression bug today is shared instance state
        # across calls; concurrency is harder to test deterministically, but
        # back-to-back calls are sufficient to catch state leakage).
        r1 = local_worker.organize_files(str(ws_a), "first")
        r2 = local_worker.organize_files(str(ws_b), "second")

        assert r1["moves"][0]["from"].endswith("x.txt")
        assert r2["moves"][0]["from"].endswith("y.txt")
        assert captured_targets == [str(ws_a), str(ws_b)]
```

- [ ] **Step 8.2: Run tests, confirm they fail**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_local_worker.py -x -q --tb=short
```

Expected: `AttributeError: 'LocalWorkerAgent' object has no attribute 'organize_files_preview'` and back-compat assertion failures (`reason` missing from moves dict, etc.).

- [ ] **Step 8.3: Rewrite `organize_files` and add new methods**

In `fda/local_worker_agent.py`:

Replace the entire `organize_files()` method (lines ~1023-1114) with the wrapper below. Also delete the now-unused organize state initialization in the `__init__` constructor (`self._organize_moves`, `self._organize_deletions`, `self._organize_dirs_created`, `self._repos_skipped`) — search for those names and remove their assignments. Leave `self._current_project`, `self._pending_changes`, and `self._files_read` (still used by `analyze_and_fix`). Also remove the entire `_execute_organize_tool` method and the `_orgtool_*` methods (lines ~1120-1355), since the new package handles all organization logic. Keep `_is_inside_git_repo` and `_human_size` in place (re-exporting `_fs.is_inside_git_repo` is optional, but back-compat tests in `test_local_worker.py::TestFileOrganization::test_is_inside_git_repo` reference the agent method, so keep it as a thin delegator):

```python
    def _is_inside_git_repo(self, path: Path) -> bool:
        """Back-compat shim — delegates to fda.organize._fs."""
        from fda.organize import _fs
        return _fs.is_inside_git_repo(path)
```

Replace `organize_files` with:

```python
    def organize_files(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Organize files in `target_path` using the planner+executor+verifier
        pipeline. Returns the back-compat dict shape used by Telegram, the
        web UI, and the orchestrator."""
        from fda.organize import organize as _organize
        from fda.organize.models import PlanResult

        try:
            target_path = self.resolve_project_path(target_path)
            result = _organize(
                target_path,
                instructions,
                backend=self._backend,
                allowed_roots=self.projects,
                progress_callback=progress_callback,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"organize_files failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

        assert isinstance(result, PlanResult)
        return _plan_result_to_dict(result)

    def organize_files_preview(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        """Build a Plan without executing it. Returns a fda.organize.Plan."""
        from fda.organize import organize as _organize

        target_path = self.resolve_project_path(target_path)
        return _organize(
            target_path,
            instructions,
            preview=True,
            backend=self._backend,
            allowed_roots=self.projects,
            progress_callback=progress_callback,
        )

    def organize_files_apply(
        self,
        plan,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Apply a previously-generated Plan. Returns the back-compat dict."""
        from fda.organize import apply_plan as _apply

        try:
            result = _apply(plan, progress_callback=progress_callback)
        except Exception as e:
            logger.error(f"organize_files_apply failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
        return _plan_result_to_dict(result)
```

Add this module-level helper function (place it just above `class LocalWorkerAgent`):

```python
def _plan_result_to_dict(result) -> dict[str, Any]:
    """Translate a fda.organize.PlanResult into the back-compat dict shape.

    Back-compat: `success`, `summary`, `moves`, `deletions`, `dirs_created`,
    `repos_skipped`. Additive: `reason` per move, `discrepancies`, and
    `leftover_empty_dirs`.
    """
    from fda.organize.models import OperationKind

    moves = []
    deletions = []
    dirs_created = []
    for outcome in result.outcomes:
        # "skipped" means the operation's intent is already satisfied
        # (e.g., idempotent re-run found the file already at destination).
        # Surface these as moves/deletes/dirs the same as applied.
        if outcome.status not in ("applied", "rescued", "skipped"):
            continue
        op = outcome.operation
        if op.kind == OperationKind.MOVE:
            moves.append({
                "from": op.source,
                "to": op.destination,
                "reason": op.reason,
            })
        elif op.kind == OperationKind.DELETE:
            deletions.append({"path": op.source, "reason": op.reason})
        elif op.kind == OperationKind.CREATE_DIR:
            dirs_created.append(op.destination)

    success = (
        all(o.status in ("applied", "rescued", "skipped") for o in result.outcomes)
        and not result.discrepancies
    )

    return {
        "success": success,
        "summary": result.summary,
        "moves": moves,
        "deletions": deletions,
        "dirs_created": dirs_created,
        "repos_skipped": list(result.repos_skipped),
        "discrepancies": list(result.discrepancies),
        "leftover_empty_dirs": list(result.leftover_empty_dirs),
    }
```

Finally, remove the now-dead code in `local_worker_agent.py`:
- The constants `_FILE_ORGANIZE_TOOLS` and `_JUNK_FILES` (top of file, ~lines 171-308) — both are now owned by `fda.organize`.
- The `ORGANIZE_SYSTEM_PROMPT` class attribute on `LocalWorkerAgent` (~lines 986-1021).
- The `_execute_organize_tool` method and all `_orgtool_*` helpers (~lines 1120-1355).
- The instance-field initializations for organize state inside `organize_files` (no longer used).

- [ ] **Step 8.4: Run the local worker tests, confirm they pass**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_local_worker.py -x -q --tb=short
```

Expected: pre-existing safety tests still pass via `_is_inside_git_repo` shim; new back-compat / preview / apply / concurrency tests pass.

- [ ] **Step 8.5: Run the full suite to catch any unexpected breakage**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: all 113+ tests pass.

- [ ] **Step 8.6: Commit**

```bash
git add fda/local_worker_agent.py tests/test_local_worker.py
git commit -m "feat(organize): wire LocalWorkerAgent.organize_files to new pipeline

- organize_files() now wraps fda.organize.organize() and translates
  PlanResult to the existing dict shape
- adds organize_files_preview() and organize_files_apply() (API only)
- removes shared mutable organize state from the agent (fixes latent
  concurrency bug)
- removes dead _FILE_ORGANIZE_TOOLS, _JUNK_FILES, ORGANIZE_SYSTEM_PROMPT,
  _execute_organize_tool, _orgtool_* (now owned by fda.organize)"
```

---

## Task 9: Orchestrator — task-status mapping and journal entry

**Files:**
- Modify: `fda/orchestrator.py` (`_handle_local_organize_request`, ~line 1207)
- Modify: existing tests in `tests/test_local_worker.py` if any depend on old journal shape (verify after editing)

- [ ] **Step 9.1: Inspect the current handler to confirm the patch points**

```bash
sed -n '1207,1325p' /Users/hogyeongkim/Desktop/Projects/FDA/FDA/fda/orchestrator.py
```

Expected: confirm the structure laid out in the spec — task creation, `worker_local.organize_files()` call, success branch with journal entry, failure branch.

- [ ] **Step 9.2: Update the handler**

In `fda/orchestrator.py`, modify `_handle_local_organize_request`:

1. Keep the task creation, `resolve_project_path`, and `organize_files()` call as-is.
2. After `result = self.worker_local.organize_files(...)`:
   - If `result["success"]`: mark task `completed`.
   - Else: mark task `blocked`.
3. **Always** write the journal entry, regardless of success.
4. Extend the journal content with the new sections.

Replace the success/failure branches (current `if result.get("success"):` block) with:

```python
        success = bool(result.get("success"))
        self.state.update_task(
            task_id,
            status="completed" if success else "blocked",
        )

        moves = result.get("moves", [])
        deletions = result.get("deletions", [])
        repos_skipped = result.get("repos_skipped", [])
        dirs_created = result.get("dirs_created", [])
        discrepancies = result.get("discrepancies", [])
        leftover_empty_dirs = result.get("leftover_empty_dirs", [])
        summary = result.get("summary", "")
        error = result.get("error")

        parts = [f"## Target\n`{target_path}`"]
        if instructions:
            parts.append(f"## Instructions\n{instructions}")
        if summary:
            parts.append(f"## Plan Summary\n{summary[:2000]}")

        if moves:
            move_lines = "\n".join(
                f"- `{m['from']}` → `{m['to']}` — {m.get('reason', '')}".rstrip(" — ")
                for m in moves[:50]
            )
            parts.append(f"## Files Moved ({len(moves)})\n{move_lines}")

        if dirs_created:
            dir_lines = "\n".join(f"- `{d}`" for d in dirs_created)
            parts.append(f"## Directories Created\n{dir_lines}")

        if deletions:
            del_lines = "\n".join(
                f"- `{d.get('path', d)}`" for d in deletions
            )
            parts.append(f"## Junk Deleted\n{del_lines}")

        if leftover_empty_dirs:
            empty_lines = "\n".join(f"- `{d}`" for d in leftover_empty_dirs)
            parts.append(f"## Empty Folders Cleaned\n{empty_lines}")

        if repos_skipped:
            repo_lines = "\n".join(f"- `{r}`" for r in repos_skipped)
            parts.append(f"## Git Repos Skipped\n{repo_lines}")

        if discrepancies:
            disc_lines = "\n".join(f"- {d}" for d in discrepancies)
            parts.append(f"## Discrepancies\n{disc_lines}")

        if error:
            parts.append(f"## Error\n{error}")

        content = "\n\n".join(parts)
        brief = instructions[:60] if instructions else f"Organize {dir_name}"
        journal_summary = (
            f"[LOCAL] File organization: {brief} "
            f"({len(moves)} moves, {len(deletions)} deletions, "
            f"{len(leftover_empty_dirs)} cleanups)"
        )

        try:
            self._journal.write_entry(
                author="orchestrator",
                tags=["worker", "local", "file-organization"],
                summary=journal_summary,
                content=content,
                relevance_decay="medium",
            )
        except Exception as e:
            logger.warning(f"Failed to write organize journal entry: {e}")

        return result
```

This replaces both the old `if result.get("success"):` happy-path block and the old `else:` failure block. The handler now always writes a journal entry and never silently drops the result.

- [ ] **Step 9.3: Run the full suite**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: all tests still pass. (No new tests added in this task — orchestrator behavior is exercised through `test_local_worker.py` and integration via `test_organize_pipeline.py`.)

- [ ] **Step 9.4: Commit**

```bash
git add fda/orchestrator.py
git commit -m "refactor(orchestrator): always-journal organize results; map partial failures to blocked"
```

---

## Task 10: Manual smoke test + Phase A wrap-up

**Files:** none modified — manual verification step only.

- [ ] **Step 10.1: Pick a low-risk smoke target**

Make a backup copy of a real folder (do NOT smoke-test against `~/Documents` directly):

```bash
cp -R ~/Downloads /tmp/fda-smoke-downloads
```

Then add `/tmp` to `LOCAL_WORKER_PROJECTS` in `fda/config.py` temporarily, OR run an inline Python session that passes `allowed_roots=[Path("/tmp")]` directly to `fda.organize.organize`.

- [ ] **Step 10.2: Run preview and inspect the plan**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python - <<'PY'
from pathlib import Path
from fda.organize import organize
plan = organize(
    "/tmp/fda-smoke-downloads",
    "sort by purpose",
    preview=True,
    allowed_roots=[Path("/tmp")],
)
print(f"{len(plan.operations)} operations")
for op in plan.operations[:20]:
    print(f"  {op.kind.value}: {op.source} -> {op.destination}  ({op.reason})")
PY
```

Expected: a plain text plan listing operations with reasons. Sanity-check that:
- No paths reference `~/Downloads` (smoke-target is `/tmp/...`).
- No operations target inside any `.git/` tree.
- Reasons are human-readable.

- [ ] **Step 10.3: Apply the plan and check the result**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python - <<'PY'
from pathlib import Path
from fda.organize import organize
result = organize(
    "/tmp/fda-smoke-downloads",
    "sort by purpose",
    allowed_roots=[Path("/tmp")],
)
print(result.summary)
print("discrepancies:", result.discrepancies)
print("cleaned:", result.leftover_empty_dirs)
PY
```

Expected: rendered summary including reasons; `discrepancies` empty; cleanup list non-empty if any source dirs were emptied.

- [ ] **Step 10.4: Force a destination collision and confirm `failed` outcome**

```bash
mkdir -p /tmp/fda-smoke-collision/Texts
echo "existing" > /tmp/fda-smoke-collision/Texts/a.txt
echo "to-move" > /tmp/fda-smoke-collision/a.txt
```

Then re-run organize against `/tmp/fda-smoke-collision`. The plan will likely propose moving `a.txt` into `Texts/`; the executor should record `status="failed"` for that operation, the journal entry should include a `## Discrepancies` or `## Couldn't Complete` section, and `result["success"]` should be `False`.

- [ ] **Step 10.5: Clean up smoke directories and revert any temporary config changes**

```bash
rm -rf /tmp/fda-smoke-downloads /tmp/fda-smoke-collision
git diff fda/config.py  # ensure no accidental commits to config
git checkout fda/config.py  # if you modified it for testing
```

- [ ] **Step 10.6: Final test sweep + commit-free wrap-up**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short
```

Expected: full suite green. No commit needed for the smoke test itself.

- [ ] **Step 10.7: Tag Phase A complete in the spec**

Edit `docs/superpowers/specs/2026-05-04-organize-success-rate-design.md` header:

Change `**Status:** draft — revised after Codex review (pending user approval)` to `**Status:** Phase A landed YYYY-MM-DD; Phase B pending separate plan` (replace YYYY-MM-DD with today's date).

```bash
git add docs/superpowers/specs/2026-05-04-organize-success-rate-design.md
git commit -m "docs(organize): mark Phase A complete in spec status"
```

---

## Notes for the implementer

- **Test runner path** (`/Users/john/.pyenv/versions/3.12.8/bin/python`) comes from project CLAUDE.md. If your local pyenv is elsewhere, substitute, but make sure your runner matches the pre-commit hook's runner — otherwise commits will block on phantom failures.
- **Don't add Phase B features** (Claude rescue, plan persistence, Telegram `--preview`, web UI). They're explicitly out of scope and have their own follow-on plan.
- **Don't generalize `_fs.py` beyond what tests use.** YAGNI — Phase B will add what it needs.
- **If a step's expected output doesn't match reality, stop and investigate.** This plan was written without running the code; small fix-ups (e.g., import order, an extra `.resolve()` somewhere) may be needed. Don't paper over a real failure to keep moving.
