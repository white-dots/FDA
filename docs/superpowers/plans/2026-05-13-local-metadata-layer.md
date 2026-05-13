# Local Metadata Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build stage 5 of `fda organize` — a per-file metadata layer that classifies every file with `department`, `document_type`, `confidentiality`, `summary`, and bilingual `keywords` via batched Sonnet calls, persists rows to SQLite + FTS5 at `~/.fda/metadata.db`, and exposes `fda metadata` / `fda search` CLI subcommands.

**Architecture:** A new `fda/metadata/` package: Pydantic v2 strict schema → deterministic enrichment (sha256, mime, language) → batched Claude classifier with bisect-on-retry-failure + post-process fail-closed override → SQLite storage with `documents` (per-sha256) + `document_paths` (per-location) + FTS5 trigram index → keyword search with Korean-particle matching. Runs as the final stage of `fda organize` (opt-out via `--no-metadata`) or standalone via `fda metadata`. Spec: `docs/superpowers/specs/2026-05-12-local-metadata-layer-design.md`.

**Tech Stack:** Python 3.12, Pydantic v2, sqlite3 (FTS5 + trigram), Claude Sonnet (`claude-sonnet-4-6`), `fcntl` for advisory locking.

**Test runner:** `.venv/bin/python -m pytest tests/ -x -q --tb=short` (the path in CLAUDE.md is wrong on this machine — see memory `feedback_fda_python_path.md`).

---

## File Structure

**New files:**

- `fda/metadata/__init__.py` — public `run(target, *, catalog, backend, logger, progress_callback)` entry; orchestrates enrich → classify → store → prune; writes audit sidecar.
- `fda/metadata/vocab.py` — canonical English codes + Korean display labels for `department`, `document_type`, `confidentiality`.
- `fda/metadata/schema.py` — Pydantic v2 strict models + SQLite DDL string constants + `RunReport` / `DocumentRow` / `PathRow` dataclasses.
- `fda/metadata/enrich.py` — `sha256_of(path)`, `mime_of(path)`, `language_of(text)`, `mtime_iso(path)`. No LLM.
- `fda/metadata/store.py` — `connect()`, `_assert_fts5_trigram()`, `init_schema()`, `upsert_document()`, `upsert_path()`, `prune_missing_paths()`, `acquire_lock()`.
- `fda/metadata/context.py` — load `~/.fda/business_context.md`: missing-OK, 50KB cap.
- `fda/metadata/classifier.py` — batched Sonnet calls with retry+bisect; post-process fail-closed override; returns validated `Classification` records.
- `fda/metadata/search.py` — FTS5 query builder + filter composition; returns `(sha256, path, …)` rows.
- `fda/metadata/cli.py` — `handle_metadata()` and `handle_search()` argparse handlers.
- `fda/metadata/skills/metadata-classifier/SKILL.md` — Sonnet prompt + JSON schema.
- `tests/test_metadata_schema.py` — Pydantic validation tests (matrix rows 1–5).
- `tests/test_metadata_enrich.py` — enrich function tests.
- `tests/test_metadata_store.py` — DDL, probe, triggers, upsert, prune, WAL (matrix rows 9–16, 22).
- `tests/test_metadata_classifier.py` — fail-closed override + post-process tests (matrix rows 6–8).
- `tests/test_metadata_retry.py` — retry + bisect tests (matrix rows 17–19).
- `tests/test_metadata_context.py` — business_context loading (matrix rows 20–21).
- `tests/test_metadata_search.py` — FTS5 + filter tests (matrix rows 31–34).
- `tests/test_metadata_cli.py` — CLI flag matrix, exit codes, lock contention (matrix rows 23, 26–30).
- `tests/test_organize_metadata_integration.py` — stage 5 hook + progress_callback (matrix rows 24–25).
- `tests/fixtures/metadata_corpus/` — synthetic Korean + English files for live smoke.
- `docs/superpowers/plans/2026-05-13-local-metadata-layer.md` — this plan.

**Modified files:**

- `fda/cli.py` — add `--no-metadata`, `--metadata-only` to `organize`; add `metadata` and `search` subparsers.
- `fda/local_worker_agent.py:899-927` — forward `metadata`, `metadata_only`, `no_metadata` flags through `organize_files`.
- `fda/organize/__init__.py:31-188` — call `fda.metadata.run(...)` after `router.route(...)` with progress_callback counts.
- `pyproject.toml:11-22` — add `pydantic>=2.0`.

---

## Task 1: Add pydantic dependency

**Files:**
- Modify: `pyproject.toml:11-22`

- [ ] **Step 1: Edit dependencies block**

Edit `pyproject.toml` to add Pydantic v2 to the `[project] dependencies` array. The current array ends at line 22 with `"pyyaml>=6.0",`. Insert `"pydantic>=2.0",` before the closing `]`:

```toml
dependencies = [
    "anthropic>=0.45.0",
    "pandas>=1.5.0",
    "openpyxl>=3.0.9",
    "python-docx>=1.0.0",
    "python-pptx>=1.0.2",
    "defusedxml>=0.7.1",
    "pyhwp>=0.1b15",
    "msal>=1.20.0",
    "requests>=2.28.0",
    "pyyaml>=6.0",
    "pydantic>=2.0",
]
```

- [ ] **Step 2: Install into venv**

Run: `.venv/bin/pip install -e .`
Expected: pydantic-2.x installs without error.

- [ ] **Step 3: Verify import**

Run: `.venv/bin/python -c "import pydantic; print(pydantic.VERSION)"`
Expected: prints a version string starting with `2.`.

- [ ] **Step 4: Run existing tests to confirm nothing broke**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS (113 tests baseline).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "metadata: add pydantic v2 dependency"
```

---

## Task 2: Vocabulary module

**Files:**
- Create: `fda/metadata/__init__.py` (empty placeholder for now)
- Create: `fda/metadata/vocab.py`
- Create: `tests/test_metadata_vocab.py`

- [ ] **Step 1: Create empty package marker**

Create `fda/metadata/__init__.py` with one line:

```python
"""FDA metadata layer (stage 5 of `fda organize`)."""
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_metadata_vocab.py`:

```python
# tests/test_metadata_vocab.py
"""Tests for fda.metadata.vocab."""
from __future__ import annotations

import pytest


class TestVocab:
    def test_department_codes_are_stable_lowercase_english(self):
        from fda.metadata.vocab import DEPARTMENTS
        assert "sales" in DEPARTMENTS
        assert "finance" in DEPARTMENTS
        assert "hr" in DEPARTMENTS
        assert "production" in DEPARTMENTS
        assert "rd" in DEPARTMENTS
        assert "legal" in DEPARTMENTS
        assert "operations" in DEPARTMENTS
        assert "marketing" in DEPARTMENTS
        assert "executive" in DEPARTMENTS
        assert "unknown" in DEPARTMENTS

    def test_document_type_codes(self):
        from fda.metadata.vocab import DOCUMENT_TYPES
        assert {"invoice", "contract", "report", "proposal", "memo",
                "policy", "presentation", "spreadsheet", "image", "data",
                "archive", "correspondence", "unknown"}.issubset(set(DOCUMENT_TYPES))

    def test_confidentiality_codes(self):
        from fda.metadata.vocab import CONFIDENTIALITY
        assert set(CONFIDENTIALITY) == {"public", "internal", "confidential",
                                         "restricted"}

    def test_korean_labels_resolve_for_every_department_code(self):
        from fda.metadata.vocab import DEPARTMENTS, ko_label_for_department
        for code in DEPARTMENTS:
            label = ko_label_for_department(code)
            assert isinstance(label, str) and len(label) > 0

    def test_korean_label_for_unknown_code_falls_back_to_english(self):
        from fda.metadata.vocab import ko_label_for_department
        assert ko_label_for_department("not-a-real-code") == "not-a-real-code"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_metadata_vocab.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fda.metadata.vocab'`.

- [ ] **Step 4: Implement vocab.py**

Create `fda/metadata/vocab.py`:

```python
# fda/metadata/vocab.py
"""Canonical English vocabularies for the metadata layer + Korean display
labels for UI rendering.

The DB stores stable English codes (these maps are the source of truth).
Adding a code here is a code change. Mapping a customer's local Korean name
(e.g. `영업기획부`) to a canonical code (`sales`) is handled by their
`~/.fda/business_context.md`, not by editing this file.
"""
from __future__ import annotations

DEPARTMENTS: tuple[str, ...] = (
    "sales", "finance", "hr", "production", "rd",
    "legal", "operations", "marketing", "executive", "unknown",
)

DOCUMENT_TYPES: tuple[str, ...] = (
    "invoice", "contract", "report", "proposal", "memo",
    "policy", "presentation", "spreadsheet", "image", "data",
    "archive", "correspondence", "unknown",
)

CONFIDENTIALITY: tuple[str, ...] = (
    "public", "internal", "confidential", "restricted",
)

_DEPARTMENT_KO: dict[str, str] = {
    "sales": "영업",
    "finance": "재무",
    "hr": "인사",
    "production": "생산",
    "rd": "연구개발",
    "legal": "법무",
    "operations": "운영",
    "marketing": "마케팅",
    "executive": "경영진",
    "unknown": "미분류",
}

_DOCUMENT_TYPE_KO: dict[str, str] = {
    "invoice": "청구서",
    "contract": "계약서",
    "report": "보고서",
    "proposal": "제안서",
    "memo": "메모",
    "policy": "정책문서",
    "presentation": "발표자료",
    "spreadsheet": "스프레드시트",
    "image": "이미지",
    "data": "데이터",
    "archive": "압축파일",
    "correspondence": "서신",
    "unknown": "미분류",
}

_CONFIDENTIALITY_KO: dict[str, str] = {
    "public": "공개",
    "internal": "내부",
    "confidential": "기밀",
    "restricted": "제한",
}


def ko_label_for_department(code: str) -> str:
    """Return Korean label for a department code; fall back to the code itself."""
    return _DEPARTMENT_KO.get(code, code)


def ko_label_for_document_type(code: str) -> str:
    return _DOCUMENT_TYPE_KO.get(code, code)


def ko_label_for_confidentiality(code: str) -> str:
    return _CONFIDENTIALITY_KO.get(code, code)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_metadata_vocab.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add fda/metadata/__init__.py fda/metadata/vocab.py tests/test_metadata_vocab.py
git commit -m "metadata: add canonical vocabularies + Korean label map"
```

---

## Task 3: Pydantic schema models

**Files:**
- Create: `fda/metadata/schema.py`
- Create: `tests/test_metadata_schema.py`

