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

import sqlite3
from pathlib import Path

from fda.metadata.schema import (
    DDL_DOCUMENT_INDEXES,
    DDL_DOCUMENT_PATHS,
    DDL_DOCUMENTS,
    DDL_FTS,
    DDL_FTS_TRIGGERS,
    DDL_PATH_INDEXES,
    DDL_RUNS,
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
