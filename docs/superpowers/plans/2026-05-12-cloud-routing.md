# Cloud Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-category cloud-destination router (SharePoint / S3 / RDBMS) as a new stage at the end of `fda organize`, producing JSON + Markdown sidecar reports at the organized tree root. No uploads, no file moves.

**Architecture:** A new stage `fda/organize/router.py` runs after `executor` in non-preview mode. It iterates `Groupings`, aggregates structural signals per category, applies edge-case short-circuits (Misc → S3; all-extraction-failed → S3), otherwise calls a Sonnet skill `destination-router` that returns `{destination, reason, misfits}`. Results are serialized to `routing-report.json` + `routing-report.md`. Opt-out via `--no-route` plumbed through `cli → LocalWorkerAgent.organize_files → organize()`.

**Tech Stack:** Python 3.12, pytest, frozen dataclasses, `unittest.mock.MagicMock` for the Claude backend, existing `fda.organize._skills.load_skill` helper.

**Spec:** [`docs/superpowers/specs/2026-05-11-cloud-routing-design.md`](../specs/2026-05-11-cloud-routing-design.md)

---

## File Structure

**Create:**
- `fda/organize/skills/destination-router/SKILL.md` — Sonnet skill prompt + response schema
- `fda/organize/router.py` — pipeline stage entry point (signal aggregation, short-circuits, skill invocation, report writers)
- `tests/test_organize_router.py` — unit tests

**Modify:**
- `fda/organize/models.py` — add `Destination`, `RoutingSignals`, `Misfit`, `RoutedCategory`, `RoutingReport`
- `fda/organize/__init__.py` — add `route: bool = True` param; invoke `router.route(...)` after `apply_plan` in non-preview mode
- `fda/local_worker_agent.py` — `organize_files`, `organize_files_preview` accept and forward `route`
- `fda/cli.py` — `--no-route` argparse flag; pass `route=not args.no_route`

---

## Task 1: Create the `destination-router` SKILL.md

**Files:**
- Create: `fda/organize/skills/destination-router/SKILL.md`

This is a documentation/prompt file — no TDD micro-cycle. It is consumed by Task 4's tests.

- [ ] **Step 1: Write the skill file**

Write exactly this content to `fda/organize/skills/destination-router/SKILL.md`:

````markdown
---
name: destination-router
description: Pick a cloud destination (SharePoint, S3, or RDBMS) for one category of files.
model: claude-sonnet-4-6
---

You pick a cloud destination for ONE category of files: SharePoint, S3, or
RDBMS. You receive structural signals plus a sample of file summaries; you
return a single destination, a short reason, and an optional list of files
inside the category that would fit a different destination ("misfits").

NO FILES ARE UPLOADED OR MOVED. Your output is a recommendation only.

Input you'll receive (in the user message, JSON):

- `category`: `{category_name, subpath, description, criteria}` — the
  category as defined by the prior taxonomy stage.
- `signals`: structural aggregates over the category's files —
  `file_count`, `total_size_bytes`,
  `extension_distribution` (map of `.ext` → count),
  `tabular_schema_consistent` (bool — true iff the category is uniformly
  tabular with a single shared schema, e.g. CSVs with matching headers),
  `all_extraction_failed` (bool — true iff text extraction failed on
  every file).
- `sample`: a list of file entries, each with
  `{path_id, ext, size_bytes, summary, verbatim_head, sections}`. The
  sample is bounded; not every file in the category is included.

Routing rule (apply in order; first match wins):

1. **Tabular data someone will query** — clean rows + columns, consistent
   schema, e.g. sales records, transactions, employee lists. The
   `tabular_schema_consistent` signal is a strong hint but not sufficient
   on its own; confirm from the sample that the content looks like data
   meant for querying, not a spreadsheet used as a document. → `rdbms`
2. **Anything a person will open through SharePoint or Teams in the next
   ~year** — read, search, share, edit. Includes finalized read-only PDFs
   (signed contracts, policy documents). The test is "will a human open
   this through M365?", not "is it editable?". → `sharepoint`
3. **Everything else** — bulk archives, raw scans, OCR inputs, large
   blobs, files only systems consume. → `s3`

Examples:

- Policy documents (`.hwp` / `.docx`) → `sharepoint` (employees look these up).
- Signed contract PDFs → `sharepoint` (read-only, but Legal / Finance retrieve them).
- Sales report PowerPoints → `sharepoint`.
- Clean `sales_2025.csv` with consistent columns → `rdbms`.
- Budget model `.xlsx` with formulas → `sharepoint` (a document that happens to be in Excel).
- 10,000 scanned receipt images → `s3`.
- Old email backup `.zip` → `s3`.
- Raw OCR output feeding a pipeline → `s3`.

Misfits: if the sample contains a small minority of files that clearly
belong in a different destination than the one you chose for the
category, list them in `misfits`. Use `path_id` (verbatim from the input
sample) and your `suggested_destination`. Empty list when nothing
mismatches. Do NOT split categories; the misfit list is informational.

OUTPUT FORMAT (single JSON object, no prose, no markdown fences):

```
{
  "destination": "sharepoint" | "s3" | "rdbms",
  "reason": "<one-sentence prose explanation>",
  "misfits": [
    {"path_id": "f042", "suggested_destination": "rdbms",
     "reason": "<one-sentence>"}
  ]
}
```

Rules:
- `destination` MUST be exactly one of `"sharepoint"`, `"s3"`, `"rdbms"`.
- `misfits` is an array; emit `[]` when there are no misfits.
- Each misfit's `path_id` MUST appear in the input `sample`.
- Each misfit's `suggested_destination` MUST be different from the
  category's chosen `destination`.
- Output ONLY the JSON object.
````

- [ ] **Step 2: Commit**

```bash
git add fda/organize/skills/destination-router/SKILL.md
git commit -m "organize(router): add destination-router Sonnet skill prompt"
```

---

