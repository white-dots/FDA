# `.fda-ignore` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pin system/manifest files at the FDA target root so the organize pipeline leaves them untouched, eliminating the silent reorganization of `manifest.csv`, `README.md`, `LICENSE`, etc.

**Architecture:** Reader-stage filter, mirroring `git_repos_skipped`. A new `_fda_ignore` module loads built-in defaults plus an optional `target/.fda-ignore` file; the reader partitions root-level files into "pinned" (skipped entirely) and "to summarize" (normal path). Pinned absolute paths are carried on `Catalog.files_pinned`; the router converts them to relative paths on `RoutingReport.files_pinned` and emits a Korean `## 고정됨 — .fda-ignore (N)` section. No changes to classifier/plan_builder/executor/verifier — pinned files never enter the plan.

**Tech Stack:** Python 3.12, stdlib `fnmatch`, pytest. Run tests with `.venv/bin/python -m pytest tests/ -x -q --tb=short`.

**Source spec:** `docs/superpowers/specs/2026-05-14-fda-ignore-design.md`

---

## File Structure

**Create:**
- `fda/organize/_fda_ignore.py` — `BUILTIN_DEFAULTS`, `load_patterns()`, `is_pinned()`
- `tests/test_organize_fda_ignore.py` — unit tests for the new module

**Modify:**
- `fda/organize/models.py` — add `Catalog.files_pinned: tuple[str, ...] = ()` and `RoutingReport.files_pinned: tuple[str, ...] = ()`
- `fda/organize/reader.py` — partition root files into pinned vs to-summarize after junk filtering; thread `files_pinned` onto the returned `Catalog`; new log fields/events
- `fda/organize/router.py` — populate `RoutingReport.files_pinned` (relative paths) inside `route()`; emit `"pinned": [...]` in `_report_to_dict`; emit `## 고정됨 — .fda-ignore (N)` section in `_write_md_report`
- `fda/organize/__init__.py` — `_translate_catalog_for_stage5` forwards `files_pinned`

**Test files extended:**
- `tests/test_organize_reader.py` — pinning at root, log events, edge cases
- `tests/test_organize_router.py` — JSON + Markdown report changes
- `tests/test_organize_pipeline.py` — end-to-end: manifest stays at root, no ghost folders, pinned beats quarantine

---

## Task 1: Add `files_pinned` field to `Catalog` and `RoutingReport`

**Files:**
- Modify: `fda/organize/models.py:92-96` (Catalog dataclass)
- Modify: `fda/organize/models.py:201-207` (RoutingReport dataclass)
- Test: `tests/test_organize_models.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_models.py`:

```python
def test_catalog_files_pinned_defaults_to_empty_tuple():
    from fda.organize.models import Catalog
    c = Catalog(target="/tmp/x", entries=(), git_repos_skipped=())
    assert c.files_pinned == ()


def test_catalog_files_pinned_can_be_set():
    from fda.organize.models import Catalog
    c = Catalog(
        target="/tmp/x", entries=(), git_repos_skipped=(),
        files_pinned=("/tmp/x/README.md", "/tmp/x/manifest.csv"),
    )
    assert c.files_pinned == ("/tmp/x/README.md", "/tmp/x/manifest.csv")


def test_routing_report_files_pinned_defaults_to_empty_tuple():
    from fda.organize.models import RoutingReport
    r = RoutingReport(
        version="1.0", generated_at="t", target_root="/tmp/x",
        categories=(),
    )
    assert r.files_pinned == ()


def test_routing_report_files_pinned_can_be_set():
    from fda.organize.models import RoutingReport
    r = RoutingReport(
        version="1.0", generated_at="t", target_root="/tmp/x",
        categories=(),
        files_pinned=("README.md", "manifest.csv"),
    )
    assert r.files_pinned == ("README.md", "manifest.csv")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_models.py -x -q -k "files_pinned"`
Expected: 4 FAIL (unexpected keyword argument `files_pinned`).

- [ ] **Step 3: Add the fields**

In `fda/organize/models.py`, change the `Catalog` dataclass:

```python
@dataclass(frozen=True)
class Catalog:
    target: str
    entries: tuple[CatalogEntry, ...]
    git_repos_skipped: tuple[str, ...]
    files_pinned: tuple[str, ...] = ()
```

And the `RoutingReport` dataclass:

```python
@dataclass(frozen=True)
class RoutingReport:
    version: str
    generated_at: str
    target_root: str
    categories: tuple[RoutedCategory, ...]
    quarantine: tuple[QuarantineGroup, ...] = ()
    files_pinned: tuple[str, ...] = ()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_models.py -x -q -k "files_pinned"`