Covers test-matrix rows 1–5.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_schema.py`:

```python
# tests/test_metadata_schema.py
"""Tests for fda.metadata.schema Pydantic models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _valid_kwargs():
    return dict(
        department="finance",
        document_type="invoice",
        confidentiality="confidential",
        summary="2025년 3분기 매출 청구서",
        keywords={"ko": ["청구서", "2025"], "en": ["invoice", "2025"]},
        confidence=0.82,
    )


class TestClassification:
    def test_accepts_valid_record(self):
        from fda.metadata.schema import Classification
        c = Classification(**_valid_kwargs())
        assert c.department == "finance"
        assert c.fail_closed_override is False

    def test_rejects_unknown_department(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["department"] = "not-a-real-department"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_unknown_document_type(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["document_type"] = "novel"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_unknown_confidentiality(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["confidentiality"] = "super-secret"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_keywords_over_eight_per_language(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["keywords"] = {"ko": [f"k{i}" for i in range(9)], "en": []}
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_rejects_unknown_keys_strict_mode(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["extra_field"] = "nope"
        with pytest.raises(ValidationError):
            Classification(**kw)

    def test_confidence_must_be_in_range(self):
        from fda.metadata.schema import Classification
        kw = _valid_kwargs()
        kw["confidence"] = 1.5
        with pytest.raises(ValidationError):
            Classification(**kw)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fda.metadata.schema'`.

- [ ] **Step 3: Implement schema.py**

Create `fda/metadata/schema.py`:

```python
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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, v):
        # Allow dict input from JSON (Pydantic v2 will validate it).
        if isinstance(v, dict):
            return Keywords(**v)
        return v


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_schema.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/schema.py tests/test_metadata_schema.py
git commit -m "metadata: add Pydantic schema models + SQLite DDL constants"
```

---

## Task 4: Enrich module (sha256, mime, language, mtime)

**Files:**
- Create: `fda/metadata/enrich.py`
- Create: `tests/test_metadata_enrich.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_enrich.py`:

```python
# tests/test_metadata_enrich.py
"""Tests for fda.metadata.enrich."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestSha256:
    def test_hash_is_deterministic(self, tmp_path):
        from fda.metadata.enrich import sha256_of
        p = tmp_path / "a.txt"
        p.write_bytes(b"hello world")
        h1 = sha256_of(p)
        h2 = sha256_of(p)
        assert h1 == h2
        assert len(h1) == 64
        assert all(c in "0123456789abcdef" for c in h1)

    def test_different_content_yields_different_hash(self, tmp_path):
        from fda.metadata.enrich import sha256_of
        a = tmp_path / "a.txt"; a.write_bytes(b"AAA")
        b = tmp_path / "b.txt"; b.write_bytes(b"BBB")
        assert sha256_of(a) != sha256_of(b)


class TestMime:
    def test_detects_pdf_by_extension(self, tmp_path):
        from fda.metadata.enrich import mime_of
        p = tmp_path / "doc.pdf"; p.write_bytes(b"%PDF-1.4\n")
        assert mime_of(p) == "application/pdf"

    def test_detects_text_for_unknown_extension(self, tmp_path):
        from fda.metadata.enrich import mime_of
        p = tmp_path / "doc.unknownext"; p.write_text("hello", encoding="utf-8")
        mime = mime_of(p)
        assert isinstance(mime, str) and mime != ""


class TestLanguage:
    def test_detects_korean_when_hangul_present(self):
        from fda.metadata.enrich import language_of
        assert language_of("안녕하세요 매출 보고서입니다") == "ko"

    def test_detects_english_when_no_hangul(self):
        from fda.metadata.enrich import language_of
        assert language_of("This is an invoice for Q3 2025.") == "en"

    def test_detects_korean_for_mixed_text_with_any_hangul(self):
        from fda.metadata.enrich import language_of
        # "Korean wins" — any hangul flips to ko in the simple heuristic.
        assert language_of("Invoice 청구서") == "ko"

    def test_empty_text_returns_unknown(self):
        from fda.metadata.enrich import language_of
        assert language_of("") == "unknown"


class TestMtime:
    def test_returns_iso_8601_utc(self, tmp_path):
        from fda.metadata.enrich import mtime_iso
        p = tmp_path / "a.txt"; p.write_text("x")
        s = mtime_iso(p)
        # Format: "2026-05-13T..." ending with "Z"
        assert s.endswith("Z")
        assert "T" in s
        assert s[:4].isdigit()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_enrich.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement enrich.py**

Create `fda/metadata/enrich.py`:

```python
# fda/metadata/enrich.py
"""Deterministic per-file enrichment: sha256, mime, language, mtime.

No LLM. No network. Pure functions of file content + path.
"""
from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

_HANGUL_RANGES = ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F))


def sha256_of(path: Path | str, *, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of the file's content, hex-encoded."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def mime_of(path: Path | str) -> str:
    """Best-effort MIME type. Falls back to application/octet-stream."""
    guess, _ = mimetypes.guess_type(str(path))
    if guess:
        return guess
    # mimetypes returns None for many text-y files; return a generic fallback.
    return "application/octet-stream"


def language_of(text: str) -> str:
    """Tiny language heuristic: ko if any Hangul codepoint, else en, else unknown.

    Deliberately simple — the metadata layer just needs a 'which surface
    label set should I default to' hint. Real per-document language is a
    Phase 2 concern.
    """
    if not text:
        return "unknown"
    for ch in text:
        cp = ord(ch)
        for lo, hi in _HANGUL_RANGES:
            if lo <= cp <= hi:
                return "ko"
    return "en"


def mtime_iso(path: Path | str) -> str:
    """File mtime as ISO-8601 UTC ending in Z (seconds resolution)."""
    ts = Path(path).stat().st_mtime
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_enrich.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/enrich.py tests/test_metadata_enrich.py
git commit -m "metadata: add deterministic enrich (sha256, mime, language, mtime)"
```

---

## Task 5: Storage — FTS5 probe + connect + init_schema + WAL

**Files:**
- Create: `fda/metadata/store.py`
- Create: `tests/test_metadata_store.py`