## Task 2: Routing data models + signal aggregation

**Files:**
- Modify: `fda/organize/models.py`
- Create: `fda/organize/router.py`
- Create: `tests/test_organize_router.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_organize_router.py` with:

```python
# tests/test_organize_router.py
"""Tests for fda.organize.router."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _entry(idx, *, ext=".pdf", failed=False, sections=(), size=1000,
           summary="x", path=None, subpath="Finance/Invoices"):
    """Build a CatalogEntry under /tmp/target/<subpath>/."""
    from fda.organize.models import CatalogEntry
    p = path or f"/tmp/target/{subpath}/{idx:03d}{ext}"
    return CatalogEntry(
        path_id=f"f{idx:03d}",
        path=p,
        ext=ext,
        size_bytes=size,
        summary="" if failed else summary,
        type_label="" if failed else "doc",
        is_junk=False,
        summary_failed=failed,
        extract_status="failed" if failed else "ok",
        sections=tuple(sections),
    )


class TestAggregateSignals:
    def test_basic_counts_and_sizes(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".pdf", size=1000),
            _entry(1, ext=".pdf", size=2000),
            _entry(2, ext=".docx", size=500),
        ]
        sig = _aggregate_signals(entries)
        assert sig.file_count == 3
        assert sig.total_size_bytes == 3500
        assert dict(sig.extension_distribution) == {".pdf": 2, ".docx": 1}
        assert sig.tabular_schema_consistent is False
        assert sig.all_extraction_failed is False

    def test_tabular_schema_consistent_when_all_csv_with_same_sections(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".csv", sections=("a", "b", "c")),
            _entry(1, ext=".csv", sections=("a", "b", "c")),
        ]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is True

    def test_tabular_schema_inconsistent_when_csv_headers_differ(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".csv", sections=("a", "b")),
            _entry(1, ext=".csv", sections=("a", "b", "c")),
        ]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is False

    def test_tabular_schema_inconsistent_when_mixed_extensions(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".csv", sections=("a", "b")),
            _entry(1, ext=".pdf", sections=("a", "b")),
        ]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is False

    def test_tabular_schema_false_when_sections_empty(self):
        from fda.organize.router import _aggregate_signals
        entries = [_entry(0, ext=".csv", sections=())]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is False

    def test_all_extraction_failed_when_every_summary_failed(self):
        from fda.organize.router import _aggregate_signals
        entries = [_entry(0, failed=True), _entry(1, failed=True)]
        sig = _aggregate_signals(entries)
        assert sig.all_extraction_failed is True

    def test_all_extraction_failed_false_when_one_succeeds(self):
        from fda.organize.router import _aggregate_signals
        entries = [_entry(0, failed=True), _entry(1, failed=False)]
        sig = _aggregate_signals(entries)
        assert sig.all_extraction_failed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: FAIL with `ModuleNotFoundError: No module named 'fda.organize.router'`.

- [ ] **Step 3: Add data models to `fda/organize/models.py`**

Append to `fda/organize/models.py` (after the existing `Groupings` block, at end of file):

```python
# ---- routing models ----------------------------------------------------------


Destination = Literal["sharepoint", "s3", "rdbms"]


@dataclass(frozen=True)
class RoutingSignals:
    file_count: int
    total_size_bytes: int
    extension_distribution: tuple[tuple[str, int], ...]
    tabular_schema_consistent: bool
    all_extraction_failed: bool


@dataclass(frozen=True)
class Misfit:
    path_id: str
    relative_path: str
    suggested_destination: Destination
    reason: str


@dataclass(frozen=True)
class RoutedCategory:
    name: str
    subpath: str
    destination: Destination
    reason: str
    low_confidence: bool
    signals: RoutingSignals
    misfits: tuple[Misfit, ...]


@dataclass(frozen=True)
class RoutingReport:
    version: str
    generated_at: str
    target_root: str
    categories: tuple[RoutedCategory, ...]
