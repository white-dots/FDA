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
