# tests/test_metadata_store.py
"""Tests for fda.metadata.store."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


class TestFts5Probe:
    def test_probe_succeeds_on_working_sqlite(self, tmp_path):
        from fda.metadata.store import _assert_fts5_trigram
        conn = sqlite3.connect(tmp_path / "probe.db")
        try:
            _assert_fts5_trigram(conn)  # should not raise
        finally:
            conn.close()

    def test_probe_raises_actionable_error_on_old_sqlite(self):
        """`sqlite3.Connection.execute` is a C-level read-only method —
        `patch.object` won't work. Use a duck-typed fake instead: any
        object whose `.execute` raises sqlite3.OperationalError satisfies
        the probe's contract (it only calls `.execute` on the conn).
        """
        from fda.metadata.store import _assert_fts5_trigram

        class FakeConn:
            def execute(self, sql, *a, **kw):
                raise sqlite3.OperationalError("no such module: fts5")

        with pytest.raises(RuntimeError, match="SQLite ≥ 3.34"):
            _assert_fts5_trigram(FakeConn())


class TestConnect:
    def test_connect_enables_wal_mode(self, tmp_path):
        from fda.metadata.store import connect
        db = tmp_path / "m.db"
        conn = connect(db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_connect_enables_foreign_keys(self, tmp_path):
        from fda.metadata.store import connect
        conn = connect(tmp_path / "m.db")
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk == 1
        finally:
            conn.close()


class TestInitSchema:
    def test_creates_all_four_tables(self, tmp_path):
        from fda.metadata.store import connect, init_schema
        conn = connect(tmp_path / "m.db")
        try:
            init_schema(conn)
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "documents" in names
            assert "document_paths" in names
            assert "runs" in names
            assert "documents_fts" in names
        finally:
            conn.close()

    def test_init_schema_is_idempotent(self, tmp_path):
        from fda.metadata.store import connect, init_schema
        conn = connect(tmp_path / "m.db")
        try:
            init_schema(conn)
            init_schema(conn)  # second call must not raise
        finally:
            conn.close()


class TestFtsTriggers:
    def _ready(self, tmp_path):
        from fda.metadata.store import connect, init_schema
        conn = connect(tmp_path / "m.db")
        init_schema(conn)
        conn.execute(
            "INSERT INTO runs(run_id, started_at, target_root, files_seen, "
            "files_classified, files_failed, batches_total, batches_retried, "
            "fda_version, model) VALUES ('r1', '2026-05-13T00:00:00Z', '/t', "
            "0, 0, 0, 0, 0, '0.1.0', 'claude-sonnet-4-6')"
        )
        return conn

    def _insert_doc(self, conn, sha, summary, kw):
        conn.execute(
            "INSERT INTO documents(sha256, mime, size_bytes, language, "
            "department, document_type, confidentiality, summary, keywords, "
            "confidence, extract_status, run_id, created_at, updated_at) "
            "VALUES (?, 'application/pdf', 1, 'ko', 'finance', 'invoice', "
            "'confidential', ?, ?, 0.9, 'ok', 'r1', "
            "'2026-05-13T00:00:00Z', '2026-05-13T00:00:00Z')",
            (sha, summary, kw),
        )

    def test_insert_trigger_syncs_fts(self, tmp_path):
        conn = self._ready(tmp_path)
        try:
            self._insert_doc(conn, "a" * 64, "매출 청구서", '{"ko":["청구서"]}')
            n = conn.execute("SELECT count(*) FROM documents_fts").fetchone()[0]
            assert n == 1
        finally:
            conn.close()

    def test_delete_trigger_removes_fts(self, tmp_path):
        conn = self._ready(tmp_path)
        try:
            self._insert_doc(conn, "a" * 64, "x", '{"ko":[]}')
            conn.execute("DELETE FROM documents WHERE sha256 = ?", ("a" * 64,))
            n = conn.execute("SELECT count(*) FROM documents_fts").fetchone()[0]
            assert n == 0
        finally:
            conn.close()

    def test_update_trigger_replaces_fts(self, tmp_path):
        conn = self._ready(tmp_path)
        try:
            self._insert_doc(conn, "a" * 64, "old", '{"ko":[]}')
            conn.execute(
                "UPDATE documents SET summary = 'new', "
                "updated_at = '2026-05-13T01:00:00Z' WHERE sha256 = ?",
                ("a" * 64,),
            )
            rows = conn.execute(
                "SELECT summary FROM documents_fts WHERE documents_fts MATCH 'new'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()
