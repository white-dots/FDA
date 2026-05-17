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
    _fs, classifier, executor, plan_builder, reader, router,
    storage_blobs, verifier,
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


def _translate_catalog_for_stage5(catalog, outcomes):
    """Return a Catalog whose entries point at POST-move paths.

    Stage 1 records each entry's original on-disk path. Stage 4 then moves
    files. Stage 5 reads file bytes (for sha256/mime/mtime), so it must see
    the post-move locations. Only applied/rescued MOVE outcomes update
    paths; failed/skipped moves and CREATE_DIR/DELETE outcomes leave the
    entry path alone.
    """
    import dataclasses
    from fda.organize.models import Catalog, OperationKind
    move_map = {
        o.operation.source: o.operation.destination
        for o in outcomes
        if o.status in ("applied", "rescued")
        and o.operation.kind == OperationKind.MOVE
        and o.operation.source and o.operation.destination
    }
    translated_entries = tuple(
        dataclasses.replace(e, path=move_map.get(e.path, e.path))
        for e in catalog.entries
    )
    return Catalog(
        target=catalog.target,
        entries=translated_entries,
        git_repos_skipped=catalog.git_repos_skipped,
        files_pinned=catalog.files_pinned,
    )


def _run_metadata_stage(
    *,
    target_path: Path,
    catalog,
    backend,
    olog: OrganizeLogger,
    progress_callback: Callable[[str], None] | None,
) -> None:
    """Invoke stage 5 (metadata). Defensive: never raises.

    Crashes here MUST NOT invalidate the on-disk tree from stages 1-4.
    Counts (and any fatal error) surface to `progress_callback` so the
    user sees what happened — stage 5 is slow (~20 LLM calls per 200
    files), so silent failures would be a real footgun.
    """
    try:
        from fda.metadata import run as metadata_run
        m_report = metadata_run(
            target_path=target_path, catalog=catalog,
            backend=backend, logger=olog,
            progress_callback=progress_callback,
        )
        if progress_callback:
            try:
                progress_callback(
                    f"metadata: {m_report.files_classified}/"
                    f"{m_report.files_seen} classified, "
                    f"{m_report.files_failed} failed"
                )
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)
    except Exception as e:  # noqa: BLE001
        logger.error("metadata stage failed: %s", e, exc_info=True)
        olog.log("METADATA_FAIL_FATAL", error=str(e))
        if progress_callback:
            try:
                progress_callback(
                    f"⚠ metadata stage failed: {type(e).__name__}: {e}"
                    " — see log; organize stages 1-4 succeeded."
                )
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)


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
    metadata: bool = True,
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

        # Persistent organize-time preferences: ~/.fda/business_context.md.
        # Reuse the metadata-layer loader (50KB-capped, UTF-8 safe).
        # Missing file → empty string → classifier behaves exactly as
        # today. We do not log the sha here; the metadata stage tracks
        # its own load separately.
        from fda.metadata import fda_home
        from fda.metadata.context import load_business_context
        bc = load_business_context(fda_home() / "business_context.md")
        if bc.info_message and progress_callback:
            try:
                progress_callback(bc.info_message)
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)

        catalog = reader.read(target_path, backend=backend, logger=olog)
        groupings = classifier.classify(
            catalog, instructions,
            backend=backend, logger=olog,
            business_context=bc.text,
        )
        from fda.organize.models import Groupings, quarantine_bucket
        # Partition catalog into four independent streams:
        #   - extractable: non-junk, readable → classifier (LLM) groups
        #   - storage blobs: non-junk, unreadable, KNOWN blob type →
        #       synthetic real groups (no LLM), routed to S3
        #   - quarantine: non-junk, unreadable, NOT a blob type →
        #       _NoExtractor/_ExtractionFailed, no cloud dest (honesty)
        #   - junk: DELETE path
        # Storage blobs have a quarantine_bucket today; they are removed
        # from quarantine_entries here so they are not moved twice.
        extractable = [
            e for e in catalog.entries
            if not e.is_junk and quarantine_bucket(e) is None
        ]
        storage_blob_entries = [
            e for e in catalog.entries
            if not e.is_junk
            and storage_blobs.storage_blob_bucket(e) is not None
        ]
        quarantine_entries = [
            e for e in catalog.entries
            if quarantine_bucket(e) is not None
            and storage_blobs.storage_blob_bucket(e) is None
        ]
        # path_by_id MUST include storage-blob entries — plan_builder
        # raises PlanBuilderError("unknown path_id") for a Grouping whose
        # file_ids are absent from it (plan_builder.py:179-180).
        path_by_id = {
            e.path_id: e.path
            for e in extractable + storage_blob_entries
        }
        junk_paths = [e.path for e in catalog.entries if e.is_junk]
        # Merge synthetic storage-blob groups AFTER the classifier groups.
        # plan_builder sorts its own ops, so on-disk results are
        # order-independent; router + the report follow groupings.items
        # order directly, so appending last keeps content categories
        # first then blobs (stable, deterministic). Empty input →
        # build_groupings returns [] → Groupings is value-identical to
        # today (zero behavior change on blob-free corpora).
        sb_groupings = storage_blobs.build_groupings(storage_blob_entries)
        groupings = Groupings(
            items=groupings.items + tuple(sb_groupings),
            overall_reason=groupings.overall_reason,
        )
        plan = plan_builder.build(
            target_dir=str(target_path),
            groupings=groupings,
            path_by_id=path_by_id,
            junk_paths=junk_paths,
            quarantine=quarantine_entries,
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
                    plan=plan,
                )
            except Exception as e:  # noqa: BLE001
                # Don't fail the whole organize run if routing fails — log
                # and continue. The organized tree is already on disk.
                logger.error("router stage failed: %s", e, exc_info=True)
                olog.log("ROUTER_FAIL_FATAL", error=str(e))
        # Stage 5 — runs independently of routing. --no-metadata skips it.
        # Hook is in _run_metadata_stage (testable independently of stages 1-4).
        # Catalog from stage 1 has pre-move paths; translate to post-move
        # so enrich.sha256_of() can actually open the files.
        if metadata:
            stage5_catalog = _translate_catalog_for_stage5(
                catalog, result.outcomes,
            )
            _run_metadata_stage(
                target_path=target_path, catalog=stage5_catalog,
                backend=backend, olog=olog,
                progress_callback=progress_callback,
            )
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
    "_run_metadata_stage",
    "_translate_catalog_for_stage5",
    "Plan",
    "Operation",
    "OperationKind",
    "OperationOutcome",
    "PlanResult",
]
