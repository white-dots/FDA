---
status: DRAFT — 2026-05-12 (awaiting review)
topic: Local metadata layer (stage 5 of `fda organize`)
scope: Per-file metadata extraction, LLM-classified labels, SQLite + FTS5 search index. No SharePoint upload, no embeddings, no web UI.
---

# Local Metadata Layer Design

After the organize pipeline produces a tree of category folders on the user's
local disk (stages 1–4) and the cloud-router writes its sidecar reports
(stage 4.5), stage 5 builds a per-file metadata index so the user — and
later, an AI agent — can search the corpus. The output is a single SQLite
database at `~/.fda/metadata.db` plus a per-run audit sidecar.

**No files are uploaded, moved, or rewritten by this stage.** The metadata
layer reads the organized tree and writes only to `~/.fda/`.

## Why a separate stage

Stage 4 (router) decides *where* a category should go in the cloud; stage 5
decides *what* each file is, *who owns it*, *how sensitive it is*, and *what
it's about*. Those two questions are different:

- The router operates per-category, with one Claude call per ~20 files
  sampled. Coarse, fast, structural.
- The metadata layer operates per-file, batched ~10 files per call but
  producing per-file records. Fine-grained, slower, semantic.

Splitting them means router can ship and corpus-eyeball without waiting for
metadata, and metadata can re-run independently when business context
changes — no re-routing required.

## The decision the layer makes per file

For every file FDA touches, persist one row with:

| Field | Type | Source | Example |
|---|---|---|---|
| `path_id` | text PK | reader (stable) | `f042` |
| `sha256` | text | enrich (deterministic) | `a1b2…` |
| `path` | text | reader | `/Users/.../Organized/Finance/Invoices/2025_Q3.pdf` |
| `mime` | text | enrich | `application/pdf` |
| `size_bytes` | int | enrich | 1_234_567 |
| `mtime` | text (ISO 8601) | enrich | `2026-05-12T08:30:00Z` |
| `language` | text | enrich (heuristic) | `ko` / `en` |
| `department` | text | classifier (LLM) | `finance` |
| `document_type` | text | classifier (LLM) | `invoice` |
| `confidentiality` | text | classifier (LLM) | `confidential` |
| `summary` | text | reader (existing) | Source-language one-paragraph summary |
| `keywords` | json | classifier (LLM) | `{"ko": ["청구서","2025년"], "en": ["invoice","2025"]}` |
| `confidence` | real | classifier (LLM) | `0.82` |
| `extract_status` | text | reader (existing) | `ok` / `failed` / `tool_missing` / `no_extractor` |
| `sharepoint_url` | text NULL | Phase 3 | (NULL in Phase 1) |
| `run_id` | text FK | classifier | `2026-05-12T14:30:00Z-abc` |
| `created_at` | text | store | ISO 8601 |
| `updated_at` | text | store | ISO 8601 |

Every field except `sharepoint_url` is filled in Phase 1.

### Vocabulary (English codes in DB, Korean labels in UI)

The DB stores stable English codes. A separate `fda/metadata/vocab.py`
maps each code to a Korean display label. UI surfaces (CLI search output,
later web app) translate at render time.

- `department`: `sales`, `finance`, `hr`, `production`, `rd`, `legal`,
  `operations`, `marketing`, `executive`, `unknown`
- `document_type`: `invoice`, `contract`, `report`, `proposal`, `memo`,
  `policy`, `presentation`, `spreadsheet`, `image`, `data`, `archive`,
  `correspondence`, `unknown`
- `confidentiality`: `public`, `internal`, `confidential`, `restricted`

These are the *initial canonical* vocabularies. The lists are
version-controlled in `vocab.py`; the classifier SKILL.md inlines them
so the model can only emit known codes. Unknown values surface as
`unknown` (department / document_type) or `restricted`
(confidentiality — fail-closed, see below).

**Company-specific Korean names do not require a code change.** Each
company's actual department names (`영업기획부`, `생산관리1팀`, etc.)
live in `~/.fda/business_context.md`, which maps them to the canonical
English codes the model emits:

```markdown
## Departments
- 영업기획부 (sales) — 매출 기획, 제안서
- 생산관리1팀 (production) — 1라인 제조 일정
- 인사총무팀 (hr) — 채용, 인사 평가, 급여
```