Covers test-matrix rows 9–13, 22.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_store.py`:

```python
# tests/test_metadata_store.py
"""Tests for fda.metadata.store."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

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
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
            ).fetchall()}
            # documents_fts is a virtual table but registers as type='table'
            # in sqlite_master; check name presence:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement store.py (probe + connect + init_schema only — upsert lands in Task 6)**

Create `fda/metadata/store.py`:

```python
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


def _assert_fts5_trigram(conn: sqlite3.Connection) -> None:
    """Verify FTS5 + trigram tokenizer are available; raise actionable error otherwise."""
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
    if n_docs > 0 and n_fts == 0:
        conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/store.py tests/test_metadata_store.py
git commit -m "metadata: add SQLite store (FTS5 probe, schema init, sync triggers)"
```

---

## Task 6: Storage — upsert + path tracking + prune

**Files:**
- Modify: `fda/metadata/store.py` (append upsert/prune functions)
- Modify: `tests/test_metadata_store.py` (append upsert/prune tests)

Covers test-matrix rows 14–16.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metadata_store.py`:

```python
class TestUpsert:
    def _setup(self, tmp_path):
        from fda.metadata.store import connect, init_schema, insert_run
        conn = connect(tmp_path / "m.db")
        init_schema(conn)
        insert_run(conn, run_id="r1", target_root="/t",
                   model="claude-sonnet-4-6", fda_version="0.1.0",
                   business_context_sha256=None,
                   started_at="2026-05-13T00:00:00Z")
        return conn

    def test_one_sha256_with_two_paths_yields_two_path_rows(self, tmp_path):
        from fda.metadata.store import (
            upsert_document, upsert_path,
        )
        from fda.metadata.schema import DocumentRow, PathRow
        conn = self._setup(tmp_path)
        try:
            doc = DocumentRow(
                sha256="a" * 64, mime="application/pdf", size_bytes=10,
                language="ko", department="finance", document_type="invoice",
                confidentiality="confidential", summary="x",
                keywords_json='{"ko":[],"en":[]}', confidence=0.9,
                fail_closed_override=False, extract_status="ok",
                sharepoint_url=None, run_id="r1",
                created_at="2026-05-13T00:00:00Z",
                updated_at="2026-05-13T00:00:00Z",
            )
            upsert_document(conn, doc)
            upsert_path(conn, PathRow(
                path_id="f000", sha256="a" * 64, path="/t/a.pdf",
                mtime="2026-05-13T00:00:00Z", last_seen_run="r1",
            ))
            upsert_path(conn, PathRow(
                path_id="f001", sha256="a" * 64, path="/t/copy/a.pdf",
                mtime="2026-05-13T00:00:00Z", last_seen_run="r1",
            ))
            rows = conn.execute(
                "SELECT d.sha256, p.path FROM documents d "
                "JOIN document_paths p ON p.sha256 = d.sha256 "
                "ORDER BY p.path"
            ).fetchall()
            assert len(rows) == 2
            assert rows[0][1] == "/t/a.pdf"
            assert rows[1][1] == "/t/copy/a.pdf"
        finally:
            conn.close()

    def test_rerun_same_path_bumps_last_seen(self, tmp_path):
        from fda.metadata.store import upsert_document, upsert_path, insert_run
        from fda.metadata.schema import DocumentRow, PathRow
        conn = self._setup(tmp_path)
        try:
            sha = "a" * 64
            upsert_document(conn, DocumentRow(
                sha256=sha, mime="application/pdf", size_bytes=1,
                language="ko", department="finance", document_type="invoice",
                confidentiality="confidential", summary="x",
                keywords_json='{"ko":[],"en":[]}', confidence=0.9,
                fail_closed_override=False, extract_status="ok",
                sharepoint_url=None, run_id="r1",
                created_at="2026-05-13T00:00:00Z",
                updated_at="2026-05-13T00:00:00Z",
            ))
            upsert_path(conn, PathRow(
                path_id="f000", sha256=sha, path="/t/a.pdf",
                mtime="2026-05-13T00:00:00Z", last_seen_run="r1",
            ))
            insert_run(conn, run_id="r2", target_root="/t",
                       model="claude-sonnet-4-6", fda_version="0.1.0",
                       business_context_sha256=None,
                       started_at="2026-05-13T01:00:00Z")
            upsert_path(conn, PathRow(
                path_id="f000", sha256=sha, path="/t/a.pdf",
                mtime="2026-05-13T01:00:00Z", last_seen_run="r2",
            ))
            row = conn.execute(
                "SELECT path_id, last_seen_run, mtime FROM document_paths "
                "WHERE path = '/t/a.pdf'"
            ).fetchone()
            assert row == ("f000", "r2", "2026-05-13T01:00:00Z")
            n = conn.execute("SELECT count(*) FROM document_paths").fetchone()[0]
            assert n == 1
        finally:
            conn.close()

    def test_two_runs_can_both_use_f000_for_different_paths(self, tmp_path):
        """path_id is per-run; different trees reuse it. The DB must allow
        path_id='f000' to appear twice as long as the paths differ.
        Regression guard against the spec/plan bug where path_id was PK.
        """
        from fda.metadata.store import (
            insert_run, upsert_document, upsert_path,
        )
        from fda.metadata.schema import DocumentRow, PathRow
        conn = self._setup(tmp_path)
        try:
            sha_a, sha_b = "a" * 64, "b" * 64
            for sha in (sha_a, sha_b):
                upsert_document(conn, DocumentRow(
                    sha256=sha, mime="application/pdf", size_bytes=1,
                    language="ko", department="finance",
                    document_type="invoice", confidentiality="confidential",
                    summary="x", keywords_json='{"ko":[],"en":[]}',
                    confidence=0.9, fail_closed_override=False,
                    extract_status="ok", sharepoint_url=None, run_id="r1",
                    created_at="2026-05-13T00:00:00Z",
                    updated_at="2026-05-13T00:00:00Z",
                ))
            insert_run(conn, run_id="r2", target_root="/B",
                       model="claude-sonnet-4-6", fda_version="0.1.0",
                       business_context_sha256=None,
                       started_at="2026-05-13T01:00:00Z")
            upsert_path(conn, PathRow(path_id="f000", sha256=sha_a,
                                       path="/A/x.pdf",
                                       mtime="2026-05-13T00:00:00Z",
                                       last_seen_run="r1"))
            upsert_path(conn, PathRow(path_id="f000", sha256=sha_b,
                                       path="/B/x.pdf",
                                       mtime="2026-05-13T01:00:00Z",
                                       last_seen_run="r2"))
            paths = sorted(r[0] for r in conn.execute(
                "SELECT path FROM document_paths"
            ).fetchall())
            assert paths == ["/A/x.pdf", "/B/x.pdf"]
        finally:
            conn.close()


class TestPrune:
    def test_prune_removes_paths_under_target_not_seen_this_run(self, tmp_path):
        from fda.metadata.store import (
            connect, init_schema, insert_run, prune_missing_paths,
        )
        from fda.metadata.schema import PathRow
        conn = connect(tmp_path / "m.db")
        init_schema(conn)
        insert_run(conn, run_id="r1", target_root="/t",
                   model="claude-sonnet-4-6", fda_version="0.1.0",
                   business_context_sha256=None,
                   started_at="2026-05-13T00:00:00Z")
        sha = "b" * 64
        conn.execute(
            "INSERT INTO documents(sha256, mime, size_bytes, language, "
            "department, document_type, confidentiality, summary, keywords, "
            "confidence, extract_status, run_id, created_at, updated_at) "
            "VALUES (?, 'application/pdf', 1, 'ko', 'finance', 'invoice', "
            "'confidential', 'x', '{}', 0.9, 'ok', 'r1', "
            "'2026-05-13T00:00:00Z', '2026-05-13T00:00:00Z')",
            (sha,),
        )
        from fda.metadata.store import upsert_path
        upsert_path(conn, PathRow(path_id="f000", sha256=sha,
                                   path="/t/old.pdf",
                                   mtime="2026-05-13T00:00:00Z",
                                   last_seen_run="r1"))
        upsert_path(conn, PathRow(path_id="f001", sha256=sha,
                                   path="/elsewhere/keep.pdf",
                                   mtime="2026-05-13T00:00:00Z",
                                   last_seen_run="r1"))
        # New run sees only /t/new.pdf under target /t.
        insert_run(conn, run_id="r2", target_root="/t",
                   model="claude-sonnet-4-6", fda_version="0.1.0",
                   business_context_sha256=None,
                   started_at="2026-05-13T01:00:00Z")
        upsert_path(conn, PathRow(path_id="f002", sha256=sha,
                                   path="/t/new.pdf",
                                   mtime="2026-05-13T01:00:00Z",
                                   last_seen_run="r2"))
        prune_missing_paths(conn, target_root="/t", current_run_id="r2")
        paths = sorted(r[0] for r in conn.execute(
            "SELECT path FROM document_paths"
        ).fetchall())
        # /t/old.pdf pruned (under target, not seen this run).
        # /elsewhere/keep.pdf kept (not under target).
        # /t/new.pdf kept (seen this run).
        assert paths == ["/elsewhere/keep.pdf", "/t/new.pdf"]
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_store.py::TestUpsert tests/test_metadata_store.py::TestPrune -v`
Expected: FAIL with `ImportError: cannot import name 'insert_run'` (or similar).

- [ ] **Step 3: Append upsert + prune to store.py**

Append to `fda/metadata/store.py`:

```python
from fda.metadata.schema import DocumentRow, PathRow  # added import


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
        "AND (path = ? OR path LIKE ? || '/%')",
        (current_run_id, target_root, target_root),
    )
    return cur.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: all tests pass (8 from Task 5 + 3 new).

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/store.py tests/test_metadata_store.py
git commit -m "metadata: add upsert + path tracking + prune-missing helpers"
```

---

## Task 7: Lock acquisition (advisory flock)

**Files:**
- Modify: `fda/metadata/store.py` (append `acquire_lock` context manager)
- Modify: `tests/test_metadata_store.py` (append lock test)

Covers test-matrix row 23.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metadata_store.py`:

```python
class TestLock:
    def test_second_lock_attempt_raises(self, tmp_path):
        from fda.metadata.store import acquire_lock, LockBusy
        lock_path = tmp_path / "m.db.lock"
        with acquire_lock(lock_path):
            with pytest.raises(LockBusy):
                with acquire_lock(lock_path):
                    pass  # never reached

    def test_lock_released_after_context_exit(self, tmp_path):
        from fda.metadata.store import acquire_lock
        lock_path = tmp_path / "m.db.lock"
        with acquire_lock(lock_path):
            pass
        # Should re-acquire cleanly.
        with acquire_lock(lock_path):
            pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_metadata_store.py::TestLock -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `acquire_lock`**

Append to `fda/metadata/store.py`:

```python
import fcntl
from contextlib import contextmanager
from typing import Iterator


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_store.py::TestLock -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/store.py tests/test_metadata_store.py
git commit -m "metadata: add fcntl-based advisory lock with LockBusy"
```

---

## Task 8: Business context loader

**Files:**
- Create: `fda/metadata/context.py`
- Create: `tests/test_metadata_context.py`

Covers test-matrix rows 20–21.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_context.py`:

```python
# tests/test_metadata_context.py
"""Tests for fda.metadata.context (business_context.md loader)."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestLoadContext:
    def test_missing_file_returns_empty_text_and_none_sha(self, tmp_path):
        from fda.metadata.context import load_business_context
        path = tmp_path / "does_not_exist.md"
        result = load_business_context(path)
        assert result.text == ""
        assert result.sha256 is None
        assert result.was_truncated is False
        assert "no business_context.md" in (result.info_message or "").lower()

    def test_normal_file_loads_full_content(self, tmp_path):
        from fda.metadata.context import load_business_context
        path = tmp_path / "bc.md"
        path.write_text("## Departments\n- 영업 (sales)\n", encoding="utf-8")
        result = load_business_context(path)
        assert "영업" in result.text
        assert result.sha256 is not None
        assert len(result.sha256) == 64
        assert result.was_truncated is False

    def test_oversize_file_truncates_at_50kb_and_warns(self, tmp_path):
        from fda.metadata.context import load_business_context, MAX_BYTES
        path = tmp_path / "bc.md"
        big = "x" * (MAX_BYTES + 1000)
        path.write_text(big, encoding="utf-8")
        result = load_business_context(path)
        assert result.was_truncated is True
        assert len(result.text.encode("utf-8")) <= MAX_BYTES
        assert result.sha256 is not None
        assert "truncated" in (result.info_message or "").lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_context.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement context.py**

Create `fda/metadata/context.py`:

```python
# fda/metadata/context.py
"""Loader for ~/.fda/business_context.md.

Rules (per spec § Business context injection):
- Missing file: return empty text, info message, sha=None.
- Oversize file (> 50KB): truncate to 50KB on a UTF-8 char boundary,
  warn via info_message, SHA over the truncated payload.
- Cap is on the BYTES we send to the model (token budget), not on the
  user's file on disk.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 50 * 1024  # 50 KB


@dataclass(frozen=True)
class BusinessContext:
    text: str
    sha256: str | None
    was_truncated: bool
    info_message: str | None


def _truncate_utf8(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # Truncate to max_bytes, then back off to a valid UTF-8 boundary.
    trimmed = encoded[:max_bytes]
    while trimmed:
        try:
            return trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return ""


def load_business_context(path: Path | str) -> BusinessContext:
    p = Path(path)
    if not p.exists():
        return BusinessContext(
            text="",
            sha256=None,
            was_truncated=False,
            info_message=(
                f"ℹ no business_context.md at {p}; using generic classification"
            ),
        )
    raw = p.read_text(encoding="utf-8")
    raw_bytes = raw.encode("utf-8")
    truncated = len(raw_bytes) > MAX_BYTES
    text = _truncate_utf8(raw, MAX_BYTES) if truncated else raw
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    info = None
    if truncated:
        info = (
            f"⚠ business_context.md is {len(raw_bytes)}B; "
            f"truncated to {MAX_BYTES}B for prompt budget"
        )
    return BusinessContext(
        text=text, sha256=sha, was_truncated=truncated, info_message=info,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_context.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/context.py tests/test_metadata_context.py
git commit -m "metadata: add business_context.md loader (missing-OK, 50KB cap)"
```

---

## Task 9: Classifier SKILL.md

**Files:**
- Create: `fda/metadata/skills/metadata-classifier/SKILL.md`

No test; this is a prompt asset. Validation arrives via Task 10's classifier tests.

- [ ] **Step 1: Write the skill file**

Create `fda/metadata/skills/metadata-classifier/SKILL.md`:

