# S3 Storage-Blob Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route known storage-blob file types (video/image/audio, archives, DB backups) into three real named folders and give them a deterministic, high-confidence S3 destination in the routing report, instead of silently dumping them into the quarantine pile.

**Architecture:** Add a fourth catalog-partition stream ("storage blobs") carved out of what would otherwise be quarantine. Storage blobs travel as ordinary frozen `Grouping` objects merged into the classifier's `Groupings` (no LLM call — these files have no extractable text), so the existing plan_builder → executor → router path produces real folders and the router can assign them a cloud destination. The router gets one new deterministic short-circuit rule scoped to the three storage-blob category ids.

**Tech Stack:** Python 3.12, pytest. Run the interpreter as `.venv/bin/python` (the `/Users/john/.pyenv/...` path in some legacy docs is wrong on this machine).

**Spec:** `docs/superpowers/specs/2026-05-17-s3-storage-blob-routing-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `fda/organize/storage_blobs.py` | The storage-blob taxonomy (3 buckets) + grouping. Single responsibility. Imports **only** `fda.organize.models`. | Create |
| `tests/test_organize_storage_blobs.py` | Unit tests for the new module + a sanitization safety net. | Create |
| `fda/organize/router.py` | One deterministic S3 short-circuit rule + high-confidence flag for the three storage-blob ids. | Modify |
| `tests/test_organize_router.py` | Unit + integration tests for the new router rule. | Modify (append) |
| `fda/organize/__init__.py` | Wire in the fourth partition stream and merge synthetic groupings. | Modify |
| `tests/test_organize_pipeline.py` | End-to-end test: `.mp4`/`.zip` → real folders + S3 in the report; non-blob unreadable still quarantines. | Modify (append) |

**Import constraint (hard requirement):** `storage_blobs.py` imports **only** from `fda.organize.models`. It must never import from `fda.organize.__init__`, `router`, or `classifier`. `__init__.py` imports `router`, and `router` will import `storage_blobs`, so any back-import from `storage_blobs` creates a cycle.

---

## Task 1: storage-blob taxonomy + `storage_blob_bucket`

**Files:**
- Create: `fda/organize/storage_blobs.py`
- Test: `tests/test_organize_storage_blobs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_organize_storage_blobs.py`:

```python
# tests/test_organize_storage_blobs.py
"""Tests for fda.organize.storage_blobs."""
from __future__ import annotations

import pytest

from fda.organize.models import CatalogEntry


def _entry(idx, *, ext, extract_status="no_extractor", is_junk=False):
    """Build a CatalogEntry with the minimum fields the taxonomy reads.

    Default extract_status="no_extractor" mirrors how the reader tags a
    non-junk file it has no extractor for (built via reader._quarantine_entry
    with is_junk=False, summary_failed=False).
    """
    return CatalogEntry(
        path_id=f"f{idx:03d}",
        path=f"/tmp/target/{idx:03d}{ext}",
        ext=ext,
        size_bytes=1000,
        summary="",
        type_label="",
        is_junk=is_junk,
        summary_failed=False,
        extract_status=extract_status,
    )