The classifier reads this map at the start of each run and uses it to
canonicalize messy real-world names into the stable `vocab.py` codes
when writing the DB row. A new customer with new department names
edits their own `business_context.md` and re-runs `fda metadata` —
**no FDA release required**. Editing `vocab.py` is reserved for adding
a genuinely new *canonical concept* (e.g., introducing a new top-level
department category company-wide), not for accommodating a customer's
local naming.

## The fail-closed rule for confidentiality

When the classifier is uncertain about a file's confidentiality, the
stored row must be `restricted`. Never `internal` or `public` on a
hunch. The user is better served by an over-classified file (extra
friction when sharing) than by an under-classified file (a leak).

**Mechanism: deterministic post-process override, not a model-side
validator.** Earlier drafts of this spec used a Pydantic cross-field
validator that rejected responses with `confidence < 0.3` and
`confidentiality != 'restricted'`, expecting the model to "raise its
confidence or fall back to restricted." That formulation taught the
model how to game the rule (return `confidence = 0.31` and any
confidentiality it liked); the rule belongs outside the model's
output, not inside it.

The implementation:

1. The SKILL.md asks for an honest `confidence` and the
    confidentiality the model genuinely thinks fits.
2. After Pydantic validates structure (vocab compliance, ranges,
    no-unknown-keys — but no cross-field confidence rule), the
    classifier runs one deterministic post-processing pass:
    ```python
    if record.confidence < 0.3 and record.confidentiality != "restricted":
        record = record.model_copy(update={
            "confidentiality": "restricted",
            "fail_closed_override": True,
        })
        logger.log("METADATA_FAIL_CLOSED", path_id=..., reason="confidence<0.3")
    ```
3. `documents.fail_closed_override` (boolean column, see *Storage*)
    records that the override fired, so the user can find these rows
    later via `fda search --fail-closed-only`.

This way:

- The model cannot avoid the rule by inflating confidence — the
  override is deterministic and runs on the validated record.
- The override never triggers a retry; one Sonnet call per batch
  remains the steady state.
- The audit trail records *why* a row is `restricted` (model judgment
  vs. fail-closed override).

## Korean-first surface

[[project_fda_korean_first]] applies throughout:

- `summary` is stored in the file's **source language**. A Korean PDF
  produces a Korean summary; an English PDF an English one. We do not
  translate.
- `keywords` is a JSON object with `ko` and `en` arrays, each 3–8 items.
  The classifier emits both. A purely English file gets `"ko": []`; a
  purely Korean file gets the English array via machine translation by
  the same Sonnet call (cost is negligible at batch size 10).
- The FTS5 virtual table uses the **trigram tokenizer**, not the default
  unicode61. Trigrams handle Korean particles (`매출은`/`매출을`/`매출이`
  all match a `매출` query) without per-language stemming. The same
  tokenizer works for English (worse stemming than Porter, but acceptable
  in Phase 1; Phase 2 can revisit).
- CLI output uses Korean labels by default. `--english` flips to codes
  for scripting.

## Business context injection

External config file at `~/.fda/business_context.md`. The repo ships
`docs/business_context_guide.md` (how to write one) and
`docs/business_context.example.md` (starter template). The Obsidian vault
note at `00_Me/02_Side_Hustle/Lion_Chemtech/FDA/Business Context Guide.md`
mirrors the guide for the user's reference.

Loading rules:

- The classifier reads the file at the start of each run and includes its
  contents in every batched prompt.
- Cap: **50KB** (≈ 12K Korean tokens). Larger files are truncated with a
  warning to stdout (`⚠ business_context.md is 73KB; truncated to 50KB`).
- **Missing file is OK.** Log one info line (`ℹ no business_context.md;
  using generic classification`) and continue with no company-specific
  context.
- File is **not** committed to the repo. Each company keeps its own.

This decision exists for one reason: the LLM does not know your company's
internal codes, department names, project codenames, or retention policy.
Without this hook, every classification is generic; with it, the same
catalyst-formulation PDF gets `restricted` instead of `internal`.

## CLI surface

