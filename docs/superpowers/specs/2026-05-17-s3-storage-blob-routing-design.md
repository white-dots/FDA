# S3 Storage-Blob Routing — Design Spec

**Date:** 2026-05-17
**Status:** Draft (awaiting Codex review + user review)
**Scope:** Small, self-contained code change to the organize pipeline. The
end-to-end corpus test that validates it lives in a *separate* spec
(`docs/superpowers/specs/2026-05-16-bilingual-pipeline-test-design.md`,
to be revised after this ships).

---

## 1. Problem

The cloud router has three destinations: SharePoint, S3, RDBMS. In practice
the S3 branch almost never fires. Root cause:

- The files that conceptually belong in S3 are *storage blobs* — video,
  images, audio, archives, database backups/dumps.
- FDA has no text extractor for any of these. The reader tags them as
  unreadable (`extract_status == "no_extractor"`). The classifier is
  called with the full catalog but internally restricts itself to
  *extractable* entries — `real = [e for e in catalog.entries if not
  e.is_junk]; extractable = [e for e in real if quarantine_bucket(e) is
  None]` (`fda/organize/classifier.py:605-606`) — so it never produces a
  grouping for an unreadable file. The orchestrator partition then sends
  those files down the quarantine MOVE path: see
  `fda/organize/__init__.py:159-179` (partition, *after* the
  `classifier.classify(...)` call at `__init__.py:153-158`) and
  `fda/organize/plan_builder.py:248-296` (quarantine MOVEs).
- `route()` will route any `Groupings` it is handed (it iterates
  `groupings.items`, `fda/organize/router.py:360`), but the current
  caller passes only the classifier's groupings
  (`fda/organize/__init__.py:260-268`). Quarantine groups are passed via
  the separate `plan`/`_build_quarantine_groups` path
  (`router.py:291-331`) and reported with *no* cloud destination.

Net effect: every file that *should* go to S3 is removed from the routing
path one stage before the router can see it. S3 is starved by design.

A second, smaller fact confirms a naive fix won't work: the existing
`all_extraction_failed` short-circuit (`router.py:61-63, 92-93`) checks
`e.summary_failed`, but quarantine entries are built with
`summary_failed=False` (`fda/organize/reader.py:85`). So even if storage
blobs reached the router, that rule would not catch them. A dedicated rule
is required.

## 2. Goal

Files of known storage-blob type are:

1. sorted into **three real, named folders** (not the quarantine pile), and
2. given a **deterministic S3 destination** in the routing report (no LLM
   call, marked high-confidence).

Non-goal / honesty boundary: an unreadable file that is **not** a known
storage-blob type (e.g. a corrupt `.docx`, an unknown `.xyz` binary) keeps
its current behavior — it goes to quarantine for a human to inspect. We
only auto-route to S3 the types we are certain about. No fabricated
content reasons.

## 3. The three buckets

Coarse on purpose (one known problem is over-fragmentation — too many tiny
folders). All three land in the same place (S3), so finer splitting would
add folders without changing destinations.

| Stable id (English) | On-disk folder (subpath) | Extensions |
|---|---|---|
| `StorageBlobMedia` | `미디어_Media` | `.mp4 .mov .avi .mkv .png .jpg .jpeg .gif .webp .mp3 .wav .m4a .flac` |
| `StorageBlobArchive` | `압축파일_Archives` | `.zip .rar .7z .tar .gz .tgz` |
| `StorageBlobBackup` | `백업_Backups` | `.bak .sql .dump .dmp` |

Notes:
- `CatalogEntry.ext` is the lowercased final suffix. Python sees
  `archive.tar.gz` as `.gz`, so `.gz`/`.tgz` cover gzipped tarballs; no
  multi-suffix parsing is added (YAGNI).
- Stable English id vs Korean surface folder mirrors the existing
  convention (identity fields stay stable, Korean is the surface name).
- These exact extension sets are the v1 list. Adding/removing an
  extension later is a one-line edit in `storage_blobs.py`.

## 4. Architecture

The pipeline already partitions catalog entries into independent streams
before the classifier: **normal** (LLM-sorted), **junk** (deleted),
**quarantine** (unreadable → `_NoExtractor`/`_ExtractionFailed`). This
change adds a fourth stream, **storage blobs**, carved out of what would
otherwise be quarantine.