```markdown
---
name: metadata-classifier
description: Classify a batch of files (≤10) with department, document_type, confidentiality, summary, bilingual keywords, and a calibrated confidence.
model: claude-sonnet-4-6
---

You classify a batch of files for a per-file metadata index. Each file
gets one record. Your output is a JSON array, one element per input
file, in the SAME order as the input.

Output schema (per record, all fields REQUIRED, NO unknown keys):

```
{
  "department": "<one of: sales|finance|hr|production|rd|legal|operations|marketing|executive|unknown>",
  "document_type": "<one of: invoice|contract|report|proposal|memo|policy|presentation|spreadsheet|image|data|archive|correspondence|unknown>",
  "confidentiality": "<one of: public|internal|confidential|restricted>",
  "summary": "<one short paragraph (~2 sentences) in the file's source language>",
  "keywords": {"ko": ["..."], "en": ["..."]},
  "confidence": <float in [0.0, 1.0]>
}
```

Output rules:

- Emit ONLY a JSON array. No markdown fences, no prose, no commentary.
- One element per input file, same order.
- Use ONLY the codes listed above. If unsure, use `unknown` for
  department/document_type. NEVER invent codes.
- `summary` is in the file's source language (Korean files → Korean
  summary; English files → English summary). Do NOT translate.
- `keywords.ko` and `keywords.en` are each 0–8 short strings (≤ 4
  words each). Provide both lists; a purely English file gets `"ko":
  []` and vice versa. When a file has both languages, populate both.
- `confidence` reflects YOUR honest belief. Do NOT inflate. We use
  low confidence as a fail-closed signal downstream.

Confidentiality rules:

- `restricted` — trade secrets, proprietary formulations, anything
  that would harm the company if leaked externally. ALSO use this
  when you are unsure: when in doubt, default to `restricted`.
- `confidential` — internal contracts, personnel records (salary,
  reviews), strategy decks, customer lists.
- `internal` — routine business documents (meeting minutes, ordinary
  reports, internal memos) not meant for external sharing.
- `public` — marketing materials marked for release, press releases,
  published reports.

The user's business context may follow this prompt; if present,
apply its rules. The user's local Korean department names (e.g.
`영업기획부`) map to the canonical English codes (e.g. `sales`)
per the business context map.

Input format:

You will receive a JSON object:

```
{
  "business_context": "<verbatim ~/.fda/business_context.md, may be empty>",
  "files": [
    {
      "path_id": "f042",
      "ext": ".pdf",
      "language_hint": "ko",
      "summary": "<text-extraction summary from FDA reader, source-language>",
      "verbatim_head": "<first ~500 chars of extracted text>"
    },
    ...
  ]
}
```

Process the `files` array in order, emit one record per file, return a
JSON array.
```

- [ ] **Step 2: Verify the skill is loadable**

Run: `.venv/bin/python -c "from fda.organize._skills import load_skill; from pathlib import Path; s = load_skill(Path('fda/metadata/skills/metadata-classifier')); print(s.name, s.model)"`
Expected: prints `metadata-classifier claude-sonnet-4-6`.

- [ ] **Step 3: Commit**

```bash
git add fda/metadata/skills/metadata-classifier/SKILL.md
git commit -m "metadata: add metadata-classifier SKILL.md prompt"
```

---

## Task 10: Classifier — single batch + fail-closed override

**Files:**
- Create: `fda/metadata/classifier.py`
- Create: `tests/test_metadata_classifier.py`

Covers test-matrix rows 6–8.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_classifier.py`:

```python
# tests/test_metadata_classifier.py
"""Tests for fda.metadata.classifier post-process override and single-batch call."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _record_dict(**overrides):
    base = dict(
        department="finance",
        document_type="invoice",
        confidentiality="internal",
        summary="x",
        keywords={"ko": [], "en": ["invoice"]},
        confidence=0.8,
    )
    base.update(overrides)
    return base


class TestFailClosedOverride:
    def test_override_fires_below_threshold(self):
        from fda.metadata.classifier import apply_fail_closed_override
        from fda.metadata.schema import Classification
        c = Classification(**_record_dict(confidence=0.2,
                                          confidentiality="internal"))
        out, fired = apply_fail_closed_override(c)
        assert out.confidentiality == "restricted"
        assert out.fail_closed_override is True
        assert fired is True

    def test_override_does_not_fire_above_threshold(self):
        from fda.metadata.classifier import apply_fail_closed_override
        from fda.metadata.schema import Classification
        c = Classification(**_record_dict(confidence=0.35,
                                          confidentiality="internal"))
        out, fired = apply_fail_closed_override(c)
        assert out.confidentiality == "internal"
        assert out.fail_closed_override is False
        assert fired is False

    def test_override_skipped_when_already_restricted(self):
        from fda.metadata.classifier import apply_fail_closed_override
        from fda.metadata.schema import Classification
        c = Classification(**_record_dict(confidence=0.1,
                                          confidentiality="restricted"))
        out, fired = apply_fail_closed_override(c)
        assert out.confidentiality == "restricted"
        assert out.fail_closed_override is False
        assert fired is False


class TestClassifyBatch:
    def test_single_batch_happy_path(self):
        from fda.metadata.classifier import classify_batch
        records = [_record_dict(), _record_dict(confidence=0.9)]
        backend = MagicMock()
        backend.complete.return_value = json.dumps(records)
        skill = MagicMock(body="prompt", model="claude-sonnet-4-6")
        files = [
            {"path_id": "f000", "ext": ".pdf", "language_hint": "en",
             "summary": "s1", "verbatim_head": "h1"},
            {"path_id": "f001", "ext": ".pdf", "language_hint": "en",
             "summary": "s2", "verbatim_head": "h2"},
        ]
        out = classify_batch(
            files=files, backend=backend, skill=skill, business_context="",
        )
        assert len(out) == 2
        assert out[0].department == "finance"
        assert out[1].confidence == 0.9
        backend.complete.assert_called_once()

    def test_classify_batch_raises_on_invalid_json(self):
        from fda.metadata.classifier import (
            ClassifierResponseError, classify_batch,
        )
        backend = MagicMock()
        backend.complete.return_value = "not json"
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        with pytest.raises(ClassifierResponseError):
            classify_batch(files=[{"path_id": "f000", "ext": ".pdf",
                                   "language_hint": "en", "summary": "s",
                                   "verbatim_head": "h"}],
                           backend=backend, skill=skill,
                           business_context="")

    def test_classify_batch_raises_on_count_mismatch(self):
        from fda.metadata.classifier import (
            ClassifierResponseError, classify_batch,
        )
        backend = MagicMock()
        backend.complete.return_value = json.dumps([_record_dict()])
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        with pytest.raises(ClassifierResponseError, match="count"):
            classify_batch(files=[
                {"path_id": "f000", "ext": ".pdf", "language_hint": "en",
                 "summary": "s", "verbatim_head": "h"},
                {"path_id": "f001", "ext": ".pdf", "language_hint": "en",
                 "summary": "s", "verbatim_head": "h"},
            ], backend=backend, skill=skill, business_context="")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement classifier.py (Task 10 surface only — bisect lands in Task 11)**

Create `fda/metadata/classifier.py`:

```python
# fda/metadata/classifier.py
"""Batched Sonnet classifier for the metadata layer.

Public surface (this file):
- classify_batch(files, backend, skill, business_context) -> list[Classification]
  One Claude call. Raises ClassifierResponseError on any structural problem.
- apply_fail_closed_override(record) -> (Classification, bool)
  Deterministic post-process; sets confidentiality='restricted' when
  confidence < FAIL_CLOSED_THRESHOLD and confidentiality is not already
  'restricted'. Returns the (possibly-replaced) record and a boolean
  indicating whether the override fired.

Task 11 will add classify_batch_with_retry_and_bisect() on top of this.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from fda.metadata.schema import Classification

logger = logging.getLogger(__name__)

FAIL_CLOSED_THRESHOLD = 0.3
MAX_CLAUDE_TOKENS = 4096


class ClassifierResponseError(Exception):
    """Raised when the classifier returns an unparseable or wrong-shaped JSON response."""


def _build_prompt_payload(
    files: list[dict[str, Any]], business_context: str,
) -> str:
    return json.dumps(
        {"business_context": business_context, "files": files},
        ensure_ascii=False,
    )


def _parse_response(raw: str, expected_count: int) -> list[Classification]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClassifierResponseError(
            f"classifier returned invalid JSON: {e}"
        ) from e
    if not isinstance(parsed, list):
        raise ClassifierResponseError(
            f"classifier response is not a JSON array (got {type(parsed).__name__})"
        )
    if len(parsed) != expected_count:
        raise ClassifierResponseError(
            f"classifier response count mismatch: got {len(parsed)}, "
            f"expected {expected_count}"
        )
    out: list[Classification] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ClassifierResponseError(
                f"classifier item {i} is not an object (got {type(item).__name__})"
            )
        try:
            out.append(Classification(**item))
        except ValidationError as e:
            raise ClassifierResponseError(
                f"classifier item {i} failed schema validation: {e}"
            ) from e
    return out


def classify_batch(
    *,
    files: list[dict[str, Any]],
    backend,
    skill,
    business_context: str,
) -> list[Classification]:
    """Run one Claude call over `files` (≤10). Returns validated records."""
    payload = _build_prompt_payload(files, business_context)
    raw = backend.complete(
        system=skill.body,
        messages=[{"role": "user", "content": payload}],
        model=skill.model,
        max_tokens=MAX_CLAUDE_TOKENS,
        temperature=0.0,
    )
    return _parse_response(raw, expected_count=len(files))


def apply_fail_closed_override(
    record: Classification,
) -> tuple[Classification, bool]:
    """Force confidentiality='restricted' when confidence < threshold.

    Returns (record, fired). Idempotent: a record already marked
    `restricted` is left alone, and `fail_closed_override` is preserved
    if the model emitted it (but typically the model returns False here).
    """
    if (
        record.confidence < FAIL_CLOSED_THRESHOLD
        and record.confidentiality != "restricted"
    ):
        replaced = record.model_copy(update={
            "confidentiality": "restricted",
            "fail_closed_override": True,
        })
        return replaced, True
    return record, False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_classifier.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/classifier.py tests/test_metadata_classifier.py
git commit -m "metadata: add classifier batch call + fail-closed post-process"
```

---

## Task 11: Classifier — retry + bisect

**Files:**
- Modify: `fda/metadata/classifier.py` (append `classify_with_retry_and_bisect`)
- Create: `tests/test_metadata_retry.py`

Covers test-matrix rows 17–19.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_retry.py`:

```python
# tests/test_metadata_retry.py
"""Tests for retry + bisect policy in fda.metadata.classifier."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _record(**kw):
    base = dict(
        department="finance", document_type="invoice",
        confidentiality="internal", summary="x",
        keywords={"ko": [], "en": []}, confidence=0.8,
    )
    base.update(kw)
    return base


def _files(n):
    return [
        {"path_id": f"f{i:03d}", "ext": ".pdf", "language_hint": "en",
         "summary": "s", "verbatim_head": "h"}
        for i in range(n)
    ]


class TestRetryThenBisect:
    def test_first_call_succeeds_no_retry(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        backend.complete.return_value = json.dumps([_record() for _ in range(3)])
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(3), backend=backend, skill=skill,
            business_context="",
        )
        assert len(result.records_by_path_id) == 3
        assert result.batches_retried == 0
        assert result.failed_path_ids == []
        assert backend.complete.call_count == 1

    def test_first_invalid_then_retry_succeeds(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        backend.complete.side_effect = [
            "not json",                                  # attempt 1
            json.dumps([_record() for _ in range(3)]),   # retry succeeds
        ]
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(3), backend=backend, skill=skill,
            business_context="",
        )
        assert len(result.records_by_path_id) == 3
        assert result.batches_retried == 1
        assert backend.complete.call_count == 2

    def test_two_failures_bisect_into_halves(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        # 10 files; both top-level calls fail; bisect into [0..4] + [5..9].
        # Each half succeeds on first attempt of its sub-batch.
        good_half = json.dumps([_record() for _ in range(5)])
        backend = MagicMock()
        backend.complete.side_effect = [
            "not json", "not json",   # top-level + its retry
            good_half,                # left half first attempt
            good_half,                # right half first attempt
        ]
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(10), backend=backend, skill=skill,
            business_context="",
        )
        assert len(result.records_by_path_id) == 10
        # 1 retry counted at the top-level; sub-batches succeeded first try
        assert result.batches_retried == 1
        assert backend.complete.call_count == 4

    def test_single_file_double_failure_marks_failed(self):
        from fda.metadata.classifier import classify_with_retry_and_bisect
        backend = MagicMock()
        backend.complete.side_effect = ["bad", "bad"]   # both attempts fail
        skill = MagicMock(body="p", model="claude-sonnet-4-6")
        result = classify_with_retry_and_bisect(
            files=_files(1), backend=backend, skill=skill,
            business_context="",
        )
        assert result.records_by_path_id == {}
        assert result.failed_path_ids == ["f000"]
        assert backend.complete.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_retry.py -v`