Stage 5 runs **inside `fda organize`** by default, after routing:

```
fda organize <folder>
```

Two new flags on `fda organize`:

```
fda organize <folder> --no-metadata     # skip stage 5
fda organize <folder> --metadata-only   # skip stages 1–4 AND routing;
                                        # run stage 5 only on an
                                        # already-organized tree
```

### Flag matrix

What runs for each invocation:

| Invocation | Stages 1–4 (organize) | Stage 4.5 (router) | Stage 5 (metadata) |
|---|---|---|---|
| `fda organize <folder>` | ✓ | ✓ | ✓ |
| `fda organize <folder> --no-route` | ✓ | ✗ | ✓ |
| `fda organize <folder> --no-metadata` | ✓ | ✓ | ✗ |
| `fda organize <folder> --no-route --no-metadata` | ✓ | ✗ | ✗ |
| `fda organize <folder> --metadata-only` | ✗ | ✗ | ✓ |
| `fda metadata <folder>` | ✗ | ✗ | ✓ |
| `fda organize <folder> --metadata-only --no-route` | (rejected: redundant; CLI exits with usage error) | | |

`--metadata-only` is **mutually exclusive** with both `--no-metadata`
(obvious contradiction) and `--no-route` (router is already skipped by
`--metadata-only`'s definition). Combining either pair exits with a
usage error.

For users who want a fresh **routing pass** without re-running stages
1–4: that case is **not in Phase 1 scope**. The router operates on the
in-memory `Catalog` produced by stage 1's reader, so it cannot be
re-run standalone without re-reading the tree. If this becomes a real
need it can be added later as `fda organize <folder> --route-only`;
not promised here.

### Standalone metadata + search

```
fda metadata <folder>     # equivalent to: fda organize <folder> --metadata-only
fda search "매출 보고서"   # FTS5 search over ~/.fda/metadata.db
fda search "invoice" --department finance --confidentiality confidential
fda search "PR-2026" --limit 50 --english
```

The `fda search` subcommand is the Phase 1 surface for keyword search.
Filters are `--department`, `--document-type`, `--confidentiality`,
`--language`, `--since` (mtime), `--limit`. Output is a tabular CLI
list; `--json` switches to machine-readable.

## Storage

SQLite at `~/.fda/metadata.db`. Four tables.

### Runtime requirement: FTS5 + trigram tokenizer

The trigram tokenizer requires **SQLite ≥ 3.34.0 built with `ENABLE_FTS5`**.
This is *not* guaranteed by Python 3.12 alone — `sqlite3` binds to whatever
SQLite the CPython build linked against, which on some systems (Linux
distros, older macOS images) is older or lacks FTS5.

`fda/metadata/store.py` runs a startup probe before any DDL:

```python
def _assert_fts5_trigram(conn):
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.__probe USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE temp.__probe")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "FDA metadata layer requires SQLite ≥ 3.34 with FTS5 + trigram "
            "tokenizer. Current sqlite3 reports: "
            f"{sqlite3.sqlite_version}. Install `pysqlite3-binary` or rebuild "
            "Python against a newer SQLite. Original error: " + str(e)
        ) from e
```

The probe fires on first connect (after `init_schema`) and on every
`run()`. If it fails, the run aborts with an actionable error *before*
any organize stage starts modifying disk (when invoked via `fda
organize`, the metadata stage refuses to begin and the organize run
short-circuits with a clear message; stages 1–4 still complete).

### `documents` — one row per content hash

The canonical content/classification row. One row per `sha256`. Paths
live in a separate table (see `document_paths` below) so duplicate
copies of the same file remain searchable.

```sql
CREATE TABLE documents (
  sha256 TEXT PRIMARY KEY,
  mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  language TEXT NOT NULL,
  department TEXT NOT NULL,
  document_type TEXT NOT NULL,
  confidentiality TEXT NOT NULL,
  summary TEXT NOT NULL,
  keywords TEXT NOT NULL,         -- JSON: {"ko": [...], "en": [...]}
  confidence REAL NOT NULL,
  fail_closed_override INTEGER NOT NULL DEFAULT 0,
                                  -- 1 iff post-process forced confidentiality
                                  -- to 'restricted' because confidence < 0.3
  extract_status TEXT NOT NULL,
  sharepoint_url TEXT,            -- NULL until Phase 3
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX idx_documents_department ON documents(department);
CREATE INDEX idx_documents_doctype ON documents(document_type);
CREATE INDEX idx_documents_conf ON documents(confidentiality);
CREATE INDEX idx_documents_language ON documents(language);
```

### `document_paths` — one row per on-disk location

A single file may exist at multiple locations (an original plus copies,
or the same content in two organized categories). Each location gets a
row; all share one `sha256` back to `documents`.

```sql
CREATE TABLE document_paths (
  path TEXT PRIMARY KEY,          -- on-disk absolute path
  sha256 TEXT NOT NULL,
  path_id TEXT NOT NULL,          -- reader's per-run ID; informational,
                                  -- NOT globally unique (different runs
                                  -- over different trees may both produce
                                  -- 'f000'). Keep for audit/debug only.
  mtime TEXT NOT NULL,
  last_seen_run TEXT NOT NULL,
  FOREIGN KEY (sha256) REFERENCES documents(sha256) ON DELETE CASCADE,
  FOREIGN KEY (last_seen_run) REFERENCES runs(run_id)
);

CREATE INDEX idx_document_paths_sha256 ON document_paths(sha256);
CREATE INDEX idx_document_paths_mtime ON document_paths(mtime);
```

**Why `path` (not `path_id`) is the primary key:** `CatalogEntry.path_id`
is reader-assigned per organize run (`f000`, `f001`, …) and resets for
every new tree. Two `fda metadata` runs over different trees would both
emit `f000`, so making `path_id` the PK would collide. Paths, in
contrast, are globally unique on disk and durable across runs — exactly
what a long-lived index needs. `path_id` stays in the row for audit
purposes (so an audit sidecar can reference reader IDs) but is not
constrained unique.

Search returns one result per `(sha256, path)` pair; the user sees every
location the matching content lives at. Rows whose path no longer exists
on disk after a re-run are pruned at the end of `run()` (only paths
under the just-scanned `target_root` are pruned; paths under other roots
are left alone).

### `documents_fts` — keyword index

FTS5 virtual table over `summary` and `keywords` text, joined by
`rowid` to `documents`. External-content FTS5 does **not** auto-maintain
itself; the schema includes explicit triggers below.

```sql
CREATE VIRTUAL TABLE documents_fts USING fts5(
  summary, keywords,
  content='documents', content_rowid='rowid',
  tokenize='trigram'
);

-- Sync triggers (external-content pattern)
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, summary, keywords)
  VALUES (new.rowid, new.summary, new.keywords);
END;

CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, summary, keywords)
  VALUES ('delete', old.rowid, old.summary, old.keywords);
END;

CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, summary, keywords)
  VALUES ('delete', old.rowid, old.summary, old.keywords);
  INSERT INTO documents_fts(rowid, summary, keywords)
  VALUES (new.rowid, new.summary, new.keywords);
END;
```

For databases that contain rows before the FTS table was created (or
after a schema fix), a one-shot rebuild brings the index in sync:

```sql
INSERT INTO documents_fts(documents_fts) VALUES('rebuild');
```

`store.py` runs `rebuild` exactly once, conditionally, on schema
initialization when it detects an existing `documents` table without a
populated `documents_fts`.

The trigram tokenizer is the load-bearing Korean-first decision: it
does not require segmentation, so `매출은`, `매출을`, `매출이` all
return on a query for `매출`. English queries also work (with reduced
precision vs. Porter stemming; acceptable in Phase 1).

### `runs`

One row per `fda metadata` / `fda organize` invocation. Audit trail.

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,        -- ISO-8601 timestamp + short hash
  started_at TEXT NOT NULL,
  finished_at TEXT,
  target_root TEXT NOT NULL,
  files_seen INTEGER NOT NULL,
  files_classified INTEGER NOT NULL,
  files_failed INTEGER NOT NULL,
  batches_total INTEGER NOT NULL,
  batches_retried INTEGER NOT NULL,
  business_context_sha256 TEXT,   -- NULL if no business_context.md was loaded
  fda_version TEXT NOT NULL,
  model TEXT NOT NULL             -- e.g. "claude-sonnet-4-6"
);
```

`business_context_sha256` lets future re-classification runs detect when
the context file has changed and offer to re-run.

### Audit sidecar

Per run, write a markdown file to `~/.fda/runs/<run_id>.md`:

```markdown
# Metadata run <run_id>