```
reader.read()  ──>  catalog.entries
                       │
   ┌───────────────────┼────────────────────────────┐
   │                   │                             │
 junk            quarantine                     storage blobs   <-- NEW
 (DELETE)        (_NoExtractor/…)               (3 real folders)
   │                   │                             │
   │            (no cloud dest)              real Groupings  ──┐
   │                                                           │
 normal (extractable) ── classifier (LLM) ── Groupings ────────┤
                                                               ▼
                                           plan_builder ── executor
                                                               │
                                                            router
                                       storage-blob groups ──> S3
                                       (deterministic, high-confidence)
```

Key decision: storage blobs travel as **real `Grouping` objects merged
into the classifier's `Groupings`**, *not* as a second quarantine-style
path. Rationale: the router only assigns cloud destinations to
`groupings.items`; anything routed through the quarantine path
(`_build_quarantine_groups`) is reported with no destination by design.
To reach S3, storage blobs must be ordinary sorted groups. They are built
deterministically (no LLM call) because the files have no extractable
text.

### Units

**`fda/organize/storage_blobs.py` (new, single responsibility: the
storage-blob taxonomy + grouping)**

- Module-level bucket spec: ordered tuple of
  `(category_id: str, subpath: str, exts: frozenset[str])` for the three
  buckets in §3.
- `STORAGE_BLOB_CATEGORY_NAMES: frozenset[str]` — the three stable ids.
  Single source of truth, imported by the router.
- `storage_blob_bucket(entry: CatalogEntry) -> tuple[str, str] | None`
  Returns `(category_id, subpath)` when **both**:
  `quarantine_bucket(entry) is not None` (i.e. the file is unreadable and
  would otherwise be quarantined — this guarantees we never steal a file
  the LLM classifier could read) **and** `entry.ext` is in one bucket's
  extension set. Otherwise `None`. Junk entries return `None` (they have
  `quarantine_bucket == None`).
- `build_groupings(entries: Sequence[CatalogEntry]) -> list[Grouping]`
  Groups the passed entries by bucket; emits one `Grouping` per **non-empty**
  bucket with `category=category_id`, `subpath=subpath`,
  `file_ids=tuple(sorted path_ids)`, and a fixed reason string
  (e.g. `"미디어/압축/백업 — 저장소(S3) 대상 파일 유형"`). Deterministic
  ordering (bucket order from the spec tuple; path_ids sorted) so plans
  are reproducible.

**Import constraint (hard requirement):** `storage_blobs.py` may import
**only** from `fda.organize.models` (which has no intra-package imports,
`fda/organize/models.py:11-14`). It must never import from
`fda.organize.__init__`, `router`, or `classifier` — `__init__.py:16-18`
already imports `router`, and `router` will import `storage_blobs`, so any
back-import from `storage_blobs` would create a cycle.

**Public surface (YAGNI guard):** the module exposes exactly four names —
the bucket spec tuple, `STORAGE_BLOB_CATEGORY_NAMES`,
`storage_blob_bucket`, `build_groupings`. Nothing else (no config hooks,
no exported helpers).

**Load-bearing invariant:** because the classifier already excludes
quarantine entries (`classifier.py:605-606`), storage-blob `path_id`s
never appear in classifier groupings. The merged synthetic groupings are
therefore disjoint from classifier output, so plan_builder's
"path_id appears in two groupings" check (`plan_builder.py:181-186`)
cannot trip from this change.

**`fda/organize/__init__.py` (modified: wire in the fourth stream)**

Change the partition block (`__init__.py:159-179`). After
`classifier.classify(...)` returns `groupings`:

1. `storage_blob_entries = [e for e in catalog.entries if not e.is_junk and
   storage_blobs.storage_blob_bucket(e) is not None]`
2. `quarantine_entries` excludes storage-blob entries:
   `[e for e in catalog.entries if quarantine_bucket(e) is not None and
   storage_blobs.storage_blob_bucket(e) is None]`
   (Storage blobs have a quarantine bucket today; this removes them from
   the quarantine MOVE path so they are not duplicated.)