Expected: FAIL with `ImportError` on `classify_with_retry_and_bisect`.

- [ ] **Step 3: Append retry+bisect to classifier.py**

Append to `fda/metadata/classifier.py`:

```python
from dataclasses import dataclass, field


@dataclass
class BisectResult:
    """Aggregated outcome of one classify_with_retry_and_bisect call."""
    records_by_path_id: dict[str, Classification] = field(default_factory=dict)
    failed_path_ids: list[str] = field(default_factory=list)
    batches_retried: int = 0
    batches_total: int = 0


def classify_with_retry_and_bisect(
    *,
    files: list[dict[str, Any]],
    backend,
    skill,
    business_context: str,
) -> BisectResult:
    """Classify `files` with: first-attempt → retry-whole-batch → bisect.

    Returns a BisectResult with successful records keyed by path_id and a
    list of path_ids that failed both attempts at single-file granularity.
    """
    result = BisectResult()
    _bisect(files=files, backend=backend, skill=skill,
            business_context=business_context, out=result)
    return result


def _bisect(*, files, backend, skill, business_context, out: BisectResult) -> None:
    out.batches_total += 1
    # Attempt 1
    try:
        records = classify_batch(
            files=files, backend=backend, skill=skill,
            business_context=business_context,
        )
        for f, r in zip(files, records):
            out.records_by_path_id[f["path_id"]] = r
        return
    except ClassifierResponseError as e:
        logger.warning("metadata classifier attempt 1 failed (n=%d): %s",
                       len(files), e)
    # Attempt 2 (whole-batch retry) — only at the top of each branch
    out.batches_retried += 1
    try:
        records = classify_batch(
            files=files, backend=backend, skill=skill,
            business_context=business_context,
        )
        for f, r in zip(files, records):
            out.records_by_path_id[f["path_id"]] = r
        return
    except ClassifierResponseError as e:
        logger.warning("metadata classifier attempt 2 failed (n=%d): %s",
                       len(files), e)
    # Both attempts failed.
    if len(files) == 1:
        out.failed_path_ids.append(files[0]["path_id"])
        return
    mid = len(files) // 2
    _bisect(files=files[:mid], backend=backend, skill=skill,
            business_context=business_context, out=out)
    _bisect(files=files[mid:], backend=backend, skill=skill,
            business_context=business_context, out=out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_retry.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/classifier.py tests/test_metadata_retry.py
git commit -m "metadata: add retry-then-bisect policy with BisectResult"
```

---

## Task 12: Search — FTS5 query + filter composition

**Files:**
- Create: `fda/metadata/search.py`
- Create: `tests/test_metadata_search.py`

Covers test-matrix rows 31–34.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_search.py`:

```python
# tests/test_metadata_search.py
"""Tests for fda.metadata.search."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _seed(tmp_path, rows):
    """Create a DB with init_schema + a run row + given rows."""
    from fda.metadata.store import connect, init_schema, insert_run, upsert_document, upsert_path
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
            mtime="2026-05-13T00:00:00Z", last_seen_run="r1",
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
                                   mtime="2026-05-13T00:00:00Z",
                                   last_seen_run="r1"))
        hits = search(conn, query="invoice")
        assert len(hits) == 2
        assert {h.path for h in hits} == {"/t/0.pdf", "/t/copy/0.pdf"}
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_search.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement search.py**

Create `fda/metadata/search.py`:

```python
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
    if query:
        sql = (
            f"SELECT {select_cols} FROM documents_fts f "
            "JOIN documents d ON d.rowid = f.rowid "
            "JOIN document_paths p ON p.sha256 = d.sha256 "
            "WHERE documents_fts MATCH ?"
        )
        params: list = [query]
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_search.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/metadata/search.py tests/test_metadata_search.py
git commit -m "metadata: add FTS5-backed search with filter composition"
```

---

## Task 13: Orchestration — `fda.metadata.run()`

**Files:**
- Modify: `fda/metadata/__init__.py`
- Create: `tests/test_metadata_run.py`

This is the top-level entry called by both the standalone CLI and the
organize-stage hook.

- [ ] **Step 1: Write the failing test**

Create `tests/test_metadata_run.py`:

```python
# tests/test_metadata_run.py
"""Tests for fda.metadata.run() orchestration."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _catalog_with(entries):
    from fda.organize.models import Catalog
    return Catalog(target="/t", entries=tuple(entries),
                   git_repos_skipped=())


def _entry(idx, *, path, ext=".pdf", failed=False, summary="hello world"):
    from fda.organize.models import CatalogEntry
    return CatalogEntry(
        path_id=f"f{idx:03d}", path=path, ext=ext, size_bytes=100,
        summary=summary, type_label="doc", is_junk=False,
        summary_failed=failed,
        extract_status="failed" if failed else "ok",
        verbatim_head=summary, sections=(),
    )


def _good_response(n):
    return json.dumps([{
        "department": "finance", "document_type": "invoice",
        "confidentiality": "confidential", "summary": "x",
        "keywords": {"ko": [], "en": ["invoice"]}, "confidence": 0.9,
    } for _ in range(n)])