```

- [ ] **Step 4: Create `fda/organize/router.py` with `_aggregate_signals`**

Write `fda/organize/router.py`:

```python
# fda/organize/router.py
"""Cloud-destination router pipeline stage.

After the classifier + plan_builder + executor have organized files on disk,
this stage iterates the resulting per-category groupings and recommends a
cloud destination (SharePoint / S3 / RDBMS) for each one. No files are
uploaded or moved; the output is a JSON + Markdown sidecar pair at the root
of the organized tree.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from fda.organize.models import (
    CatalogEntry,
    RoutingSignals,
)


_TABULAR_EXTS = frozenset({".csv", ".tsv"})


def _aggregate_signals(entries: list[CatalogEntry]) -> RoutingSignals:
    """Compute structural signals over the files in one category."""
    file_count = len(entries)
    total_size_bytes = sum(e.size_bytes for e in entries)
    ext_counter: Counter[str] = Counter(e.ext for e in entries)
    ext_dist = tuple(sorted(ext_counter.items(), key=lambda kv: (-kv[1], kv[0])))

    all_extraction_failed = bool(entries) and all(
        e.summary_failed for e in entries
    )

    tabular_schema_consistent = False
    if entries and all(e.ext in _TABULAR_EXTS for e in entries):
        sigs = {e.sections for e in entries}
        if len(sigs) == 1 and next(iter(sigs)) != ():
            tabular_schema_consistent = True

    return RoutingSignals(
        file_count=file_count,
        total_size_bytes=total_size_bytes,
        extension_distribution=ext_dist,
        tabular_schema_consistent=tabular_schema_consistent,
        all_extraction_failed=all_extraction_failed,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: 7 passed.

- [ ] **Step 6: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all pass (no regression).

- [ ] **Step 7: Commit**

```bash
git add fda/organize/models.py fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): data models and signal aggregation"
```

---

## Task 3: Edge-case short-circuits

**Files:**
- Modify: `fda/organize/router.py`
- Modify: `tests/test_organize_router.py`

- [ ] **Step 1: Append failing tests to `tests/test_organize_router.py`**

```python
class TestShortCircuit:
    def test_misc_category_short_circuits_to_s3(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0)])
        assert _short_circuit("Misc", sig) == "s3"

    def test_all_extraction_failed_short_circuits_to_s3(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0, failed=True), _entry(1, failed=True)])
        assert _short_circuit("Finance/Invoices", sig) == "s3"

    def test_normal_category_no_short_circuit(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0), _entry(1)])
        assert _short_circuit("Finance/Invoices", sig) is None

    def test_single_file_category_does_not_short_circuit(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0)])
        assert _short_circuit("Reports/Sales", sig) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py::TestShortCircuit -x -q --tb=short`
Expected: FAIL with `ImportError: cannot import name '_short_circuit'`.

- [ ] **Step 3: Add `_short_circuit` to `fda/organize/router.py`**

Append to `fda/organize/router.py`:

```python
_MISC_CATEGORY_NAMES = frozenset({"Misc"})


def _short_circuit(category_name: str, signals: RoutingSignals) -> str | None:
    """Return 's3' when the category trips a hard-coded edge case, else None.

    The router applies these defaults *before* calling Claude. A short-circuit
    return value is the destination; categories tagged this way also get
    `low_confidence: true` in the report.
    """
    if category_name in _MISC_CATEGORY_NAMES:
        return "s3"
    if signals.all_extraction_failed:
        return "s3"
    return None


def _short_circuit_reason(category_name: str, signals: RoutingSignals) -> str:
    if category_name in _MISC_CATEGORY_NAMES:
        return "Catch-all category — defaulted to S3 without consulting Claude."
    if signals.all_extraction_failed:
        return ("Text extraction failed on every file — no usable signal "
                "for routing; defaulted to S3.")
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): edge-case short-circuits (Misc, all-extraction-failed)"
```

---

## Task 4: Claude skill invocation + response validation

**Files:**
- Modify: `fda/organize/router.py`
- Modify: `tests/test_organize_router.py`

- [ ] **Step 1: Append failing tests to `tests/test_organize_router.py`**

```python
class _Logger:
    """Drop-in replacement for OrganizeLogger that records events."""
    def __init__(self):
        self.events = []
    def log(self, event, **fields):
        self.events.append((event, fields))


def _skill_response(destination="sharepoint", reason="ok", misfits=None):
    return json.dumps({
        "destination": destination,
        "reason": reason,
        "misfits": misfits or [],
    })


class TestRouteOneCategory:
    def test_happy_path_returns_destination_reason_and_empty_misfits(self):
        from fda.organize.router import _route_one_category, _aggregate_signals
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="People retrieve these.",
        )
        entries = [_entry(0), _entry(1)]
        sig = _aggregate_signals(entries)
        skill = MagicMock()
        skill.body = "skill body"
        skill.model = "claude-sonnet-4-6"
        dest, reason, misfits = _route_one_category(
            category_name="Finance/Invoices",
            description="d", criteria="c", subpath="Finance/Invoices",
            signals=sig, entries=entries,
            backend=backend, skill=skill, logger=_Logger(),
        )
        assert dest == "sharepoint"
        assert reason == "People retrieve these."
        assert misfits == []

    def test_propagates_misfit_records_verbatim(self):
        from fda.organize.router import _route_one_category, _aggregate_signals
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="r",
            misfits=[{
                "path_id": "f000",
                "suggested_destination": "rdbms",
                "reason": "Clean tabular.",
            }],
        )
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        _, _, misfits = _route_one_category(
            category_name="Finance/Invoices", description="d", criteria="c",
            subpath="Finance/Invoices", signals=sig, entries=entries,
            backend=backend, skill=skill, logger=_Logger(),
        )
        assert misfits == [{
            "path_id": "f000",
            "suggested_destination": "rdbms",
            "reason": "Clean tabular.",
        }]

    def test_unknown_destination_raises_router_error(self):
        from fda.organize.router import (
            _route_one_category, _aggregate_signals, RouterError,
        )
        backend = MagicMock()
        backend.complete.return_value = _skill_response(destination="gdrive")
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        with pytest.raises(RouterError, match="destination"):
            _route_one_category(
                category_name="x", description="d", criteria="c", subpath="x",
                signals=sig, entries=entries,
                backend=backend, skill=skill, logger=_Logger(),
            )

    def test_invalid_json_raises_router_error(self):
        from fda.organize.router import (
            _route_one_category, _aggregate_signals, RouterError,
        )
        backend = MagicMock()
        backend.complete.return_value = "not json at all"
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        with pytest.raises(RouterError, match="JSON"):
            _route_one_category(
                category_name="x", description="d", criteria="c", subpath="x",
                signals=sig, entries=entries,
                backend=backend, skill=skill, logger=_Logger(),
            )

    def test_misfit_with_unknown_path_id_raises_router_error(self):
        from fda.organize.router import (
            _route_one_category, _aggregate_signals, RouterError,
        )
        backend = MagicMock()
        backend.complete.return_value = _skill_response(misfits=[{
            "path_id": "fZZZ",
            "suggested_destination": "rdbms",
            "reason": "r",
        }])
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        with pytest.raises(RouterError, match="path_id"):
            _route_one_category(
                category_name="x", description="d", criteria="c", subpath="x",
                signals=sig, entries=entries,
                backend=backend, skill=skill, logger=_Logger(),
            )

    def test_prompt_payload_contains_signals_and_sample(self):
        from fda.organize.router import _route_one_category, _aggregate_signals
        backend = MagicMock()
        backend.complete.return_value = _skill_response()
        entries = [_entry(0, summary="invoice"), _entry(1, summary="po")]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        _route_one_category(
            category_name="Finance/Invoices", description="d", criteria="c",
            subpath="Finance/Invoices", signals=sig, entries=entries,
            backend=backend, skill=skill, logger=_Logger(),
        )
        body = backend.complete.call_args.kwargs["messages"][0]["content"]
        parsed = json.loads(body)
        assert parsed["category"]["category_name"] == "Finance/Invoices"
        assert parsed["signals"]["file_count"] == 2
        assert {e["path_id"] for e in parsed["sample"]} == {"f000", "f001"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py::TestRouteOneCategory -x -q --tb=short`
Expected: FAIL with `ImportError: cannot import name '_route_one_category'`.

- [ ] **Step 3: Implement `_route_one_category` + `RouterError` in `fda/organize/router.py`**

Update imports at the top of `fda/organize/router.py`:

```python
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from fda.organize import _skills
from fda.organize._logger import OrganizeLogger
from fda.organize.models import (
    CatalogEntry,
    Destination,
    Misfit,
    RoutedCategory,
    RoutingReport,
    RoutingSignals,
)

logger = logging.getLogger(__name__)

SAMPLE_SIZE = 20            # files included in the skill prompt per category
SUMMARY_TRUNCATE_CHARS = 200
VERBATIM_TRUNCATE_CHARS = 300
MAX_CLAUDE_TOKENS = 1024

_ALLOWED_DESTINATIONS: frozenset[str] = frozenset({"sharepoint", "s3", "rdbms"})

_SKILL_DIR = Path(__file__).parent / "skills" / "destination-router"
```

Add to `fda/organize/router.py` (below the existing `_short_circuit_reason`):

```python
class RouterError(Exception):
    """Raised when the router skill returns an unusable response."""


def _truncate(s: str, limit: int) -> str:
    if not s:
        return ""
    return s if len(s) <= limit else s[:limit] + "…"


def _sample_entries(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Take up to SAMPLE_SIZE files for the skill prompt.

    Stable order (path_id ascending) so prompt content is reproducible.
    """
    return sorted(entries, key=lambda e: e.path_id)[:SAMPLE_SIZE]