3. Build `sb_groupings = storage_blobs.build_groupings(storage_blob_entries)`.
4. Merge into the classifier output (both `Grouping` and `Groupings` are
   frozen — construct a new `Groupings`), appending storage-blob groups
   **after** the classifier groups:
   `groupings = Groupings(items=groupings.items + tuple(sb_groupings),
   overall_reason=groupings.overall_reason)`.
   Order rationale: plan_builder sorts its own operations internally
   (`plan_builder.py:222-226, 298-308`), so on-disk results are
   order-independent; but `router.route()` and the routing report follow
   `groupings.items` order directly (`router.py:360, 453-458`). Appending
   last makes the report list content categories first, then the
   storage-blob groups — stable and deterministic.
5. **Hard requirement — extend `path_by_id`** to include storage-blob
   entries so `plan_builder.build` can resolve their MOVE sources:
   `path_by_id = {e.path_id: e.path for e in extractable + storage_blob_entries}`.
   This is not optional: a `Grouping` whose `file_ids` are absent from
   `path_by_id` makes `plan_builder.build` raise `PlanBuilderError`
   ("unknown path_id", `plan_builder.py:179-180`) and the whole organize
   run fails.

`junk_paths` is unchanged. `plan_builder.build(...)` signature is
unchanged — the new groups flow through the existing groupings MOVE path
(`plan_builder.py:164-246`) and produce `미디어_Media/`, `압축파일_Archives/`,
`백업_Backups/` exactly like any other sorted category.

**`fda/organize/router.py` (modified: one deterministic S3 rule)**

- Import `STORAGE_BLOB_CATEGORY_NAMES` from `fda.organize.storage_blobs`.
- `_short_circuit` (`router.py:83-94`): add the storage-blob check **as
  the first branch**, before the `Misc` and `all_extraction_failed`
  checks: `if category_name in STORAGE_BLOB_CATEGORY_NAMES: return "s3"`.
- `_short_circuit_reason` (`router.py:97-103`): add the matching branch in
  **the same first position**. The two functions must stay in lockstep —
  same branch order — or a category could short-circuit on one rule but
  report another rule's reason. (In practice a storage-blob group cannot
  also be `all_extraction_failed`: that signal is
  `all(e.summary_failed)`, `router.py:61-63`, and quarantine-origin
  entries have `summary_failed=False`, `reader.py:85`. The lockstep
  ordering is required regardless, as defensive correctness.) The reason
  string is cosmetic — it appears only in logs and the routing report,
  not in any decision logic — so its exact Korean wording (e.g.
  `"저장소 전용 파일 유형(미디어/압축/백업) — 규칙에 따라 S3로 라우팅."`) is
  for consistency, not a correctness requirement.
- Confidence: the short-circuit branch in `route()` currently hard-codes
  `low_confidence=True` (`router.py:371-379`). Storage-blob routing is a
  firm deterministic rule, not a guess, so it must be
  `low_confidence=False`. Change that branch to compute
  `low_confidence = g.category not in STORAGE_BLOB_CATEGORY_NAMES`. This
  flips **only** the three storage-blob categories to high-confidence;
  `Misc` and `all_extraction_failed` short-circuits stay
  `low_confidence=True` exactly as today. Do **not** generalize this to
  "all S3 short-circuits are high-confidence" — the scoping is on the
  category id, not on the destination.

No change to `_aggregate_signals`, `_build_quarantine_groups`, the routing
report schema (`models.py:202-209`), or the MD/JSON writers — storage-blob
groups are ordinary `RoutedCategory` rows with `destination="s3"`.

## 5. Data flow (one storage-blob file, `clip.mp4`)

1. `reader.read()` → `CatalogEntry(ext=".mp4", extract_status="no_extractor",
   summary_failed=False, is_junk=False)`.
2. `__init__.py`: `storage_blob_bucket` → `("StorageBlobMedia",
   "미디어_Media")`. Entry goes to `storage_blob_entries`, removed from
   `quarantine_entries`, added to `path_by_id`.
3. `build_groupings` → `Grouping(category="StorageBlobMedia",
   subpath="미디어_Media", file_ids=("f0NN",), reason=...)`, merged into
   `groupings.items`.
4. `plan_builder.build` → MOVE `clip.mp4` → `<target>/미디어_Media/clip.mp4`
   (+ CREATE_DIR), via the normal groupings path.
