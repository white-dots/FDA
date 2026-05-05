"""Filesystem primitives for the organize pipeline.

Single source of truth for path validation, git-repo detection (both
source and destination), junk-file rules, and the deterministic apply
operations used by the executor.
"""

from __future__ import annotations

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
