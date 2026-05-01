# Local Metadata & Retrieval Layer — Plan

**Status:** design only — no code changes in this doc.
**Date:** 2026-05-01

---

## 1. Goal

Add a **local metadata and retrieval layer** that wraps the existing agentic
file-organization classifier (`LocalWorkerAgent.organize_files`) so that, for
every file the organizer touches, we capture a strict, schema-validated
classification record and persist it to a queryable local store. The store is
designed to support both keyword search now and AI semantic search later,
without coupling either of them to the organizer's source code.

The layer is built **from scratch**. It does not extend or replace the existing
path-only file indexer (`fda/file_indexer.py`) or the librarian agent
(`fda/librarian_agent.py`); those are treated as background context only.

## 2. Scope & non-goals

**In scope (this plan):**

- A wrapper pipeline around the existing `organize_files()` agent.
- A strict classification output schema (Phase 1 fields + Phase 2 reservations).
- A new local SQLite store (`metadata.db`) with FTS5 for keyword search.
- A retrieval surface (filtered SQL + FTS5).
- Synthetic-fixture-only testing.

**Out of scope (this plan):**

- Implementing any code. This is documentation and design only.
- Modifying `LocalWorkerAgent.organize_files` or any of its `_orgtool_*` methods.
- Modifying `fda/file_indexer.py` or `fda/librarian_agent.py`.
- Anything KakaoTalk-related (the only existing classifier-named code in the
  repo is `orchestrator.py::classify_message` — a KakaoTalk message classifier,
  not a document classifier).
- SharePoint upload, Microsoft Graph, MSAL flows.
- Running indexing or classification against real local user directories.
- File-level embeddings, vector search, pgvector, or chunked retrieval (Phase 2).

## 3. What exists today in the repo

### 3.1 The agentic classifier — `LocalWorkerAgent.organize_files`

Location: `fda/local_worker_agent.py:986–1115`.

This is the existing classifier referenced by this plan. It is an **agentic
Claude tool-loop** that organizes a target directory by file purpose:

- System prompt (`ORGANIZE_SYSTEM_PROMPT`, line 986) describes the workflow:
  list directory → inspect each file → group by purpose → create folders →
  move files.
- Tools (`_FILE_ORGANIZE_TOOLS`, line 171): `list_directory`, `get_file_info`,
  `read_file`, `move_file`, `create_directory`, `delete_file`, `run_command`.
- Guardrails (hard-enforced in the tool implementations):
  - Never touch a path inside a git repository
    (`_is_inside_git_repo`, line 1149).
  - Never delete arbitrary files. Only `_JUNK_FILES` (line 305) or zero-byte
    files may be removed.
  - All paths must resolve inside the target directory.

Return shape:

```python
{
    "success": bool,
    "summary": str,                       # free-text Claude summary
    "moves": list[{"from": str, "to": str}],
    "deletions": list[str],
    "dirs_created": list[str],
    "repos_skipped": list[str],
    "error": Optional[str],
}
```

**The destination folder is the implicit classification today.** The organizer
does not currently emit structured per-file metadata such as department,
document type, business category, confidentiality, summary, or keywords.

### 3.2 Existing indexer / retrieval — background only, **not suitable to build on**

Location: `fda/file_indexer.py` (366 lines) and `fda/librarian_agent.py`
(2423 lines).

`file_indexer.py` builds a **path-and-filename-only** semantic index using
`fastembed` embeddings stored in SQLite via `ProjectState`. It indexes the
filename, parent-dir tokens, and extension — not file content. It does not
classify files into departments, document types, or any structured taxonomy.
It walks user directories (Documents, Downloads, Desktop) by configuration and
does not gate on git repos.

`librarian_agent.py` wraps `file_indexer` for search and adds project-knowledge
features (route discovery, code analysis, journal management). It also has no
classification fields.

This plan **explicitly does not build on these modules**. Reasons:

- They embed only filename and path tokens, not file content.
- They have no classification schema, no controlled vocabulary, no validation.
- They share state with `ProjectState`, which mixes journal/task/KPI concerns
  unrelated to document metadata.
- They walk real local user directories — incompatible with this plan's
  synthetic-fixtures-only constraint.

The new metadata layer is a separate, parallel system with its own SQLite DB
(`metadata.db`), its own schema, and its own pipeline entrypoint.

