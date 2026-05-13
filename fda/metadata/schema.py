# fda/metadata/schema.py
"""Pydantic v2 strict models for classifier output + SQLite DDL constants.

The Classification model is the *boundary* between Claude and our DB. It
rejects unknown keys (strict mode), unknown vocabulary codes, and out-of-
range confidence. The fail-closed confidentiality rule (`confidence < 0.3
→ confidentiality must be 'restricted'`) is NOT enforced here — it is a
deterministic post-processing pass in classifier.py. See the spec at
docs/superpowers/specs/2026-05-12-local-metadata-layer-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fda.metadata.vocab import CONFIDENTIALITY, DEPARTMENTS, DOCUMENT_TYPES


Department = Literal[
    "sales", "finance", "hr", "production", "rd",
    "legal", "operations", "marketing", "executive", "unknown",
]
DocumentType = Literal[
    "invoice", "contract", "report", "proposal", "memo",
    "policy", "presentation", "spreadsheet", "image", "data",
    "archive", "correspondence", "unknown",
]
Confidentiality = Literal["public", "internal", "confidential", "restricted"]


class Keywords(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ko: list[str] = Field(default_factory=list, max_length=8)
    en: list[str] = Field(default_factory=list, max_length=8)


class Classification(BaseModel):
    """One classifier output record. Strict mode; unknown keys rejected."""
    model_config = ConfigDict(extra="forbid", strict=True)

    department: Department
    document_type: DocumentType
    confidentiality: Confidentiality
    summary: str
    keywords: Keywords
    confidence: float = Field(ge=0.0, le=1.0)
    fail_closed_override: bool = False


@dataclass(frozen=True)
class DocumentRow:
    """Persisted shape for one `documents` row (one per sha256)."""
    sha256: str
    mime: str
    size_bytes: int
    language: str
    department: str
    document_type: str
    confidentiality: str
    summary: str
    keywords_json: str
    confidence: float
    fail_closed_override: bool
    extract_status: str
    sharepoint_url: str | None
    run_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PathRow:
    """Persisted shape for one `document_paths` row (one per location)."""
    path_id: str
    sha256: str
    path: str
    mtime: str
    last_seen_run: str


@dataclass(frozen=True)
class RunReport:
    """Returned by fda.metadata.run() — surfaces to progress_callback."""
    run_id: str
    files_seen: int
    files_classified: int
    files_failed: int
    batches_total: int
    batches_retried: int


# --- SQLite DDL ---------------------------------------------------------------

DDL_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
  sha256 TEXT PRIMARY KEY,
  mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  language TEXT NOT NULL,
  department TEXT NOT NULL,
  document_type TEXT NOT NULL,
  confidentiality TEXT NOT NULL,
  summary TEXT NOT NULL,
  keywords TEXT NOT NULL,
  confidence REAL NOT NULL,
  fail_closed_override INTEGER NOT NULL DEFAULT 0,
  extract_status TEXT NOT NULL,
  sharepoint_url TEXT,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""

DDL_DOCUMENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_documents_department ON documents(department);",
    "CREATE INDEX IF NOT EXISTS idx_documents_doctype ON documents(document_type);",
    "CREATE INDEX IF NOT EXISTS idx_documents_conf ON documents(confidentiality);",
    "CREATE INDEX IF NOT EXISTS idx_documents_language ON documents(language);",
)

DDL_DOCUMENT_PATHS = """
CREATE TABLE IF NOT EXISTS document_paths (
  path TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  path_id TEXT NOT NULL,
  mtime TEXT NOT NULL,
  last_seen_run TEXT NOT NULL,
  FOREIGN KEY (sha256) REFERENCES documents(sha256) ON DELETE CASCADE,
  FOREIGN KEY (last_seen_run) REFERENCES runs(run_id)
);
"""

DDL_PATH_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_document_paths_sha256 ON document_paths(sha256);",
    "CREATE INDEX IF NOT EXISTS idx_document_paths_mtime ON document_paths(mtime);",
)

DDL_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  target_root TEXT NOT NULL,
  files_seen INTEGER NOT NULL,
  files_classified INTEGER NOT NULL,
  files_failed INTEGER NOT NULL,
  batches_total INTEGER NOT NULL,
  batches_retried INTEGER NOT NULL,
  business_context_sha256 TEXT,
  fda_version TEXT NOT NULL,
  model TEXT NOT NULL
);
"""

DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  summary, keywords,
  content='documents', content_rowid='rowid',
  tokenize='trigram'
);
"""

DDL_FTS_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
      INSERT INTO documents_fts(rowid, summary, keywords)
      VALUES (new.rowid, new.summary, new.keywords);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
      INSERT INTO documents_fts(documents_fts, rowid, summary, keywords)
      VALUES ('delete', old.rowid, old.summary, old.keywords);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
      INSERT INTO documents_fts(documents_fts, rowid, summary, keywords)
      VALUES ('delete', old.rowid, old.summary, old.keywords);
      INSERT INTO documents_fts(rowid, summary, keywords)
      VALUES (new.rowid, new.summary, new.keywords);
    END;
    """,
)