class TestStorageBlobBucket:
    def test_media_extensions_map_to_media_bucket(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        for ext in (".mp4", ".mov", ".png", ".jpg", ".mp3", ".flac"):
            assert storage_blob_bucket(_entry(0, ext=ext)) == (
                "StorageBlobMedia", "미디어_Media"
            ), ext

    def test_archive_extensions_map_to_archive_bucket(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        for ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"):
            assert storage_blob_bucket(_entry(0, ext=ext)) == (
                "StorageBlobArchive", "압축파일_Archives"
            ), ext

    def test_backup_extensions_map_to_backup_bucket(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        for ext in (".bak", ".sql", ".dump", ".dmp"):
            assert storage_blob_bucket(_entry(0, ext=ext)) == (
                "StorageBlobBackup", "백업_Backups"
            ), ext

    def test_non_blob_unreadable_extension_returns_none(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        assert storage_blob_bucket(_entry(0, ext=".xyz")) is None

    def test_junk_entry_returns_none(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        # Junk wins even when the extension is a storage-blob type.
        assert storage_blob_bucket(
            _entry(0, ext=".zip", is_junk=True)
        ) is None

    def test_readable_file_with_blob_extension_returns_none(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        # Load-bearing guard: never steal a file the classifier could read.
        assert storage_blob_bucket(
            _entry(0, ext=".zip", extract_status="ok")
        ) is None


class TestCategoryNames:
    def test_category_names_match_bucket_spec(self):
        from fda.organize.storage_blobs import (
            STORAGE_BLOB_BUCKETS,
            STORAGE_BLOB_CATEGORY_NAMES,
        )
        assert STORAGE_BLOB_CATEGORY_NAMES == frozenset(
            cat for cat, _, _ in STORAGE_BLOB_BUCKETS
        )
        assert STORAGE_BLOB_CATEGORY_NAMES == frozenset({
            "StorageBlobMedia", "StorageBlobArchive", "StorageBlobBackup",
        })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_organize_storage_blobs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fda.organize.storage_blobs'`

- [ ] **Step 3: Write minimal implementation**

Create `fda/organize/storage_blobs.py`:

```python
# fda/organize/storage_blobs.py
"""Storage-blob taxonomy + deterministic grouping.

A "storage blob" is a file FDA has no text extractor for but whose type we
recognize with certainty (video/image/audio, archives, DB backups). These
belong in S3. They are carved out of what would otherwise be the quarantine
pile and emitted as ordinary Grouping objects (no LLM call — there is no
text to classify).

IMPORTANT: this module imports ONLY from fda.organize.models. Importing from
fda.organize.__init__, router, or classifier would create an import cycle
(__init__ imports router, router imports this module).
"""
from __future__ import annotations

from collections.abc import Sequence

from fda.organize.models import CatalogEntry, Grouping, quarantine_bucket

# Exactly the four public names the spec permits — keeps `import *` from
# re-exporting Sequence/CatalogEntry/Grouping/quarantine_bucket.
__all__ = [
    "STORAGE_BLOB_BUCKETS",
    "STORAGE_BLOB_CATEGORY_NAMES",
    "storage_blob_bucket",
    "build_groupings",
]

# Ordered: (stable english id, on-disk subpath, extensions). Order is the
# emission order of build_groupings (deterministic plans). Editing an
# extension set is a one-line change here; nothing else needs to change.
STORAGE_BLOB_BUCKETS: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "StorageBlobMedia",
        "미디어_Media",
        frozenset({
            ".mp4", ".mov", ".avi", ".mkv",
            ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".mp3", ".wav", ".m4a", ".flac",
        }),
    ),
    (
        "StorageBlobArchive",
        "압축파일_Archives",
        frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}),
    ),
    (
        "StorageBlobBackup",
        "백업_Backups",
        frozenset({".bak", ".sql", ".dump", ".dmp"}),
    ),
)

# Single source of truth for the router's deterministic S3 rule.
STORAGE_BLOB_CATEGORY_NAMES: frozenset[str] = frozenset(
    category_id for category_id, _, _ in STORAGE_BLOB_BUCKETS
)


def storage_blob_bucket(entry: CatalogEntry) -> tuple[str, str] | None:
    """Return (category_id, subpath) for a storage-blob entry, else None.

    Both conditions must hold:
      1. quarantine_bucket(entry) is not None — the file is unreadable and
         would otherwise be quarantined. This guarantees we never divert a
         file the classifier could read (and excludes junk, whose
         quarantine_bucket is None).
      2. entry.ext is in exactly one bucket's extension set.
    """
    if quarantine_bucket(entry) is None:
        return None
    for category_id, subpath, exts in STORAGE_BLOB_BUCKETS:
        if entry.ext in exts:
            return (category_id, subpath)
    return None


def build_groupings(entries: Sequence[CatalogEntry]) -> list[Grouping]:
    """One Grouping per non-empty bucket, deterministic order.

    Bucket order follows STORAGE_BLOB_BUCKETS; file_ids are sorted. Entries
    that are not storage blobs are skipped. Empty input → [].
    """
    by_bucket: dict[str, list[str]] = {}
    for entry in entries:
        bucket = storage_blob_bucket(entry)
        if bucket is None:
            continue
        category_id, _ = bucket
        by_bucket.setdefault(category_id, []).append(entry.path_id)

    groups: list[Grouping] = []
    for category_id, subpath, _ in STORAGE_BLOB_BUCKETS:
        path_ids = by_bucket.get(category_id)
        if not path_ids:
            continue
        groups.append(Grouping(
            category=category_id,
            subpath=subpath,
            file_ids=tuple(sorted(path_ids)),
            reason="미디어/압축/백업 — 저장소(S3) 대상 파일 유형",
        ))
    return groups
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_organize_storage_blobs.py -q`
Expected: PASS (7 tests — 6 in `TestStorageBlobBucket`, 1 in `TestCategoryNames`)

- [ ] **Step 5: Commit**

```bash
git add fda/organize/storage_blobs.py tests/test_organize_storage_blobs.py
git commit -m "feat(organize): storage-blob taxonomy + storage_blob_bucket

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `build_groupings`

**Files:**
- Modify: `fda/organize/storage_blobs.py` (already contains `build_groupings` from Task 1 — Task 2 only adds its tests)
- Test: `tests/test_organize_storage_blobs.py:append`

> Note: `build_groupings` was written in Task 1's implementation step so the module is internally consistent. This task adds the dedicated tests that lock its behavior. If you are doing strict TDD and prefer `build_groupings` to be driven by its own failing test, you may instead delete the `build_groupings` body in Task 1 Step 3 and add it here at Step 3 — but the recommended path is to keep Task 1's complete module and just add tests here.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_storage_blobs.py`:

```python
class TestBuildGroupings:
    def test_empty_input_returns_empty_list(self):
        from fda.organize.storage_blobs import build_groupings
        assert build_groupings([]) == []

    def test_mixed_entries_one_group_per_nonempty_bucket(self):
        from fda.organize.storage_blobs import build_groupings
        entries = [
            _entry(2, ext=".mp4"),
            _entry(0, ext=".zip"),
            _entry(1, ext=".png"),
            _entry(3, ext=".xyz"),        # non-blob → skipped
            _entry(4, ext=".zip", is_junk=True),  # junk → skipped
        ]
        groups = build_groupings(entries)
        # StorageBlobBackup has no files → no group for it.
        assert [(g.category, g.subpath) for g in groups] == [
            ("StorageBlobMedia", "미디어_Media"),
            ("StorageBlobArchive", "압축파일_Archives"),
        ]
        media, archive = groups
        # file_ids sorted; only f001 (.png) + f002 (.mp4) are media.
        assert media.file_ids == ("f001", "f002")
        assert archive.file_ids == ("f000",)
        assert media.reason == "미디어/압축/백업 — 저장소(S3) 대상 파일 유형"

    def test_bucket_order_is_deterministic_regardless_of_input_order(self):
        from fda.organize.storage_blobs import build_groupings
        # Backup entry first, media last: output order must still follow
        # STORAGE_BLOB_BUCKETS (media, archive, backup).
        entries = [
            _entry(0, ext=".sql"),
            _entry(1, ext=".tar"),
            _entry(2, ext=".jpg"),
        ]
        groups = build_groupings(entries)
        assert [g.category for g in groups] == [
            "StorageBlobMedia", "StorageBlobArchive", "StorageBlobBackup",
        ]
```

- [ ] **Step 2: Run test to verify it passes (module already implemented in Task 1)**

Run: `.venv/bin/python -m pytest tests/test_organize_storage_blobs.py::TestBuildGroupings -q`
Expected: PASS (3 tests)

If it FAILS with `ImportError`/`AttributeError`, `build_groupings` is missing — implement it exactly as shown in Task 1 Step 3, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_organize_storage_blobs.py
git commit -m "test(organize): lock build_groupings behavior

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Korean subpath sanitization safety net

The three on-disk subpaths are Korean. plan_builder runs every subpath through `_sanitize_subpath` / `_resolve_destination_dir` (`plan_builder.py:58-84`), which strips control/illegal chars and rejects traversal. This test proves the Korean subpath constants survive that pipeline unchanged and resolve under target — so the synthetic groups produce exactly `<target>/<subpath>/<basename>`.

**Files:**
- Test: `tests/test_organize_storage_blobs.py:append`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_storage_blobs.py`:

```python
class TestSubpathSanitizationSafetyNet:
    def test_korean_subpaths_survive_plan_builder_sanitization(self, tmp_path):
        from fda.organize.plan_builder import (
            _sanitize_subpath, _resolve_destination_dir,
        )
        from fda.organize.storage_blobs import STORAGE_BLOB_BUCKETS
        for _, subpath, _ in STORAGE_BLOB_BUCKETS:
            # Subpath is unchanged by sanitization (single clean component).
            assert _sanitize_subpath(subpath) == subpath
            # Resolves to exactly <target>/<subpath>, under target.
            resolved = _resolve_destination_dir(tmp_path, subpath)
            assert resolved == (tmp_path / subpath).resolve()
            assert resolved.is_relative_to(tmp_path.resolve())
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_organize_storage_blobs.py::TestSubpathSanitizationSafetyNet -q`
Expected: PASS (1 test). `_sanitize_subpath` only strips control/Windows-illegal chars, collapses whitespace, and trims leading/trailing dots+spaces — none of which touch `미디어_Media`, `압축파일_Archives`, `백업_Backups`.

If it FAILS, a subpath constant in `storage_blobs.py` contains a character the sanitizer rewrites — fix the constant (do not change the sanitizer; it is shared).

- [ ] **Step 3: Commit**

```bash
git add tests/test_organize_storage_blobs.py
git commit -m "test(organize): assert Korean blob subpaths survive sanitization

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: router `_short_circuit` + `_short_circuit_reason`

Add the storage-blob rule **as the first branch** of both functions, in lockstep. Order matters: if the two functions branch in a different order, a category could short-circuit on one rule but report another rule's reason.

**Files:**
- Modify: `fda/organize/router.py:83-103`
- Test: `tests/test_organize_router.py:append` (after `class TestShortCircuit`, around line 111)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_router.py` inside the existing `class TestShortCircuit` (or as a new class right after it — both run; a new class is cleaner):

```python
class TestShortCircuitStorageBlob:
    def test_storage_blob_category_short_circuits_to_s3(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0, ext=".mp4")])
        for cat in (
            "StorageBlobMedia", "StorageBlobArchive", "StorageBlobBackup",
        ):
            assert _short_circuit(cat, sig) == "s3", cat

    def test_storage_blob_reason_is_distinct_and_nonempty(self):
        from fda.organize.router import (
            _aggregate_signals, _short_circuit_reason,
        )
        sig = _aggregate_signals([_entry(0, ext=".mp4")])
        reason = _short_circuit_reason("StorageBlobMedia", sig)
        assert reason  # non-empty
        # Not the Misc reason and not the all-extraction-failed reason.
        assert "Catch-all" not in reason
        assert "Text extraction failed" not in reason

    def test_misc_and_extraction_failed_unchanged(self):
        from fda.organize.router import (
            _aggregate_signals, _short_circuit, _short_circuit_reason,
        )
        # Misc still short-circuits with its own reason.
        sig_misc = _aggregate_signals([_entry(0, ext=".pdf")])
        assert _short_circuit("Misc", sig_misc) == "s3"
        assert "Catch-all" in _short_circuit_reason("Misc", sig_misc)
        # all_extraction_failed still short-circuits with its own reason.
        sig_failed = _aggregate_signals(
            [_entry(0, failed=True), _entry(1, failed=True)]
        )
        assert _short_circuit("Finance/Invoices", sig_failed) == "s3"
        assert "Text extraction failed" in _short_circuit_reason(
            "Finance/Invoices", sig_failed
        )
        # A normal category still does not short-circuit.
        sig_ok = _aggregate_signals([_entry(0, ext=".pdf")])
        assert _short_circuit("Finance/Invoices", sig_ok) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest "tests/test_organize_router.py::TestShortCircuitStorageBlob" -q`
Expected: FAIL (2 of 3 tests) — `test_storage_blob_category_short_circuits_to_s3` fails because `_short_circuit("StorageBlobMedia", sig)` returns `None` (no rule yet), and `test_storage_blob_reason_is_distinct_and_nonempty` fails because `_short_circuit_reason("StorageBlobMedia", sig)` returns `""` so `assert reason` fails. `test_misc_and_extraction_failed_unchanged` already PASSES (those rules exist today). No `ImportError` (router exists).

- [ ] **Step 3: Add the import and the two lockstep branches**

In `fda/organize/router.py`, add the import. The existing model import block ends at line 37; add this line immediately after it (line 38 area), before `logger = logging.getLogger(__name__)`:

```python
from fda.organize.storage_blobs import STORAGE_BLOB_CATEGORY_NAMES
```

Replace `_short_circuit` (currently `router.py:83-94`):

```python
def _short_circuit(category_name: str, signals: RoutingSignals) -> str | None:
    """Return 's3' when the category trips a hard-coded edge case, else None.

    The router applies these defaults *before* calling Claude. A short-circuit
    return value is the destination. Branch order MUST match
    _short_circuit_reason exactly.
    """
    if category_name in STORAGE_BLOB_CATEGORY_NAMES:
        return "s3"
    if category_name in _MISC_CATEGORY_NAMES:
        return "s3"
    if signals.all_extraction_failed:
        return "s3"
    return None
```

Replace `_short_circuit_reason` (currently `router.py:97-103`):

```python
def _short_circuit_reason(category_name: str, signals: RoutingSignals) -> str:
    if category_name in STORAGE_BLOB_CATEGORY_NAMES:
        return ("저장소 전용 파일 유형(미디어/압축/백업) — "
                "규칙에 따라 S3로 라우팅.")
    if category_name in _MISC_CATEGORY_NAMES:
        return "Catch-all category — defaulted to S3 without consulting Claude."
    if signals.all_extraction_failed:
        return ("Text extraction failed on every file — no usable signal "
                "for routing; defaulted to S3.")
    return ""
```

(`_MISC_CATEGORY_NAMES` is defined at `router.py:80` and is unchanged. The new import line goes above it; that is fine — module-level imports run before `_MISC_CATEGORY_NAMES` is referenced inside the function bodies at call time.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest "tests/test_organize_router.py::TestShortCircuitStorageBlob" "tests/test_organize_router.py::TestShortCircuit" -q`
Expected: PASS (new class + the pre-existing TestShortCircuit tests still green)

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "feat(router): deterministic S3 short-circuit for storage blobs

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: router `route()` — high-confidence flag + integration

The short-circuit branch in `route()` hard-codes `low_confidence=True` (`router.py:371-379`). Storage-blob routing is a firm deterministic rule, so the three storage-blob ids must be `low_confidence=False`. `Misc` and `all_extraction_failed` stay `low_confidence=True`. Scope the flip on the **category id**, not the destination.

**Files:**
- Modify: `fda/organize/router.py:370-384` (the `if short is not None:` branch)
- Test: `tests/test_organize_router.py:append` (new class after `TestShortCircuitStorageBlob`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_router.py`:

```python
class TestRouteStorageBlobIntegration:
    def test_storage_blob_grouping_routes_to_s3_high_confidence(
        self, tmp_path
    ):
        from fda.organize.router import route
        entries = [
            _entry(0, ext=".mp4",
                   path=str(tmp_path / "미디어_Media/clip.mp4")),
            _entry(1, ext=".zip",
                   path=str(tmp_path / "압축파일_Archives/a.zip")),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([
            _grouping("StorageBlobMedia", ["f000"], subpath="미디어_Media"),
            _grouping("StorageBlobArchive", ["f001"],
                      subpath="압축파일_Archives"),
        ])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        backend.complete.assert_not_called()
        assert {c.name for c in report.categories} == {
            "StorageBlobMedia", "StorageBlobArchive",
        }
        for c in report.categories:
            assert c.destination == "s3", c.name
            assert c.low_confidence is False, c.name

    def test_misc_short_circuit_stays_low_confidence(self, tmp_path):
        # Regression guard: the confidence flip is scoped to storage-blob
        # ids only — Misc must still be low_confidence=True.
        from fda.organize.router import route
        entries = [_entry(0, path=str(tmp_path / "Misc/a.pdf"),
                          subpath="Misc")]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Misc", ["f000"], subpath="Misc")])
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=MagicMock(), logger=_Logger(),
        )
        assert report.categories[0].destination == "s3"
        assert report.categories[0].low_confidence is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest "tests/test_organize_router.py::TestRouteStorageBlobIntegration" -q`
Expected: FAIL on `test_storage_blob_grouping_routes_to_s3_high_confidence` — `c.low_confidence` is `True` (still hard-coded). `test_misc_short_circuit_stays_low_confidence` should already PASS.

- [ ] **Step 3: Flip confidence on the storage-blob branch**

In `fda/organize/router.py`, replace the `if short is not None:` block (currently `router.py:370-379`):

```python
        if short is not None:
            routed.append(RoutedCategory(
                name=g.category,
                subpath=g.subpath,
                destination=short,
                reason=_short_circuit_reason(g.category, signals),
                low_confidence=g.category not in STORAGE_BLOB_CATEGORY_NAMES,
                signals=signals,
                misfits=(),
            ))
            logger.log(
                "ROUTER_SHORT_CIRCUIT",
                category=g.category, destination=short,
            )
            continue
```

(Only `low_confidence=True` → `low_confidence=g.category not in STORAGE_BLOB_CATEGORY_NAMES` changes; everything else in the block is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest "tests/test_organize_router.py::TestRouteStorageBlobIntegration" -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "feat(router): storage-blob categories route to S3 high-confidence

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: wire the fourth stream into `organize()` + end-to-end

Add the storage-blob stream to the catalog partition in `__init__.py` so storage blobs become real `Grouping`s, get a `path_by_id` entry (without which plan_builder raises `PlanBuilderError("unknown path_id")`), and are removed from the quarantine MOVE path (so they are not duplicated).

**Files:**
- Modify: `fda/organize/__init__.py:16-18` (add `storage_blobs` to package import), `fda/organize/__init__.py:159-179` (partition block)
- Test: `tests/test_organize_pipeline.py:append`

- [ ] **Step 1: Write the failing end-to-end test**

Append to `tests/test_organize_pipeline.py`:

```python
class TestOrganizeStorageBlobs:
    def _scripted_backend(self):
        """Summarizer + Stage A/B for the one .txt; router for any
        LLM-routed category. Storage blobs never reach the summarizer
        (reader quarantines unreadable files before the LLM call) and
        short-circuit in the router, so the backend is never asked about
        them."""
        backend = MagicMock()
        summary = json.dumps(
            {"type_label": "text", "summary": "plain text"}
        )
        taxonomy = json.dumps({
            "categories": [{
                "category_name": "Texts", "subpath": "Texts",
                "description": "plain text files",
                "criteria": "text-shaped",
            }],
            "fallback_category": {
                "category_name": "Misc", "subpath": "Misc",
                "description": "fallback",
                "criteria": "could not categorize",
            },
        })

        def fake_complete(*, messages, **_):
            body = messages[0]["content"]
            if "PATH:" in body:
                return summary
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return ""
            if "category" in parsed and "signals" in parsed:
                return json.dumps({
                    "destination": "sharepoint",
                    "reason": "text content",
                    "misfits": [],
                })
            if "BATCH" in parsed:
                return json.dumps({
                    "assignments": [
                        {"path_id": e["path_id"],
                         "category_name": "Texts"}
                        for e in parsed["BATCH"]
                    ],
                })
            if "CATALOG" in parsed:
                return taxonomy
            return ""

        backend.complete.side_effect = fake_complete
        return backend

    def test_blobs_get_real_folders_and_s3_nonblob_quarantines(
        self, tmp_path
    ):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "note.txt").write_text("hello world")
        # Storage blobs: bytes so they are not zero-byte; reader has no
        # extractor → extract_status="no_extractor".
        (ws / "clip.mp4").write_bytes(b"\x00\x01fake mp4 payload" * 8)
        (ws / "data.zip").write_bytes(b"PK\x03\x04fake zip payload" * 8)
        # Non-blob unreadable: stays in quarantine (honesty boundary).
        (ws / "weird.xyz").write_bytes(b"unknown binary blob" * 8)

        organize(
            str(ws), instructions="",
            backend=self._scripted_backend(),
            allowed_roots=[ws.parent],
            metadata=False,
        )

        # Real, named folders — not the quarantine pile.
        assert (ws / "미디어_Media" / "clip.mp4").exists()
        assert (ws / "압축파일_Archives" / "data.zip").exists()
        # Normal file still sorted by the classifier.
        assert (ws / "Texts" / "note.txt").exists()
        # Non-blob unreadable still quarantined.
        assert (ws / "_NoExtractor" / "xyz" / "weird.xyz").exists()
        # Blobs are NOT in quarantine.
        assert not (ws / "_NoExtractor" / "mp4").exists()
        assert not (ws / "_NoExtractor" / "zip").exists()

        report = json.loads(
            (ws / "routing-report.json").read_text()
        )
        by_name = {c["name"]: c for c in report["categories"]}
        for name, subpath in (
            ("StorageBlobMedia", "미디어_Media"),
            ("StorageBlobArchive", "압축파일_Archives"),
        ):
            assert name in by_name, report["categories"]
            assert by_name[name]["destination"] == "s3"
            assert by_name[name]["low_confidence"] is False
            assert by_name[name]["subpath"] == subpath
        # The empty backup bucket produced no category.
        assert "StorageBlobBackup" not in by_name
        # Non-blob unreadable reported under quarantine, no cloud dest.
        q_exts = {(g["bucket"], g["ext"]) for g in report["quarantine"]}
        assert ("_NoExtractor", "xyz") in q_exts
        assert ("_NoExtractor", "mp4") not in q_exts
        assert ("_NoExtractor", "zip") not in q_exts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest "tests/test_organize_pipeline.py::TestOrganizeStorageBlobs" -q`
Expected: FAIL — without the wiring, `clip.mp4`/`data.zip` land in `_NoExtractor/mp4`/`_NoExtractor/zip` (quarantine), so `(ws / "미디어_Media" / "clip.mp4").exists()` is `False`.

- [ ] **Step 3: Add `storage_blobs` to the package import**

In `fda/organize/__init__.py`, the import block is currently:

```python
from fda.organize import (
    _fs, classifier, executor, plan_builder, reader, router, verifier,
)
```

Replace it with (adds `storage_blobs`, keeping the existing order):

```python
from fda.organize import (
    _fs, classifier, executor, plan_builder, reader, router,
    storage_blobs, verifier,
)
```

- [ ] **Step 4: Wire the fourth stream into the partition block**

In `fda/organize/__init__.py`, the partition block is currently `lines 159-179`:

```python
        from fda.organize.models import quarantine_bucket
        # Partition catalog. Classifier-aligned id map covers only files the
        # classifier actually saw (non-junk, extractable). Junk goes through
        # the DELETE path; quarantine goes through the MOVE path to a
        # dedicated bucket; both are surfaced separately from path_by_id.
        extractable = [
            e for e in catalog.entries
            if not e.is_junk and quarantine_bucket(e) is None
        ]
        quarantine_entries = [
            e for e in catalog.entries if quarantine_bucket(e) is not None
        ]
        path_by_id = {e.path_id: e.path for e in extractable}
        junk_paths = [e.path for e in catalog.entries if e.is_junk]
        plan = plan_builder.build(
            target_dir=str(target_path),
            groupings=groupings,
            path_by_id=path_by_id,
            junk_paths=junk_paths,
            quarantine=quarantine_entries,
        )
```

Replace it with:

```python
        from fda.organize.models import Groupings, quarantine_bucket
        # Partition catalog into four independent streams:
        #   - extractable: non-junk, readable → classifier (LLM) groups
        #   - storage blobs: non-junk, unreadable, KNOWN blob type →
        #       synthetic real groups (no LLM), routed to S3
        #   - quarantine: non-junk, unreadable, NOT a blob type →
        #       _NoExtractor/_ExtractionFailed, no cloud dest (honesty)
        #   - junk: DELETE path
        # Storage blobs have a quarantine_bucket today; they are removed
        # from quarantine_entries here so they are not moved twice.
        extractable = [
            e for e in catalog.entries
            if not e.is_junk and quarantine_bucket(e) is None
        ]
        storage_blob_entries = [
            e for e in catalog.entries
            if not e.is_junk
            and storage_blobs.storage_blob_bucket(e) is not None
        ]
        quarantine_entries = [
            e for e in catalog.entries
            if quarantine_bucket(e) is not None
            and storage_blobs.storage_blob_bucket(e) is None
        ]
        # path_by_id MUST include storage-blob entries — plan_builder
        # raises PlanBuilderError("unknown path_id") for a Grouping whose
        # file_ids are absent from it (plan_builder.py:179-180).
        path_by_id = {
            e.path_id: e.path
            for e in extractable + storage_blob_entries
        }
        junk_paths = [e.path for e in catalog.entries if e.is_junk]
        # Merge synthetic storage-blob groups AFTER the classifier groups.
        # plan_builder sorts its own ops, so on-disk results are
        # order-independent; router + the report follow groupings.items
        # order directly, so appending last keeps content categories
        # first then blobs (stable, deterministic). Empty input →
        # build_groupings returns [] → Groupings is value-identical to
        # today (zero behavior change on blob-free corpora).
        sb_groupings = storage_blobs.build_groupings(storage_blob_entries)
        groupings = Groupings(
            items=groupings.items + tuple(sb_groupings),
            overall_reason=groupings.overall_reason,
        )
        plan = plan_builder.build(
            target_dir=str(target_path),
            groupings=groupings,
            path_by_id=path_by_id,
            junk_paths=junk_paths,
            quarantine=quarantine_entries,
        )
```

(Note: the merged `groupings` is the same variable later passed to `router.route(... groupings=groupings ...)` at `__init__.py:260-268` and used for `plan.grouping_summary` at `__init__.py:185` — no change needed there; `overall_reason` is preserved.)

- [ ] **Step 5: Run the end-to-end test to verify it passes**

Run: `.venv/bin/python -m pytest "tests/test_organize_pipeline.py::TestOrganizeStorageBlobs" -q`
Expected: PASS (1 test)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS — all pre-existing tests plus the new ones. Pay attention to `tests/test_organize_pipeline.py` and `tests/test_organize_router.py` (the blob-free path must be value-identical: a corpus with no blobs yields `sb_groupings == []` and a `Groupings` equal to today's).

- [ ] **Step 7: Commit**

```bash
git add fda/organize/__init__.py tests/test_organize_pipeline.py
git commit -m "feat(organize): wire storage-blob stream into the partition

Storage blobs become real Groupings merged after classifier output,
get path_by_id entries, and are excluded from the quarantine MOVE
path. Blob-free corpora are unaffected (empty merge is value-identical).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage:**

| Spec section | Task |
|---|---|
| §3 three buckets (ids, subpaths, ext sets) | Task 1 (`STORAGE_BLOB_BUCKETS`) |
| §4 `storage_blob_bucket` (both-conditions guard) | Task 1 |
| §4 `STORAGE_BLOB_CATEGORY_NAMES` single source | Task 1 |
| §4 `build_groupings` (one group/non-empty bucket, deterministic, fixed reason) | Task 1 impl + Task 2 tests |
| §4 import constraint (models only) | Task 1 (module docstring + import line) |
| §4 router import + `_short_circuit` first branch | Task 4 |
| §4 `_short_circuit_reason` lockstep | Task 4 |
| §4 confidence flip scoped to category id | Task 5 |
| §4 `__init__` partition: storage_blob_entries, quarantine exclusion, path_by_id, merge-after | Task 6 |
| §5 data flow (`clip.mp4`) | Task 6 end-to-end asserts the exact `미디어_Media/clip.mp4` path |
| §6 junk wins / non-blob quarantines / empty bucket / no-blob no-op | Task 1 (junk, non-blob, readable), Task 2 (empty bucket → no group), Task 6 (non-blob `.xyz` quarantines), Task 6 Step 6 note (no-blob no-op) |
| §7 unit tests per unit | Tasks 1, 2, 4, 5 |
| §7 Korean subpath sanitization | Task 3 |
| §7 organize() end-to-end | Task 6 |
| §7 full suite | Task 6 Step 6 |

No gaps.

**2. Placeholder scan:** No `TBD`/`TODO`/"add error handling"/"similar to Task N". Every code step has complete code.

**3. Type consistency:** `STORAGE_BLOB_BUCKETS`, `STORAGE_BLOB_CATEGORY_NAMES`, `storage_blob_bucket`, `build_groupings` named identically across Tasks 1, 4, 5, 6. `Grouping`/`Groupings` fields (`category`, `subpath`, `file_ids`, `reason` / `items`, `overall_reason`) match `models.py:120-131`. `RoutedCategory` fields match `models.py:157-165`. Test helpers (`_entry`, `_grouping`, `_groupings`, `_catalog`, `_Logger`) reuse the existing signatures in `tests/test_organize_router.py`.

---

## Execution Handoff

(Filled in by the writing-plans skill after Codex review.)
