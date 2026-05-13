# fda/metadata/__init__.py
"""FDA metadata layer — stage 5 of `fda organize`.

Public entry: run(target_path, *, catalog, backend, logger,
                  progress_callback=None) -> RunReport

Behavior:
1. Acquire ~/.fda/metadata.db.lock (raises LockBusy on contention).
2. Connect to ~/.fda/metadata.db with WAL + FTS5 trigram probe.
3. Init schema (idempotent).
4. Load ~/.fda/business_context.md (missing-OK; 50KB cap).
5. Insert a new `runs` row.
6. For each non-junk catalog entry:
     - Compute sha256, mime, mtime (deterministic enrichment).
     - Bucket: classifiable (extract_status='ok' AND NOT summary_failed)
       vs. fail-closed (anything else).
7. Classify the classifiable bucket via classify_with_retry_and_bisect
   in BATCH_SIZE-sized chunks.
8. Apply fail-closed override to each successful record.
9. Upsert one document row + one path row per file.
10. Prune missing paths under target_root.
11. Finalize `runs` row, write audit sidecar, release lock.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fda.metadata import classifier, context, enrich, store
from fda.metadata.schema import (
    Classification, DocumentRow, PathRow, RunReport,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
FDA_VERSION = "0.1.0"
MODEL = "claude-sonnet-4-6"


def _now_iso() -> str:
    """ISO-8601 UTC with microsecond resolution, ending in Z.

    Microseconds make run_ids sortable AND prevent same-second
    collisions in the runs table PK (an immediate retry after a crash
    within the same wall-clock second would otherwise INSERT-fail on
    the PK).
    """
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _new_run_id(target_root: str) -> str:
    stamp = _now_iso().replace(":", "").replace("-", "")
    h = hashlib.sha1(target_root.encode("utf-8")).hexdigest()[:6]
    return f"{stamp}-{h}"


def fda_home() -> Path:
    """Return FDA's per-machine state directory.

    Reads `FDA_HOME` env var if set (used by tests and power users to
    redirect state without touching `$HOME` — the latter would also
    break the `claude` CLI's auth lookup).
    """
    override = os.environ.get("FDA_HOME")
    if override:
        return Path(override)
    return Path.home() / ".fda"


def _fail_closed_row(*, sha256, mime, size_bytes, language, run_id, ts) -> DocumentRow:
    return DocumentRow(
        sha256=sha256, mime=mime, size_bytes=size_bytes, language=language,
        department="unknown", document_type="unknown",
        confidentiality="restricted",
        summary="", keywords_json='{"ko": [], "en": []}',
        confidence=0.0, fail_closed_override=True, extract_status="failed",
        sharepoint_url=None, run_id=run_id, created_at=ts, updated_at=ts,
    )


def _row_from_record(
    *, sha256, mime, size_bytes, language, record: Classification,
    extract_status: str, run_id: str, ts: str,
) -> DocumentRow:
    return DocumentRow(
        sha256=sha256, mime=mime, size_bytes=size_bytes, language=language,
        department=record.department, document_type=record.document_type,
        confidentiality=record.confidentiality,
        summary=record.summary,
        keywords_json=json.dumps(
            {"ko": record.keywords.ko, "en": record.keywords.en},
            ensure_ascii=False,
        ),
        confidence=record.confidence,
        fail_closed_override=record.fail_closed_override,
        extract_status=extract_status,
        sharepoint_url=None, run_id=run_id, created_at=ts, updated_at=ts,
    )


def _write_audit_sidecar(
    *, run_id: str, target_root: Path, started_at: str, finished_at: str,
    files_seen: int, files_classified: int, files_failed: int,
    batches_total: int, batches_retried: int,
    business_context_path: Path, business_context_sha256: str | None,
    failed_path_ids: list[str], failed_path_map: dict[str, str],
) -> Path:
    home = fda_home()
    sidecar_dir = home / "runs"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / f"{run_id}.md"
    bc_line = (
        f"{business_context_path} (sha256 {business_context_sha256})"
        if business_context_sha256 else "(none — generic classification)"
    )
    lines = [
        f"# Metadata run {run_id}",
        "",
        f"Target: {target_root}",
        f"Started: {started_at}",
        f"Finished: {finished_at}",
        f"Model: {MODEL}",
        f"Business context: {bc_line}",
        "",
        "## Stats",
        f"- Files seen: {files_seen}",
        f"- Classified OK: {files_classified}",
        f"- Failed: {files_failed}",
        f"- Batches: {batches_total} total, {batches_retried} retried",
        "",
    ]
    if failed_path_ids:
        lines.append("## Failures")
        for pid in failed_path_ids:
            lines.append(f"- {pid} `{failed_path_map.get(pid, '?')}`")
        lines.append("")
    sidecar.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sidecar


def run(
    target_path: Path,
    *,
    catalog,
    backend,
    logger=None,
    progress_callback: Callable[[str], None] | None = None,
) -> RunReport:
    """Run the metadata layer over a Catalog. See module docstring."""
    home = fda_home()
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "metadata.db"
    lock_path = home / "metadata.db.lock"
    bc_path = home / "business_context.md"

    run_id = _new_run_id(str(target_path))
    started_at = _now_iso()

    with store.acquire_lock(lock_path):
        conn = store.connect(db_path)
        try:
            store.init_schema(conn)  # outside transaction: idempotent, survives failed runs
            conn.execute("BEGIN")
            try:
                bc = context.load_business_context(bc_path)
                if bc.info_message and progress_callback:
                    progress_callback(bc.info_message)
                store.insert_run(
                    conn, run_id=run_id, target_root=str(target_path),
                    model=MODEL, fda_version=FDA_VERSION,
                    business_context_sha256=bc.sha256,
                    started_at=started_at,
                )
                from fda.organize._skills import load_skill
                skill = load_skill(
                    Path(__file__).parent / "skills" / "metadata-classifier"
                )

                real_entries = [e for e in catalog.entries if not e.is_junk]
                # A file is classifiable only when stage-1 extraction produced
                # ok status AND the summarizer succeeded. extract_status alone
                # is not enough (a tool_missing entry with a stub summary is
                # not real text); summary_failed alone is not enough either
                # (a Korean PDF could have ok summary on garbled text). Both.
                def _classifiable(e):
                    return e.extract_status == "ok" and not e.summary_failed
                classifiable = [e for e in real_entries if _classifiable(e)]
                unclassifiable = [e for e in real_entries if not _classifiable(e)]

                ts = _now_iso()
                # Bucket: build prompt files for classifiable entries.
                prompt_files = []
                entry_by_pid: dict[str, object] = {}
                sha_by_pid: dict[str, str] = {}
                for e in classifiable:
                    sha = enrich.sha256_of(e.path)
                    sha_by_pid[e.path_id] = sha
                    entry_by_pid[e.path_id] = e
                    prompt_files.append({
                        "path_id": e.path_id,
                        "ext": e.ext,
                        "language_hint": enrich.language_of(e.summary),
                        "summary": e.summary,
                        "verbatim_head": e.verbatim_head[:500] if e.verbatim_head else "",
                    })

                files_classified = 0
                files_failed = 0
                batches_total = 0
                batches_retried = 0
                failed_path_ids: list[str] = []
                failed_path_map: dict[str, str] = {}

                # Classify in BATCH_SIZE-sized batches.
                for chunk_start in range(0, len(prompt_files), BATCH_SIZE):
                    chunk = prompt_files[chunk_start:chunk_start + BATCH_SIZE]
                    bres = classifier.classify_with_retry_and_bisect(
                        files=chunk, backend=backend, skill=skill,
                        business_context=bc.text,
                    )
                    batches_total += bres.batches_total
                    batches_retried += bres.batches_retried
                    for pid, record in bres.records_by_path_id.items():
                        record, _ = classifier.apply_fail_closed_override(record)
                        e = entry_by_pid[pid]
                        sha = sha_by_pid[pid]
                        row = _row_from_record(
                            sha256=sha, mime=enrich.mime_of(e.path),
                            size_bytes=e.size_bytes,
                            language=enrich.language_of(e.summary),
                            record=record, extract_status=e.extract_status,
                            run_id=run_id, ts=ts,
                        )
                        store.upsert_document(conn, row)
                        store.upsert_path(conn, PathRow(
                            path_id=pid, sha256=sha, path=e.path,
                            mtime=enrich.mtime_iso(e.path),
                            last_seen_run=run_id,
                        ))
                        files_classified += 1
                    for pid in bres.failed_path_ids:
                        e = entry_by_pid[pid]
                        sha = sha_by_pid[pid]
                        store.upsert_document(conn, _fail_closed_row(
                            sha256=sha, mime=enrich.mime_of(e.path),
                            size_bytes=e.size_bytes,
                            language=enrich.language_of(e.summary or ""),
                            run_id=run_id, ts=ts,
                        ))
                        store.upsert_path(conn, PathRow(
                            path_id=pid, sha256=sha, path=e.path,
                            mtime=enrich.mtime_iso(e.path),
                            last_seen_run=run_id,
                        ))
                        files_failed += 1
                        failed_path_ids.append(pid)
                        failed_path_map[pid] = e.path

                # Unclassifiable (extract_status != ok or summary_failed) →
                # fail-closed rows.
                for e in unclassifiable:
                    sha = enrich.sha256_of(e.path)
                    store.upsert_document(conn, _fail_closed_row(
                        sha256=sha, mime=enrich.mime_of(e.path),
                        size_bytes=e.size_bytes,
                        language=enrich.language_of(e.summary or ""),
                        run_id=run_id, ts=ts,
                    ))
                    store.upsert_path(conn, PathRow(
                        path_id=e.path_id, sha256=sha, path=e.path,
                        mtime=enrich.mtime_iso(e.path),
                        last_seen_run=run_id,
                    ))
                    files_failed += 1
                    failed_path_ids.append(e.path_id)
                    failed_path_map[e.path_id] = e.path

                files_seen = len(real_entries)
                store.prune_missing_paths(
                    conn, target_root=str(target_path), current_run_id=run_id,
                )
                finished_at = _now_iso()
                store.finalize_run(
                    conn, run_id=run_id, finished_at=finished_at,
                    files_seen=files_seen, files_classified=files_classified,
                    files_failed=files_failed, batches_total=batches_total,
                    batches_retried=batches_retried,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            # Audit sidecar written AFTER COMMIT: only on success,
            # never for partial/failed runs.
            _write_audit_sidecar(
                run_id=run_id, target_root=target_path,
                started_at=started_at, finished_at=finished_at,
                files_seen=files_seen, files_classified=files_classified,
                files_failed=files_failed, batches_total=batches_total,
                batches_retried=batches_retried,
                business_context_path=bc_path,
                business_context_sha256=bc.sha256,
                failed_path_ids=failed_path_ids,
                failed_path_map=failed_path_map,
            )
            return RunReport(
                run_id=run_id, files_seen=files_seen,
                files_classified=files_classified,
                files_failed=files_failed, batches_total=batches_total,
                batches_retried=batches_retried,
            )
        finally:
            conn.close()


__all__ = ["run", "RunReport", "fda_home"]