Target: /Users/.../Organized
Started: 2026-05-12T14:30:00Z
Finished: 2026-05-12T14:36:12Z
Model: claude-sonnet-4-6
Business context: ~/.fda/business_context.md (sha256 a1b2…) — 47KB

## Stats
- Files seen: 412
- Classified OK: 408
- Failed (after retry): 4
- Batches: 41 total, 2 retried

## Failures
- f203 `/Users/.../scan_garbled.pdf` — classifier returned invalid JSON twice
- f311 `/Users/.../empty.docx` — extract_status=failed; row stored with restricted/unknown
- ...
```

Plain markdown. The DB is the source of truth; this file exists so the
user can read what just happened without opening sqlite.

### Concurrency / durability

- WAL mode enabled at connection time (`PRAGMA journal_mode=WAL;`).
- One process at a time. The CLI takes a `~/.fda/metadata.db.lock`
  flock; a second invocation prints `⚠ another fda metadata run is in
  progress` and exits.
- Upsert by `sha256` on conflict — re-running the stage on the same tree
  updates rows in place, bumps `updated_at`, and adds a new `run_id`.

### Phase 2 forward-compat

Phase 2 will add embeddings + text-to-SQL. The plan:

- Add `embedding BLOB` column to `documents` (nullable, no migration
  needed — `ALTER TABLE ADD COLUMN`).
- Add a separate `embeddings` table only if multiple vector spaces are
  needed; otherwise inline.
- Text-to-SQL targets `documents` directly with the filter columns above.

Phase 1 ships nothing for Phase 2 beyond not painting itself into a
corner. No premature abstractions.

## Classifier (the LLM stage)

### Skill

New skill at `fda/metadata/skills/metadata-classifier/SKILL.md`. Follows
the same shape as `taxonomy-proposer` and `destination-router`:

- Inlines the full vocabulary (department / document_type /
  confidentiality codes).
- Inlines the fail-closed rule for confidentiality.
- Inlines the Korean-first keyword instruction (`ko` and `en` arrays).
- Inlines a "you may see business context below; apply it" preamble; the
  caller substitutes the actual business_context.md content.
- Receives a batch of file summaries (≤ 10) and returns a JSON array of
  per-file classifications.

Model: `claude-sonnet-4-6`. Temperature `0.0`.

### Batching

Group files into batches of **10**. Empirically this fits comfortably
under Sonnet's context window even with a 50KB business_context.md and
each file's ~2KB summary + verbatim_head. A 200-file directory takes
~20 LLM calls in the happy path; ~2 minutes wall-clock with no
rate-limit retries.

### Retry + bisect policy

Bulk-failing 10 files because one item poisoned a JSON response is the
wrong granularity. The classifier instead bisects on retry failure so
that at most a single file is marked failed per genuinely-unrecoverable
response:

1. **First attempt** — send the full batch (≤ 10 files). On Pydantic
    success, persist all records.
2. **Retry-as-whole-batch** — on first failure, retry the same batch
    once. On Pydantic success, persist all records and bump
    `runs.batches_retried`.
3. **Bisect** — on second failure, split the batch in half and send
    each half as a fresh batch (each gets its own one retry).
4. **Single-file probe** — recurse until each failing branch is a
    single file. On a single-file probe that fails twice, mark *that
    file* as `extract_status=failed`,
    `confidentiality=restricted`, `confidence=0.0`,
    `fail_closed_override=True`, and bump `runs.files_failed`.
5. **No deeper retry.** Successful sub-batches inside a bisect tree
    persist normally; the audit sidecar records which sub-batches
    were retried and which file(s) ultimately failed.

Worst case: one truly-broken file causes ~`log2(10)` ≈ 4 extra LLM
calls (5 sub-batch boundaries). Acceptable cost on the failure path
for per-file accuracy on the happy path.

Stage 5 still must not propagate exceptions into stages 1–4. Bisect
failures stay scoped to the metadata stage.

### Per-file failure semantics

Even files the classifier could not handle still get a row, with
fail-closed defaults:

| Cause | `extract_status` | `confidentiality` | `confidence` | `fail_closed_override` |
|---|---|---|---|---|
| Source extraction failed (no usable text) | `failed` | `restricted` | `0.0` | `True` |
| Single-file probe failed twice after bisect | `failed` | `restricted` | `0.0` | `True` |
| File classified normally | `ok` | (model output) | (model output) | `False` (or `True` if post-process override fired) |

These rows are findable via `fda search --fail-closed-only` (boolean
filter on `documents.fail_closed_override`). Selective re-classification
of just failed rows (`--retry-failed`) is **out of scope** in Phase 1 —
the user can re-run `fda metadata <folder>` to retry the whole tree, and
the upsert path takes care of the rest.

### Output validation

Every classifier response goes through **Pydantic v2 strict** validation
at the boundary. Schema lives in `fda/metadata/schema.py`. Required:

- Each `department`, `document_type`, `confidentiality` must be a known
  code (string Literal types).
- `keywords.ko` and `keywords.en` are each lists of 0–8 short strings.
- `confidence ∈ [0.0, 1.0]`.
- Strict mode rejects unknown keys.

The fail-closed confidentiality rule is **not** a Pydantic validator;
it's a deterministic post-processing pass after Pydantic succeeds (see
*The fail-closed rule for confidentiality* above). This split keeps the
validator focused on structural correctness and the override on
semantic policy.

Validation failure → batch retry (then bisect, then mark single file
failed) per *Retry + bisect policy* above.

## Module layout

```
fda/metadata/
  __init__.py            # public entry: run(target, *, backend, ...) → RunReport
  schema.py              # Pydantic v2 models + SQLite DDL strings
  enrich.py              # sha256, mime, language detect, mtime (no LLM)
  classifier.py          # batched Sonnet calls, retry policy
  store.py               # connect, init, upsert, query
  search.py              # FTS5 search + filter composition
  cli.py                 # `fda metadata`, `fda search` subcommands
  vocab.py               # English code → Korean label map (and reverse)
  skills/
    metadata-classifier/SKILL.md