5. `executor` moves the file; `verifier` checks it.
6. `router.route`: for that grouping, `_short_circuit("StorageBlobMedia",
   signals)` → `"s3"`; `low_confidence=False`. Emitted as a
   `RoutedCategory` with `destination="s3"` in `routing-report.json` /
   `routing-report.md` under the normal "카테고리별 라우팅" section.

## 6. Error handling / edge cases

- **Storage-blob file also flagged junk**: junk wins, defensively.
  Junk is detected by exact filename only (`is_junk_file` → name in
  `{.DS_Store, Thumbs.db, desktop.ini}`, `_fs.py:69-70`,
  `reader.py:241`); size is **not** a junk signal, so a 0-byte `.zip`
  is *not* junk — it is `no_extractor` and routes to S3 as
  `StorageBlobArchive` (deterministic and honest: it genuinely is a
  `.zip`). Because no storage-blob extension can equal a junk filename,
  this case cannot arise in practice today. The guard is still correct
  for defense in depth: `quarantine_bucket` returns `None` for junk
  (`models.py:179-180`), so `storage_blob_bucket` returns `None` and a
  file would follow the DELETE path if junk detection ever changed.
  Unchanged.
- **Unreadable but not a storage type** (corrupt `.docx` →
  `extract_status="failed"`; unknown `.xyz` → `"no_extractor"`):
  `entry.ext` not in any bucket set → `storage_blob_bucket` returns
  `None` → stays in `quarantine_entries` → quarantine pile, as today.
- **Readable file that happens to share an extension**: not possible for
  the v1 sets (none of them have an FDA extractor). The
  `quarantine_bucket(entry) is not None` guard is still applied so the
  rule can never divert a file the classifier could read, even if the
  extractor table changes later.
- **Empty bucket**: `build_groupings` emits no `Grouping` for it
  (plan_builder rejects empty `file_ids`, `plan_builder.py:165-169`).
- **No storage blobs in a run**: `storage_blob_entries == []`,
  `sb_groupings == []`, `groupings`/`path_by_id`/`quarantine_entries`
  identical to today. Zero behavior change on corpora without blobs.
- **Stage 5 (metadata)**: unchanged. Storage blobs still have no
  extractable text; the metadata stage records them exactly as it
  records any other no-text file today. Out of scope.

## 7. Testing strategy

Unit-testable per unit (a sign the boundaries are right):

- `storage_blobs.storage_blob_bucket`: media/archive/backup ext → correct
  bucket; non-blob unreadable ext → `None`; junk → `None`; readable ext
  → `None`.
- `storage_blobs.build_groupings`: mixed entries → one group per non-empty
  bucket, correct ids/subpaths, deterministic order; empty input → `[]`.
- `router._short_circuit`: a `StorageBlob*` id → `"s3"`; unchanged for
  `"Misc"` and `all_extraction_failed`.
- `router.route` integration: a storage-blob grouping yields a
  `RoutedCategory(destination="s3", low_confidence=False)`.
- `organize()` end-to-end (tmp dir, mocked backend): a `.mp4` + a `.zip`
  land in `미디어_Media/` / `압축파일_Archives/`; a corrupt non-blob file
  still lands in quarantine; `routing-report.json` shows those two groups
  with `destination: "s3"` and `low_confidence: false`.
- Korean-named subpath passes plan_builder's sanitization unchanged:
  assert the synthetic subpaths (`미디어_Media`, `압축파일_Archives`,
  `백업_Backups`) survive `_sanitize_subpath` / `_resolve_destination_dir`
  (`plan_builder.py:58-84`) and resolve under target (no `..`, no illegal
  chars, non-empty components) — i.e. the produced MOVE destinations are
  exactly `<target>/<subpath>/<basename>`.

Full suite must pass: `.venv/bin/python -m pytest tests/ -x -q
--tb=short`.

## 8. Out of scope

- The bilingual end-to-end corpus test (separate spec, revised next).
- Any change to quarantine behavior for non-storage unreadable files.
- Multi-suffix extension parsing (`.tar.gz` handled via `.gz`).
- Metadata-stage handling of blobs.
- Configurable / user-overridable extension lists.
