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
