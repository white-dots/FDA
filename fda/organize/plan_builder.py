# fda/organize/plan_builder.py
"""Deterministic Plan construction.

Pure function: Groupings + path_by_id + junk_paths -> Plan.
No filesystem mutation. Filesystem reads (existence checks, on-disk
collision detection) are read-only.
"""

from __future__ import annotations

import logging
import re
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
# Subpaths arrive from the Classifier as plain strings; they may contain
# either forward or back slashes. We split on both so a Windows-style
# "..\\escape" can't slip past the traversal check on POSIX.
_SUBPATH_SEP = re.compile(r"[/\\]")
# Cap on how many "name (N).ext" attempts the basename resolver makes
# before giving up. A degenerate destination directory pre-populated with
# every variant up to the cap is the only thing this guards against.
_MAX_COLLISION_RETRIES = 100


def _sanitize_component(part: str) -> str:
    """Sanitize one path component (no separators expected)."""
    cleaned = _CTRL.sub("", part)
    cleaned = _WIN_ILLEGAL.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(". ")
    return cleaned


def _sanitize_subpath(subpath: str) -> str:
    parts: list[str] = []
    for raw in _SUBPATH_SEP.split(subpath):
        if raw == "":
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
    planned_sources: frozenset[Path],
) -> str:
    """Pick the lowest-N basename that doesn't collide with another planned
    move OR an unrelated file already on disk.

    `planned` is keyed by `(dest_dir, casefolded_basename)` so that on
    case-insensitive filesystems (default macOS/Windows) `Report.pdf` and
    `report.pdf` aren't both planned into the same directory.

    `planned_sources` lets us ignore disk files that are themselves about
    to move away — they'd otherwise force a spurious "(2)" rename.
    """
    base = src.name
    src_resolved = src.resolve()
    n = 2
    candidate = base
    for _ in range(_MAX_COLLISION_RETRIES + 1):
        key = (str(dest_dir), candidate.casefold())
        on_disk_collision = False
        if on_disk_check:
            existing = dest_dir / candidate
            if existing.exists():
                existing_resolved = existing.resolve()
                if (
                    existing_resolved != src_resolved
                    and existing_resolved not in planned_sources
                ):
                    on_disk_collision = True
        if key not in planned and not on_disk_collision:
            return candidate
        candidate = _next_basename(base, n)
        n += 1
    raise PlanBuilderError(
        f"too many basename collisions for {src.name!r} in {dest_dir} "
        f"(retried {_MAX_COLLISION_RETRIES} times)"
    )


def build(
    target_dir: str,
    groupings: Groupings,
    path_by_id: Mapping[str, str],
    junk_paths: Sequence[str],
) -> Plan:
    """Construct a Plan from Groupings."""
    target = Path(target_dir).resolve()

    # Reject overlap between grouped sources and junk paths upfront. A path
    # in both would be MOVED (relocating it) and then DELETEd at the old
    # location, where apply_delete silently no-ops — leaving the file in
    # the categorized destination instead of removed. That's almost
    # certainly an upstream Reader/Classifier bug; refuse the plan.
    grouped_resolved = {Path(p).resolve() for p in path_by_id.values()}
    junk_resolved = {Path(p).resolve() for p in junk_paths}
    overlap = grouped_resolved & junk_resolved
    if overlap:
        sample = next(iter(overlap))
        raise PlanBuilderError(
            f"path appears in both groupings and junk_paths: {sample}"
        )

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
                    f"in two groupings: {seen_ids[pid]!r} and "
                    f"{grp.category!r} (subpath={grp.subpath!r})"
                )
            seen_ids[pid] = f"{grp.category!r} (subpath={grp.subpath!r})"
            moves_intent.append((grp, Path(path_by_id[pid]), grp.reason))

    # Resolve destination dirs once per grouping, so two groupings with the
    # same subpath share a single create_dir.
    dest_dir_by_grouping: dict[Grouping, Path] = {}
    for grp in groupings.items:
        dest_dir_by_grouping[grp] = _resolve_destination_dir(target, grp.subpath)

    # Build candidate moves: drop non-file sources; drop sources already in
    # dest dir. `is_file()` (vs `exists()`) also rejects directory sources,
    # which the rest of the pipeline isn't designed to move.
    candidates: list[tuple[Grouping, Path, Path, str]] = []  # (grp, src, dest_dir, reason)
    for grp, src, reason in moves_intent:
        dest_dir = dest_dir_by_grouping[grp]
        if not src.is_file():
            logger.info("dropping non-file source: %s", src)
            continue
        if src.parent.resolve() == dest_dir:
            logger.info("dropping no-op move (already in dest): %s", src)
            continue
        candidates.append((grp, src, dest_dir, reason))

    # Snapshot of all paths that *will* leave their current location, so
    # _resolve_basename can ignore on-disk "collisions" with files that
    # are themselves planned sources moving away.
    planned_sources = frozenset(src.resolve() for _, src, _, _ in candidates)

    # Resolve basenames deterministically. Sort by source path so collisions
    # break ties alphabetically and reproducibly.
    candidates.sort(key=lambda t: str(t[1]))
    planned: set[tuple[str, str]] = set()
    move_ops: list[Operation] = []
    for grp, src, dest_dir, reason in candidates:
        basename = _resolve_basename(
            dest_dir, src, planned,
            on_disk_check=True, planned_sources=planned_sources,
        )
        key = (str(dest_dir), basename.casefold())
        planned.add(key)
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
            planned.discard(key)
            continue
        move_ops.append(op)

    # create_dir for each destination dir that has at least one accepted move.
    surviving_dirs = {Path(op.destination).parent for op in move_ops}
    create_dir_ops = [
        Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(d),
            reason="destination for grouping",
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