### 3.3 KakaoTalk message classifier — explicitly excluded

`orchestrator.py::classify_message` (`MESSAGE_CLASSIFIER_PROMPT`, line 58) is a
**KakaoTalk-message classifier** for client chat triage. It is not a document
classifier and is out of scope for this plan. No design or code in this layer
references it.

## 4. What is missing

For the agentic organizer to drive a future AI search experience, these
capabilities do not yet exist anywhere in the repo:

- **Structured per-file output** — the 12 metadata fields the user requires
  (department, document_type, business_category, confidentiality, summary,
  keywords, etc.) are not produced today.
- **Schema validation** — no pydantic / jsonschema usage; nothing enforces that
  classifier output matches a strict shape or controlled vocabulary.
- **Persistent classification store** — there is no metadata table keyed by
  content hash. SQLite is used for project state, not document metadata.
- **Keyword search over classification fields** — no FTS5 over summary,
  keywords, or filename. The existing indexer uses cosine similarity over
  filename embeddings only.
- **A way to derive a SharePoint-bound path** — the organizer's chosen
  destination folder must eventually map to a SharePoint folder; nothing
  computes or records that mapping.
- **Identity stability across moves** — files are identified by current path;
  there is no SHA-256 keying that survives the organizer moving them.
- **Phase-2 reservations** — no place in the schema for embeddings, text
  chunks, or SharePoint IDs to be added later without migrations.

## 5. Proposed local-only pipeline (from scratch)

The pipeline wraps the existing organizer; **it does not modify it**. Each
stage is isolated, idempotent on SHA-256, and runs against a synthetic
test directory only.

```
┌──────────────────────────────────────────────────────────────────────┐
│                     metadata pipeline run                            │
│                                                                      │
│  1. discover            walk synthetic fixture dir                   │
│  2. fingerprint         sha256 each file BEFORE the organizer runs   │
│                         (so identity is stable across moves)         │
│  3. extract_text        safe per-extension text extraction           │
│                         (.txt/.md/.csv direct read; .pdf/.docx via   │
│                         optional libs; binary → empty body)          │
│  4. organize            CALL organize_files() UNMODIFIED             │
│                         capture {moves, summary, deletions, ...}     │
│  5. classify_strict     NEW component — one strict-JSON Claude call  │
│                         per file, given content + organizer dest     │
│  6. derive_sp_path      compute suggested_sharepoint_path from       │
│                         organizer's destination folder               │
│  7. validate            pydantic v2 strict parse against schema      │
│  8. persist             upsert into metadata.db keyed by sha256      │
│  9. index_for_search    refresh FTS5 virtual table                   │
│                                                                      │
│  10. report             classifier_runs row: counts + organizer      │
│                         summary + duration                           │
└──────────────────────────────────────────────────────────────────────┘
```

Key properties:

- **Step 4 is unchanged**. We treat `organize_files()` as a black box and read
  its return value.
- **Step 5 is the only Claude call this layer adds.** It is a separate
  strict-output prompt, not a modification of the organizer's tool loop.
- **SHA-256 is computed in step 2**, before the organizer moves anything. Every
  later step keys on that hash, so a move never breaks identity.
- **Steps 3 and 5 are both per-file** and can be parallelized in implementation
  if needed.

## 6. Strict classifier output schema

The strict classifier (step 5) returns a JSON object that pydantic v2 must
parse without errors. The schema below is the v1 shape. Phase 2 columns are
reserved as nullable from day one to avoid migrations later.

### 6.1 Phase 1 fields — required for v1

```
ClassificationRecord {
  # identity
  file_name:                str        # e.g. "q3_revenue_report.xlsx"
  original_local_path:      str        # absolute path BEFORE the organizer moved it
  sha256:                   str        # 64-char hex; primary key
  size_bytes:               int
  mime_type:                str        # from `file --mime-type`
  language:                 str        # ISO 639-1: "en" | "ko" | ...

  # the user's required taxonomy fields (controlled vocabulary)
  department:               enum       # see §6.3
  document_type:            enum       # see §6.3
  business_category:        enum       # see §6.3
  confidentiality:          enum       # public | internal | confidential | restricted

  # generated content fields (drive keyword + semantic search)
  title:                    str        # doc-internal title, falls back to file_name
  summary:                  str        # 1–3 sentence abstract; main FTS field
  keywords:                 list[str]  # 5–15 terms; non-empty
  body_excerpt:             str        # first ~2000 chars of extracted text;
                                       # empty for binaries
  reason:                   str        # classifier rationale; aids debugging

  # routing
  suggested_sharepoint_path: str       # e.g. "Sales/2026/Q1/Reports/q3_revenue_report.xlsx"
                                       # derived from organizer's chosen destination

  # quality
  confidence:               float      # ∈ [0.0, 1.0]

  # timestamps
  file_modified_at:         datetime   # source file mtime
  classified_at:            datetime   # when this record was written
}
```