Expected: 4 PASS.

Run the full suite to confirm no regressions: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/models.py tests/test_organize_models.py
git commit -m "organize(models): add files_pinned to Catalog and RoutingReport"
```

---

## Task 2: Create `_fda_ignore.py` skeleton with `BUILTIN_DEFAULTS`

**Files:**
- Create: `fda/organize/_fda_ignore.py`
- Test: `tests/test_organize_fda_ignore.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_organize_fda_ignore.py`:

```python
# tests/test_organize_fda_ignore.py
"""Tests for fda.organize._fda_ignore."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_builtin_defaults_contains_expected_names():
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS
    assert ".fda-ignore" in BUILTIN_DEFAULTS
    assert "manifest.csv" in BUILTIN_DEFAULTS
    assert "README.md" in BUILTIN_DEFAULTS
    assert "README.*" in BUILTIN_DEFAULTS
    assert "LICENSE" in BUILTIN_DEFAULTS
    assert "LICENSE.*" in BUILTIN_DEFAULTS


def test_builtin_defaults_is_frozenset():
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS
    assert isinstance(BUILTIN_DEFAULTS, frozenset)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fda.organize._fda_ignore'`.

- [ ] **Step 3: Create the module**

Create `fda/organize/_fda_ignore.py`:

```python
"""Root-only file-pinning via .fda-ignore.

