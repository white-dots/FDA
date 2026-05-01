# Local Metadata & Retrieval Layer — Plan

**Status:** design only — no code changes in this doc.
**Date:** 2026-05-01

---

## 1. Goal

We already have an "organizer" agent (`LocalWorkerAgent.organize_files`) that
sorts files into folders by purpose. What we **don't** have is a record of
*what* each file is — its department, type, summary, keywords, etc.

This plan adds a thin layer around that organizer. For every file the
organizer touches, we:

1. Ask a separate Claude call to classify it into a strict schema.
2. Save that classification into a small local SQLite database.
3. Make it searchable — keyword search now, AI semantic search later.

The new layer wraps the organizer; it does not change it. It is also
**built from scratch** — the existing path-only indexer
(`fda/file_indexer.py`) and the librarian agent (`fda/librarian_agent.py`)
are not used as a foundation. They are background context only.

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

This is the "classifier" we wrap. It's a **Claude tool-loop**: Claude is
given a directory and a small toolbox, and it organizes the directory by
file purpose. It works like a careful intern with a checklist:

- **Workflow** (from `ORGANIZE_SYSTEM_PROMPT`, line 986):
  list the directory → inspect each file → group files by purpose →
  create folders → move files into them.
- **Tools** Claude can use (`_FILE_ORGANIZE_TOOLS`, line 171):
  `list_directory`, `get_file_info`, `read_file`, `move_file`,
  `create_directory`, `delete_file`, `run_command`.
- **Hard rules** built into the tool implementations (Claude cannot bypass):
  - Never touch anything inside a git repo (`_is_inside_git_repo`, line 1149).
  - Never delete arbitrary files — only well-known junk like `.DS_Store`
    (`_JUNK_FILES`, line 305) or zero-byte files.
  - Never operate outside the target directory.

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

**Today, the only "classification" is the destination folder Claude picks.**
There is no structured per-file output — no department, document type,
business category, confidentiality label, summary, or keywords. That is
exactly the gap this plan fills.

### 3.2 Existing indexer / retrieval — background only, **not suitable to build on**

Location: `fda/file_indexer.py` (366 lines) and `fda/librarian_agent.py`
(2423 lines).

What they do today, briefly:

- `file_indexer.py` embeds **only the filename and path tokens** of files
  it walks (Documents, Downloads, Desktop). It never reads file contents
  and never knows what a file is *about*.
- `librarian_agent.py` is a wrapper around `file_indexer` plus extra
  project-knowledge features. Same limitation — no content, no taxonomy.

We are not building on them. The reasons, plainly:

- **They never look inside files.** Filename and path tokens are not enough
  for keyword or semantic search over real document content.
- **They have no schema.** No department, no document type, no validation —
  nothing to retrieve against.
- **They share storage with `ProjectState`,** which is a grab-bag of journal,
  task, and KPI data. Mixing document metadata into that store would couple
  two unrelated concerns.
- **They walk the user's real folders.** This plan is synthetic-fixtures-only,
  so we can't reuse code that's wired into live user directories.

The new layer is its own thing — its own SQLite file (`metadata.db`), its
own schema, its own entrypoint. The two systems can coexist; they just
don't share code.

### 3.3 KakaoTalk message classifier — explicitly excluded

`orchestrator.py::classify_message` (`MESSAGE_CLASSIFIER_PROMPT`, line 58) is a
**KakaoTalk-message classifier** for client chat triage. It is not a document
classifier and is out of scope for this plan. No design or code in this layer
references it.

## 4. What is missing

To turn the organizer into something a future AI search agent can call, we
need the following pieces — none of which exist yet:

- **A structured output shape per file.** Today the organizer only picks a
  folder. We need it (or a wrapper) to also produce department,
  document_type, business_category, confidentiality, summary, keywords,
  title, language, body_excerpt, etc. The full list is in §6.1.
- **Validation.** Without pydantic or jsonschema, there is nothing stopping
  a malformed classification from being saved. We need strict parsing.
- **Somewhere to put the results.** No table exists for document metadata.
  The `ProjectState` SQLite file is for journal/task data, not this.
- **Keyword search.** The existing indexer is filename-only embeddings. There
  is no FTS5 (SQLite full-text search) over summaries, keywords, or titles.
- **A future SharePoint path.** Eventually each file will live in SharePoint
  at a specific path. Nothing today computes that path or stores it.
- **A stable file ID.** Files are identified by their current path, which
  breaks the moment the organizer moves them. We need a SHA-256 hash so
  identity survives moves.
- **Room to grow.** When Phase 2 adds embeddings and text chunks, and Phase 3
  adds SharePoint IDs, we don't want a schema migration. Those columns
  should be reserved as nullable from day one.

## 5. Proposed local-only pipeline (from scratch)

The pipeline runs in 11 small steps. The organizer is one of those steps;
the rest are bookkeeping around it. Nothing in this pipeline changes the
organizer — it just wraps it.

A few principles to keep in mind while reading the diagram below:

- **SHA-256 is the file's identity.** We compute it *before* the organizer
  runs, so even if the organizer moves a file, we still know which file is
  which.
- **Idempotent.** Running the pipeline twice over the same directory is safe
  — the same files produce the same rows.
