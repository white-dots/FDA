"""Filesystem primitives for the organize pipeline.

Single source of truth for path validation, git-repo detection (both
source and destination), junk-file rules, and the deterministic apply
operations used by the executor.

Concurrency model: the apply primitives assume a single writer for the
target tree during a run. `apply_move` checks `dest.exists()` before
calling `shutil.move`, and `apply_delete` re-validates and unlinks.
A second concurrent process modifying the same files between those
steps can produce a TOCTOU race (silent overwrite or unintended
unlink). The orchestrator guarantees per-target serialization, so
callers outside that path must not invoke organize concurrently
against the same directory.
"""

from __future__ import annotations

import shutil
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

    Resolves symlinks and `..` components first so a path that lexically
    sits outside a repo but physically resolves into one (via a symlink
    or traversal) is detected. `Path.resolve(strict=False)` handles
    non-existent leaf components.
    """
    current = path.resolve()
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
    # Resolve both sides so `..` traversal can't bypass the lexical
    # `is_relative_to` check (e.g., `/target/../outside` would otherwise
    # appear to start with `/target/`).
    resolved_p = p.resolve()
    resolved_target = target.resolve()
    if not (
        resolved_p == resolved_target
        or resolved_p.is_relative_to(resolved_target)
    ):
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


def validate_plan_shape(operations: list[Operation]) -> None:
    """Plan-level shape check: a plan that only creates directories does
    nothing useful — reject it so the planner is forced to include the
    moves (or junk deletes) that actually organize the directory.

    Empty list is NOT this helper's concern; the caller handles that
    separately so it can return its own message.

    Raises ValueError on rejection.
    """
    if not operations:
        return
    if all(op.kind == OperationKind.CREATE_DIR for op in operations):
        raise ValueError(
            "plan contains only create_dir operations and no moves; a "
            "plan must include at least one move (or junk delete) to "
            "actually organize files"
        )


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
    """Delete source if it exists; no-op if already gone.

    Existence is checked before validation so a re-run sees a clean
    no-op even though `validate_operation` would reject a non-existent,
    non-junk path at plan time.
    """
    if op.source is not None and not Path(op.source).exists():
        return
    validate_operation(op, target)
    Path(op.source).unlink()