Reader stage utility: decide which root-level files FDA must leave untouched
during organization. Patterns combine built-in defaults with the user's
optional target/.fda-ignore file (additive only — no negations).
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BUILTIN_DEFAULTS: frozenset[str] = frozenset({
    ".fda-ignore",
    "manifest.csv",
    "README.md",
    "README.*",
    "LICENSE",
    "LICENSE.*",
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/_fda_ignore.py tests/test_organize_fda_ignore.py
git commit -m "organize: scaffold _fda_ignore module with BUILTIN_DEFAULTS"
```

---

## Task 3: `load_patterns()` — no file present → defaults only

**Files:**
- Modify: `fda/organize/_fda_ignore.py` (add `load_patterns`)
- Test: `tests/test_organize_fda_ignore.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_fda_ignore.py`:

```python
def test_load_patterns_returns_defaults_when_no_file(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    result = load_patterns(tmp_path)
    # Defaults first (sorted), then user (empty).
    assert result == tuple(sorted(BUILTIN_DEFAULTS))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q -k load_patterns`
Expected: FAIL (`load_patterns` not defined / ImportError).

- [ ] **Step 3: Implement `load_patterns()` minimally**

Append to `fda/organize/_fda_ignore.py`:

```python
def load_patterns(target: Path) -> tuple[str, ...]:
    """Return defaults ∪ user patterns. Order: defaults first, then user.

    Reads `target/.fda-ignore` if present. Strips `# comments` and blank
    lines. Unreadable file (permission error, decode error) → defaults only,
    with a warning to the logger. Never raises.
    """
    user: list[str] = []
    ignore_file = target / ".fda-ignore"
    if ignore_file.is_file():
        try:
            for raw in ignore_file.read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].strip()
                if line:
                    user.append(line)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(".fda-ignore unreadable at %s: %s", ignore_file, e)
    return tuple(sorted(BUILTIN_DEFAULTS)) + tuple(user)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q -k load_patterns`
Expected: 1 PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/_fda_ignore.py tests/test_organize_fda_ignore.py
git commit -m "organize(_fda_ignore): load_patterns returns defaults when no file"
```

---

## Task 4: `load_patterns()` — user file merges, comments stripped

**Files:**
- Test: `tests/test_organize_fda_ignore.py`

(Implementation already covers these — these tests verify behavior.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_fda_ignore.py`:

```python
def test_load_patterns_merges_user_patterns_with_defaults(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").write_text("inventory.csv\nNOTES.md\n")
    result = load_patterns(tmp_path)
    # Defaults appear first, then user patterns in file order.
    assert result == tuple(sorted(BUILTIN_DEFAULTS)) + ("inventory.csv", "NOTES.md")


def test_load_patterns_strips_comments_and_blank_lines(tmp_path):
    from fda.organize._fda_ignore import load_patterns
    (tmp_path / ".fda-ignore").write_text(
        "# a comment\n"
        "\n"
        "manifest.csv\n"
        "report-*.txt  # trailing comment\n"
        "   # full-line comment with leading spaces\n"
        "\n"
    )
    result = load_patterns(tmp_path)
    user = result[len(result) - 2:]
    assert user == ("manifest.csv", "report-*.txt")


def test_load_patterns_empty_or_comments_only_returns_defaults(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").write_text(
        "# just comments\n"
        "\n"
        "   # another\n"
    )
    result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q -k load_patterns`
Expected: 4 PASS (1 existing + 3 new).

- [ ] **Step 3: Commit**

```bash
git add tests/test_organize_fda_ignore.py
git commit -m "test(_fda_ignore): merges + comment/blank stripping"
```

---

## Task 5: `load_patterns()` — error fallback and non-file paths

**Files:**
- Test: `tests/test_organize_fda_ignore.py`

(Implementation already covers all these via the `is_file()` guard + try/except.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_fda_ignore.py`:

```python
def test_load_patterns_falls_back_to_defaults_on_decode_error(tmp_path, caplog):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    # Bytes that are not valid UTF-8 (0xff start byte).
    (tmp_path / ".fda-ignore").write_bytes(b"\xff\xfe\xfd")
    with caplog.at_level("WARNING"):
        result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))
    assert any(".fda-ignore unreadable" in r.message for r in caplog.records)


def test_load_patterns_falls_back_to_defaults_on_unreadable_file(
    tmp_path, caplog, monkeypatch,
):
    from pathlib import Path
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").write_text("manifest.csv\n")
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == ".fda-ignore":
            raise PermissionError("forced for test")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with caplog.at_level("WARNING"):
        result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))
    assert any(".fda-ignore unreadable" in r.message for r in caplog.records)


def test_load_patterns_when_fda_ignore_is_directory(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").mkdir()
    result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))


def test_load_patterns_follows_symlink_to_regular_file(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    real = tmp_path / "real.txt"
    real.write_text("inventory.csv\n")
    (tmp_path / ".fda-ignore").symlink_to(real)
    result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS)) + ("inventory.csv",)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q -k load_patterns`
Expected: 8 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_organize_fda_ignore.py
git commit -m "test(_fda_ignore): error fallback, directory guard, symlink follow"
```

---

## Task 6: `is_pinned()` — wildcard matching

**Files:**
- Modify: `fda/organize/_fda_ignore.py` (add `is_pinned`)
- Test: `tests/test_organize_fda_ignore.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_fda_ignore.py`:

```python
def test_is_pinned_matches_exact_filename():
    from fda.organize._fda_ignore import is_pinned
    assert is_pinned("manifest.csv", ("manifest.csv", "README.md"))


def test_is_pinned_matches_wildcard():
    from fda.organize._fda_ignore import is_pinned
    patterns = ("README.*",)
    assert is_pinned("README.md", patterns)
    assert is_pinned("README.txt", patterns)
    assert is_pinned("README.rst", patterns)


def test_is_pinned_no_match():
    from fda.organize._fda_ignore import is_pinned
    assert not is_pinned("data.csv", ("manifest.csv",))
    assert not is_pinned("readme.md", ("README.md",))  # case-sensitive on POSIX
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q -k is_pinned`
Expected: 3 FAIL (`is_pinned` not defined).

- [ ] **Step 3: Implement `is_pinned`**

Append to `fda/organize/_fda_ignore.py`:

```python
def is_pinned(filename: str, patterns: tuple[str, ...]) -> bool:
    """True iff `filename` matches any of `patterns` via fnmatch.fnmatch.

    Caller is responsible for restricting to root-depth files.
    """
    return any(fnmatch.fnmatch(filename, pat) for pat in patterns)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_fda_ignore.py -x -q`
Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/_fda_ignore.py tests/test_organize_fda_ignore.py
git commit -m "organize(_fda_ignore): is_pinned via fnmatch"
```

---

## Task 7: Reader — pin root files, skip extractor/LLM, populate `Catalog.files_pinned`

**Files:**
- Modify: `fda/organize/reader.py` (import + insert pinning block in `read()`, return field)
- Test: `tests/test_organize_reader.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_reader.py`:

```python
class TestFdaIgnorePinning:
    def test_pins_default_filenames_at_root(self, workspace, fake_backend, logger):
        from fda.organize import reader
        (workspace / "manifest.csv").write_text("id\n1\n")
        (workspace / "README.md").write_text("# project\n")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        pinned_names = {Path(p).name for p in catalog.files_pinned}
        assert pinned_names == {"manifest.csv", "README.md"}
        entry_names = {Path(e.path).name for e in catalog.entries}
        assert "manifest.csv" not in entry_names
        assert "README.md" not in entry_names
        assert "a.txt" in entry_names

    def test_pins_user_patterns_at_root(self, workspace, fake_backend, logger):
        from fda.organize import reader
        (workspace / ".fda-ignore").write_text("inventory.csv\n")
        (workspace / "inventory.csv").write_text("sku\n1\n")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        pinned_names = {Path(p).name for p in catalog.files_pinned}
        # inventory.csv (user), plus .fda-ignore itself (default).
        assert pinned_names == {"inventory.csv", ".fda-ignore"}

    def test_does_not_pin_same_name_in_subfolder(self, workspace, fake_backend, logger):
        from fda.organize import reader
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "manifest.csv").write_text("id\n1\n")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        assert catalog.files_pinned == ()
        entry_names = {Path(e.path).name for e in catalog.entries}
        assert "manifest.csv" in entry_names

    def test_pinned_files_skip_extractor_and_summary(self, workspace, fake_backend, logger):
        from fda.organize import reader
        (workspace / "manifest.csv").write_text("id\n1\n")
        (workspace / "a.txt").write_text("a")
        reader.read(workspace, backend=fake_backend, logger=logger)
        # Backend was called for a.txt only — never for manifest.csv.
        called_paths = [
            c.kwargs["messages"][0]["content"]
            for c in fake_backend.complete.call_args_list
        ]
        assert all("manifest.csv" not in body for body in called_paths)
        assert any("a.txt" in body for body in called_paths)

    def test_catalog_files_pinned_is_sorted(self, workspace, fake_backend, logger):
        from fda.organize import reader
        (workspace / "manifest.csv").write_text("x")
        (workspace / "README.md").write_text("x")
        (workspace / "LICENSE").write_text("x")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        assert list(catalog.files_pinned) == sorted(catalog.files_pinned)

    def test_duplicate_patterns_pin_file_once(
        self, workspace, fake_backend, logger,
    ):
        """Two patterns matching one file → file appears in files_pinned exactly once."""
        from fda.organize import reader
        # README.md is pinned by both the literal default `README.md` AND the
        # wildcard default `README.*`. Set semantics in the partition guard
        # against double-counting.
        (workspace / "README.md").write_text("# x\n")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        readme_paths = [p for p in catalog.files_pinned if p.endswith("README.md")]
        assert len(readme_paths) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q -k "TestFdaIgnorePinning"`
Expected: FAILs (`files_pinned` is empty / pinned files still extracted).

- [ ] **Step 3: Edit the reader**

In `fda/organize/reader.py`, add to the import block (top of file, with the other intra-package imports):

```python
from fda.organize import _extractors, _fda_ignore, _fs, _skills
```

Inside `read()`, after the existing junk-partition lines (`reader.py:241-242`):

```python
    junks = [p for p in files if _fs.is_junk_file(p)]
    real = [p for p in files if not _fs.is_junk_file(p)]

    # Root-only pin via .fda-ignore.
    # Order: junks first → junk in .fda-ignore still DELETEd.
    # Pinning before extraction → pinned files skip the extractor *and*
    # quarantine entirely.
    patterns = _fda_ignore.load_patterns(target)
    pinned_set = {
        p for p in real
        if p.parent == target and _fda_ignore.is_pinned(p.name, patterns)
    }
    real = [p for p in real if p not in pinned_set]
    pinned = tuple(sorted(str(p) for p in pinned_set))
```

And update the `Catalog(...)` construction at the end of `read()` (currently around `reader.py:348-352`):

```python
    return Catalog(
        target=str(target),
        entries=finalized,
        git_repos_skipped=tuple(skipped),
        files_pinned=pinned,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q -k "TestFdaIgnorePinning"`
Expected: 6 PASS.

Run the full suite: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/reader.py tests/test_organize_reader.py
git commit -m "organize(reader): pin root files via .fda-ignore, skip extractor"
```

---

## Task 8: Reader — log events (`READER_PINNED`, `pinned=N` on start/end)

**Files:**
- Modify: `fda/organize/reader.py` (log statements)
- Test: `tests/test_organize_reader.py`

- [ ] **Step 1: Write the failing tests**

`OrganizeLogger.log()` writes lines like `[hh:mm:ss.mmm] EVENT key=value key=value` (see `fda/organize/_logger.py:97-103`) — not JSONL. The tests below assert on substrings of the raw log file text.

Append to `tests/test_organize_reader.py`:

```python
class TestFdaIgnoreLogging:
    def test_emits_pinned_event_per_pinned_file(self, workspace, fake_backend, logger):
        from fda.organize import reader
        (workspace / "manifest.csv").write_text("x")
        (workspace / "README.md").write_text("x")
        (workspace / "a.txt").write_text("a")
        reader.read(workspace, backend=fake_backend, logger=logger)
        logger.close()
        log_text = Path(logger.path).read_text(encoding="utf-8")
        manifest_lines = [
            ln for ln in log_text.splitlines()
            if "READER_PINNED" in ln and "manifest.csv" in ln
        ]
        readme_lines = [
            ln for ln in log_text.splitlines()
            if "READER_PINNED" in ln and "README.md" in ln
        ]
        assert len(manifest_lines) == 1
        assert len(readme_lines) == 1

    def test_reader_start_and_end_carry_pinned_count(self, workspace, fake_backend, logger):
        from fda.organize import reader
        (workspace / "manifest.csv").write_text("x")
        (workspace / "README.md").write_text("x")
        (workspace / "a.txt").write_text("a")
        reader.read(workspace, backend=fake_backend, logger=logger)
        logger.close()
        log_text = Path(logger.path).read_text(encoding="utf-8")
        start_line = next(ln for ln in log_text.splitlines() if "READER_START" in ln)
        end_line = next(ln for ln in log_text.splitlines() if "READER_END" in ln)
        assert "pinned=2" in start_line
        assert "pinned=2" in end_line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q -k "TestFdaIgnoreLogging"`
Expected: 2 FAIL (`pinned` field missing on events).

- [ ] **Step 3: Update the log statements in `reader.py`**

Change the existing `READER_START` line (currently `reader.py:244`):

```python
    logger.log(
        "READER_START",
        files=len(files), real=len(real), junk=len(junks),
        pinned=len(pinned),
    )
```

Right after the pinning block (after `pinned = tuple(sorted(...))`), emit one `READER_PINNED` event per pinned file:

```python
    for p_abs in pinned:
        logger.log("READER_PINNED", path=p_abs)
```

Change the existing `READER_END` line (currently `reader.py:344-347`):

```python
    logger.log(
        "READER_END",
        ok=ok, failed=failed, quarantine=quarantine_count,
        total=len(finalized), pinned=len(pinned),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q -k "TestFdaIgnoreLogging"`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/reader.py tests/test_organize_reader.py
git commit -m "organize(reader): log READER_PINNED + pinned count on start/end"
```

---

## Task 9: Reader — edge cases (junk-in-ignore, dir, symlink, subfolder, git-repo target)

**Files:**
- Test: `tests/test_organize_reader.py`

These behaviors should already be implemented from Tasks 6–8; this task locks them with regression tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_reader.py`:

```python
class TestFdaIgnoreEdgeCases:
    def test_junk_filename_in_ignore_is_still_deleted(
        self, workspace, fake_backend, logger,
    ):
        from fda.organize import reader
        (workspace / ".fda-ignore").write_text(".DS_Store\n")
        (workspace / ".DS_Store").write_bytes(b"\x00")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        # .DS_Store is a junk entry, not pinned.
        ds_pinned = any(p.endswith(".DS_Store") for p in catalog.files_pinned)
        assert not ds_pinned
        ds_entry = next(
            (e for e in catalog.entries if e.path.endswith(".DS_Store")), None,
        )
        assert ds_entry is not None and ds_entry.is_junk

    def test_fda_ignore_is_directory_falls_back_to_defaults(
        self, workspace, fake_backend, logger,
    ):
        from fda.organize import reader
        (workspace / ".fda-ignore").mkdir()
        (workspace / "manifest.csv").write_text("id\n1\n")
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        # Default pinning of manifest.csv still applies.
        pinned_names = {Path(p).name for p in catalog.files_pinned}
        assert "manifest.csv" in pinned_names

    def test_fda_ignore_is_symlink_is_followed(
        self, workspace, fake_backend, logger,
    ):
        from fda.organize import reader
        real = workspace / "real_ignore.txt"
        real.write_text("inventory.csv\n")
        (workspace / ".fda-ignore").symlink_to(real)
        (workspace / "inventory.csv").write_text("sku\n1\n")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        pinned_names = {Path(p).name for p in catalog.files_pinned}
        assert "inventory.csv" in pinned_names

    def test_symlinked_root_file_not_pinned(
        self, workspace, fake_backend, logger,
    ):
        from fda.organize import reader
        outside = workspace.parent / "outside.csv"
        outside.write_text("x")
        (workspace / "manifest.csv").symlink_to(outside)
        (workspace / "a.txt").write_text("a")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        # _walk skips symlinks → manifest.csv never enters files list.
        assert all(not p.endswith("manifest.csv") for p in catalog.files_pinned)

    def test_git_repo_target_yields_empty_pinned(
        self, workspace, fake_backend, logger,
    ):
        from fda.organize import reader
        (workspace / ".git").mkdir()
        (workspace / "manifest.csv").write_text("id\n1\n")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        # _walk early-returns on .git presence → no files, no pinning.
        assert catalog.files_pinned == ()
        assert catalog.entries == ()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q -k "TestFdaIgnoreEdgeCases"`
Expected: 5 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_organize_reader.py
git commit -m "test(reader): pinning edge cases — junk, dir, symlink, subfolder, git-target"
```

---

## Task 10: `_translate_catalog_for_stage5` forwards `files_pinned`

**Files:**
- Modify: `fda/organize/__init__.py:53-57`
- Test: `tests/test_organize_pipeline.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_pipeline.py`:

```python
def test_translate_catalog_for_stage5_forwards_files_pinned():
    from fda.organize import _translate_catalog_for_stage5
    from fda.organize.models import Catalog
    catalog = Catalog(
        target="/tmp/x",
        entries=(),
        git_repos_skipped=("/tmp/x/.git",),
        files_pinned=("/tmp/x/manifest.csv", "/tmp/x/README.md"),
    )
    translated = _translate_catalog_for_stage5(catalog, outcomes=())
    assert translated.files_pinned == (
        "/tmp/x/manifest.csv", "/tmp/x/README.md",
    )
    assert translated.git_repos_skipped == ("/tmp/x/.git",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_organize_pipeline.py -x -q -k forwards_files_pinned`
Expected: FAIL — `translated.files_pinned == ()`.

- [ ] **Step 3: Edit `_translate_catalog_for_stage5`**

In `fda/organize/__init__.py`, change the `Catalog(...)` constructor at lines 53–57:

```python
    return Catalog(
        target=catalog.target,
        entries=translated_entries,
        git_repos_skipped=catalog.git_repos_skipped,
        files_pinned=catalog.files_pinned,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_organize_pipeline.py -x -q -k forwards_files_pinned`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/__init__.py tests/test_organize_pipeline.py
git commit -m "organize: forward files_pinned across stage5 catalog translation"
```

---

## Task 11: Router — populate `RoutingReport.files_pinned` with relative paths

**Files:**
- Modify: `fda/organize/router.py:453-459`
- Test: `tests/test_organize_router.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_organize_router.py` (uses the existing `_Logger` helper at the top of the file + inline `MagicMock()` backend, matching the style of `test_quarantine_only_plan_does_not_call_skill` at line 570):

```python
def test_router_routing_report_files_pinned_are_relative_paths(tmp_path):
    """RoutingReport.files_pinned holds paths relative to target_root, sorted."""
    from unittest.mock import MagicMock
    from fda.organize import router
    from fda.organize.models import Catalog, Groupings, Plan

    target = tmp_path
    catalog = Catalog(
        target=str(target),
        entries=(),
        git_repos_skipped=(),
        files_pinned=(
            str(target / "manifest.csv"),
            str(target / "README.md"),
        ),
    )
    plan = Plan(
        target=str(target), instructions="", operations=(),
        grouping_summary="",
    )
    report = router.route(
        catalog=catalog, groupings=Groupings(items=(), overall_reason=""),
        target_path=target, backend=MagicMock(), logger=_Logger(),
        plan=plan,
    )
    assert report.files_pinned == ("README.md", "manifest.csv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q -k "files_pinned_are_relative_paths"`
Expected: FAIL — `report.files_pinned == ()`.

- [ ] **Step 3: Edit `route()`**

In `fda/organize/router.py`, change the `RoutingReport(...)` constructor at lines 453–459:

```python
    report = RoutingReport(
        version="1.0",
        generated_at=_now_iso(),
        target_root=str(target_path),
        categories=tuple(routed),
        quarantine=quarantine_groups,
        files_pinned=tuple(sorted(
            str(Path(p).relative_to(target_path))
            for p in catalog.files_pinned
        )),
    )
```

`Path` is already imported at the top of `router.py`; no new import needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q -k "files_pinned_are_relative_paths"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): populate RoutingReport.files_pinned with relative paths"
```

---

## Task 12: Router — emit `## 고정됨 — .fda-ignore (N)` section in Markdown

**Files:**
- Modify: `fda/organize/router.py` (`_write_md_report`, after the failed-groups block ~line 610)
- Test: `tests/test_organize_router.py` or `tests/test_routing_report.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_router.py`:

```python
def test_router_emits_고정됨_section_when_pinned(tmp_path):
    from fda.organize.router import _write_md_report
    from fda.organize.models import RoutingReport

    md_path = tmp_path / "routing-report.md"
    report = RoutingReport(
        version="1.0", generated_at="t", target_root=str(tmp_path),
        categories=(),
        files_pinned=("README.md", "manifest.csv"),
    )
    _write_md_report(report, md_path)
    body = md_path.read_text(encoding="utf-8")
    assert "## 고정됨 — .fda-ignore (2)" in body
    assert "- `README.md`" in body
    assert "- `manifest.csv`" in body


def test_router_omits_고정됨_section_when_none(tmp_path):
    from fda.organize.router import _write_md_report
    from fda.organize.models import RoutingReport

    md_path = tmp_path / "routing-report.md"
    report = RoutingReport(
        version="1.0", generated_at="t", target_root=str(tmp_path),
        categories=(),
    )
    _write_md_report(report, md_path)
    body = md_path.read_text(encoding="utf-8")
    assert "고정됨" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q -k "고정됨"`
Expected: 2 FAIL.

- [ ] **Step 3: Edit `_write_md_report`**

In `fda/organize/router.py`, just before the final `path.write_text(...)` line of `_write_md_report` (currently line 612), add:

```python
    if report.files_pinned:
        lines.append(f"## 고정됨 — .fda-ignore ({len(report.files_pinned)})")
        lines.append("")
        for rel in report.files_pinned:
            lines.append(f"- `{rel}`")
        lines.append("")
```

(The section appears after the two quarantine blocks; consistent with §6.4 of the spec.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q -k "고정됨"`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): emit 고정됨 section in routing-report.md"
```

---

## Task 13: Router — always emit `"pinned"` key in JSON sidecar

**Files:**
- Modify: `fda/organize/router.py` (`_report_to_dict`, lines 469–515)
- Test: `tests/test_organize_router.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_router.py`:

```python
def test_router_json_pinned_key_always_present_when_empty(tmp_path):
    import json
    from fda.organize.router import _write_json_report
    from fda.organize.models import RoutingReport

    p = tmp_path / "routing-report.json"
    report = RoutingReport(
        version="1.0", generated_at="t", target_root=str(tmp_path),
        categories=(),
    )
    _write_json_report(report, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["pinned"] == []


def test_router_json_pinned_key_when_populated(tmp_path):
    import json
    from fda.organize.router import _write_json_report
    from fda.organize.models import RoutingReport

    p = tmp_path / "routing-report.json"
    report = RoutingReport(
        version="1.0", generated_at="t", target_root=str(tmp_path),
        categories=(),
        files_pinned=("manifest.csv", "README.md"),
    )
    _write_json_report(report, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["pinned"] == ["manifest.csv", "README.md"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q -k "json_pinned_key"`
Expected: 2 FAIL (KeyError 'pinned' or missing key).

- [ ] **Step 3: Edit `_report_to_dict`**

In `fda/organize/router.py`, add a `"pinned"` key to the dict returned by `_report_to_dict`, after the `"quarantine"` entry:

```python
        "quarantine": [
            {
                "bucket": g.bucket,
                "ext": g.ext,
                "entries": [
                    {
                        "relative_path": e.relative_path,
                        "size_bytes": e.size_bytes,
                        "note": e.note,
                    }
                    for e in g.entries
                ],
            }
            for g in report.quarantine
        ],
        "pinned": list(report.files_pinned),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q -k "json_pinned_key"`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): always emit pinned key in routing-report.json"
```

---

## Task 14: End-to-end — manifest stays at root, no ghost folders, pinning beats quarantine

**Files:**
- Test: `tests/test_organize_pipeline.py`

These integration tests run `organize()` over a tmp tree using the existing scripted backend pattern, asserting overall behavior. No production code changes — pinning was implemented in Tasks 7–13 — but these are the load-bearing acceptance tests for the feature.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_pipeline.py`:

```python
def test_organize_leaves_manifest_at_root(workspace, tmp_path):
    """manifest.csv at the root survives organize() untouched."""
    (workspace / "manifest.csv").write_text("id,name\n1,a\n")
    backend = _scripted_backend(workspace)
    result = organize(
        str(workspace), instructions="", backend=backend,
        allowed_roots=[tmp_path],
        route=False, metadata=False,
    )
    assert (workspace / "manifest.csv").is_file()
    assert (workspace / "manifest.csv").read_text() == "id,name\n1,a\n"


def test_organize_no_ghost_folders_when_pinned(workspace, tmp_path):
    """A pinned root file does not anchor a ghost subfolder.

    Setup: place an organizable file in a nested folder (`inbox/`) that the
    organize pipeline should drain. After the run, the source folder must be
    gone (verifier rmdir'd it) AND root pinned files must still be there.
    Without the pinning change, pinned files would also have moved out — but
    that's not what this test checks; this one specifically guards the
    "no ghost folders" invariant by confirming the verifier cleaned up the
    emptied source directory while leaving the pinned root file alone.
    """
    (workspace / "manifest.csv").write_text("id\n1\n")
    (workspace / "README.md").write_text("# x\n")
    inbox = workspace / "inbox"
    inbox.mkdir()
    (inbox / "inner.txt").write_text("inner")  # this file should be organized away
    backend = _scripted_backend(workspace)
    result = organize(
        str(workspace), instructions="", backend=backend,
        allowed_roots=[tmp_path],
        route=False, metadata=False,
    )
    # Verifier reports no leftover empty dirs.
    assert result.leftover_empty_dirs == ()
    # Emptied source folder is gone.
    assert not inbox.exists()
    # Pinned files still at root.
    assert (workspace / "manifest.csv").is_file()
    assert (workspace / "README.md").is_file()


def test_pinned_takes_precedence_over_quarantine(workspace, tmp_path):
    """A pinned filename with an unextractable extension is pinned, not quarantined."""
    # README.md is pinned by default; create a file whose pin pattern matches
    # but whose extension would otherwise be a quarantine candidate. We use
    # a custom .fda-ignore to make this concrete.
    (workspace / ".fda-ignore").write_text("inventory.xyz\n")
    (workspace / "inventory.xyz").write_bytes(b"\x00\x01\x02")  # unextractable ext
    backend = _scripted_backend(workspace)
    result = organize(
        str(workspace), instructions="", backend=backend,
        allowed_roots=[tmp_path],
        route=False, metadata=False,
    )
    # File is still at root, was never moved into a quarantine bucket.
    assert (workspace / "inventory.xyz").is_file()
    assert not (workspace / "_NoExtractor").exists()
    assert not (workspace / "_ExtractionFailed").exists()
```

- [ ] **Step 2: Run tests to verify behavior**

Run: `.venv/bin/python -m pytest tests/test_organize_pipeline.py -x -q -k "manifest_at_root or no_ghost_folders or pinned_takes_precedence"`
Expected: 3 PASS (pinning was wired up in Tasks 7–13; this verifies end-to-end).

If any test fails, investigate before changing it: a real bug in earlier tasks is more likely than a flawed acceptance test.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all green, count ≥ 641 + new tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_pipeline.py
git commit -m "test(organize): manifest at root, no ghost folders, pin>quarantine"
```

---

## Task 15: Update follow-ups memory + final verification

**Files:**
- Modify: `/Users/hogyeongkim/.claude/projects/-Users-hogyeongkim-Desktop-Projects-FDA-FDA/memory/project_organize_test_followups.md`

- [ ] **Step 1: Run the full test suite one more time**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all green.

- [ ] **Step 2: Update the follow-ups memory file**

Move follow-up #3 from "Pending" into a "Shipped" entry alongside #1 and #2. Keep the format consistent with #1/#2:

```markdown
- **#3 — System/manifest files reorganized** — SHIPPED 2026-05-14. New `.fda-ignore` root-only pin mechanism (default list: `.fda-ignore`, `manifest.csv`, `README.md`, `README.*`, `LICENSE`, `LICENSE.*`; additive user file at `target/.fda-ignore`). Reader-stage filter (`fda/organize/_fda_ignore.py`) mirrors `git_repos_skipped`; pinned files skip the extractor, never enter the plan, stay at the root. Korean `## 고정됨 — .fda-ignore (N)` section in routing-report.md, `"pinned": []` always in JSON sidecar. Spec: `docs/superpowers/specs/2026-05-14-fda-ignore-design.md`. Plan: `docs/superpowers/plans/2026-05-14-fda-ignore.md`.
```

Remove the corresponding "#3 — System/manifest files get reorganized…" bullet from the "Pending" list.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-05-14-fda-ignore-design.md \
        docs/superpowers/plans/2026-05-14-fda-ignore.md
# (memory file is outside the repo — no git add for it)
git commit -m "docs(organize): .fda-ignore spec + plan committed"
```

(If the spec is already committed from the brainstorming step, only the plan needs committing; adjust as needed.)

---

## Done criteria

- All new tests pass; full suite remains green (`.venv/bin/python -m pytest tests/ -x -q --tb=short`).
- `manifest.csv` at the target root survives an `organize()` run.
- `routing-report.md` contains `## 고정됨 — .fda-ignore (N)` whenever pinned files exist.
- `routing-report.json` always has a `"pinned"` key.
- `Catalog.files_pinned` propagates through `_translate_catalog_for_stage5` into the metadata stage's catalog input.
- Memory entry for follow-up #3 moved to "Shipped".
