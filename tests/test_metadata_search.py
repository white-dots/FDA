# tests/test_metadata_search.py
"""Tests for fda.metadata.search."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _seed(tmp_path, rows):
    """Create a DB with init_schema + a run row + given rows."""
    from fda.metadata.store import (
        connect, init_schema, insert_run, upsert_document, upsert_path,
    )
    from fda.metadata.schema import DocumentRow, PathRow
    db = tmp_path / "m.db"
    conn = connect(db)
    init_schema(conn)
    insert_run(conn, run_id="r1", target_root="/t",
               model="claude-sonnet-4-6", fda_version="0.1.0",
               business_context_sha256=None,
               started_at="2026-05-13T00:00:00Z")
    for i, r in enumerate(rows):
        sha = f"{i:064x}"
        upsert_document(conn, DocumentRow(
            sha256=sha, mime="application/pdf", size_bytes=1, language=r.get("lang", "ko"),
            department=r.get("department", "finance"),
            document_type=r.get("document_type", "invoice"),
            confidentiality=r.get("confidentiality", "confidential"),
            summary=r.get("summary", ""),
            keywords_json=json.dumps(r.get("keywords", {"ko": [], "en": []}),
                                     ensure_ascii=False),
            confidence=r.get("confidence", 0.9),
            fail_closed_override=r.get("fail_closed_override", False),
            extract_status="ok", sharepoint_url=None, run_id="r1",
            created_at="2026-05-13T00:00:00Z",
            updated_at="2026-05-13T00:00:00Z",
        ))
        upsert_path(conn, PathRow(
            path_id=f"f{i:03d}", sha256=sha, path=r.get("path", f"/t/{i}.pdf"),
            mtime="2026-05-13T00:00:00.000000Z", last_seen_run="r1",
        ))
    return conn


class TestSearch:
    def test_korean_particle_match(self, tmp_path):
        """Spec semantics: documents contain Korean text WITH particles
        (`매출은`, `매출을`); the query is the bare stem (`매출`). The
        FTS5 trigram tokenizer matches because `매출` is a substring of
        every particle-form. (The reverse direction — bare stem in doc,
        query with particle — does NOT match, and is not the use case.)
        """
        from fda.metadata.search import search
        conn = _seed(tmp_path, [
            {"summary": "2025년 매출은 증가했습니다",
             "keywords": {"ko": ["매출은"], "en": []}},
        ])
        hits = search(conn, query="매출")
        assert len(hits) == 1
        conn.close()

    def test_filter_by_department_and_confidentiality(self, tmp_path):
        from fda.metadata.search import search
        conn = _seed(tmp_path, [
            {"department": "finance", "confidentiality": "confidential",
             "summary": "invoice", "keywords": {"ko": [], "en": ["invoice"]}},
            {"department": "hr", "confidentiality": "confidential",
             "summary": "salary", "keywords": {"ko": [], "en": ["salary"]}},
            {"department": "finance", "confidentiality": "public",
             "summary": "press release", "keywords": {"ko": [], "en": ["press"]}},
        ])
        hits = search(conn, query="",
                      department="finance", confidentiality="confidential")
        assert len(hits) == 1
        assert hits[0].department == "finance"
        assert hits[0].confidentiality == "confidential"
        conn.close()

    def test_fail_closed_only_filter(self, tmp_path):
        from fda.metadata.search import search
        conn = _seed(tmp_path, [
            {"summary": "ok", "fail_closed_override": False,
             "keywords": {"ko": [], "en": ["a"]}},
            {"summary": "overridden", "fail_closed_override": True,
             "keywords": {"ko": [], "en": ["b"]}},
        ])
        hits = search(conn, query="", fail_closed_only=True)
        assert len(hits) == 1
        assert hits[0].summary == "overridden"
        conn.close()

    def test_one_sha_two_paths_yields_two_results(self, tmp_path):
        from fda.metadata.search import search
        from fda.metadata.store import upsert_path
        from fda.metadata.schema import PathRow
        conn = _seed(tmp_path, [
            {"summary": "duplicated invoice",
             "keywords": {"ko": [], "en": ["invoice"]}},
        ])
        # Add a second path for the same sha256.
        sha = conn.execute("SELECT sha256 FROM documents").fetchone()[0]
        upsert_path(conn, PathRow(path_id="f999", sha256=sha,
                                   path="/t/copy/0.pdf",
                                   mtime="2026-05-13T00:00:00.000000Z",
                                   last_seen_run="r1"))
        hits = search(conn, query="invoice")
        assert len(hits) == 2
        assert {h.path for h in hits} == {"/t/0.pdf", "/t/copy/0.pdf"}
        conn.close()