### 6.2 Phase 2 fields — reserved as nullable, **not populated in v1**

These columns exist in the table from day one but are `NULL` until Phase 2
turns them on. This avoids a schema migration when semantic search ships.

```
  # semantic search (Phase 2)
  embedding_card_text:      str | NULL # canonical text fed to the embedder
                                       # (filename + title + summary + keywords)
  embedding:                BLOB | NULL # file-level vector
  embedding_model:          str | NULL  # e.g. "multilingual-MiniLM-L12-v2"

  # entities for who/what/when queries (Phase 2)
  entities:                 JSON | NULL # [{type, value, span?}]

  # SharePoint integration (Phase 3, see §9)
  sharepoint_site_id:       str | NULL
  sharepoint_drive_id:      str | NULL
  sharepoint_item_id:       str | NULL
  sharepoint_url:           str | NULL
  final_sharepoint_path:    str | NULL
```

Text chunks and per-chunk embeddings live in a sibling table
(`document_chunks`, also reserved for Phase 2). They are not part of the
flat `documents` row.

### 6.3 Controlled vocabularies (v1 starter set)

Vocabularies are intentionally small for v1; adding values is cheap, removing
is breaking.

- `department`: `sales | finance | hr | legal | engineering | operations | marketing | executive | unknown`
- `document_type`: `report | contract | invoice | policy | proposal | presentation | spreadsheet | email | memo | other`
- `business_category`: `revenue | compliance | personnel | product | strategy | partnership | vendor | customer | unknown`
- `confidentiality`: `public | internal | confidential | restricted`

The classifier must return `unknown` rather than guess when the file content
does not support a confident assignment. `confidence` should reflect this.

### 6.4 Validation rules (enforced by pydantic)

- `sha256` matches `^[0-9a-f]{64}$`.
- `confidence ∈ [0.0, 1.0]`.
- `keywords` length ≥ 1.
- `summary` length between 20 and 800 characters.
- `body_excerpt` is truncated by step 3 to a soft target of ~2000 characters
  with a hard cap of 4000 characters; values exceeding the cap fail validation.
- All enum fields strictly match the controlled vocabulary above; unknown
  values raise.
- `suggested_sharepoint_path` is a forward-slash POSIX-style relative path,
  no leading slash, no `..`.
- Phase 2 fields are accepted as `None` only.

## 7. Local metadata storage

### 7.1 Database location and isolation

A new SQLite database file at `~/.fda/metadata.db`. This is **separate from**
the `ProjectState` database to keep document metadata uncontaminated by
journal/task/KPI state and to make the future migration to PostgreSQL
straightforward (a one-DB dump → load).

### 7.2 Tables

