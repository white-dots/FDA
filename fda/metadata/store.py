# fda/metadata/store.py
"""SQLite storage for the metadata layer.

Tables (init_schema):
- documents (one row per sha256)
- document_paths (one row per on-disk location)
- runs (one row per fda metadata invocation)
- documents_fts (FTS5 + trigram, external-content + sync triggers)

Connection:
- WAL mode (concurrent readers, single writer)
- foreign_keys=ON
- FTS5 + trigram probed on every connect; fatal RuntimeError if unsupported.
"""
from __future__ import annotations

import fcntl
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fda.metadata.schema import (
    DDL_DOCUMENT_INDEXES,
    DDL_DOCUMENT_PATHS,
    DDL_DOCUMENTS,
    DDL_FTS,
    DDL_FTS_TRIGGERS,
    DDL_PATH_INDEXES,
    DDL_RUNS,
    DocumentRow,
    PathRow,
)


def _assert_fts5_trigram(conn) -> None:
    """Verify FTS5 + trigram tokenizer are available; raise actionable error otherwise.

    Accepts any duck-typed conn with .execute(sql) — used to make the
    failure path testable with a fake connection (sqlite3.Connection.execute
    is a C-level read-only method that can't be monkeypatched).
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE temp.__fts_probe")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "FDA metadata layer requires SQLite ≥ 3.34 with FTS5 + trigram "
            f"tokenizer. Current sqlite3 reports: {sqlite3.sqlite_version}. "
            "Install `pysqlite3-binary` or rebuild Python against a newer "
            f"SQLite. Original error: {e}"
        ) from e


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with WAL mode, foreign keys, and FTS5 probe."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _assert_fts5_trigram(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables, indexes, and triggers if not already present.

    Idempotent — every DDL uses IF NOT EXISTS.
    """
    conn.execute(DDL_RUNS)          # runs must exist before documents (FK)
    conn.execute(DDL_DOCUMENTS)
    for stmt in DDL_DOCUMENT_INDEXES:
        conn.execute(stmt)
    conn.execute(DDL_DOCUMENT_PATHS)
    for stmt in DDL_PATH_INDEXES:
        conn.execute(stmt)
    conn.execute(DDL_FTS)
    for stmt in DDL_FTS_TRIGGERS:
        conn.execute(stmt)
    # Conditional one-shot rebuild for pre-existing rows without FTS sync.
    n_docs = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    n_fts = conn.execute("SELECT count(*) FROM documents_fts").fetchone()[0]
    if n_fts != n_docs:
        conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    target_root: str,
    model: str,
    fda_version: str,
    business_context_sha256: str | None,
    started_at: str,
) -> None:
    """Insert a new `runs` row with zeroed counters; counters bumped at run end."""
    conn.execute(
        "INSERT INTO runs(run_id, started_at, target_root, files_seen, "
        "files_classified, files_failed, batches_total, batches_retried, "
        "business_context_sha256, fda_version, model) "
        "VALUES (?, ?, ?, 0, 0, 0, 0, 0, ?, ?, ?)",
        (run_id, started_at, target_root, business_context_sha256,
         fda_version, model),
    )


def finalize_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    finished_at: str,
    files_seen: int,
    files_classified: int,
    files_failed: int,
    batches_total: int,
    batches_retried: int,
) -> None:
    """Bump counters + set finished_at on an existing runs row."""
    conn.execute(
        "UPDATE runs SET finished_at = ?, files_seen = ?, "
        "files_classified = ?, files_failed = ?, batches_total = ?, "
        "batches_retried = ? WHERE run_id = ?",
        (finished_at, files_seen, files_classified, files_failed,
         batches_total, batches_retried, run_id),
    )


def upsert_document(conn: sqlite3.Connection, doc: DocumentRow) -> None:
    """Insert or replace one `documents` row by sha256.

    On conflict, preserves `created_at`, bumps everything else.
    `sharepoint_url` uses COALESCE so a previously-set URL survives a
    re-run that emits None.
    """
    conn.execute(
        "INSERT INTO documents(sha256, mime, size_bytes, language, department, "
        "document_type, confidentiality, summary, keywords, confidence, "
        "fail_closed_override, extract_status, sharepoint_url, run_id, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(sha256) DO UPDATE SET "
        "mime=excluded.mime, size_bytes=excluded.size_bytes, "
        "language=excluded.language, department=excluded.department, "
        "document_type=excluded.document_type, "
        "confidentiality=excluded.confidentiality, summary=excluded.summary, "
        "keywords=excluded.keywords, confidence=excluded.confidence, "
        "fail_closed_override=excluded.fail_closed_override, "
        "extract_status=excluded.extract_status, "
        "sharepoint_url=COALESCE(documents.sharepoint_url, excluded.sharepoint_url), "
        "run_id=excluded.run_id, updated_at=excluded.updated_at",
        (
            doc.sha256, doc.mime, doc.size_bytes, doc.language, doc.department,
            doc.document_type, doc.confidentiality, doc.summary,
            doc.keywords_json, doc.confidence, int(doc.fail_closed_override),
            doc.extract_status, doc.sharepoint_url, doc.run_id,
            doc.created_at, doc.updated_at,
        ),
    )


def upsert_path(conn: sqlite3.Connection, p: PathRow) -> None:
    """Insert or update a path row, keyed on `path` (the PRIMARY KEY).

    `path_id` is reader-assigned per organize run (`f000`, …) and is NOT
    globally unique across runs over different trees; the durable key is
    the on-disk path itself. On re-seeing a path, refresh sha256/mtime/
    last_seen_run; refresh path_id too (the latest reader assignment is
    fine for audit, since older audit sidecars hold their own copies).
    """
    conn.execute(
        "INSERT INTO document_paths(path, sha256, path_id, mtime, last_seen_run) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "sha256=excluded.sha256, path_id=excluded.path_id, "
        "mtime=excluded.mtime, last_seen_run=excluded.last_seen_run",
        (p.path, p.sha256, p.path_id, p.mtime, p.last_seen_run),
    )


def prune_missing_paths(
    conn: sqlite3.Connection, *, target_root: str, current_run_id: str,
) -> int:
    """Delete `document_paths` rows under `target_root` whose `last_seen_run`
    is not the current run. Returns the number of rows deleted.

    Paths *outside* `target_root` are never pruned by this call — different
    `fda metadata` runs may target different trees on the same DB.
    """
    cur = conn.execute(
        "DELETE FROM document_paths WHERE last_seen_run != ? "
        "AND (path = ? OR path GLOB ? || '/*')",
        (current_run_id, target_root, target_root),
    )
    return cur.rowcount or 0


class LockBusy(RuntimeError):
    """Raised when another fda metadata process holds the lock."""


@contextmanager
def acquire_lock(lock_path: Path | str) -> Iterator[None]:
    """Non-blocking advisory lock on `lock_path` via fcntl.flock.

    Raises LockBusy immediately if another process holds the lock. The
    file is created if missing; it is NOT deleted on exit (releasing the
    flock is enough — the file is a long-lived lock target).
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            fd.close()
            raise LockBusy(
                f"another fda metadata run is in progress (lock at {lock_path})"
            ) from e
        try:
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            finally:
                fd.close()
    except LockBusy:
        raise