class TestRun:
    def test_happy_path_one_batch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata import run
        # Create two real files so sha256/mime/mtime work.
        f0 = tmp_path / "a.pdf"; f0.write_text("hello")
        f1 = tmp_path / "b.pdf"; f1.write_text("world")
        catalog = _catalog_with([
            _entry(0, path=str(f0)), _entry(1, path=str(f1)),
        ])
        backend = MagicMock()
        backend.complete.return_value = _good_response(2)
        logger = MagicMock()
        report = run(target_path=tmp_path, catalog=catalog,
                     backend=backend, logger=logger,
                     progress_callback=None)
        assert report.files_seen == 2
        assert report.files_classified == 2
        assert report.files_failed == 0
        assert report.batches_total == 1
        assert report.batches_retried == 0
        # DB row created.
        import sqlite3
        conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
        n = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert n == 2
        conn.close()

    def test_failed_extract_creates_fail_closed_row(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata import run
        f0 = tmp_path / "garbled.pdf"; f0.write_bytes(b"\x00\x01")
        catalog = _catalog_with([
            _entry(0, path=str(f0), failed=True, summary=""),
        ])
        backend = MagicMock()
        # Even though backend would be called for 0 successful-extract files,
        # we pre-create the response for safety:
        backend.complete.return_value = _good_response(0)
        logger = MagicMock()
        report = run(target_path=tmp_path, catalog=catalog,
                     backend=backend, logger=logger)
        assert report.files_seen == 1
        # Extraction failed → row stored with fail-closed defaults,
        # NOT counted as "classified" by the LLM.
        assert report.files_failed == 1
        import sqlite3
        conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
        row = conn.execute(
            "SELECT confidentiality, fail_closed_override, extract_status "
            "FROM documents"
        ).fetchone()
        assert row == ("restricted", 1, "failed")
        conn.close()

    def test_audit_sidecar_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata import run
        f0 = tmp_path / "a.pdf"; f0.write_text("hi")
        catalog = _catalog_with([_entry(0, path=str(f0))])
        backend = MagicMock()
        backend.complete.return_value = _good_response(1)
        logger = MagicMock()
        report = run(target_path=tmp_path, catalog=catalog,
                     backend=backend, logger=logger)
        sidecar = tmp_path / ".fda" / "runs" / f"{report.run_id}.md"
        assert sidecar.exists()
        text = sidecar.read_text(encoding="utf-8")
        assert "Files seen: 1" in text
        assert report.run_id in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_run.py -v`
Expected: FAIL with `ImportError: cannot import name 'run' from 'fda.metadata'`.

- [ ] **Step 3: Implement `fda/metadata/__init__.py`**

Replace `fda/metadata/__init__.py` with:

```python
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
     - Bucket: classifiable (extract_status=ok) vs. fail-closed (anything else).
7. Classify the classifiable bucket via classify_with_retry_and_bisect.
8. Apply fail-closed override to each successful record.
9. Upsert one document row + one path row per file.
10. Prune missing paths under target_root.
11. Finalize `runs` row, write audit sidecar, release lock.
"""
from __future__ import annotations

import hashlib
import json
import logging
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
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _new_run_id(target_root: str) -> str:
    stamp = _now_iso().replace(":", "").replace("-", "")
    h = hashlib.sha1(target_root.encode("utf-8")).hexdigest()[:6]
    return f"{stamp}-{h}"


def _fda_home() -> Path:
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
    home = _fda_home()
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
    home = _fda_home()
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "metadata.db"
    lock_path = home / "metadata.db.lock"
    bc_path = home / "business_context.md"

    run_id = _new_run_id(str(target_path))
    started_at = _now_iso()

    with store.acquire_lock(lock_path):
        conn = store.connect(db_path)
        try:
            store.init_schema(conn)
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

            # Unclassifiable (extract_status != ok) → fail-closed rows.
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


__all__ = ["run", "RunReport"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metadata_run.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full metadata test suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/test_metadata_*.py -v`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add fda/metadata/__init__.py tests/test_metadata_run.py
git commit -m "metadata: add run() orchestration + audit sidecar"
```

---

## Task 14: CLI — `fda metadata` and `fda search` subcommands

**Files:**
- Create: `fda/metadata/cli.py`
- Modify: `fda/cli.py` (add subparsers + handlers)
- Create: `tests/test_metadata_cli.py`

Covers test-matrix rows 23, 26–30, 32–33.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metadata_cli.py`:

```python
# tests/test_metadata_cli.py
"""Tests for CLI surfaces (fda metadata, fda search, organize flags)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _python():
    return ".venv/bin/python"


def _run_cli(*args, env=None):
    return subprocess.run(
        [_python(), "-m", "fda.cli", *args],
        capture_output=True, text=True, env=env,
    )


class TestFlagMutualExclusion:
    def test_metadata_only_with_no_route_exits_2(self, tmp_path):
        cp = _run_cli(
            "organize", str(tmp_path), "--metadata-only", "--no-route",
        )
        assert cp.returncode == 2
        assert "mutually exclusive" in (cp.stderr + cp.stdout).lower()

    def test_metadata_only_with_no_metadata_exits_2(self, tmp_path):
        cp = _run_cli(
            "organize", str(tmp_path), "--metadata-only", "--no-metadata",
        )
        assert cp.returncode == 2
        assert "mutually exclusive" in (cp.stderr + cp.stdout).lower()


class TestStandaloneMetadataExit:
    def test_crash_exits_1(self, tmp_path, monkeypatch):
        # Point HOME at an unwritable path so the run crashes.
        env = {"HOME": "/nonexistent/forbidden", "PATH": "/usr/bin:/bin"}
        cp = _run_cli("metadata", str(tmp_path), env=env)
        assert cp.returncode == 1


class TestSearchKoreanDefault:
    def test_default_output_uses_korean_labels(self, tmp_path, monkeypatch):
        # Seed a DB at HOME/.fda/metadata.db with one row.
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata.store import (
            connect, init_schema, insert_run, upsert_document, upsert_path,
        )
        from fda.metadata.schema import DocumentRow, PathRow
        (tmp_path / ".fda").mkdir()
        conn = connect(tmp_path / ".fda" / "metadata.db")
        init_schema(conn)
        insert_run(conn, run_id="r1", target_root="/t",
                   model="claude-sonnet-4-6", fda_version="0.1.0",
                   business_context_sha256=None,
                   started_at="2026-05-13T00:00:00Z")
        upsert_document(conn, DocumentRow(
            sha256="c" * 64, mime="application/pdf", size_bytes=1,
            language="ko", department="finance", document_type="invoice",
            confidentiality="confidential", summary="청구서",
            keywords_json='{"ko":["청구서"],"en":[]}', confidence=0.9,
            fail_closed_override=False, extract_status="ok",
            sharepoint_url=None, run_id="r1",
            created_at="2026-05-13T00:00:00Z",
            updated_at="2026-05-13T00:00:00Z",
        ))
        upsert_path(conn, PathRow(path_id="f0", sha256="c" * 64,
                                   path="/t/a.pdf",
                                   mtime="2026-05-13T00:00:00Z",
                                   last_seen_run="r1"))
        conn.close()
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
        cp = _run_cli("search", "청구서", env=env)
        assert cp.returncode == 0
        # Default = Korean labels.
        assert "재무" in cp.stdout       # finance → 재무
        assert "기밀" in cp.stdout       # confidential → 기밀

    def test_english_flag_outputs_codes(self, tmp_path, monkeypatch):
        # Reuse the same seed via a fixture-style helper would be DRY-er,
        # but the writing-plans skill says to repeat code so the engineer
        # can read tasks out of order.
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata.store import (
            connect, init_schema, insert_run, upsert_document, upsert_path,
        )
        from fda.metadata.schema import DocumentRow, PathRow
        (tmp_path / ".fda").mkdir()
        conn = connect(tmp_path / ".fda" / "metadata.db")
        init_schema(conn)
        insert_run(conn, run_id="r1", target_root="/t",
                   model="claude-sonnet-4-6", fda_version="0.1.0",
                   business_context_sha256=None,
                   started_at="2026-05-13T00:00:00Z")
        upsert_document(conn, DocumentRow(
            sha256="d" * 64, mime="application/pdf", size_bytes=1,
            language="ko", department="finance", document_type="invoice",
            confidentiality="confidential", summary="청구서",
            keywords_json='{"ko":["청구서"],"en":[]}', confidence=0.9,
            fail_closed_override=False, extract_status="ok",
            sharepoint_url=None, run_id="r1",
            created_at="2026-05-13T00:00:00Z",
            updated_at="2026-05-13T00:00:00Z",
        ))
        upsert_path(conn, PathRow(path_id="f0", sha256="d" * 64,
                                   path="/t/a.pdf",
                                   mtime="2026-05-13T00:00:00Z",
                                   last_seen_run="r1"))
        conn.close()
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
        cp = _run_cli("search", "청구서", "--english", env=env)
        assert cp.returncode == 0
        assert "finance" in cp.stdout
        assert "confidential" in cp.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metadata_cli.py -v`
Expected: FAIL (CLI surfaces don't exist yet).

- [ ] **Step 3: Implement `fda/metadata/cli.py`**

Create `fda/metadata/cli.py`:

```python
# fda/metadata/cli.py
"""argparse handlers for `fda metadata <folder>` and `fda search <query>`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def handle_metadata(args: argparse.Namespace) -> int:
    """Run the metadata stage standalone on an already-organized tree."""
    from fda.claude_backend import get_claude_backend
    from fda.metadata import run as metadata_run
    from fda.metadata.store import LockBusy
    from fda.organize import reader
    from fda.organize._logger import OrganizeLogger

    target = Path(args.path).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 1

    backend = get_claude_backend()
    olog = OrganizeLogger(log_path=None, target_basename=target.name,
                          progress_callback=lambda m: print(m))
    try:
        catalog = reader.read(target, backend=backend, logger=olog)
        report = metadata_run(
            target_path=target, catalog=catalog, backend=backend,
            logger=olog, progress_callback=lambda m: print(m),
        )
        print(
            f"metadata: {report.files_classified}/{report.files_seen} "
            f"classified, {report.files_failed} failed "
            f"(run_id={report.run_id})"
        )
        return 0
    except LockBusy as e:
        print(f"⚠ {e}", file=sys.stderr)
        return 1
    except Exception as e:
        logger.exception("fda metadata crashed")
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def handle_search(args: argparse.Namespace) -> int:
    """FTS5 + filter search over ~/.fda/metadata.db."""
    from fda.metadata.search import search
    from fda.metadata.store import connect
    from fda.metadata.vocab import (
        ko_label_for_confidentiality,
        ko_label_for_department,
        ko_label_for_document_type,
    )

    db = Path.home() / ".fda" / "metadata.db"
    if not db.exists():
        print(f"error: no metadata DB at {db}; run `fda metadata <folder>` first",
              file=sys.stderr)
        return 1
    try:
        conn = connect(db)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        hits = search(
            conn,
            query=" ".join(args.query) if args.query else "",
            department=args.department,
            document_type=args.document_type,
            confidentiality=args.confidentiality,
            language=args.language,
            since=args.since,
            fail_closed_only=args.fail_closed_only,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        import json as _json
        print(_json.dumps([{
            "sha256": h.sha256, "path": h.path,
            "department": h.department, "document_type": h.document_type,
            "confidentiality": h.confidentiality, "language": h.language,
            "summary": h.summary, "confidence": h.confidence,
            "fail_closed_override": h.fail_closed_override,
            "mtime": h.mtime,
        } for h in hits], ensure_ascii=False, indent=2))
        return 0

    if not hits:
        print("(no matches)")
        return 0

    use_ko = not args.english
    for h in hits:
        dep = ko_label_for_department(h.department) if use_ko else h.department
        dt = ko_label_for_document_type(h.document_type) if use_ko else h.document_type
        conf = (
            ko_label_for_confidentiality(h.confidentiality)
            if use_ko else h.confidentiality
        )
        marker = " ⚠fail-closed" if h.fail_closed_override else ""
        print(f"{h.path}")
        print(f"  {dep} / {dt} / {conf}{marker}  (confidence={h.confidence:.2f})")
        if h.summary:
            print(f"  {h.summary[:200]}")
    return 0


def register_metadata_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "metadata",
        help="Run the metadata stage on an already-organized tree",
    )
    p.add_argument("path", help="Already-organized directory to index")
    p.set_defaults(func=handle_metadata)


def register_search_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "search",
        help="Keyword-search the metadata index (~/.fda/metadata.db)",
    )
    p.add_argument("query", nargs="*", help="Search terms (Korean OK)")
    p.add_argument("--department", help="Filter by department code")
    p.add_argument("--document-type", dest="document_type",
                   help="Filter by document_type code")
    p.add_argument("--confidentiality", help="Filter by confidentiality code")
    p.add_argument("--language", help="Filter by language (ko|en|unknown)")
    p.add_argument("--since", help="ISO mtime cutoff (rows with mtime >= since)")
    p.add_argument("--fail-closed-only", dest="fail_closed_only",
                   action="store_true",
                   help="Only rows where fail_closed_override = True")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--english", action="store_true",
                   help="Output English codes instead of Korean labels")
    p.add_argument("--json", action="store_true",
                   help="Output machine-readable JSON")
    p.set_defaults(func=handle_search)
```

- [ ] **Step 4: Wire `fda/cli.py`**

Modify `fda/cli.py`:

(a) Add the `--metadata-only` and `--no-metadata` flags to the existing organize parser. Find the block that ends with `organize_parser.set_defaults(func=handle_organize)` (around line 1961). Insert these arguments before that line:

```python
    organize_parser.add_argument(
        "--no-metadata", action="store_true", dest="no_metadata",
        help="Skip the metadata stage (no SQLite index update).",
    )
    organize_parser.add_argument(
        "--metadata-only", action="store_true", dest="metadata_only",
        help=("Skip organize stages 1–4 and routing; only run the metadata "
              "stage on the already-organized tree."),
    )
```

(b) Add mutual-exclusion validation in `handle_organize`. Find `handle_organize` (around line 1246) and add this check near the top of the function:

```python
    if args.metadata_only and (args.no_route or args.no_metadata):
        print(
            "error: --metadata-only is mutually exclusive with --no-route "
            "and --no-metadata",
            file=sys.stderr,
        )
        return 2
```

(c) Register the two new subparsers. After `organize_parser.set_defaults(func=handle_organize)`, add:

```python
    from fda.metadata.cli import (
        register_metadata_subparser, register_search_subparser,
    )
    register_metadata_subparser(subparsers)
    register_search_subparser(subparsers)
```

- [ ] **Step 5: Run the CLI tests**

Run: `.venv/bin/python -m pytest tests/test_metadata_cli.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add fda/metadata/cli.py fda/cli.py tests/test_metadata_cli.py
git commit -m "metadata: add fda metadata + fda search CLI + organize flag matrix"
```

---

## Task 15: Hook stage 5 into `fda organize`

**Files:**
- Modify: `fda/organize/__init__.py` (insert metadata call after router)
- Modify: `fda/local_worker_agent.py:899-927` (forward `no_metadata`, `metadata_only`)
- Modify: `fda/cli.py` `handle_organize` (forward `metadata_only` short-circuit and `no_metadata` flag)
- Create: `tests/test_organize_metadata_integration.py`

Covers test-matrix rows 24–26.

- [ ] **Step 1: Write the failing tests**

The hook needs to be testable WITHOUT running stages 1–4 (which would
need a fully-mocked Claude backend producing valid JSON for every
upstream classifier/taxonomy call — fragile and not what's being
tested). Extract the hook body into a private helper
`_run_metadata_stage()` and test that directly.

Create `tests/test_organize_metadata_integration.py`:

```python
# tests/test_organize_metadata_integration.py
"""Stage-5 hook integration tests.

These exercise `_run_metadata_stage()` directly, not the full
organize() pipeline — we want to verify the hook's defensive
try/except + progress_callback wiring, not re-test stages 1-4.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _empty_catalog(target):
    from fda.organize.models import Catalog
    return Catalog(target=str(target), entries=(), git_repos_skipped=())


class TestStageHook:
    def test_metadata_crash_does_not_propagate(self, tmp_path):
        """Hook swallows exceptions; caller (organize()) keeps going."""
        from fda.organize import _run_metadata_stage
        olog = MagicMock()
        captured: list[str] = []
        with patch("fda.metadata.run", side_effect=RuntimeError("boom")):
            # Must not raise.
            _run_metadata_stage(
                target_path=tmp_path, catalog=_empty_catalog(tmp_path),
                backend=MagicMock(), olog=olog,
                progress_callback=captured.append,
            )
        # User saw a warning line.
        assert any("metadata stage failed" in m for m in captured)
        # Structured log captured the fatal.
        olog.log.assert_any_call("METADATA_FAIL_FATAL", error="boom")

    def test_happy_path_surfaces_counts(self, tmp_path):
        from fda.metadata.schema import RunReport
        from fda.organize import _run_metadata_stage
        captured: list[str] = []
        fake = RunReport(
            run_id="r-test", files_seen=3, files_classified=2,
            files_failed=1, batches_total=1, batches_retried=0,
        )
        with patch("fda.metadata.run", return_value=fake):
            _run_metadata_stage(
                target_path=tmp_path, catalog=_empty_catalog(tmp_path),
                backend=MagicMock(), olog=MagicMock(),
                progress_callback=captured.append,
            )
        joined = " ".join(captured)
        assert "metadata: 2/3" in joined
        assert "1 failed" in joined

    def test_progress_callback_none_is_safe(self, tmp_path):
        """No progress_callback wired? Hook still runs without crashing."""
        from fda.metadata.schema import RunReport
        from fda.organize import _run_metadata_stage
        fake = RunReport(run_id="r", files_seen=1, files_classified=1,
                          files_failed=0, batches_total=1, batches_retried=0)
        with patch("fda.metadata.run", return_value=fake):
            _run_metadata_stage(
                target_path=tmp_path, catalog=_empty_catalog(tmp_path),
                backend=MagicMock(), olog=MagicMock(),
                progress_callback=None,
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_metadata_integration.py -v`
Expected: FAIL (organize doesn't accept `metadata` kwarg or call stage 5 yet).

- [ ] **Step 3: Modify `fda/organize/__init__.py`**

Edit `fda/organize/__init__.py`. Find the `organize()` signature (currently `fda/organize/__init__.py:31-41`) and add a `metadata: bool = True` parameter:

```python
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
```

Add a module-level helper `_run_metadata_stage()` near the top of the file (above `organize()`). Extracting the hook into a helper makes it independently testable without running stages 1-4:

```python
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
```

Wire the call. **Important:** the call must go AFTER the `if route:`
block, NOT inside it — otherwise `--no-route` would incorrectly skip
metadata too (spec flag matrix forbids that). Find the `if route:`
block (currently `fda/organize/__init__.py:158-172`) and insert this
AFTER its closing `except` clause (and after the `RUN_END` log
emission no — actually before it, so the run-end log reflects the
final state):

```python
        # Stage 5 — runs independently of routing. --no-metadata skips it.
        if metadata:
            _run_metadata_stage(
                target_path=target_path, catalog=catalog,
                backend=backend, olog=olog,
                progress_callback=progress_callback,
            )
```

Export `_run_metadata_stage` so the integration tests can patch
`fda.metadata.run` and call the helper. Add it to `__all__` or simply
rely on `from fda.organize import _run_metadata_stage` working (which
it does — leading-underscore module members are importable, just
conventionally private).

- [ ] **Step 4: Modify `fda/local_worker_agent.py`**

The existing `organize_files` (fda/local_worker_agent.py:899-931) has this exact shape — preserve it verbatim, add only the new keyword args + the `metadata_only` short-circuit branch:

```python
    def organize_files(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
        *,
        route: bool = True,
        metadata: bool = True,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        """Organize files in `target_path` via the
        reader+classifier+plan_builder+executor+verifier+router+metadata
        pipeline. Returns the back-compat dict shape used by Telegram, the
        web UI, and the orchestrator.

        --metadata-only short-circuits stages 1-4 + router; only stage 5
        runs. The reader is still invoked to build a Catalog over the
        already-organized tree.
        """
        from fda.organize import organize as _organize
        from fda.organize.models import PlanResult

        try:
            target_path = self.resolve_project_path(target_path)
            if metadata_only:
                from fda.metadata import run as metadata_run
                from fda.organize import reader
                from fda.organize._logger import OrganizeLogger
                olog = OrganizeLogger(
                    log_path=None,
                    target_basename=Path(target_path).name,
                    progress_callback=progress_callback,
                )
                catalog = reader.read(
                    Path(target_path), backend=self._backend, logger=olog,
                )
                report = metadata_run(
                    target_path=Path(target_path), catalog=catalog,
                    backend=self._backend, logger=olog,
                    progress_callback=progress_callback,
                )
                return {
                    "success": True,
                    "metadata_only": True,
                    "run_id": report.run_id,
                    "files_seen": report.files_seen,
                    "files_classified": report.files_classified,
                    "files_failed": report.files_failed,
                }
            result = _organize(
                target_path,
                instructions,
                backend=self._backend,
                allowed_roots=self.projects,
                progress_callback=progress_callback,
                route=route,
                metadata=metadata,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"organize_files failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

        assert isinstance(result, PlanResult)
        return _plan_result_to_dict(result)
```

Confirmed against the actual class (read at plan time):
- attribute is `self._backend`, not `self._claude_backend`
- allowed-roots attribute is `self.projects`, not `self.allowed_roots`
- path validation goes through `self.resolve_project_path()`, not bare `Path().resolve()`
- helper is module-level `_plan_result_to_dict()`, not `self._planresult_to_dict()`
- exceptions return `{"success": False, "error": ...}`, never re-raise
- the existing call signature has `progress_callback` as a positional-style kwarg — keep it.

- [ ] **Step 5: Modify `handle_organize` in `fda/cli.py`**

Find the `worker.organize_files(...)` call around line 1272 and update it to forward the new flags. Add this just before the call:

```python
    if args.metadata_only and (args.no_route or args.no_metadata):
        print(
            "error: --metadata-only is mutually exclusive with --no-route "
            "and --no-metadata",
            file=sys.stderr,
        )
        return 2
```

Then modify the `result = worker.organize_files(...)` call:

```python
    result = worker.organize_files(
        target_path,
        " ".join(args.instructions) if args.instructions else "",
        route=not args.no_route,
        metadata=not args.no_metadata,
        metadata_only=args.metadata_only,
    )
```

- [ ] **Step 6: Run integration tests**

Run: `.venv/bin/python -m pytest tests/test_organize_metadata_integration.py -v`
Expected: 2 passed.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add fda/organize/__init__.py fda/local_worker_agent.py fda/cli.py tests/test_organize_metadata_integration.py
git commit -m "metadata: wire stage-5 hook + --no-metadata/--metadata-only flags"
```

---

## Task 16: Synthetic fixture corpus + live-mode smoke test

**Files:**
- Create: `tests/fixtures/metadata_corpus/` (~15 small files)
- Create: `tests/test_metadata_live_smoke.py` (gated on `--live` flag)
- Modify: `pyproject.toml` (add `live` marker)

This is the layer-2 / layer-3 validation per spec § Validation.

- [ ] **Step 1: Create the fixture corpus**

Create these files under `tests/fixtures/metadata_corpus/` — each is a tiny realistic example. Use simple `.txt`/`.md`/`.csv` files so the FDA reader can extract text without external tools.

Create `tests/fixtures/metadata_corpus/finance_invoice_ko.txt`:

```
청구서

수신: 주식회사 라이온케미텍
발행일: 2025-09-30
청구 금액: 12,340,000원

2025년 3분기 매출 청구서입니다. 30일 이내 결제 부탁드립니다.
담당: 영업기획부 김철수
```

Create `tests/fixtures/metadata_corpus/hr_review_ko.txt`:

```
2025년 상반기 인사평가서

대상자: 이영희 (인사총무팀)
평가자: 박철호 팀장
종합 등급: A
연봉 조정: +5%

본 문서는 인사 기밀이며 외부 유출을 금합니다.
```

Create `tests/fixtures/metadata_corpus/rd_formula_en.txt`:

```
Catalyst Formulation — Process Aurora (XYZ-200 series)

Internal R&D document. DO NOT distribute outside the lab.

Composition:
- Component A: 42.3% wt
- Component B: 28.7% wt
- Component C: balance

Process notes: see PR-2025-018 for run conditions.
```

Create `tests/fixtures/metadata_corpus/sales_proposal_ko.txt`:

```
제안서 — 삼화제약 신규 공급 계약

작성: 영업기획부
2025-08-15

귀사의 발주 물량을 검토한 결과, 월 5톤 공급이 가능합니다.
가격, 결제 조건, 납기는 별첨 부속서에 따릅니다.
```

Create `tests/fixtures/metadata_corpus/marketing_press_en.md`:

```
# Lion Chemtech Announces 2025 ESG Report

FOR IMMEDIATE RELEASE — Public.

Lion Chemtech today released its 2025 ESG report,
highlighting a 22% reduction in process emissions.
```

Create `tests/fixtures/metadata_corpus/production_log_en.csv`:

```
run_id,date,line,output_kg,yield_pct
PR-2025-016,2025-09-12,1,12450,98.4
PR-2025-017,2025-09-13,1,12380,98.1
PR-2025-018,2025-09-14,2,11990,97.6
```

Create `tests/fixtures/metadata_corpus/legal_contract_ko.txt`:

```
공급 계약서 — 2025-09-01 체결

매도인: 라이온케미텍 (주)
매수인: 한국정밀화학 (주)

계약 기간: 2025-10-01 ~ 2026-09-30
공급 단가: 별첨 부속서 참조 (기밀)

본 계약서는 양 당사자 간 기밀이며 제3자 공개를 금합니다.
```

Create `tests/fixtures/metadata_corpus/exec_minutes_ko.txt`:

```
2025-09 월간 경영 회의 의사록

참석: 대표이사 외 4인
주요 안건:
- 3분기 매출 전망 (영업)
- 2026년 R&D 예산 (연구개발)
- 인력 충원 계획 (인사)

본 의사록은 임원 내부 기밀 자료입니다.
```

Create `tests/fixtures/metadata_corpus/empty.txt`:

```

```

(Yes — one blank file. Tests the fail-closed path for empty content.)

Create `tests/fixtures/metadata_corpus/garbled.bin`:

Use bash to create binary garbage:

```bash
printf '\x00\x01\x02\x03\xff\xfe\xfd' > tests/fixtures/metadata_corpus/garbled.bin
```

- [ ] **Step 2: Create the live-mode smoke test**

Create `tests/test_metadata_live_smoke.py`:

```python
# tests/test_metadata_live_smoke.py
"""Live smoke test — runs the metadata stage against the synthetic corpus
using a real Sonnet backend. Gated on the `--live` pytest flag.

Asserts STRUCTURAL correctness only:
- Every classifiable file gets a row with a valid vocab triple.
- Pydantic validation passed (no records would have been stored otherwise).
- Fail-closed override fires on at least one of the deliberately
  low-confidence fixtures (empty.txt / garbled.bin).

Does NOT assert specific department/document_type labels — those are
Claude's judgment.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _live() -> bool:
    return os.environ.get("FDA_LIVE_SMOKE") == "1"


@pytest.mark.skipif(not _live(),
                    reason="set FDA_LIVE_SMOKE=1 to run against real Sonnet")
def test_metadata_live_smoke_on_fixture_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fda.claude_backend import get_claude_backend
    from fda.metadata import run as metadata_run
    from fda.metadata.vocab import (
        CONFIDENTIALITY, DEPARTMENTS, DOCUMENT_TYPES,
    )
    from fda.organize import reader
    from fda.organize._logger import OrganizeLogger

    fixture_dir = Path("tests/fixtures/metadata_corpus").resolve()
    backend = get_claude_backend()
    olog = OrganizeLogger(log_path=None, target_basename="smoke",
                          progress_callback=lambda m: print(m))
    catalog = reader.read(fixture_dir, backend=backend, logger=olog)
    report = metadata_run(
        target_path=fixture_dir, catalog=catalog,
        backend=backend, logger=olog,
        progress_callback=lambda m: print(m),
    )
    assert report.files_seen >= 9

    import sqlite3
    conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
    rows = conn.execute(
        "SELECT department, document_type, confidentiality, "
        "fail_closed_override FROM documents"
    ).fetchall()
    assert len(rows) == report.files_seen
    for dep, dt, conf, fc in rows:
        assert dep in DEPARTMENTS
        assert dt in DOCUMENT_TYPES
        assert conf in CONFIDENTIALITY
    # At least one row must be the fail-closed empty/garbled file.
    assert any(fc == 1 for _, _, _, fc in rows), \
        "expected at least one fail_closed_override row"
    conn.close()
```

- [ ] **Step 3: Sanity-check the corpus + skip when flag absent**

Run: `.venv/bin/python -m pytest tests/test_metadata_live_smoke.py -v`
Expected: 1 skipped (no `FDA_LIVE_SMOKE=1`).

- [ ] **Step 4: Commit the corpus + smoke test**

```bash
git add tests/fixtures/metadata_corpus/ tests/test_metadata_live_smoke.py
git commit -m "metadata: add synthetic fixture corpus + live-mode smoke test"
```

- [ ] **Step 5: Run the live smoke manually (one-shot, NOT in CI)**

Run: `FDA_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/test_metadata_live_smoke.py -v -s`
Expected: 1 passed. Inspect printed progress lines + `~/.fda/runs/<run_id>.md` for any obviously-wrong classifications. If labels look wrong, edit `fda/metadata/skills/metadata-classifier/SKILL.md` and re-run.

(Do not commit anything from this step automatically — only if you tune the SKILL.md prompt, commit that change as a separate follow-up.)

---

## Task 17: Corpus eyeballing — Lion Chemtech + Korean A + English v1

**Files:** (none — manual validation pass)

This is the spec's Layer-3 validation. No code changes; the engineer
runs the metadata stage against the three real corpora and reads the
audit sidecars + spot-checks `fda search` results.

- [ ] **Step 1: Run against each corpus**

For each of: English v1 corpus, Korean A corpus, Lion Chemtech corpus,
run:

```bash
.venv/bin/python -m fda.cli metadata <path-to-organized-corpus>
```

- [ ] **Step 2: Read the audit sidecar**

Open `~/.fda/runs/<run_id>.md` for each run. Check:
- `Files seen` matches the actual file count on disk.
- `Failed` is a small, justified number (each failure should be
  garbled / extraction-tool-missing, not unexplained).
- `Business context` line shows your `~/.fda/business_context.md`
  loaded successfully (or the "(none)" line if you haven't created
  one).

- [ ] **Step 3: Spot-check 10 rows per corpus**

For each corpus, run 10 representative `fda search` queries — mix
keyword search and filter-only queries:

```bash
.venv/bin/python -m fda.cli search "매출"
.venv/bin/python -m fda.cli search "invoice" --department finance
.venv/bin/python -m fda.cli search "" --confidentiality restricted --limit 20
.venv/bin/python -m fda.cli search "" --fail-closed-only
```

For each result, ask: would a Korean office worker agree this
file belongs in `<department> / <document_type> / <confidentiality>`?
Note any obviously-wrong calls.

- [ ] **Step 4: Tune SKILL.md if needed**

If patterns of wrong classifications surface (e.g., "policy" documents
keep getting tagged as `report`), edit `fda/metadata/skills/metadata-classifier/SKILL.md`
to add an example or clarify the rule. Re-run step 1 on the affected
corpus. Commit any prompt edits as a separate commit:

```bash
git add fda/metadata/skills/metadata-classifier/SKILL.md
git commit -m "metadata: tune classifier prompt based on corpus eyeballing"
```

- [ ] **Step 5: Update the project memory**

Once eyeballing passes on all three corpora, update
`/Users/hogyeongkim/.claude/projects/-Users-hogyeongkim-Desktop-Projects-FDA-FDA/memory/project_metadata_layer_design.md`
to reflect the shipped state.

---

## Self-Review Coverage Check

Mapping spec sections → tasks:

| Spec section | Task |
|---|---|
| Per-file decision (18-column table) | Tasks 3, 4 (schema + enrich) |
| Vocabulary (codes + Korean labels) | Task 2 |
| Vocab-via-business_context (no code release per customer) | Tasks 8, 9 (loader + SKILL.md preamble) |
| Fail-closed rule (post-process override) | Task 10 |
| Korean-first surface (FTS5 trigram, Korean keywords, --english) | Tasks 5, 12, 14 |
| Business context (~/.fda/business_context.md) | Task 8 |
| CLI surface (`--no-metadata`, `--metadata-only`, `fda metadata`, `fda search`) | Task 14 |
| Flag matrix + mutex | Task 14 |
| Storage — FTS5 + trigram probe | Task 5 |
| `documents` table | Tasks 3, 6 |
| `document_paths` table | Tasks 3, 6 |
| FTS5 triggers (INSERT/DELETE/UPDATE + rebuild) | Tasks 3, 5 |
| `runs` table | Tasks 3, 5 |
| Audit sidecar | Task 13 |
| Concurrency (WAL + flock) | Tasks 5, 7 |
| Phase 2 forward-compat (no `embedding` column yet) | (spec deferral; no task) |
| Classifier SKILL.md | Task 9 |
| Batching (10/call) | Task 13 (run() loops in BATCH_SIZE chunks) |
| Retry + bisect | Task 11 |
| Per-file failure semantics | Tasks 10, 11, 13 |
| Output validation (Pydantic strict, no cross-field) | Task 3 |
| Module layout (`fda/metadata/`) | Tasks 2–14 |
| Organize hook + exit codes + progress_callback | Task 15 |
| Layer 1 unit tests (matrix rows 1–34) | Tasks 3, 5–7, 10–15 |
| Layer 2 live smoke | Task 16 |
| Layer 3 corpus eyeballing | Task 17 |
| Out-of-scope items (embeddings, text-to-SQL, upload, OCR, ...) | (spec deferrals) |

All 34 test-matrix rows are mapped: rows 1–5 → Task 3; 6–8 → Task 10; 9–13 → Task 5; 14–16 → Task 6; 17–19 → Task 11; 20–21 → Task 8; 22 → Task 5; 23 → Task 7 + 14; 24–26 → Task 15; 27–30 → Task 14; 31–34 → Tasks 12 + 14.

No placeholders. No "TBD". Every step has either exact code or an exact command with expected output.

---

## Post-Codex revisions (2026-05-13)

Codex reviewed the first draft and raised 6 issues; all accepted and fixed:

| # | Issue | Fix |
|---|---|---|
| 1 | `patch.object(conn, "execute", ...)` fails on `sqlite3.Connection` (C-level method is read-only) | Task 5 probe test rewritten to use a duck-typed `FakeConn` class. |
| 2 | `document_paths.path_id` as PK collides across runs (reader resets `f000` per organize run) | Spec + Task 3 DDL updated: `path TEXT PRIMARY KEY`, `path_id TEXT NOT NULL` (informational only). `upsert_path` now `ON CONFLICT(path)`. New Task 6 regression test for the two-runs-both-emit-f000 scenario. |
| 3 | Korean particle test direction was reversed (doc had bare stem, query had particle — trigrams won't match that way) | Task 12 test now seeds doc WITH particle (`매출은`), queries WITHOUT (`매출`), matching spec semantics + how trigram FTS5 actually works. |
| 4 | Bucketing on `summary_failed` only missed `extract_status ∈ {tool_missing, no_extractor}` | Task 13 predicate now `extract_status == "ok" AND NOT summary_failed`. |
| 5 | Task 15 `LocalWorkerAgent` rewrite used wrong attribute names (`_claude_backend`, `allowed_roots`, `_planresult_to_dict`) and dropped `progress_callback` / exception-to-dict behavior | Rewrite verified against actual file: `self._backend`, `self.projects`, `_plan_result_to_dict`, `self.resolve_project_path`, exception → `{"success": False, "error": str(e)}`, `progress_callback` preserved. |
| 6 | Integration test used `MagicMock` backend against full `organize()` — would fail in stages 1-4 before reaching the metadata hook | Hook extracted into `_run_metadata_stage()` helper; integration tests now exercise the helper directly. |

Also fixed a latent ordering bug surfaced while wiring fix #6: original draft put the metadata call INSIDE the `if route:` block, which would have made `--no-route` skip metadata too. The helper is now invoked from a separate top-level `if metadata:` after the routing block, matching the spec's flag matrix.