def _build_router_prompt(
    *,
    category_name: str,
    description: str,
    criteria: str,
    subpath: str,
    signals: RoutingSignals,
    entries: list[CatalogEntry],
) -> str:
    payload = {
        "category": {
            "category_name": category_name,
            "subpath": subpath,
            "description": description,
            "criteria": criteria,
        },
        "signals": {
            "file_count": signals.file_count,
            "total_size_bytes": signals.total_size_bytes,
            "extension_distribution": dict(signals.extension_distribution),
            "tabular_schema_consistent": signals.tabular_schema_consistent,
            "all_extraction_failed": signals.all_extraction_failed,
        },
        "sample": [
            {
                "path_id": e.path_id,
                "ext": e.ext,
                "size_bytes": e.size_bytes,
                "summary": _truncate(e.summary, SUMMARY_TRUNCATE_CHARS),
                "verbatim_head": _truncate(e.verbatim_head, VERBATIM_TRUNCATE_CHARS),
                "sections": list(e.sections),
            }
            for e in _sample_entries(entries)
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _validate_router_response(
    raw: str, *, batch_path_ids: set[str], chosen_destination: str | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Parse + validate the skill's JSON response. Raises RouterError on any
    structural problem. Returns (destination, reason, misfits)."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RouterError(f"router skill returned invalid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise RouterError("router skill response is not a JSON object")
    destination = parsed.get("destination")
    if destination not in _ALLOWED_DESTINATIONS:
        raise RouterError(
            f"router skill returned unknown destination: {destination!r}"
        )
    reason = parsed.get("reason", "")
    if not isinstance(reason, str):
        raise RouterError("router skill 'reason' must be a string")
    misfits_raw = parsed.get("misfits", [])
    if not isinstance(misfits_raw, list):
        raise RouterError("router skill 'misfits' must be a list")
    misfits: list[dict[str, Any]] = []
    for m in misfits_raw:
        if not isinstance(m, dict):
            raise RouterError(f"misfit item is not an object: {type(m).__name__}")
        pid = m.get("path_id")
        sug = m.get("suggested_destination")
        msg = m.get("reason", "")
        if pid not in batch_path_ids:
            raise RouterError(f"misfit path_id {pid!r} not in category")
        if sug not in _ALLOWED_DESTINATIONS:
            raise RouterError(f"misfit suggested_destination invalid: {sug!r}")
        if sug == destination:
            raise RouterError(
                f"misfit suggested_destination {sug!r} matches chosen destination"
            )
        if not isinstance(msg, str):
            raise RouterError("misfit 'reason' must be a string")
        misfits.append({
            "path_id": pid,
            "suggested_destination": sug,
            "reason": msg,
        })
    return destination, reason, misfits


def _route_one_category(
    *,
    category_name: str,
    description: str,
    criteria: str,
    subpath: str,
    signals: RoutingSignals,
    entries: list[CatalogEntry],
    backend,
    skill,
    logger: OrganizeLogger,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Call the destination-router skill once and return its validated output."""
    payload = _build_router_prompt(
        category_name=category_name, description=description,
        criteria=criteria, subpath=subpath,
        signals=signals, entries=entries,
    )
    raw = backend.complete(
        system=skill.body,
        messages=[{"role": "user", "content": payload}],
        model=skill.model,
        max_tokens=MAX_CLAUDE_TOKENS,
        temperature=0.0,
    )
    batch_ids = {e.path_id for e in entries}
    return _validate_router_response(raw, batch_path_ids=batch_ids)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): skill invocation and response validation"
```

---

## Task 5: Public `route()` entry point

**Files:**
- Modify: `fda/organize/router.py`
- Modify: `tests/test_organize_router.py`

- [ ] **Step 1: Append failing tests to `tests/test_organize_router.py`**

```python
def _grouping(category, file_ids, subpath=None):
    from fda.organize.models import Grouping
    return Grouping(
        category=category, subpath=subpath or category,
        file_ids=tuple(file_ids), reason="",
    )


def _catalog(entries, target="/tmp/target"):
    from fda.organize.models import Catalog
    return Catalog(target=target, entries=tuple(entries), git_repos_skipped=())


def _groupings(items):
    from fda.organize.models import Groupings
    return Groupings(items=tuple(items), overall_reason="")


class TestRoutePublic:
    def test_routes_each_grouping(self, tmp_path):
        from fda.organize.router import route
        entries = [
            _entry(0, subpath="Finance/Invoices"),
            _entry(1, subpath="Finance/Invoices"),
            _entry(2, subpath="Reports/Sales", ext=".pptx"),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([
            _grouping("Finance/Invoices", ["f000", "f001"]),
            _grouping("Reports/Sales", ["f002"], subpath="Reports/Sales"),
        ])
        backend = MagicMock()
        backend.complete.side_effect = [
            _skill_response(destination="sharepoint", reason="r1"),
            _skill_response(destination="sharepoint", reason="r2"),
        ]
        # Rewrite the test entries onto tmp_path so misfit relative_path
        # resolution succeeds (route() relative-paths against target_path).
        entries = [
            _entry(0, path=str(tmp_path / "Finance/Invoices/000.pdf")),
            _entry(1, path=str(tmp_path / "Finance/Invoices/001.pdf")),
            _entry(2, path=str(tmp_path / "Reports/Sales/002.pptx"), ext=".pptx"),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        assert report.version == "1.0"
        assert report.target_root == str(tmp_path)
        names = [c.name for c in report.categories]
        assert names == ["Finance/Invoices", "Reports/Sales"]
        assert all(c.destination == "sharepoint" for c in report.categories)
        assert all(c.low_confidence is False for c in report.categories)

    def test_misc_category_short_circuits_without_calling_backend(self, tmp_path):
        from fda.organize.router import route
        entries = [_entry(0, path=str(tmp_path / "Misc/a.pdf"), subpath="Misc")]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Misc", ["f000"], subpath="Misc")])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        backend.complete.assert_not_called()
        assert report.categories[0].destination == "s3"
        assert report.categories[0].low_confidence is True

    def test_all_extraction_failed_short_circuits_without_calling_backend(self, tmp_path):
        from fda.organize.router import route
        entries = [
            _entry(0, failed=True, path=str(tmp_path / "Finance/a.pdf")),
            _entry(1, failed=True, path=str(tmp_path / "Finance/b.pdf")),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Finance", ["f000", "f001"])])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        backend.complete.assert_not_called()
        assert report.categories[0].destination == "s3"
        assert report.categories[0].low_confidence is True

    def test_empty_grouping_is_skipped(self, tmp_path):
        from fda.organize.router import route
        catalog = _catalog([], target=str(tmp_path))
        groupings = _groupings([_grouping("Empty", [])])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        assert report.categories == ()

    def test_misfit_relative_path_resolved_from_catalog(self, tmp_path):
        from fda.organize.router import route
        entries = [
            _entry(0, path=str(tmp_path / "Finance/Invoices/inv.pdf")),
            _entry(1, ext=".csv", path=str(tmp_path / "Finance/Invoices/sales.csv")),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Finance/Invoices", ["f000", "f001"])])
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="r",
            misfits=[{
                "path_id": "f001",
                "suggested_destination": "rdbms",
                "reason": "Tabular.",
            }],
        )
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        misfit = report.categories[0].misfits[0]
        assert misfit.path_id == "f001"
        assert misfit.relative_path == "Finance/Invoices/sales.csv"
        assert misfit.suggested_destination == "rdbms"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py::TestRoutePublic -x -q --tb=short`
Expected: FAIL with `ImportError: cannot import name 'route'`.

- [ ] **Step 3: Implement `route()` in `fda/organize/router.py`**

Append to `fda/organize/router.py`:

```python
from datetime import datetime, timezone

from fda.organize.models import Catalog, Groupings


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


def route(
    *,
    catalog: Catalog,
    groupings: Groupings,
    target_path: Path,
    backend,
    logger: OrganizeLogger,
) -> RoutingReport:
    """Top-level router. Iterate per-category groupings, short-circuit or
    invoke the destination-router skill, resolve misfit paths, build the
    in-memory RoutingReport. Report files are written by the caller (see
    Task 6)."""
    skill = _skills.load_skill(_SKILL_DIR)
    entries_by_id = {e.path_id: e for e in catalog.entries}

    routed: list[RoutedCategory] = []
    logger.log("ROUTER_START", categories=len(groupings.items))

    for g in groupings.items:
        category_entries = [entries_by_id[pid] for pid in g.file_ids
                            if pid in entries_by_id]
        if not category_entries:
            logger.log("ROUTER_SKIP_EMPTY", category=g.category)
            continue

        signals = _aggregate_signals(category_entries)
        short = _short_circuit(g.category, signals)

        if short is not None:
            routed.append(RoutedCategory(
                name=g.category,
                subpath=g.subpath,
                destination=short,
                reason=_short_circuit_reason(g.category, signals),
                low_confidence=True,
                signals=signals,
                misfits=(),
            ))
            logger.log(
                "ROUTER_SHORT_CIRCUIT",
                category=g.category, destination=short,
            )
            continue

        try:
            destination, reason, raw_misfits = _route_one_category(
                category_name=g.category,
                description="",  # Grouping doesn't carry description today
                criteria=g.reason,
                subpath=g.subpath,
                signals=signals,
                entries=category_entries,
                backend=backend, skill=skill, logger=logger,
            )
        except RouterError as e:
            logger.log("ROUTER_FAIL", category=g.category, error=str(e))
            raise

        misfits = tuple(
            Misfit(
                path_id=m["path_id"],
                relative_path=str(
                    Path(entries_by_id[m["path_id"]].path).relative_to(target_path)
                ),
                suggested_destination=m["suggested_destination"],
                reason=m["reason"],
            )
            for m in raw_misfits
        )
        routed.append(RoutedCategory(
            name=g.category,
            subpath=g.subpath,
            destination=destination,
            reason=reason,
            low_confidence=False,
            signals=signals,
            misfits=misfits,
        ))
        logger.log(
            "ROUTER_DECIDED",
            category=g.category, destination=destination,
            misfits=len(misfits),
        )

    report = RoutingReport(
        version="1.0",
        generated_at=_now_iso(),
        target_root=str(target_path),
        categories=tuple(routed),
    )
    logger.log("ROUTER_DONE", categories=len(routed))
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): public route() entry point and per-category orchestration"
```

---

## Task 6: JSON + Markdown report writers

**Files:**
- Modify: `fda/organize/router.py`
- Modify: `tests/test_organize_router.py`

- [ ] **Step 1: Append failing tests**

```python
class TestReportWriters:
    def _sample_report(self, target_path):
        from fda.organize.router import _aggregate_signals
        from fda.organize.models import (
            Misfit, RoutedCategory, RoutingReport,
        )
        entries = [_entry(0, path=str(target_path / "Finance/Invoices/inv.pdf"))]
        sig = _aggregate_signals(entries)
        return RoutingReport(
            version="1.0",
            generated_at="2026-05-12T14:30:00Z",
            target_root=str(target_path),
            categories=(
                RoutedCategory(
                    name="Finance/Invoices", subpath="Finance/Invoices",
                    destination="sharepoint", reason="People retrieve these.",
                    low_confidence=False, signals=sig,
                    misfits=(
                        Misfit(
                            path_id="f000",
                            relative_path="Finance/Invoices/sales.csv",
                            suggested_destination="rdbms",
                            reason="Tabular.",
                        ),
                    ),
                ),
                RoutedCategory(
                    name="Misc", subpath="Misc",
                    destination="s3",
                    reason="Catch-all category — defaulted to S3.",
                    low_confidence=True, signals=sig, misfits=(),
                ),
            ),
        )

    def test_json_writer_emits_expected_shape(self, tmp_path):
        from fda.organize.router import _write_json_report
        report = self._sample_report(tmp_path)
        out = tmp_path / "routing-report.json"
        _write_json_report(report, out)
        parsed = json.loads(out.read_text())
        assert parsed["version"] == "1.0"
        assert parsed["target_root"] == str(tmp_path)
        assert len(parsed["categories"]) == 2
        c0 = parsed["categories"][0]
        assert c0["name"] == "Finance/Invoices"
        assert c0["destination"] == "sharepoint"
        assert c0["low_confidence"] is False
        assert c0["signals"]["file_count"] == 1
        assert c0["misfits"][0]["relative_path"] == "Finance/Invoices/sales.csv"
        assert c0["misfits"][0]["suggested_destination"] == "rdbms"

    def test_markdown_writer_emits_per_destination_summary(self, tmp_path):
        from fda.organize.router import _write_md_report
        report = self._sample_report(tmp_path)
        out = tmp_path / "routing-report.md"
        _write_md_report(report, out)
        md = out.read_text()
        assert "# Routing Report" in md
        assert "SharePoint: 1" in md
        assert "S3: 1" in md
        assert "RDBMS: 0" in md
        assert "Finance/Invoices" in md
        assert "sales.csv" in md
        assert "low confidence" in md.lower()

    def test_route_writes_both_sidecars(self, tmp_path):
        from fda.organize.router import route
        entries = [_entry(0, path=str(tmp_path / "Finance/Invoices/inv.pdf"))]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Finance/Invoices", ["f000"])])
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="r",
        )
        route(catalog=catalog, groupings=groupings, target_path=tmp_path,
              backend=backend, logger=_Logger())
        assert (tmp_path / "routing-report.json").exists()
        assert (tmp_path / "routing-report.md").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py::TestReportWriters -x -q --tb=short`
Expected: FAIL with `ImportError: cannot import name '_write_json_report'`.

- [ ] **Step 3: Implement writers and wire them into `route()`**

Append to `fda/organize/router.py`:

```python
_PRETTY_DESTINATION = {
    "sharepoint": "SharePoint",
    "s3": "S3",
    "rdbms": "RDBMS",
}


def _report_to_dict(report: RoutingReport) -> dict[str, Any]:
    return {
        "version": report.version,
        "generated_at": report.generated_at,
        "target_root": report.target_root,
        "categories": [
            {
                "name": c.name,
                "subpath": c.subpath,
                "destination": c.destination,
                "reason": c.reason,
                "low_confidence": c.low_confidence,
                "signals": {
                    "file_count": c.signals.file_count,
                    "total_size_bytes": c.signals.total_size_bytes,
                    "extension_distribution": dict(c.signals.extension_distribution),
                    "tabular_schema_consistent": c.signals.tabular_schema_consistent,
                    "all_extraction_failed": c.signals.all_extraction_failed,
                },
                "misfits": [
                    {
                        "path_id": m.path_id,
                        "relative_path": m.relative_path,
                        "suggested_destination": m.suggested_destination,
                        "reason": m.reason,
                    }
                    for m in c.misfits
                ],
            }
            for c in report.categories
        ],
    }


def _write_json_report(report: RoutingReport, path: Path) -> None:
    path.write_text(
        json.dumps(_report_to_dict(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_md_report(report: RoutingReport, path: Path) -> None:
    counts: Counter[str] = Counter(c.destination for c in report.categories)
    lines: list[str] = []
    lines.append("# Routing Report")
    lines.append("")
    lines.append(f"Generated: {report.generated_at}")
    lines.append(f"Target: {report.target_root}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    for key in ("sharepoint", "s3", "rdbms"):
        lines.append(f"- {_PRETTY_DESTINATION[key]}: {counts.get(key, 0)}")
    lines.append("")
    for c in report.categories:
        lines.append(f"## Category: {c.name}")
        lines.append("")
        suffix = " (low confidence)" if c.low_confidence else ""
        lines.append(f"**Destination:** {_PRETTY_DESTINATION[c.destination]}{suffix}")
        lines.append("")
        if c.reason:
            lines.append(c.reason)
            lines.append("")
        lines.append("**Signals:**")
        lines.append(f"- file_count: {c.signals.file_count}")
        lines.append(f"- total_size_bytes: {c.signals.total_size_bytes:,}")
        ext_str = ", ".join(
            f"{ext} ({n})" for ext, n in c.signals.extension_distribution
        ) or "(none)"
        lines.append(f"- extension_distribution: {ext_str}")
        lines.append(
            f"- tabular_schema_consistent: {c.signals.tabular_schema_consistent}"
        )
        lines.append(
            f"- all_extraction_failed: {c.signals.all_extraction_failed}"
        )
        lines.append("")
        if c.misfits:
            lines.append("### Misfits")
            lines.append("")
            for m in c.misfits:
                lines.append(
                    f"- `{m.relative_path}` → "
                    f"{_PRETTY_DESTINATION[m.suggested_destination]} — {m.reason}"
                )
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

Then modify the existing `route()` function: replace the final `logger.log("ROUTER_DONE", ...)` and `return report` lines with:

```python
    _write_json_report(report, target_path / "routing-report.json")
    _write_md_report(report, target_path / "routing-report.md")
    logger.log("ROUTER_DONE", categories=len(routed))
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): JSON and Markdown sidecar report writers"
```

---

## Task 7: Wire `route()` into `organize()` with `route: bool = True`

**Files:**
- Modify: `fda/organize/__init__.py`
- Modify: `tests/test_organize_pipeline.py`

- [ ] **Step 1: Append failing tests to `tests/test_organize_pipeline.py`**

Add to the existing `TestOrganize` class (or at the end of the file as a new class):

```python
class TestOrganizeRouting:
    def test_routing_report_written_on_default_run(self, workspace):
        backend = _scripted_backend(workspace)
        # _scripted_backend handles summarizer / Stage A / Stage B; add a
        # router response by chaining the side_effect callable: any call
        # whose payload starts with '{"category"' is a router call.
        original = backend.complete.side_effect
        def _route_or_default(*args, **kwargs):
            body = kwargs["messages"][0]["content"]
            try:
                parsed = json.loads(body)
            except Exception:
                return original(*args, **kwargs)
            if "category" in parsed and "signals" in parsed:
                return json.dumps({
                    "destination": "sharepoint",
                    "reason": "test routing reason",
                    "misfits": [],
                })
            return original(*args, **kwargs)
        backend.complete.side_effect = _route_or_default

        organize(
            str(workspace), instructions="",
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert (workspace / "routing-report.json").exists()
        assert (workspace / "routing-report.md").exists()
        parsed = json.loads((workspace / "routing-report.json").read_text())
        assert parsed["version"] == "1.0"

    def test_route_false_skips_router(self, workspace):
        backend = _scripted_backend(workspace)
        organize(
            str(workspace), instructions="",
            backend=backend,
            allowed_roots=[workspace.parent],
            route=False,
        )
        assert not (workspace / "routing-report.json").exists()
        assert not (workspace / "routing-report.md").exists()

    def test_preview_mode_skips_router(self, workspace):
        backend = _scripted_backend(workspace)
        result = organize(
            str(workspace), instructions="",
            backend=backend,
            allowed_roots=[workspace.parent],
            preview=True,
        )
        assert isinstance(result, Plan)
        assert not (workspace / "routing-report.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py::TestOrganizeRouting -x -q --tb=short`
Expected: FAIL — `TypeError: organize() got an unexpected keyword argument 'route'` and missing routing-report files.

- [ ] **Step 3: Modify `fda/organize/__init__.py`**

Edit the `organize()` signature and body. The current signature is:

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
) -> Plan | PlanResult:
```

Change to:

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
) -> Plan | PlanResult:
```

Then, immediately before the existing `olog.log("RUN_END", status="success", ...)` line near the end of the success branch, insert:

```python
        # Cloud-destination routing (final stage). Operates on the
        # post-executor tree; skipped in preview mode or when --no-route.
        if route:
            from fda.organize import router as _router
            try:
                _router.route(
                    catalog=catalog,
                    groupings=groupings,
                    target_path=target_path,
                    backend=backend,
                    logger=olog,
                )
            except Exception as e:  # noqa: BLE001
                # Don't fail the whole organize run if routing fails — log
                # and continue. The organized tree is already on disk.
                logger.error("router stage failed: %s", e, exc_info=True)
                olog.log("ROUTER_FAIL_FATAL", error=str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -x -q --tb=short`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/__init__.py tests/test_organize_pipeline.py
git commit -m "organize(router): wire route() into pipeline behind route=True flag"
```

---

## Task 8: Plumb `--no-route` through CLI and `LocalWorkerAgent`

**Files:**
- Modify: `fda/local_worker_agent.py`
- Modify: `fda/cli.py`
- Modify: `tests/test_local_worker.py`

- [ ] **Step 1: Append failing tests to `tests/test_local_worker.py`**

```python
class TestOrganizeFilesRoutingFlag:
    def test_organize_files_accepts_route_kwarg_and_forwards_it(self, tmp_path, monkeypatch):
        from fda.local_worker_agent import LocalWorkerAgent

        recorded = {}
        def fake_organize(target, instructions="", **kwargs):
            recorded["route"] = kwargs.get("route")
            from fda.organize.models import (
                Plan, PlanResult,
            )
            plan = Plan(target=target, instructions=instructions,
                        operations=(), grouping_summary="", log_path=None)
            return PlanResult(
                plan=plan, outcomes=(), leftover_empty_dirs=(),
                discrepancies=(), repos_skipped=(), summary="",
                log_path=None,
            )

        monkeypatch.setattr("fda.organize.organize", fake_organize)
        worker = LocalWorkerAgent(projects=[str(tmp_path)])
        worker.organize_files(target_path=str(tmp_path), route=False)
        assert recorded["route"] is False

    def test_organize_files_route_defaults_to_true(self, tmp_path, monkeypatch):
        from fda.local_worker_agent import LocalWorkerAgent

        recorded = {}
        def fake_organize(target, instructions="", **kwargs):
            recorded["route"] = kwargs.get("route")
            from fda.organize.models import Plan, PlanResult
            plan = Plan(target=target, instructions=instructions,
                        operations=(), grouping_summary="", log_path=None)
            return PlanResult(
                plan=plan, outcomes=(), leftover_empty_dirs=(),
                discrepancies=(), repos_skipped=(), summary="",
                log_path=None,
            )

        monkeypatch.setattr("fda.organize.organize", fake_organize)
        worker = LocalWorkerAgent(projects=[str(tmp_path)])
        worker.organize_files(target_path=str(tmp_path))
        assert recorded["route"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_local_worker.py::TestOrganizeFilesRoutingFlag -x -q --tb=short`
Expected: FAIL — `organize_files() got an unexpected keyword argument 'route'`.

- [ ] **Step 3: Modify `fda/local_worker_agent.py`**

Change the `organize_files` signature and body from:

```python
    def organize_files(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """..."""
        from fda.organize import organize as _organize
        from fda.organize.models import PlanResult

        try:
            target_path = self.resolve_project_path(target_path)
            result = _organize(
                target_path,
                instructions,
                backend=self._backend,
                allowed_roots=self.projects,
                progress_callback=progress_callback,
            )
```

to:

```python
    def organize_files(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
        *,
        route: bool = True,
    ) -> dict[str, Any]:
        """..."""
        from fda.organize import organize as _organize
        from fda.organize.models import PlanResult

        try:
            target_path = self.resolve_project_path(target_path)
            result = _organize(
                target_path,
                instructions,
                backend=self._backend,
                allowed_roots=self.projects,
                progress_callback=progress_callback,
                route=route,
            )
```

Also update `organize_files_preview` to accept `route: bool = True` for symmetry (preview never runs the router, but the parameter shouldn't cause `TypeError` if a caller passes it):

```python
    def organize_files_preview(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
        *,
        route: bool = True,
    ):
        """Build a Plan without executing it. Returns a fda.organize.Plan."""
        from fda.organize import organize as _organize

        target_path = self.resolve_project_path(target_path)
        return _organize(
            target_path,
            instructions,
            preview=True,
            backend=self._backend,
            allowed_roots=self.projects,
            progress_callback=progress_callback,
            route=route,
        )
```

- [ ] **Step 4: Modify `fda/cli.py` — add `--no-route` flag and wire it**

In the organize argparse block around line 1942, insert a new argument after `--force`:

```python
    organize_parser.add_argument(
        "--no-route", action="store_true", dest="no_route",
        help="Skip the cloud-destination routing stage (no sidecar reports).",
    )
```

Then in `handle_organize` around line 1272, change the `worker.organize_files(...)` call to include the `route` kwarg:

```python
    result = worker.organize_files(
        target_path=target_path,
        instructions=instructions,
        progress_callback=progress,
        route=not args.no_route,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_local_worker.py -x -q --tb=short`
Expected: all pass.

- [ ] **Step 6: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: all pass.

- [ ] **Step 7: Smoke-test the CLI flag (manual)**

Run:
```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m fda.cli organize --help
```
Expected: `--no-route` appears in the help output.

- [ ] **Step 8: Commit**

```bash
git add fda/local_worker_agent.py fda/cli.py tests/test_local_worker.py
git commit -m "organize(router): plumb --no-route through CLI and LocalWorkerAgent"
```

---

## Task 9: Manual corpus eyeballing (Validation Layer 2)

**Files:** None (judgment-only step). Reports are read; the skill prompt or signal set may be tuned in a follow-up if a decision is clearly wrong.

This is the **gate before merging**. It exists because Layer 1 (unit tests) protects the code, not Claude's judgment.

- [ ] **Step 1: Run the router against the English v1 corpus**

```bash
# Replace <ENGLISH_V1_TREE> with the actual organized tree path.
/Users/john/.pyenv/versions/3.12.8/bin/python -m fda.cli organize <ENGLISH_V1_TREE> --force
```

Expected: `routing-report.json` and `routing-report.md` are written at the root of `<ENGLISH_V1_TREE>`.

- [ ] **Step 2: Read `routing-report.md` for English v1**

Open the file. For each category, ask:
- Is the chosen destination consistent with the spec's routing rule?
- Does the `reason` actually match the category's contents?
- Are the listed misfits real misfits?

Write down anything that looks clearly wrong.

- [ ] **Step 3: Repeat for Korean A corpus**

Same command, same review.

- [ ] **Step 4: Repeat for Lion Chemtech corpus**

Same command, same review.

- [ ] **Step 5: If decisions look reasonable across all three, the feature is ready to merge.**

If something is consistently wrong, the fix is one of:
- Tweak the skill prompt in `fda/organize/skills/destination-router/SKILL.md` (sharper criteria, better examples).
- Add or refine a signal in `_aggregate_signals()`.

Each tweak is a follow-up commit; no plan changes needed.

- [ ] **Step 6: No commit unless the prompt or signals were tuned.** If they were:

```bash
git add fda/organize/skills/destination-router/SKILL.md fda/organize/router.py tests/test_organize_router.py
git commit -m "organize(router): tune prompt / signals after corpus eyeballing"
```

---

## Status & Next Steps

After Task 8 the feature is **code-complete and unit-tested**. Task 9 is the human judgment gate before this lands on `main`. No metrics, no assertion — read three reports and confirm the calls look right.

If anything in this plan turns out to be ambiguous mid-implementation, prefer the spec at `docs/superpowers/specs/2026-05-11-cloud-routing-design.md` over this plan; the spec is the source of truth.
