# fda/metadata/search.py
"""FTS5-backed search over the metadata layer.

Query model:
- `query` (str, optional): FTS5 match expression over (summary, keywords).
  Empty string skips FTS and just applies filters.
- Filters: department, document_type, confidentiality, language, since
  (ISO mtime cutoff), limit, fail_closed_only.

Returns one SearchHit per (sha256, path) pair so duplicate copies remain
visible.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchHit:
    sha256: str
    path: str
    department: str
    document_type: str
    confidentiality: str
    language: str
    summary: str
    confidence: float
    fail_closed_override: bool
    mtime: str


def search(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    department: str | None = None,
    document_type: str | None = None,
    confidentiality: str | None = None,
    language: str | None = None,
    since: str | None = None,
    fail_closed_only: bool = False,
    limit: int = 50,
) -> list[SearchHit]:
    select_cols = (
        "d.sha256, p.path, d.department, d.document_type, "
        "d.confidentiality, d.language, d.summary, d.confidence, "
        "d.fail_closed_override, p.mtime"
    )
    # FTS5 trigram requires ≥3 Unicode codepoints; fall back to LIKE for
    # short queries so Korean 2-syllable stems (e.g. "매출") still match.
    if query and len(query) >= 3:
        sql = (
            f"SELECT {select_cols} FROM documents_fts f "
            "JOIN documents d ON d.rowid = f.rowid "
            "JOIN document_paths p ON p.sha256 = d.sha256 "
            "WHERE documents_fts MATCH ?"
        )
        params: list = [query]
    elif query:
        like_pat = f"%{query}%"
        sql = (
            f"SELECT {select_cols} FROM documents d "
            "JOIN document_paths p ON p.sha256 = d.sha256 "
            "WHERE (d.summary LIKE ? OR d.keywords LIKE ?)"
        )
        params = [like_pat, like_pat]
    else:
        sql = (
            f"SELECT {select_cols} FROM documents d "
            "JOIN document_paths p ON p.sha256 = d.sha256 "
            "WHERE 1=1"
        )
        params = []
    if department:
        sql += " AND d.department = ?"; params.append(department)
    if document_type:
        sql += " AND d.document_type = ?"; params.append(document_type)
    if confidentiality:
        sql += " AND d.confidentiality = ?"; params.append(confidentiality)
    if language:
        sql += " AND d.language = ?"; params.append(language)
    if since:
        sql += " AND p.mtime >= ?"; params.append(since)
    if fail_closed_only:
        sql += " AND d.fail_closed_override = 1"
    sql += " ORDER BY p.mtime DESC LIMIT ?"; params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [SearchHit(
        sha256=r[0], path=r[1], department=r[2], document_type=r[3],
        confidentiality=r[4], language=r[5], summary=r[6],
        confidence=r[7], fail_closed_override=bool(r[8]), mtime=r[9],
    ) for r in rows]