```sql
-- Primary metadata, one row per unique file content (keyed by sha256).
CREATE TABLE documents (
  sha256                     TEXT PRIMARY KEY,
  file_name                  TEXT NOT NULL,
  original_local_path        TEXT NOT NULL,
  parent_dir                 TEXT NOT NULL,         -- denormalized for filtering
  size_bytes                 INTEGER NOT NULL,
  mime_type                  TEXT NOT NULL,
  language                   TEXT NOT NULL,

  department                 TEXT NOT NULL,
  document_type              TEXT NOT NULL,
  business_category          TEXT NOT NULL,
  confidentiality            TEXT NOT NULL,

  title                      TEXT NOT NULL,
  summary                    TEXT NOT NULL,
  body_excerpt               TEXT NOT NULL,
  reason                     TEXT NOT NULL,

  suggested_sharepoint_path  TEXT NOT NULL,

  confidence                 REAL NOT NULL,

  file_modified_at           TIMESTAMP NOT NULL,
  classified_at              TIMESTAMP NOT NULL,

  -- Phase 2 reservations (nullable; populated later)
  embedding_card_text        TEXT,
  embedding                  BLOB,
  embedding_model            TEXT,
  entities                   TEXT,                  -- JSON

  -- Phase 3 reservations — SharePoint (nullable; populated later)
  sharepoint_site_id         TEXT,
  sharepoint_drive_id        TEXT,
  sharepoint_item_id         TEXT,
  sharepoint_url             TEXT,
  final_sharepoint_path      TEXT
);

CREATE INDEX idx_documents_department         ON documents(department);
CREATE INDEX idx_documents_document_type      ON documents(document_type);
CREATE INDEX idx_documents_business_category  ON documents(business_category);
CREATE INDEX idx_documents_confidentiality    ON documents(confidentiality);
CREATE INDEX idx_documents_parent_dir         ON documents(parent_dir);
CREATE INDEX idx_documents_modified_at        ON documents(file_modified_at);

-- Keywords are split out so we can index them, count them, and surface them
-- as facets without parsing JSON on every query.
CREATE TABLE document_keywords (
  sha256   TEXT NOT NULL REFERENCES documents(sha256) ON DELETE CASCADE,
  keyword  TEXT NOT NULL,
  PRIMARY KEY (sha256, keyword)
);
CREATE INDEX idx_keywords_keyword ON document_keywords(keyword);

-- Audit log of classifier pipeline runs.
CREATE TABLE classifier_runs (
  run_id              TEXT PRIMARY KEY,
  target_dir          TEXT NOT NULL,
  started_at          TIMESTAMP NOT NULL,
  finished_at         TIMESTAMP,
  files_seen          INTEGER DEFAULT 0,
  files_classified    INTEGER DEFAULT 0,
  files_failed        INTEGER DEFAULT 0,
  organizer_summary   TEXT,
  error               TEXT
);

-- Keyword search index — Phase 1 retrieval target.
CREATE VIRTUAL TABLE documents_fts USING fts5(
  sha256       UNINDEXED,
  file_name,
  title,
  summary,
  body_excerpt,
  keywords_concat,
  reason,
  tokenize = 'unicode61 remove_diacritics 2'
);
```

`documents_fts` is kept in sync via simple triggers (insert/update/delete on
`documents` rebuilds the FTS row for that sha256). `keywords_concat` is the
space-joined keyword list, materialized at write time.

### 7.3 Phase 2 reservation — `document_chunks` (not built in v1)

The plan reserves a sibling table for sub-document retrieval. It is **not
created** in v1 to avoid empty schema noise:

```sql
-- Phase 2 only — DO NOT create in v1. Listed for design continuity.
CREATE TABLE document_chunks (
  sha256        TEXT NOT NULL REFERENCES documents(sha256) ON DELETE CASCADE,
  chunk_index   INTEGER NOT NULL,
  chunk_text    TEXT NOT NULL,
  chunk_offset  INTEGER NOT NULL,
  embedding     BLOB,
  PRIMARY KEY (sha256, chunk_index)
);
```

### 7.4 Phase 2 reservation — `folders` (not built in v1)

Folder-level metadata is **deferred to Phase 2**. In v1 we compute folder views
on the fly via aggregation:

```sql
-- Example v1 folder view (no folders table needed):
SELECT
  parent_dir,
  COUNT(*)                                  AS file_count,
  GROUP_CONCAT(DISTINCT department)         AS departments_present,
  MAX(confidentiality)                      AS max_confidentiality
FROM documents
GROUP BY parent_dir;
```

When the corpus grows past on-the-fly aggregation, Phase 2 materializes:

```sql
-- Phase 2 only — DO NOT create in v1.
CREATE TABLE folders (
  folder_path                 TEXT PRIMARY KEY,
  parent_path                 TEXT,
  file_count                  INTEGER NOT NULL,
  dominant_department         TEXT,
  dominant_business_category  TEXT,
  confidentiality_max         TEXT,
  summary                     TEXT,
  keywords                    TEXT,
  embedding                   BLOB,
  -- SharePoint reservations:
  sharepoint_site_id          TEXT,
  sharepoint_drive_id         TEXT,
  sharepoint_item_id          TEXT,
  sharepoint_url              TEXT,
  last_recomputed_at          TIMESTAMP
);
CREATE VIRTUAL TABLE folders_fts USING fts5(folder_path, summary, keywords);
```

