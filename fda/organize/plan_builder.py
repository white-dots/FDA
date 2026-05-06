# fda/organize/plan_builder.py
"""Deterministic Plan construction.

Pure function: Groupings + path_by_id + junk_paths -> Plan.
No filesystem mutation. Filesystem reads (existence checks, on-disk
collision detection) are read-only.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from fda.organize import _fs
from fda.organize.models import (
    Grouping,
    Groupings,
    Operation,
    OperationKind,
    Plan,
)

logger = logging.getLogger(__name__)


class PlanBuilderError(Exception):
    """Raised when groupings cannot produce a useful plan."""


# Windows-illegal characters; control chars handled separately.
_WIN_ILLEGAL = re.compile(r'[<>:"|?*]')
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_component(part: str) -> str:
    """Sanitize one path component (no separators expected)."""
    cleaned = _CTRL.sub("", part)
    cleaned = _WIN_ILLEGAL.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(". ")
    return cleaned


def _sanitize_subpath(subpath: str) -> str:
    parts: list[str] = []
    for raw in Path(subpath).parts:
        if raw in ("", "/", "\\"):
            continue
        if raw == "..":
            raise PlanBuilderError(f"subpath traversal not allowed: {subpath!r}")
        cleaned = _sanitize_component(raw)
        if not cleaned:
            continue
        parts.append(cleaned)
    if not parts:
        raise PlanBuilderError(f"subpath becomes empty after sanitization: {subpath!r}")
    return "/".join(parts)


def _resolve_destination_dir(target_dir: Path, subpath: str) -> Path:
    sanitized = _sanitize_subpath(subpath)
    if sanitized != subpath:
        logger.info("subpath sanitized: %r -> %r", subpath, sanitized)
    candidate = (target_dir / sanitized).resolve()
    target_resolved = target_dir.resolve()
    if not (candidate == target_resolved or candidate.is_relative_to(target_resolved)):
        raise PlanBuilderError(
            f"subpath {subpath!r} resolves outside target {target_dir}"
        )
    return candidate


def _next_basename(base: str, n: int) -> str:
    p = Path(base)
    if not p.suffix:
        return f"{base} ({n})"
    return f"{p.stem} ({n}){p.suffix}"


def _resolve_basename(
    dest_dir: Path,
    src: Path,
    planned: set[tuple[str, str]],
    on_disk_check: bool,
) -> str:
    """Pick the lowest-N basename that doesn't collide with another planned
    move OR an unrelated file already on disk."""
    base = src.name
    n = 2
    candidate = base
    while True:
        key = (str(dest_dir), candidate)
        on_disk_collision = (
            on_disk_check
            and (dest_dir / candidate).exists()
            and (dest_dir / candidate).resolve() != src.resolve()
        )
        if key not in planned and not on_disk_collision:
            return candidate
        candidate = _next_basename(base, n)
        n += 1


def build(
    target_dir: str,
    groupings: Groupings,
    path_by_id: Mapping[str, str],
    junk_paths: Sequence[str],
) -> Plan:
    """Construct a Plan from Groupings."""
    target = Path(target_dir).resolve()

    seen_ids: dict[str, str] = {}
    moves_intent: list[tuple[Grouping, Path, str]] = []  # (grouping, src, reason)
    for grp in groupings.items:
        if not grp.file_ids:
            raise PlanBuilderError(
                f"empty file_ids in grouping {grp.category!r} — "
                "Classifier must not emit empty groupings"
            )
        seen_in_group: set[str] = set()
        for pid in grp.file_ids:
            if pid in seen_in_group:
                logger.warning(
                    "duplicate path_id %s within grouping %r — deduplicated",
                    pid, grp.category,
                )
                continue
            seen_in_group.add(pid)
            if pid not in path_by_id:
                raise PlanBuilderError(f"unknown path_id {pid!r} in groupings")
            if pid in seen_ids:
                raise PlanBuilderError(
                    f"path_id {pid!r} (resolved to {path_by_id[pid]!r}) appears "
                    f"in two groupings: {seen_ids[pid]!r} and {grp.category!r}"
                )
            seen_ids[pid] = grp.category
            moves_intent.append((grp, Path(path_by_id[pid]), grp.reason))

    # Resolve destination dirs once per grouping, so two groupings with the
    # same subpath share a single create_dir.
    dest_dir_by_grouping: dict[int, Path] = {}
    for grp in groupings.items:
        dest_dir_by_grouping[id(grp)] = _resolve_destination_dir(target, grp.subpath)

    # Build candidate moves: drop missing sources; drop sources already in dest dir.
    candidates: list[tuple[Grouping, Path, Path, str]] = []  # (grp, src, dest_dir, reason)
    for grp, src, reason in moves_intent:
        dest_dir = dest_dir_by_grouping[id(grp)]
        if not src.exists():
            logger.info("dropping missing source: %s", src)
            continue
        if src.parent.resolve() == dest_dir:
            logger.info("dropping no-op move (already in dest): %s", src)
            continue
        candidates.append((grp, src, dest_dir, reason))

    # Resolve basenames deterministically. Sort by source path so collisions
    # break ties alphabetically and reproducibly.
    candidates.sort(key=lambda t: str(t[1]))
    planned: set[tuple[str, str]] = set()
    move_ops: list[Operation] = []
    for grp, src, dest_dir, reason in candidates:
        basename = _resolve_basename(dest_dir, src, planned, on_disk_check=True)
        planned.add((str(dest_dir), basename))
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest_dir / basename),
            reason=reason,
        )
        try:
            _fs.validate_operation(op, target)
        except ValueError as e:
            logger.info("dropping invalid move %s -> %s: %s", src, dest_dir / basename, e)
            planned.discard((str(dest_dir), basename))
            continue
        move_ops.append(op)

    # create_dir for each destination dir that has at least one accepted move.
    surviving_dirs = {Path(op.destination).parent for op in move_ops}
    create_dir_ops = [
        Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(d),
            reason=f"destination for grouping",
        )
        for d in sorted(surviving_dirs, key=str)
    ]

    # delete ops for junk
    delete_ops: list[Operation] = []
    for jp in junk_paths:
        op = Operation(
            kind=OperationKind.DELETE,
            source=jp,
            destination=None,
            reason="junk file",
        )
        try:
            _fs.validate_operation(op, target)
        except ValueError as e:
            logger.info("dropping invalid junk delete %s: %s", jp, e)
            continue
        delete_ops.append(op)

    operations = tuple(create_dir_ops + move_ops + delete_ops)

    had_input = bool(groupings.items) or bool(junk_paths)
    if had_input and not operations:
        raise PlanBuilderError(
            "nothing to do — all operations dropped "
            f"(groupings={len(groupings.items)}, junk={len(junk_paths)})"
        )

    return Plan(
        target=str(target),
        instructions="",
        operations=operations,
        grouping_summary=groupings.overall_reason,
    )