~/.fda/
  metadata.db            # SQLite + WAL + FTS5
  metadata.db.lock       # flock target
  business_context.md    # user-edited, NOT in repo
  runs/<run_id>.md       # per-run audit sidecar
```

The `fda organize` integration is a single hook in
`fda/organize/__init__.py`, after the existing `router.route()` call:

```python
if metadata:
    try:
        from fda.metadata import run as metadata_run
        report = metadata_run(
            target_path, catalog=catalog, backend=backend,
            logger=olog, progress_callback=progress_callback,
        )
        # Always surface counts, success or partial. Stage 5 is slow and
        # expensive — silent success on a partial run would hide bad data.
        if progress_callback:
            progress_callback(
                f"metadata: {report.files_classified}/{report.files_seen} "
                f"classified, {report.files_failed} failed"
            )
    except Exception as e:
        logger.error("metadata stage failed: %s", e, exc_info=True)
        olog.log("METADATA_FAIL_FATAL", error=str(e))
        if progress_callback:
            progress_callback(
                f"⚠ metadata stage failed: {type(e).__name__}: {e} — "
                "see log; organize stages 1–4 succeeded."
            )
```

Same defensive pattern as router (a crash in stage 5 must never
invalidate the on-disk tree stages 1–4 produced), **with the addition
that counts and any fatal error are surfaced to the user via
`progress_callback`**. Stage 5 takes ~2 minutes and ~20 LLM calls per
200 files; silently looking like success when the whole stage crashed
is a real footgun.

### Exit-code semantics

- `fda organize` exits **0** when stages 1–4 succeed, regardless of
  whether the metadata stage succeeded, failed, or was skipped. The
  organized tree is the primary deliverable.
- `fda metadata <folder>` (standalone) exits **1** when the metadata
  run crashes (raises) and **0** when it completes — even if some
  files failed classification (those failures are reflected in
  `runs.files_failed` and the audit sidecar, not in the exit code).
  A completely-crashed standalone invocation must not look like
  success to a calling script.
- `fda search` exits **0** on success, **2** on usage error, **1** on
  any other failure (DB missing, FTS5 unavailable, etc.).

CLI wiring:

- `fda/cli.py`: add `--no-metadata` and `--metadata-only` flags to
  `organize`; add `fda metadata` and `fda search` subcommands; wire
  the exit-code semantics above.
- `LocalWorkerAgent.organize_files`: forward both flags.
- `fda/metadata/__init__.run(...)`: top-level entry; returns a
  `RunReport` dataclass (`files_seen`, `files_classified`,
  `files_failed`, `batches_total`, `batches_retried`, `run_id`).
  Idempotent on re-runs.

## Validation

Two layers, mirroring the router pattern.

### Layer 1 — Unit tests (automated, every commit)

Hand-authored synthetic fixtures at `tests/fixtures/metadata_corpus/`:
small Korean + English files covering each department code, each
document_type, each confidentiality level, plus deliberate edge cases
(empty file, garbled PDF, missing `business_context.md`, oversized
`business_context.md`).

**Behavior → test matrix.** One test per row unless noted. Mocked
Claude backend (`get_claude_backend()` mock, following existing FDA
convention). Milliseconds to run. Enforced by pre-commit.

| # | Behavior | Where |
|---|---|---|
| 1 | Pydantic rejects unknown `department` code | `tests/test_metadata_schema.py` |
| 2 | Pydantic rejects unknown `document_type` code | same |
| 3 | Pydantic rejects unknown `confidentiality` code | same |
| 4 | Pydantic rejects `keywords` with > 8 items per language | same |
| 5 | Pydantic accepts a fully-valid record | same |
| 6 | Post-process override fires for `confidence < 0.3` + non-restricted | `tests/test_metadata_classifier.py` |
| 7 | Post-process override sets `fail_closed_override = True` and logs | same |
| 8 | Post-process override does **not** fire when model already returns `restricted` | same |
| 9 | FTS5 + trigram probe succeeds on a working SQLite | `tests/test_metadata_store.py` |
| 10 | FTS5 + trigram probe raises `RuntimeError` on a stubbed-old SQLite | same |
| 11 | INSERT trigger syncs row into `documents_fts` | same |
| 12 | UPDATE trigger replaces FTS row | same |
| 13 | DELETE trigger removes FTS row | same |
| 14 | `documents` + `document_paths` join: one sha256 with two paths returns two rows | same |
| 15 | Re-run on same tree: existing path keeps its `path_id`, `last_seen_run` bumps | same |
| 16 | Re-run after a file moves: old path row removed (pruned), new path row inserted | same |
| 17 | Batch retry path: first invalid JSON, second valid → succeeds, `batches_retried = 1` | `tests/test_metadata_retry.py` |
| 18 | Bisect path: 10-file batch fails twice, splits into 5+5; one half succeeds, other bisects further | same |
| 19 | Single-file probe failed twice → that file row has `extract_status=failed`, `fail_closed_override=True` | same |
| 20 | Missing `business_context.md` → run completes, info line logged, `runs.business_context_sha256` is NULL | `tests/test_metadata_context.py` |
| 21 | Oversized `business_context.md` (60KB) → truncated to 50KB, warning logged, truncated SHA stored | same |
| 22 | WAL mode enabled on connect | `tests/test_metadata_store.py` |
| 23 | Second concurrent invocation blocks on the lock, exits cleanly | `tests/test_metadata_cli.py` |
| 24 | Stage 5 crash does not propagate: `organize()` returns valid `PlanResult` | `tests/test_organize_metadata_integration.py` |
| 25 | Stage 5 partial-failure counts visible via `progress_callback` | same |
| 26 | `fda organize` exit code is 0 even when metadata stage crashes | `tests/test_metadata_cli.py` |
| 27 | `fda metadata <folder>` standalone exits 1 when run crashes | same |
| 28 | `fda metadata <folder>` standalone exits 0 when some files fail classification but run completes | same |
| 29 | `fda organize --metadata-only --no-route` exits 2 (mutually-exclusive flags) | same |
| 30 | `fda organize --metadata-only --no-metadata` exits 2 (mutually-exclusive flags) | same |
| 31 | `fda search` Korean particle match: `매출은` finds row whose keywords contain `매출` | `tests/test_metadata_search.py` |
| 32 | `fda search --english` outputs codes; default outputs Korean labels via `vocab.py` | same |
| 33 | `fda search --fail-closed-only` returns only `fail_closed_override = True` rows | same |
| 34 | Filter composition: `--department finance --confidentiality confidential` AND-combines correctly | same |

### Layer 2 — Live smoke test (manual, opt-in)

A `--live` pytest flag runs the same synthetic fixture corpus against
**real Sonnet**, asserts the structure of responses (vocab compliance,
Pydantic passes, fail-closed rule respected on the deliberately
low-confidence fixture) but does **not** assert specific department /
document_type values — those are Claude's judgment. Run once before
shipping, then on prompt edits.

### Layer 3 — Corpus eyeballing (manual, one-time, before shipping)

Run the real stage against the three existing organized trees: English
v1, Korean A, Lion Chemtech. Open `~/.fda/runs/<run_id>.md` and read it.
Spot-check 10 rows per tree with `fda search`. Tune the SKILL.md or
vocabulary if classifications are visibly wrong. No pass/fail metric;
human judgment confirms Claude's judgment is reasonable on real data.

## Out of scope (deliberately)

- **Embeddings / semantic search.** Phase 2. The schema reserves room
  (`embedding BLOB` column) but Phase 1 ships nothing related.
- **Text-to-SQL.** Phase 2. The schema is designed so a future agent can
  generate SQL against `documents` directly.
- **SharePoint upload, S3 sync, RDBMS ingest.** Phase 3.
  `documents.sharepoint_url` is reserved and NULL until then.
- **Web UI / Agentic Search.** Phase 3.
- **Cross-file relationships** (e.g., "this invoice references that
  contract"). Out of scope; would need a graph layer.
- **OCR.** Stage 1 (reader) already does what extraction it can; the
  metadata layer takes `extract_status` as given. Files that fail
  extraction get a fail-closed row.
- **Re-classifying on every `business_context.md` edit.** The user runs
  `fda metadata <folder>` explicitly when they want re-classification;
  no daemon, no file watcher.
- **Multi-user / shared DB.** `~/.fda/metadata.db` is single-user.
  Multi-user is a Phase 3+ concern that probably moves to Postgres.
- **Customer-specific canonical vocabularies.** Canonical codes
  (`sales`, `finance`, `invoice`, …) live in `vocab.py` and are stable
  across customers. Each customer's local Korean department/role names
  are mapped to those codes via their own `business_context.md` (see
  *The decision the layer makes per file → Vocabulary*). A new
  canonical code remains a code change; a new *local name* does not.
- **Schema versioning / migration framework.** Phase 1 ships v1; if
  Phase 2 needs a breaking change, that's a manual one-shot migration
  script then, not a framework now.

## Status & next steps

- **2026-05-12 — DRAFT (rev. 2, post-Codex review).** Design approved
  through brainstorm ([[project_metadata_layer_design]]). Codex review
  raised 10 issues; all 10 accepted and incorporated (FTS5 probe;
  explicit FTS5 trigger DDL; deterministic fail-closed override
  replacing the cross-field validator; `documents` + `document_paths`
  split for duplicate-path search; stdout-visible stage-5 counts +
  standalone exit codes; bisect retry policy; flag-matrix table;
  vocabulary-via-business_context clarification; behavior→test matrix
  replacing the unjustified test count; `--retry-failed` cut from
  Phase 1). Awaiting user review.
- Implementation order (finalized in the plan once spec is approved):
  schema + DDL + Pydantic models → enrich (sha256/mime/language) →
  store (SQLite open/upsert + WAL + lock) → classifier (skill +
  batching + retry) → search (FTS5 + filter composition) → CLI
  wiring (`fda metadata`, `fda search`, `--no-metadata`,
  `--metadata-only`) → organize integration hook → unit tests at every
  step → live + corpus eyeballing pass.
- Ship gate: all unit tests green, live smoke test green on the
  synthetic fixture corpus, corpus eyeballing pass on English v1 +
  Korean A + Lion Chemtech.