Reasoning for deferral: a folder row is just an aggregation of its files'
metadata. Materializing it up front means a second write on every file move,
plus a recompute job when files churn — complexity worth paying only when
on-the-fly aggregation is too slow. Reserving the schema design now means no
migration pain when we flip it on.

## 8. Retrieval / search approach

Retrieval is staged across three phases to match the system's growth.

### 8.1 Phase 1 — keyword search + structured filters (this plan)

The v1 retrieval surface combines:

1. **Structured filters** on `department`, `document_type`,
   `business_category`, `confidentiality`, `parent_dir`, `file_modified_at`.
   These are exact and cheap.
2. **FTS5 keyword search** over `file_name`, `title`, `summary`,
   `body_excerpt`, `keywords_concat`, `reason`. This handles literal-phrase
   queries (the user's exact-words case).
3. **Ranking** by FTS5 BM25 score, tiebroken by `confidence` then
   `file_modified_at DESC`.

API shape (a thin Python module — implementation Phase 1):

```python
search(
    query: str | None,                    # FTS5 query; None means filter-only
    department: list[str] | None = None,
    document_type: list[str] | None = None,
    business_category: list[str] | None = None,
    confidentiality_max: str | None = None,
    parent_dir_prefix: str | None = None,
    modified_after: datetime | None = None,
    limit: int = 50,
) -> list[ClassificationRecord]
```

CLI form (Phase 1 deliverable):

```
fda metadata search "error rate"           --department=engineering
fda metadata search --department=sales --document-type=contract
fda metadata stats
```

### 8.2 Phase 2 — semantic search + folder pre-filter (deferred)

Phase 2 turns on three reserved capabilities:

1. **File-level embeddings** — populate `documents.embedding` and
   `embedding_card_text`. Vector similarity is run in-process against the
   same SQLite DB until corpus size forces a move to pgvector.
2. **Hybrid retrieval** — merge FTS5 BM25 ranks with embedding cosine ranks
   (e.g., reciprocal-rank fusion) so the same query handles both literal and
   semantic phrasing.
3. **Folder pre-filter (two-stage)** — when `folders` is materialized, route
   every query through `search_folders(query)` first to narrow to the top-K
   folder paths, then run file-level retrieval scoped to those folders. This
   cuts the AI agent's token cost on large corpora.

Phase 2 retrieval flow:

```
user query: "find files that have data about error rates"
  ↓
parse structured constraints from the query (Phase 2.5: optional)
  ↓
stage 1: search_folders(query)                     → top-K candidate folders
  ↓
stage 2: hybrid file search within those folders   → top-N files
            FTS5 BM25 ⊕ embedding cosine
  ↓
stage 3: text_chunks for top-N files               → exact passages
  ↓
return file paths + per-file metadata + chunk excerpts + scores
```

The AI agent reads only the returned chunks, not the whole files.

### 8.3 Phase 3 — SharePoint-aware retrieval (out of scope, see §9)

Once SharePoint sync is wired up, retrieval gains:

- A union view over local-only and SharePoint-resident documents.
- The ability to return `sharepoint_url` instead of (or alongside)
  `original_local_path` when the file has been uploaded.
- Folder-level retrieval that maps `folders.folder_path` to SharePoint
  drives/folders via `sharepoint_drive_id` / `sharepoint_item_id`.

## 9. Future SharePoint integration — **OUT OF SCOPE** for this plan

> ⚠ Everything in this section is **deferred**. No SharePoint or Microsoft
> Graph code is part of v1. The schema and pipeline are designed so SharePoint
> can be turned on later without breaking changes.

When SharePoint phase begins (a separate plan), the pieces below activate:

- **Schema**: the nullable columns reserved in §6.2 and §7.2 are populated:
  - `documents.sharepoint_site_id`
  - `documents.sharepoint_drive_id`
  - `documents.sharepoint_item_id`
  - `documents.sharepoint_url`
  - `documents.final_sharepoint_path`
  - Same set on the `folders` table (when materialized in Phase 2).
- **Mapping**: a small module translates a v1 `suggested_sharepoint_path`
  into a real SharePoint drive/folder pair (e.g.,
  `"Sales/2026/Q1/Reports/..."` → site `Sales`, drive `Documents`,
  folder `2026/Q1/Reports/...`).
- **Upload**: a separate Microsoft Graph client (using the existing `msal`
  dependency) handles file upload, then writes back the assigned IDs and URL.
- **Sync**: a reconciliation step compares local `sha256` to the remote
  `quickXorHash` / `sha1Hash` to detect drift.

What this plan **does** do to make Phase 3 painless:

- All SharePoint columns exist in the v1 schema as nullable. No migration is
  needed when Phase 3 starts.
- `suggested_sharepoint_path` is computed and stored in v1, so the mapping
  module has consistent input.
- The metadata DB is isolated from `ProjectState`, so a future SharePoint
  worker can read/write it without coupling to journal/task code.

What this plan **does not** do:

- No `msal` token flow.
- No Graph API calls.
- No upload, no sync, no reconciliation.
- No `tenants/clients/sites` configuration shape.

## 10. Implementation tasks

Sequenced, small, each landable in a single PR. Implementation begins **only
after this plan is approved** and only once the writing-plans skill has
produced a per-task plan.

| # | Task | Module | Depends on |
|---|------|--------|------------|
| T1 | Define `ClassificationRecord` pydantic v2 schema and controlled vocabularies. | `fda/metadata/schema.py` | — |
| T2 | Create `metadata.db` storage module: schema bootstrap, upsert, FTS5 triggers, audit log. | `fda/metadata/store.py` | T1 |
| T3 | Text-extraction utility per extension (.txt/.md/.csv direct; .pdf/.docx via optional libs; binary → empty). Truncates to `body_excerpt` size. | `fda/metadata/extract.py` | — |
| T4 | Strict-output classifier component: one Claude call per file with strict JSON prompt; parses through pydantic. Includes a fake `Classifier` for tests. | `fda/metadata/classifier.py` | T1, T3 |
| T5 | Pipeline runner: discover → fingerprint → extract → call `organize_files()` (unmodified) → classify → derive SP path → validate → persist. | `fda/metadata/pipeline.py` | T2, T3, T4 |
| T6 | Retrieval API: `search(...)` with filters + FTS5 query. | `fda/metadata/search.py` | T2 |
| T7 | CLI subcommand: `fda metadata classify <dir>`, `fda metadata search`, `fda metadata stats`. | `fda/cli.py` (additive) | T5, T6 |
| T8 | Test suite: schema unit tests, store unit tests, fake-classifier pipeline test, end-to-end test on synthetic fixtures. | `tests/test_metadata_*.py` | all |

Each task is small enough to be reviewed independently. T1–T4 can land before
T5 wires them up; T6 can land in parallel with T5; T7 lands last.

**Constraints reaffirmed for every task:**

- Do **not** modify `fda/local_worker_agent.py::organize_files` or any
  `_orgtool_*` method.
- Do **not** modify `fda/file_indexer.py` or `fda/librarian_agent.py`.
- Do **not** import from `fda/kakaotalk/` anywhere in `fda/metadata/`.
- Do **not** add Microsoft Graph or SharePoint upload code.
- Pre-commit pytest suite must pass on every commit (per `CLAUDE.md`).

## 11. Testing plan — synthetic / sample files only

**Hard rule:** no test, fixture, or development run touches a real local user
directory. All inputs live under `tests/fixtures/sample_docs/` and are
authored by hand.

### 11.1 Fixture set

A small mixed set, ~10 files, all clearly fake:

```
tests/fixtures/sample_docs/
  fake_invoice_acme_2026Q1.txt       (~300 bytes, plain text invoice)
  fake_hr_policy_remote_work.md      (~1KB, markdown HR policy)
  fake_press_release_product_x.txt   (~500 bytes, marketing)
  fake_q3_revenue_summary.csv        (~200 bytes, finance numbers)
  fake_legal_nda_template.md         (~2KB, legal contract template)
  fake_engineering_postmortem.md     (~1.5KB, ops/eng postmortem with
                                       error-rate language — used by
                                       semantic-search recall test stub)
  fake_meeting_notes_exec.txt        (~400 bytes, executive)
  fake_vendor_contract_draft.txt     (~800 bytes, partnership)
  empty_file.txt                     (0 bytes, edge case)
  binary_blob.bin                    (random bytes, edge case)
```

None of these reference real companies, people, or systems.

### 11.2 Test cases

**Unit — schema (`test_metadata_schema.py`):**

- valid record parses; required fields enforced; enum values strict.
- invalid sha256, confidence out of range, empty keywords list all raise.
- Phase 2 fields default to `None` and accept only `None` in v1.

**Unit — store (`test_metadata_store.py`):**

- insert + retrieve by sha256.
- `documents_fts` is updated on insert/update/delete (trigger correctness).
- filter queries (`department=`, `confidentiality_max=`, `parent_dir_prefix=`).

**Unit — extractor (`test_metadata_extract.py`):**

- .txt and .md extract correctly.
- empty file returns empty body_excerpt.
- binary blob returns empty body_excerpt without raising.
- excerpt truncation respects max length.

**Unit — pipeline with fake classifier (`test_metadata_pipeline_fake.py`):**

- Substitute a `FakeClassifier` returning canned strict JSON.
- Substitute a no-op `organize_files` (returns empty `moves`) so this test
  does not require a real Claude backend.
- Asserts: every fixture file produces one row; SHA-256 stable; FTS query for
  a known keyword returns the expected row(s).

**End-to-end (`test_metadata_pipeline_e2e.py`):**

- Uses the existing test pattern of mocking the Claude backend via
  `get_claude_backend()` (per `CLAUDE.md`). A real backend is never used in
  the default test run; an opt-in env flag may enable it for local manual
  verification only.
- Runs the full pipeline against `sample_docs/` copied into a `tmp_path`.
- Asserts:
  - All 12 Phase-1 fields populated for every non-empty file.
  - Controlled-vocabulary fields all valid.
  - `suggested_sharepoint_path` is a clean POSIX relative path.
  - FTS query `"revenue"` matches the finance fixture.
  - FTS query `"error rate"` matches the postmortem fixture.
  - `classifier_runs` has one row with non-zero `files_classified`.
  - **No file was moved outside `tmp_path`.**
  - Phase 2 columns are all `NULL`.

**Search-recall stubs (`test_metadata_search.py`):**

- Filter-only queries return the right rows.
- FTS5 queries with `AND`, `OR`, phrase, and prefix all work.
- Confidence-threshold filter works.

**Integrity:**

- Pre-commit hook (`/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`) must pass with the new tests added. The 113 existing tests must remain green.

### 11.3 What testing does **not** do

- No real local user directory is scanned.
- No real `organize_files` run is performed against the user's home folder
  in tests; all runs are scoped to `tmp_path`.
- No SharePoint, no Microsoft Graph, no network calls.
- No KakaoTalk parsing or message classification.

## 12. Open questions deferred to writing-plans

These are intentionally not decided here; they belong in the per-task plan:

- Choice of PDF/Word extractor (`pypdf` vs `pdfminer.six`; `python-docx`
  presence) — depends on which produces the cleanest text on the fixture set.
- Exact prompt wording for the strict classifier — best iterated against
  the fixtures during T4.
- CLI flag shape and output format (table vs JSON) — minor, decided in T7.
- Whether to expose the metadata search via the existing MCP server
  (`fda/mcp_server.py`) as a new tool — likely yes, but a separate task
  after Phase 1 ships.

---

## Appendix A — Decisions log

- **D1.** Schema validation = pydantic v2. New dependency.
- **D2.** Classifier integration = wrapper, not modification. The strict
  classifier runs after `organize_files()` returns; existing organizer code
  is untouched.
- **D3.** Storage = separate SQLite DB `metadata.db`, independent of
  `ProjectState`. SharePoint columns reserved nullable from day one.
- **D4.** SHA-256 is computed before the organizer runs. Identity is stable
  across moves.
- **D5.** Retrieval Phase 1 = SQL filters + FTS5 only. Embeddings, pgvector,
  and chunked retrieval are Phase 2.
- **D6.** Folder metadata is planned but deferred. Phase 1 computes folder
  views by aggregation. Phase 2 materializes a `folders` table and
  enables two-stage retrieval.
- **D7.** Phase 1 schema includes `body_excerpt`, `title`, `language`,
  `mime_type`, `size_bytes`, `file_modified_at` to make keyword search useful
  out of the box; `embedding`, `embedding_card_text`, `entities`, and the
  SharePoint columns are reserved as nullable for Phase 2/3.