- **Local-only.** It only ever runs against a synthetic test directory, not
  the user's real folders.

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
│                         capture {moves, summary, deletions,          │
│                         dirs_created, repos_skipped}                 │
│  5. resolve_outcome     per-sha256, classify the organizer action:   │
│                          • moved          → file is at moves[i].to   │
│                          • unchanged      → file stayed put          │
│                          • skipped_repo   → inside a git repo;       │
│                                              organizer never touched │
│                          • deleted_junk   → in deletions list        │
│                                              (only .DS_Store etc.)   │
│                         drop deleted_junk and skipped_repo from      │
│                         further classification                       │
│  6. classify_strict     NEW component — one strict-JSON Claude call  │
│                         per remaining file, given content + the      │
│                         organizer's destination folder               │
│  7. derive_sp_path      compute suggested_sharepoint_path from       │
│                         organizer's destination folder               │
│  8. validate            pydantic v2 strict parse against schema      │
│  9. persist             single transaction upsert into metadata.db   │
│                         keyed by sha256: documents row +             │
│                         document_keywords rows + FTS refresh         │
│ 10. record_failures     any file that failed extraction, classify,   │
│                         or validation gets a row in                  │
│                         classifier_run_files (does NOT block other   │
│                         files — partial runs are first-class)        │
│ 11. report              classifier_runs row: counts + organizer      │
│                         summary + duration                           │
└──────────────────────────────────────────────────────────────────────┘
```

Key properties:

- **Step 4 — the organizer — is untouched.** We just call it and read what
  it returns. No edits to its prompt, its tools, or its behavior.
- **Step 6 is the one new Claude call.** It's a separate strict-JSON prompt
  that runs *after* the organizer is done. The organizer's loop and the
  classifier's call don't talk to each other.
- **SHA-256 lets us survive moves.** Computed in step 2 (before any moves),
  used as the key for everything afterwards. Even if the organizer moves a
  file across folders, we still know it's the same file.
- **Each record knows where the file used to be and where it is now.** Step 5
  produces both `original_local_path` (pre-move) and `current_local_path`
  (post-move), plus an `organizer_action` enum saying which one happened.
  That way, search results can point at the file's current location.
- **Junk files get logged but not classified.** When the organizer deletes a
  `.DS_Store` or `Thumbs.db`, it's gone — there's nothing left to classify.
  We just write an audit row to `classifier_run_files` with action
  `deleted_junk`.
- **Files inside git repos get logged but not classified.** Same idea: the
  organizer refuses to touch git repos, so we have no post-move identity
  for them. Audit row only.
- **One bad file doesn't kill the run.** Steps 3, 6, and 8 each catch their
  own errors. A failing file becomes a row in `classifier_run_files`; the
  rest of the run continues. The run's overall status becomes `partial` if
  anything failed.
- **Steps 3 and 6 are per-file**, so they can be parallelized later if speed
  matters.

## 6. Strict classifier output schema

The classifier (step 6 in §5) returns one JSON object per file. Pydantic v2
parses it; if any field is missing, malformed, or out of range, we reject
it instead of saving garbage.

The shape below is for v1. Phase 2 (embeddings, chunks) and Phase 3
(SharePoint) fields are listed too, but they are reserved as nullable —
they exist in the table from day one, populated later, with no migration.

### 6.1 Phase 1 fields — required for v1

```
ClassificationRecord {
  # identity
  file_name:                str        # e.g. "q3_revenue_report.xlsx"
  original_local_path:      str        # absolute path BEFORE the organizer moved it
  current_local_path:       str        # absolute path AFTER the organizer ran;
                                       # equals original_local_path when
                                       # organizer_action == "unchanged"
  organizer_action:         enum       # moved | unchanged
                                       # (skipped_repo and deleted_junk files
                                       # do not produce a ClassificationRecord;
                                       # see §5 step 5)
  sha256:                   str        # 64-char hex; primary key
  size_bytes:               int
  mime_type:                str        # primary: stdlib mimetypes.guess_type;
                                       # fallback 1: `file --mime-type` if the
                                       #             CLI is available (optional);
                                       # fallback 2: "application/octet-stream"
                                       #             (IANA "unknown binary" sentinel
                                       #             — guarantees the field is
                                       #             always a non-empty MIME-shaped
                                       #             string for validation)
  language:                 str        # ISO 639-1 lowercase ("en", "ko", ...)
                                       # or "und" if detection failed

  # the user's required taxonomy fields (controlled vocabulary)
  department:               enum       # see §6.3
  document_type:            enum       # see §6.3
  business_category:        enum       # see §6.3
  confidentiality:          enum       # public | internal | confidential | restricted
                                       # (no "unknown" — see §6.3 default policy)

  # generated content fields (drive keyword + semantic search)
  title:                    str        # doc-internal title, falls back to file_name
  summary:                  str        # 1–3 sentence abstract; main FTS field
  keywords:                 list[str]  # 3–15 terms; canonical list
  keywords_concat:          str        # space-joined keywords; denormalized
                                       # mirror written atomically with
                                       # keywords; powers FTS without a
                                       # join (see §7.2)
  body_excerpt:             str        # first ~2000 chars of extracted text;
                                       # empty string for binaries / size-0 files
  reason:                   str        # classifier rationale; for audit only;
                                       # NOT included in the FTS index (see §7.2)

  # routing — name kept per the original requirement; this value is the
  # forward-looking canonical path that becomes the SharePoint path in
  # Phase 3. v1 mapping rule: "<Department>/<BusinessCategory>/<file_name>"
  # using Title-Cased vocabulary values.
  suggested_sharepoint_path: str       # e.g. "Sales/Revenue/q3_revenue_report.xlsx"

  # quality
  confidence:               float      # ∈ [0.0, 1.0]

  # confidentiality severity rank — denormalized from confidentiality so the
  # API can do "max sensitivity ≤ X" range filters; see §7.2 mapping
  confidentiality_rank:     int        # 1=public, 2=internal, 3=confidential, 4=restricted

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

The classifier can only return values from these short lists. We keep the
lists small on purpose: adding a value later is easy, removing one is a
breaking change for any code that already filters on it.

- `department`: `sales | finance | hr | legal | engineering | operations | marketing | executive | unknown`
- `document_type`: `report | contract | invoice | policy | proposal | presentation | spreadsheet | email | memo | other`
- `business_category`: `revenue | compliance | personnel | product | strategy | partnership | vendor | customer | unknown`
- `confidentiality`: `public | internal | confidential | restricted`
- `organizer_action` (set by the pipeline, not the classifier): `moved | unchanged`

**What happens when the classifier isn't sure:**

- For `department`, `document_type`, `business_category` — return
  `unknown` or `other`, and lower `confidence` accordingly.
- For `confidentiality` — there is no `unknown` value, on purpose. A
  security label has to be definite. If the classifier can't tell, it
  must label the file `restricted` (the most restrictive option) and
  record its uncertainty in `confidence`.

Why fail closed on confidentiality? Because the cost of being wrong is
asymmetric. A public file accidentally labeled `restricted` just means
someone asks for access — recoverable. A confidential file accidentally
labeled `public` is a leak — not recoverable.

**Telling "definitely restricted" from "I don't know, so I said
restricted":** the only signal is `confidence`. We deliberately don't
add a separate "uncertain" flag, because `confidence` already carries
that information and a second flag would just split the contract. In
practice:

- Callers who want to filter out fail-closed records use `confidence_min`
  on the search API (§8.1).
- UI should always show `confidence` next to a `restricted` result so a
  human can spot low-confidence ones and re-classify.
- Automated routing (e.g., future SharePoint uploads) should treat
  `restricted` + low confidence as "send to human review," not as
  permanently blocked.

### 6.4 Validation rules (enforced by pydantic)

These rules are checked the moment the classifier output is parsed. If any
of them fail, the file is logged as `validate_failed` and skipped — no
partial rows ever reach the database.

- **`sha256`**: 64-char lowercase hex (`^[0-9a-f]{64}$`).
- **`confidence`**: a number between 0.0 and 1.0 inclusive.
- **`confidentiality_rank`**: must be 1, 2, 3, or 4, and must match
  `confidentiality` (1=public, 2=internal, 3=confidential, 4=restricted).
  The validator computes the rank from the label automatically; if the
  classifier sends an inconsistent pair, validation fails.
- **`keywords`**: a list of 3–15 items. Each keyword is a non-empty string
  up to 64 characters, with no whitespace inside it. (No whitespace is the
  trick that lets `keywords_concat = " ".join(keywords)` round-trip
  cleanly via `split(' ')`.) Multi-word terms use `_` or `-` instead —
  e.g., `"error_rate"`, `"q3-revenue"`.
- **`keywords_concat`**: must equal `" ".join(keywords)` exactly. The
  validator computes this; the classifier doesn't have to supply it.
- **`summary`**: 5 to 800 characters. The low end is intentionally
  permissive so binary or empty files can still get a one-sentence summary
  like `"Empty file."` or `"Binary blob; no text extracted."`.
- **`body_excerpt`**: truncated by step 3 to ~2000 characters target,
  4000 hard cap. Empty string is valid (binaries, zero-byte files).
- **`language`**: two lowercase letters (e.g. `"en"`, `"ko"`), or `"und"`
  when detection failed. Detection failing is fine; sending `"EN"` or
  `"eng"` is not.
- **`mime_type`**: a string shaped like `type/subtype`, never empty.
- **All enum fields** must use the values listed in §6.3. Anything else
  fails.
- **`original_local_path` and `current_local_path`**: non-empty absolute
  paths. If `organizer_action == "unchanged"`, they must be the same path.
- **`suggested_sharepoint_path`**: a relative path with forward slashes,
  no leading `/`, no `..`, no empty segments, no drive letter, no `~`.
- **Phase 2 / Phase 3 fields**: accepted only as `None` in v1. Anything
  else is rejected.

## 7. Local metadata storage

### 7.1 Database location and isolation

The store is one SQLite file at `~/.fda/metadata.db`. We deliberately
**don't** reuse `ProjectState`'s SQLite file — keeping document metadata
in its own database means:

- It can't be polluted by journal/task/KPI rows.
- When we eventually migrate to PostgreSQL, it's a single dump→load,
  not a tangle of cross-table dependencies.

**Tests never touch the real DB.** The store reads an env var
`FDA_METADATA_DB` first and only falls back to `~/.fda/metadata.db` if
it's unset. The pytest fixture always points it at a fresh `tmp_path`
file, so no test can ever open the user's real metadata database.

### 7.2 Tables

```sql
-- Primary metadata, one row per unique file content (keyed by sha256).
CREATE TABLE documents (
  sha256                     TEXT PRIMARY KEY
                             -- length=64 AND every character is a lowercase
                             -- hex digit. SQLite GLOB does not support
                             -- "match N times" so we negate: forbid any
                             -- non-hex character.
                             CHECK (length(sha256) = 64
                                    AND sha256 NOT GLOB '*[^0-9a-f]*'),
  file_name                  TEXT NOT NULL,
  original_local_path        TEXT NOT NULL,
  current_local_path         TEXT NOT NULL,
  parent_dir                 TEXT NOT NULL,         -- denormalized for filtering;
                                                    -- equals dirname(current_local_path)
  organizer_action           TEXT NOT NULL
                             CHECK (organizer_action IN ('moved', 'unchanged')),
  size_bytes                 INTEGER NOT NULL CHECK (size_bytes >= 0),
  mime_type                  TEXT NOT NULL,
  language                   TEXT NOT NULL
                             CHECK (language = 'und' OR (language GLOB '[a-z][a-z]')),

  department                 TEXT NOT NULL
                             CHECK (department IN
                               ('sales','finance','hr','legal','engineering',
                                'operations','marketing','executive','unknown')),
  document_type              TEXT NOT NULL
                             CHECK (document_type IN
                               ('report','contract','invoice','policy','proposal',
                                'presentation','spreadsheet','email','memo','other')),
  business_category          TEXT NOT NULL
                             CHECK (business_category IN
                               ('revenue','compliance','personnel','product','strategy',
                                'partnership','vendor','customer','unknown')),
  confidentiality            TEXT NOT NULL
                             CHECK (confidentiality IN
                               ('public','internal','confidential','restricted')),
  confidentiality_rank       INTEGER NOT NULL
                             CHECK (confidentiality_rank BETWEEN 1 AND 4),
  -- Paired CHECK: rank must match label. Rejects rows like
  -- ('public', 4) at the SQLite layer, not just in pydantic.
  CONSTRAINT confidentiality_label_rank_consistent CHECK (
    (confidentiality = 'public'       AND confidentiality_rank = 1) OR
    (confidentiality = 'internal'     AND confidentiality_rank = 2) OR
    (confidentiality = 'confidential' AND confidentiality_rank = 3) OR
    (confidentiality = 'restricted'   AND confidentiality_rank = 4)
  ),

  title                      TEXT NOT NULL,
  summary                    TEXT NOT NULL
                             CHECK (length(summary) BETWEEN 5 AND 800),
  body_excerpt               TEXT NOT NULL
                             CHECK (length(body_excerpt) <= 4000),
  -- Denormalized space-joined keyword list. Written atomically with the
  -- document_keywords rows in a single transaction. Powers FTS without a
  -- join, so the FTS triggers only need to fire on `documents`.
  keywords_concat            TEXT NOT NULL,
  reason                     TEXT NOT NULL,

  suggested_sharepoint_path  TEXT NOT NULL,

  confidence                 REAL NOT NULL
                             CHECK (confidence BETWEEN 0.0 AND 1.0),

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

CREATE INDEX idx_documents_department          ON documents(department);
CREATE INDEX idx_documents_document_type       ON documents(document_type);
CREATE INDEX idx_documents_business_category   ON documents(business_category);
CREATE INDEX idx_documents_confidentiality     ON documents(confidentiality);
CREATE INDEX idx_documents_confidentiality_rk  ON documents(confidentiality_rank);
CREATE INDEX idx_documents_parent_dir          ON documents(parent_dir);
CREATE INDEX idx_documents_modified_at         ON documents(file_modified_at);
CREATE INDEX idx_documents_confidence          ON documents(confidence);

-- Keywords as a normalized table for facet/filter queries (e.g., "show me
-- all docs tagged with `compliance`"). The denormalized keywords_concat on
-- `documents` is the FTS-facing copy; both are written in one transaction.
CREATE TABLE document_keywords (
  sha256   TEXT NOT NULL REFERENCES documents(sha256) ON DELETE CASCADE,
  keyword  TEXT NOT NULL,
  PRIMARY KEY (sha256, keyword)
);
CREATE INDEX idx_keywords_keyword ON document_keywords(keyword);

-- Audit log of pipeline runs (one row per run).
CREATE TABLE classifier_runs (
  run_id              TEXT PRIMARY KEY,
  target_dir          TEXT NOT NULL,
  started_at          TIMESTAMP NOT NULL,
  finished_at         TIMESTAMP,
  status              TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','ok','partial','failed')),
  files_seen          INTEGER NOT NULL DEFAULT 0 CHECK (files_seen          >= 0),
  files_classified    INTEGER NOT NULL DEFAULT 0 CHECK (files_classified    >= 0),
  files_failed        INTEGER NOT NULL DEFAULT 0 CHECK (files_failed        >= 0),
  files_skipped_repo  INTEGER NOT NULL DEFAULT 0 CHECK (files_skipped_repo  >= 0),
  files_deleted_junk  INTEGER NOT NULL DEFAULT 0 CHECK (files_deleted_junk  >= 0),
  organizer_summary   TEXT,
  error               TEXT
);

-- Per-file events for a run: every file the pipeline saw, with its outcome.
-- Includes files that did NOT make it into `documents` (skipped_repo,
-- deleted_junk, classify_failed, validate_failed). Lets us debug partial
-- runs and quantify drop-off.
CREATE TABLE classifier_run_files (
  run_id              TEXT NOT NULL REFERENCES classifier_runs(run_id) ON DELETE CASCADE,
  file_path           TEXT NOT NULL,             -- pre-organizer path (always present)
  current_local_path  TEXT,                      -- post-organizer path; NULL when
                                                 -- the file was never moved or was
                                                 -- deleted_junk / skipped_repo
  organizer_action    TEXT
                      CHECK (organizer_action IS NULL OR organizer_action IN
                             ('moved','unchanged','skipped_repo','deleted_junk')),
  sha256              TEXT,                      -- NULL if fingerprinting failed
  outcome             TEXT NOT NULL
                      CHECK (outcome IN ('classified','skipped_repo','deleted_junk',
                                         'extract_failed','classify_failed',
                                         'validate_failed')),
  error               TEXT,
  PRIMARY KEY (run_id, file_path)
);

-- Keyword search index — Phase 1 retrieval target.
-- External-content FTS5 tied to `documents`. Storage cost is the index only
-- (no duplicated text); content is read from `documents` at query time.
-- `reason` is intentionally NOT in the FTS columns — it is stored in
-- `documents` for audit but excluded from search to avoid debug rationale
-- polluting user queries.
CREATE VIRTUAL TABLE documents_fts USING fts5(
  file_name,
  title,
  summary,
  body_excerpt,
  keywords_concat,
  content='documents',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
```

**How FTS stays in sync.** `documents_fts` is "external-content," meaning
it stores the search index but reads the actual text from `documents`.
Triggers on `documents` (insert / update of any indexed column / delete)
keep the index current. We never write to `document_keywords` without
also rewriting `documents.keywords_concat` in the same transaction —
that's a contract of the `upsert_document(record)` API in the storage
module. Result: keyword changes always reach the FTS index via the
`documents` triggers, no matter how the upsert is called.

**Why a separate `confidentiality_rank` column.** Comparing labels as
strings is wrong (`"public" > "confidential"` alphabetically, but
semantically the opposite). So we store an integer rank alongside the
label: `public=1 < internal=2 < confidential=3 < restricted=4`. The API
filter `confidentiality_max="internal"` becomes
`confidentiality_rank <= 2` — fast, correct, index-friendly. The schema
forces the rank and label to agree (paired CHECK constraint + pydantic
validator).

**Why `GLOB` instead of `LIKE` for path prefixes.** This trips people up:
SQLite's `LIKE` is **case-insensitive for ASCII by default**, regardless
of how the column is declared. So `parent_dir LIKE '/tmp/Foo%'` would
also match `/tmp/foo/...`, which is wrong on a case-sensitive filesystem.
`GLOB` is always case-sensitive, and SQLite can use a B-tree index for
`GLOB :prefix || '*'` as long as the prefix has no leading wildcard —
which ours doesn't. So `parent_dir GLOB :prefix || '*'` is correct
*and* fast.

### 7.3 Phase 2 sibling — `document_chunks` (illustrative; not built in v1)

When Phase 2 turns on semantic search, we'll need a sibling table that holds
text chunks (so the AI agent can read just the relevant passages instead
of the whole file). This table is **not created in v1.** The shape below
is just a sketch; the final column set is decided when Phase 2 starts.
We expect to add at least a per-chunk content hash and a tokenizer
version fingerprint, so we can tell whether a chunk needs re-chunking
when we change the splitter:

```sql
-- Phase 2 only — illustrative shape; final columns locked in Phase 2 plan.
CREATE TABLE document_chunks (
  sha256        TEXT NOT NULL REFERENCES documents(sha256) ON DELETE CASCADE,
  chunk_index   INTEGER NOT NULL,
  chunk_text    TEXT NOT NULL,
  chunk_offset  INTEGER NOT NULL,
  -- expected additions in Phase 2:
  --   chunk_hash       TEXT       -- sha256 of chunk_text
  --   tokenizer        TEXT       -- e.g. "tiktoken-cl100k_base"
  --   tokenizer_version TEXT
  --   source_text_len  INTEGER
  embedding     BLOB,
  PRIMARY KEY (sha256, chunk_index)
);
```

### 7.4 Phase 2 reservation — `folders` (not built in v1)

Eventually we'll want metadata at the folder level too — "show me what's
in this folder" or "which folders contain finance documents?" — so an AI
agent can pre-filter to a few folders before searching files.

For v1, **we don't materialize a folders table.** We compute folder views
on the fly via SQL aggregation. This is plenty fast for small corpora and
saves us from re-running an aggregation job every time a file moves.

```sql
-- Example v1 folder view (no folders table needed). MAX is taken over
-- confidentiality_rank (integer), NOT confidentiality (string), to avoid
-- lexicographic ordering bugs (e.g., 'public' > 'confidential' as strings).
-- The label is recovered by joining back through a CASE.
SELECT
  parent_dir,
  COUNT(*)                                            AS file_count,
  GROUP_CONCAT(DISTINCT department)                   AS departments_present,
  MAX(confidentiality_rank)                           AS max_confidentiality_rank,
  CASE MAX(confidentiality_rank)
       WHEN 1 THEN 'public'
       WHEN 2 THEN 'internal'
       WHEN 3 THEN 'confidential'
       WHEN 4 THEN 'restricted'
  END                                                 AS max_confidentiality
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
  -- Stored as INTEGER, not TEXT, so range queries and rollups behave
  -- correctly. The label is derived from the rank when needed.
  confidentiality_max_rank    INTEGER
                              CHECK (confidentiality_max_rank IS NULL
                                     OR confidentiality_max_rank BETWEEN 1 AND 4),
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

Why defer? A folder row is just a summary of its files' rows. If we
materialize it now, every file change forces a second write, and we need
a recompute job to keep stale folder rows fresh. That's complexity
worth paying *only* when on-the-fly aggregation gets too slow. Sketching
the schema now means we have no migration pain when we do flip it on.

## 8. Retrieval / search approach

Search grows in three phases. We start simple (keyword + filters), add
semantic search later, and SharePoint-aware retrieval after that.

### 8.1 Phase 1 — keyword search + structured filters (this plan)

For v1, search is the combination of three things:

1. **Structured filters** — exact-match SQL `WHERE` clauses on
   `department`, `document_type`, `business_category`,
   `confidentiality_rank` (via `confidentiality_max`), `parent_dir`,
   `file_modified_at`, and `confidence`. Cheap, indexed, predictable.
2. **Keyword search** via FTS5 over `file_name`, `title`, `summary`,
   `body_excerpt`, and `keywords_concat`. This is what handles the
   "find files with the word *invoice*" case. `reason` is *not* in the
   FTS index — it's audit-only and would just clutter results.
3. **Ranking** — FTS5's built-in BM25 score, with ties broken first by
   `confidence`, then by `file_modified_at DESC`.

API shape (a thin Python module — implementation Phase 1):

```python
search(
    query: str | None = None,             # FTS5 query; None means filter-only
    department: list[str] | None = None,
    document_type: list[str] | None = None,
    business_category: list[str] | None = None,
    confidentiality_max: str | None = None,  # one of public|internal|
                                              # confidential|restricted; matches
                                              # rows with confidentiality_rank
                                              # <= rank(confidentiality_max)
    parent_dir_prefix: str | None = None, # SQL: parent_dir GLOB prefix || '*'
                                          # (always case-sensitive, index-usable)
    modified_after: datetime | None = None,
    confidence_min: float | None = None,  # rows with confidence >= value
    limit: int = 50,
) -> list[ClassificationRecord]
```

**How we get the keyword list back.** When a search returns a row, we
rebuild the `keywords` list by splitting `keywords_concat` on a single
space. Why not read from `document_keywords`? Because `document_keywords`
is a set — order isn't preserved there. `keywords_concat` is the
canonical ordered copy, written verbatim from the classifier's output.
`document_keywords` is only used for facet/filter queries like "show me
everything tagged `compliance`."

CLI form (Phase 1 deliverable):

```
fda metadata search "error rate"           --department=engineering
fda metadata search --department=sales --document-type=contract
fda metadata stats
```

### 8.2 Phase 2 — semantic search + folder pre-filter (deferred)

Phase 2 turns on three things that are sketched in v1's schema but unused:

1. **Per-file embeddings.** Fill in `documents.embedding` and
   `embedding_card_text`. We run cosine similarity in Python against the
   same SQLite database; if the corpus outgrows that, we migrate to
   pgvector.
2. **Hybrid ranking** — combine BM25 (the keyword score) with cosine
   similarity (the semantic score) using **reciprocal-rank fusion (RRF)**.
   RRF works on *ranks*, not raw scores, which sidesteps the awkward
   problem of mixing BM25 (unbounded) and cosine ([-1, 1]) directly.
   Default fusion constant: **`k = 60`** (Cormack et al.). Anyone
   overriding it must do so via config, not by silently changing code.
3. **Folder pre-filter** — once the `folders` table exists, every query
   runs through `search_folders(query)` first to pick the top-K folders,
   then does file-level retrieval only within those folders. This cuts
   token cost dramatically on large corpora because the AI agent can
   ignore most of the tree.

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

> ⚠ Everything in this section is **deferred**. v1 has no SharePoint code,
> no Microsoft Graph calls, no upload, nothing. The schema is just designed
> so we can add it later without breaking what already works.

When the SharePoint phase begins (in its own plan), here's what comes online:

- **Schema**: the nullable columns reserved in §6.2 and §7.2 are populated:
  - `documents.sharepoint_site_id`
  - `documents.sharepoint_drive_id`
  - `documents.sharepoint_item_id`
  - `documents.sharepoint_url`
  - `documents.final_sharepoint_path`
  - Same set on the `folders` table (when materialized in Phase 2).
- **Mapping**: a small module translates a v1 `suggested_sharepoint_path`
  (which in v1 follows the canonical `"<Department>/<BusinessCategory>/
  <file_name>"` rule, see §6.1) into a real SharePoint drive/folder pair
  (e.g., `"Sales/Revenue/q3_revenue_report.xlsx"` → site `Sales`, drive
  `Documents`, folder `Revenue/`).
- **Upload**: a separate Microsoft Graph client (using the existing `msal`
  dependency) handles file upload, then writes back the assigned IDs and URL.
- **Sync**: a reconciliation step compares local `sha256` to the remote
  `quickXorHash` / `sha1Hash` to detect drift.

What v1 **does** do, just to keep Phase 3 painless:

- The SharePoint columns already exist as nullable. Phase 3 will fill them
  in — no migration needed.
- `suggested_sharepoint_path` is already being computed and stored, so when
  the mapping module shows up it has a consistent input format to work with.
- The metadata DB is its own file, separate from `ProjectState`. A future
  SharePoint worker can read and write it without touching journal/task data.

What this plan **does not** do:

- No `msal` token flow.
- No Graph API calls.
- No upload, no sync, no reconciliation.
- No `tenants/clients/sites` configuration shape.

## 10. Implementation tasks

The work breaks into 8 small tasks, each one landable as a single PR. We
**don't start implementing yet** — these tasks become real only after this
plan is approved and the writing-plans skill produces a per-task plan with
acceptance criteria.

| # | Task | Module | Depends on |
|---|------|--------|------------|
| T1 | Define `ClassificationRecord` pydantic v2 schema and controlled vocabularies. | `fda/metadata/schema.py` | — |
| T2 | Create `metadata.db` storage module: schema bootstrap, upsert, FTS5 triggers, audit log. | `fda/metadata/store.py` | T1 |
| T3 | Text-extraction utility per extension (.txt/.md/.csv direct; .pdf/.docx via optional libs; binary → empty). Truncates to `body_excerpt` size. Optional libraries (`pypdf`, `python-docx`, etc.) must be `try/except`-imported and gated behind capability checks; the test suite must pass with **none** of them installed (binary extractors fall back to empty body in that case). | `fda/metadata/extract.py` | — |
| T4 | Strict-output classifier component: one Claude call per file with strict JSON prompt; parses through pydantic. Includes a fake `Classifier` for tests. | `fda/metadata/classifier.py` | T1, T3 |
| T5 | Pipeline runner: discover → fingerprint → extract → call `organize_files()` (unmodified) → **resolve_outcome** (route `skipped_repo`/`deleted_junk` to audit-only) → classify → derive SP path → validate → persist (single transaction) → **record_failures** (per-file outcomes into `classifier_run_files`, run never aborts on a single bad file) → report. Matches §5 step list exactly. | `fda/metadata/pipeline.py` | T2, T3, T4 |
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

**The hard rule:** no test, no fixture, no development run is allowed to
touch the user's real folders. Every input file is hand-authored and
lives under `tests/fixtures/sample_docs/`.

### 11.1 Fixture set

A small, deliberately mixed set of about 12 files, all obviously fake (no
real names, no real companies):

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
  fake_korean_memo.md                (~600 bytes, Korean-language memo;
                                       exercises non-English language path)
  fake_meeting_notes_exec.txt        (~400 bytes, executive)
  fake_vendor_contract_draft.txt     (~800 bytes, partnership)
  fake_large_report.txt              (~50KB, exercises body_excerpt truncation)
  empty_file.txt                     (0 bytes, edge case)
  binary_blob.bin                    (random bytes, edge case)
```

None of these reference real companies, people, or systems.

**What we expect to happen for the edge fixtures:**

- **`empty_file.txt` and `binary_blob.bin`** — these still get a row in
  `documents` (the organizer doesn't delete them). `body_excerpt` is the
  empty string. `summary` is a short factual sentinel like `"Empty file."`.
  `confidence` should be low.
- **`fake_large_report.txt`** — the test confirms `body_excerpt` was
  truncated to at most the hard cap.
- **`fake_korean_memo.md`** — `language` should be `"ko"` if the detector
  is installed, `"und"` if it isn't. Both are valid; the test accepts either.

**Files we synthesize at test time** (not checked into `sample_docs/`):

A couple of edge cases are awkward to keep in a tracked fixture directory
(you don't want a `.git/` folder living inside `tests/fixtures/`). The
pytest fixture creates them inside `tmp_path` *after* copying the
sample_docs:

- **`tmp_path/fake_repo/.git/HEAD` + `tmp_path/fake_repo/code.py`** —
  exercises the organizer's "never touch git repos" guardrail. The pipeline
  must record this file with `outcome="skipped_repo"` and produce **no**
  `documents` row for it.
- **`tmp_path/.DS_Store`** — a junk filename. The organizer is allowed to
  delete it. The pipeline must record `outcome="deleted_junk"` and again
  produce no `documents` row.

Without these two synthesized files, the E2E test in §11.2 has nothing
to match its `classifier_run_files` assertions against.

### 11.2 Test cases

**Unit — schema (`test_metadata_schema.py`):**

- valid record parses; required fields enforced; enum values strict.
- invalid sha256, confidence out of range, keywords list outside 3–15 all raise.
- `keywords_concat` mismatch with `keywords` raises.
- `confidentiality_rank` derived correctly from `confidentiality`; mismatch raises.
- `current_local_path != original_local_path` while `organizer_action=="unchanged"` raises.
- `language` accepts `"en"`/`"ko"`/`"und"`; rejects `"EN"`, `"eng"`, `""`.
- Phase 2 / Phase 3 fields default to `None` and accept only `None` in v1.
- Confidentiality default: when the classifier returns `restricted` with
  `confidence=0.1`, the record validates (we do not require a special
  "uncertain" marker; confidence carries that information).

**Unit — store (`test_metadata_store.py`):**

- insert + retrieve by sha256.
- `documents_fts` is updated on insert / update / delete (trigger correctness).
- filter queries (`department=`, `confidentiality_max=`,
  `parent_dir_prefix=`, `confidence_min=`).
- `confidentiality_max="internal"` returns only rows with rank ≤ 2.
- `parent_dir_prefix="/tmp/foo"` returns rows whose `parent_dir` begins with
  exactly that prefix; case-sensitive.
- CHECK constraints: a direct `INSERT` with an out-of-range `confidence`,
  an unknown enum value, or a malformed `sha256` is rejected by SQLite
  before pydantic ever sees it.
- **Idempotency:** running the same upsert twice with identical input is a
  no-op for `documents_fts` row count and produces no duplicate rows in
  `document_keywords`.

**Unit — extractor (`test_metadata_extract.py`):**

- .txt and .md extract correctly.
- empty file returns empty body_excerpt.
- binary blob returns empty body_excerpt without raising.
- **Large file** (`fake_large_report.txt`): excerpt is truncated to ≤ the
  hard cap; the truncation does not split a UTF-8 codepoint mid-byte.
- **Optional-extractor absence:** when `pypdf` / `python-docx` are not
  installed, requesting extraction of a `.pdf` or `.docx` returns empty
  body_excerpt and does not raise.

**Unit — pipeline with fake classifier (`test_metadata_pipeline_fake.py`):**

- Substitute a `FakeClassifier` returning canned strict JSON.
- Substitute a no-op `organize_files` (returns empty `moves`) so this test
  does not require a real Claude backend.
- Asserts: every fixture file produces one `documents` row;
  `organizer_action == "unchanged"` for all of them; SHA-256 stable; FTS
  query for a known keyword returns the expected row(s).
- **Idempotency on re-run:** running the pipeline twice over the same
  fixture dir yields the same row count, the same sha256 keys, and an
  unchanged `documents_fts` size. `classifier_runs` gains one row per run.
- **Per-file failure isolation:** point one file at a `FakeClassifier` that
  raises; assert the run finishes with status `partial`, the failing file
  has a `classifier_run_files` row with `outcome="classify_failed"`, and
  every other file is classified normally.

**Unit — language detection (`test_metadata_language.py`):**

- English fixture → `language="en"`.
- Korean fixture → `language="ko"` (or `"und"` if detector missing).
- Detector raising on input does not abort the file; record validates with
  `language="und"`.

**End-to-end (`test_metadata_pipeline_e2e.py`):**

- Uses the existing test pattern of mocking the Claude backend via
  `get_claude_backend()` (per `CLAUDE.md`). A real backend is never used in
  the default test run; an opt-in env flag may enable it for local manual
  verification only.
- Runs the full pipeline against `sample_docs/` copied into a `tmp_path`.
- `FDA_METADATA_DB` is set to a `tmp_path`-scoped DB by fixture; the user's
  real metadata DB is never touched.
- Asserts:
  - **All required Phase 1 fields** populated for every classified file
    (the per-row column set in §6.1, excluding Phase 2/3 reservations).
  - Controlled-vocabulary fields all valid.
  - `suggested_sharepoint_path` is a clean POSIX relative path.
  - FTS query `"revenue"` matches the finance fixture.
  - FTS query `"error rate"` matches the postmortem fixture.
  - `classifier_runs` has one row with non-zero `files_classified` and
    status `ok` or `partial`.
  - `classifier_run_files` has exactly one row per fixture file (including
    the empty / binary / repo-skipped / junk-deleted cases, with the
    appropriate `outcome` value).
  - **No file was moved outside `tmp_path`.**
  - Phase 2 and Phase 3 columns are all `NULL`.

**Concurrency (`test_metadata_concurrency.py`):**

- Two pipeline runs against the same `tmp_path` DB, started concurrently
  with different sha256 input sets, both complete without raising and
  produce the union of rows. SQLite WAL mode is used for the test DB.
- Two writers attempting to upsert the same sha256 simultaneously
  serialize cleanly (the second wins; row count is 1).

**Search-recall (`test_metadata_search.py`):**

- Filter-only queries return the right rows.
- FTS5 queries with `AND`, `OR`, phrase, and prefix all work.
- `confidence_min=0.7` filter excludes lower-confidence rows.
- `confidentiality_max="internal"` excludes confidential / restricted rows.
- `parent_dir_prefix="/tmp/foo/Bar"` matches `/tmp/foo/Bar/...` rows but
  **not** `/tmp/foo/bar/...` rows (verifies GLOB case-sensitivity).
- `suggested_sharepoint_path` for every classified fixture matches the
  canonical pattern `^[A-Z][A-Za-z]+/[A-Z][A-Za-z]+/[^/]+$`
  (Title-Cased department / business-category / file_name; no extra path
  segments). This locks the v1 mapping rule in §6.1 so the future Phase 3
  mapping module has a stable input format.

**Integrity:**

- Pre-commit hook (`/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`) must pass with the new tests added. The 113 existing tests must remain green.

### 11.3 What testing does **not** do

- No real local user directory is scanned.
- No real `organize_files` run is performed against the user's home folder
  in tests; all runs are scoped to `tmp_path`.
- No SharePoint, no Microsoft Graph, no network calls.
- No KakaoTalk parsing or message classification.

## 12. Open questions deferred to writing-plans

These are intentionally **not** decided in this plan. They belong in the
per-task plan, where we'll have the fixtures in front of us:

- **Which PDF / Word extractor?** `pypdf` vs `pdfminer.six`, with or
  without `python-docx`. Best decided by trying each one against the
  fixture set and seeing which produces the cleanest text.
- **The exact strict-classifier prompt.** Prompt wording is much easier
  to iterate on once we have real classifier outputs to compare against
  expected results — that's a T4 concern.
- **CLI flag shape and output format** (table vs JSON) — small details
  that get worked out in T7.
- **Should metadata search be exposed via the MCP server**
  (`fda/mcp_server.py`) as an additional tool? Probably yes, but that's
  a follow-on task once Phase 1 has shipped.

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
- **D8. Confidentiality fails closed.** No `unknown` value exists for
  `confidentiality`. When the classifier is uncertain it must default to
  `restricted` (the most-restrictive label) and reflect uncertainty in
  `confidence`. Mistakenly labeling a public file `restricted` is
  recoverable; the inverse is not.
- **D9. Schema carries both pre- and post-organizer paths.** Each record
  stores `original_local_path` (pre-move) and `current_local_path`
  (post-move) plus an explicit `organizer_action` enum. Files that the
  organizer skips (inside git repos) or deletes (junk) get a row in
  `classifier_run_files` only — never in `documents`.
- **D10. FTS5 is external-content** (`content='documents'`) and excludes
  `reason`. Triggers fire only on `documents`; `keywords_concat` is
  denormalized onto `documents` and written atomically with
  `document_keywords` so keyword changes always reach FTS.
- **D11. Confidentiality is comparable via rank.** A denormalized
  `confidentiality_rank ∈ {1..4}` is stored alongside the string label so
  `confidentiality_max` filters use integer comparison, not lexicographic.
- **D12. Per-file failures are first-class.** A `classifier_run_files`
  table records the outcome of every file the pipeline touched
  (`classified | skipped_repo | deleted_junk | extract_failed |
  classify_failed | validate_failed`). Single-file failures do not abort
  a run; the run reports `partial` status.
