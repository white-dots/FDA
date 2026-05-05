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
