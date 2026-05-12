# fda/organize/__init__.py
"""Public API for the organize pipeline.

organize(target, instructions, *, preview=False, backend=None,
         allowed_roots=None, progress_callback=None, log_path=None)
        -> Plan | PlanResult
apply_plan(plan, *, progress_callback=None) -> PlanResult
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fda.organize import (
    _fs, classifier, executor, plan_builder, reader, router, verifier,
)
from fda.organize._logger import OrganizeLogger
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
    log_path: Path | bool | None = None,
    route: bool = True,
) -> Plan | PlanResult:
    """Plan and (unless preview) execute organization for `target`."""
    if backend is None:
        from fda.claude_backend import get_claude_backend
        backend = get_claude_backend()
    if allowed_roots is None:
        from fda.config import LOCAL_WORKER_PROJECTS
        allowed_roots = [Path(p) for p in LOCAL_WORKER_PROJECTS]

    target_path = _fs.validate_target(target, allowed_roots)
    olog = OrganizeLogger(
        log_path=log_path,
        target_basename=target_path.name,
        progress_callback=progress_callback,
    )
    try:
        olog.log("RUN_START", target=str(target_path),
                 instructions=instructions or "",
                 log_path=str(olog.path) if olog.path else "")
        # Note: OrganizeLogger.__init__ already emits "📝 logging to <path>"
        # via progress_callback right after opening the file, so we don't
        # repeat it here.

        catalog = reader.read(target_path, backend=backend, logger=olog)
        groupings = classifier.classify(
            catalog, instructions, backend=backend, logger=olog,
        )
        # Filter junk: Classifier only emits groupings over non-junk entries
        # (real = [e for e in catalog.entries if not e.is_junk]), so
        # path_by_id should mirror that. Including junk here would trip
        # PlanBuilder's overlap check between grouped sources and junk_paths.
        path_by_id = {e.path_id: e.path for e in catalog.entries if not e.is_junk}
        junk_paths = [e.path for e in catalog.entries if e.is_junk]
        plan = plan_builder.build(
            target_dir=str(target_path),
            groupings=groupings,
            path_by_id=path_by_id,
            junk_paths=junk_paths,
        )
        log_path_str = str(olog.path) if olog.path else None
        plan = Plan(
            target=plan.target,
            instructions=instructions,
            operations=plan.operations,
            grouping_summary=groupings.overall_reason or plan.grouping_summary,
            log_path=log_path_str,
        )
        olog.log(
            "PLAN_BUILD_DONE",
            ops=len(plan.operations),
            create_dirs=sum(1 for op in plan.operations
                            if op.kind == OperationKind.CREATE_DIR),
            moves=sum(1 for op in plan.operations
                      if op.kind == OperationKind.MOVE),
            deletes=sum(1 for op in plan.operations
                        if op.kind == OperationKind.DELETE),
        )
        # Per-op detail goes to the log file (the spec's PLAN_OP event).
        for idx, op in enumerate(plan.operations):
            olog.log(
                "PLAN_OP",
                idx=idx, kind=op.kind.value,
                src=op.source or "", dst=op.destination or "",
                reason=op.reason,
            )
        if preview:
            olog.log("RUN_END", status="preview")
            return plan
        result = apply_plan(plan, progress_callback=progress_callback)
        # Forward executor + verifier outcomes into the structured log.
        applied = 0
        failed = 0
        skipped = 0
        for outcome in result.outcomes:
            op = outcome.operation
            if outcome.status in ("applied", "rescued"):
                applied += 1
                olog.log(
                    "EXEC_APPLY", idx=outcome.operation_index,
                    kind=op.kind.value,
                    src=op.source or "", dst=op.destination or "",
                    result=outcome.status,
                )
            elif outcome.status == "skipped":
                skipped += 1
                olog.log(
                    "EXEC_APPLY", idx=outcome.operation_index,
                    kind=op.kind.value,
                    src=op.source or "", dst=op.destination or "",
                    result="skipped",
                )
            else:
                failed += 1
                olog.log(
                    "EXEC_FAIL", idx=outcome.operation_index,
                    kind=op.kind.value, error=outcome.error or "",
                )
        olog.log("EXEC_END", applied=applied, failed=failed, skipped=skipped)
        for d in result.discrepancies:
            olog.log("VERIFY_DISCREPANCY", desc=d)
        olog.log(
            "VERIFY_END",
            discrepancies=len(result.discrepancies),
            empty_dirs_cleaned=len(result.leftover_empty_dirs),
        )
        # Stamp log_path and propagate repos_skipped from the catalog.
        result = PlanResult(
            plan=result.plan,
            outcomes=result.outcomes,
            leftover_empty_dirs=result.leftover_empty_dirs,
            discrepancies=result.discrepancies,
            repos_skipped=tuple(catalog.git_repos_skipped),
            summary=result.summary,
            log_path=log_path_str,
        )
        # Cloud-destination routing (final stage). Operates on the
        # post-executor tree; skipped in preview mode or when --no-route.
        if route:
            try:
                router.route(
                    catalog=catalog,
                    groupings=groupings,
                    target_path=target_path,
                    backend=backend,
                    logger=olog,
                    outcomes=result.outcomes,
                )
            except Exception as e:  # noqa: BLE001
                # Don't fail the whole organize run if routing fails — log
                # and continue. The organized tree is already on disk.
                logger.error("router stage failed: %s", e, exc_info=True)
                olog.log("ROUTER_FAIL_FATAL", error=str(e))
        olog.log("RUN_END", status="success",
                 ops=len(plan.operations),
                 discrepancies=len(result.discrepancies))
        return result
    except BaseException as e:
        # Record terminal failure in the log file before re-raising.
        olog.log("RUN_END", status="failed", error=str(e))
        raise
    finally:
        try:
            olog.close()
        except Exception:
            # Never let log close failures override the run's outcome
            # (success: would replace PlanResult; failure: would mask original)
            logger.debug("olog.close() raised; suppressed", exc_info=True)


def apply_plan(
    plan: Plan,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> PlanResult:
    """Execute a previously-generated Plan and verify the result.

    Preserves `plan.log_path` on the returned PlanResult so preview-then-apply
    callers (e.g. organize_files_apply in local_worker_agent) carry the log
    path forward.
    """
    target_path = Path(plan.target)
    outcomes = executor.execute_plan(plan, progress_callback=progress_callback)
    if progress_callback:
        try:
            progress_callback("verifier: starting")
        except Exception:
            logger.debug("progress_callback raised", exc_info=True)
    result = verifier.verify_plan(plan, outcomes, target_path)
    if plan.log_path and not result.log_path:
        result = PlanResult(
            plan=result.plan,
            outcomes=result.outcomes,
            leftover_empty_dirs=result.leftover_empty_dirs,
            discrepancies=result.discrepancies,
            repos_skipped=result.repos_skipped,
            summary=result.summary,
            log_path=plan.log_path,
        )
    return result


__all__ = [
    "organize",
    "apply_plan",
    "Plan",
    "Operation",
    "OperationKind",
    "OperationOutcome",
    "PlanResult",
]
