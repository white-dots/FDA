# Extractor Coverage Honesty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the organize pipeline from fabricating content-based reasons for files that were never read. Files whose extractor returned a non-"ok" status are diverted *before* the per-file LLM call into deterministic quarantine buckets (`_NoExtractor/<ext>/` or `_ExtractionFailed/<ext>/`), and surfaced as dedicated sections in `routing-report.{json,md}`.

**Architecture:** A reader-side short-circuit emits quarantine catalog entries (no LLM call, empty `summary` / `type_label`, `extract_status` carries the reason). The orchestrator partitions catalog entries into extractable + quarantine, threads quarantine through `plan_builder.build(..., quarantine=...)` and `router.route(..., plan=...)`. Plan_builder emits deterministic MOVE+CREATE_DIR ops for quarantine entries via the existing `_resolve_destination_dir` / `_resolve_basename` / `_fs.validate_operation` paths. Router partitions plan MOVEs by destination prefix, never invokes the destination-router skill for quarantine moves, and attaches `QuarantineGroup`s to `RoutingReport`. Report writers append two new Skipped sections (Korean primary, English bucket names in parens).

**Tech Stack:** Python 3.12, pytest, frozen dataclasses, `unittest.mock.MagicMock` for the Claude backend.

**Spec:** [`docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md`](../specs/2026-05-13-extractor-coverage-honesty-design.md)

**Python runtime:** `.venv/bin/python` from the repo root (the path in CLAUDE.md is wrong on this machine — see memory `feedback_fda_python_path`).

**Out of scope (do NOT touch):** extractors for `.doc`/`.xls`/`.ppt`/`.html` (each gets its own follow-up spec), low-confidence quarantine, empty-but-extracted files (`status="ok"`, `text=""`), `summary_failed=True` with `extract_status="ok"`. The `_MISC_CATEGORY_NAMES` / `all_extraction_failed` short-circuits in `router.py` stay as defense-in-depth — do not remove them.

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `fda/organize/models.py` | Modify | Add `QUARANTINE_NO_EXTRACTOR` / `QUARANTINE_FAILED` constants, `CatalogEntry.quarantine_note` field, `quarantine_bucket()` helper, `QuarantineEntry` / `QuarantineGroup` dataclasses, `RoutingReport.quarantine` field. |
| `fda/organize/reader.py` | Modify | New `_quarantine_entry` helper, short-circuit in `_summarize_one`, finalization loop copies `quarantine_note`, `READER_QUARANTINE` log event, `READER_END` ok-count excludes quarantine, delete dead `_build_user_message` branches. |
| `fda/organize/classifier.py` | Modify | Filter quarantine before LLM stages; empty-extractable early-return; failed-summary threshold scoped to extractable subset. |
| `fda/organize/plan_builder.py` | Modify | `build()` gains `quarantine: Sequence[CatalogEntry] = ()` kwarg; emit MOVE+CREATE_DIR ops via existing helpers; "nothing to do" guard considers quarantine. |
| `fda/organize/router.py` | Modify | `route()` gains `plan: Plan` kwarg; partition MOVEs into category vs quarantine; build `QuarantineGroup`s; `ROUTER_QUARANTINE_GROUP` log event; `_write_json_report` + `_write_md_report` emit quarantine. |
| `fda/organize/__init__.py` | Modify | Compute extractable + quarantine subsets; narrow `path_by_id` to extractable; pass `quarantine=` to plan_builder; pass `plan=` to router. |
| `tests/test_organize_models.py` | Modify | Add tests for `quarantine_bucket()` and the new dataclass defaults. |
| `tests/test_organize_reader.py` | Modify | Add `TestQuarantineShortCircuit` class covering no_extractor / failed / tool_missing; update existing `TestNoExtractorStub` (the stub path is being deleted). |
| `tests/test_organize_classifier.py` | Modify | Add `TestQuarantineFiltering` (extractable subset, empty early-return, threshold denominator). |
| `tests/test_organize_plan_builder.py` | Modify | Add `TestQuarantine` (basic move, no-extension, collision, validation, mixed input, empty groupings + non-empty quarantine, all-empty regression). |
| `tests/test_organize_router.py` | Modify | Add `TestQuarantineRouting` (no skill call for quarantine, group keying, mixed plan, fully-extractable preserves empty `quarantine`). |
| `tests/test_routing_report.py` | Modify | Extend the existing snapshot test with quarantine groups; assert section headers, per-file format, header summary skip-count. |
| `tests/test_organize_pipeline.py` | Modify | Update Misc-targeting assertions; add an integration test that drives mixed extractable + `.zzz` (no_extractor) + failing PDF (failed) end-to-end. |

---

## Task 1: Add quarantine constants, helper, and dataclasses to `models.py`

**Files:**
- Modify: `fda/organize/models.py`
- Modify: `tests/test_organize_models.py`

The whole layer hinges on three pieces of structure: two bucket-name constants, one pure helper that classifies a `CatalogEntry` into a bucket, and two dataclasses for the report. Land them first so every subsequent task can import them.

- [ ] **Step 1: Write failing tests in `tests/test_organize_models.py`**

Append to the end of the file:

```python
class TestQuarantineBucket:
    def _entry(self, *, is_junk=False, extract_status="ok", quarantine_note=""):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id="f000",
            path="/tmp/x",
            ext=".x",
            size_bytes=0,
            summary="",
            type_label="",
            is_junk=is_junk,
            summary_failed=False,
            extract_status=extract_status,
            quarantine_note=quarantine_note,
        )

    def test_ok_returns_none(self):
        from fda.organize.models import quarantine_bucket
        assert quarantine_bucket(self._entry(extract_status="ok")) is None

    def test_junk_returns_none_even_when_no_extractor(self):
        from fda.organize.models import quarantine_bucket
        e = self._entry(is_junk=True, extract_status="no_extractor")
        assert quarantine_bucket(e) is None

    def test_no_extractor_returns_no_extractor_bucket(self):
        from fda.organize.models import quarantine_bucket, QUARANTINE_NO_EXTRACTOR
        e = self._entry(extract_status="no_extractor")
        assert quarantine_bucket(e) == QUARANTINE_NO_EXTRACTOR

    def test_failed_returns_failed_bucket(self):
        from fda.organize.models import quarantine_bucket, QUARANTINE_FAILED
        assert quarantine_bucket(self._entry(extract_status="failed")) == QUARANTINE_FAILED

    def test_tool_missing_returns_failed_bucket(self):
        from fda.organize.models import quarantine_bucket, QUARANTINE_FAILED
        assert quarantine_bucket(self._entry(extract_status="tool_missing")) == QUARANTINE_FAILED


class TestQuarantineDataclasses:
    def test_quarantine_entry_fields(self):
        from fda.organize.models import QuarantineEntry
        qe = QuarantineEntry(
            relative_path="_NoExtractor/doc/x.doc",
            size_bytes=42,
            note="no extractor registered for .doc",
        )
        assert qe.relative_path == "_NoExtractor/doc/x.doc"
        assert qe.size_bytes == 42

    def test_quarantine_group_fields(self):
        from fda.organize.models import QuarantineEntry, QuarantineGroup
        qe = QuarantineEntry(relative_path="_NoExtractor/doc/x.doc", size_bytes=1, note="")
        qg = QuarantineGroup(bucket="_NoExtractor", ext="doc", entries=(qe,))
        assert qg.bucket == "_NoExtractor"
        assert qg.ext == "doc"
        assert qg.entries == (qe,)

    def test_routing_report_quarantine_defaults_empty(self):
        from fda.organize.models import RoutingReport
        r = RoutingReport(
            version="1.0",
            generated_at="2026-05-13T00:00:00Z",
            target_root="/tmp/x",
            categories=(),
        )
        assert r.quarantine == ()

    def test_catalog_entry_quarantine_note_defaults_empty(self):
        from fda.organize.models import CatalogEntry
        e = CatalogEntry(
            path_id="f000",
            path="/tmp/x",
            ext=".x",
            size_bytes=0,
            summary="",
            type_label="",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
        )
        assert e.quarantine_note == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_models.py -x -q --tb=short`
Expected: failures referencing `quarantine_bucket`, `QUARANTINE_NO_EXTRACTOR`, `QUARANTINE_FAILED`, `QuarantineEntry`, `QuarantineGroup`, and the new field/attribute.

- [ ] **Step 3: Add constants, helper, dataclasses, and field to `fda/organize/models.py`**

Make three changes inside `fda/organize/models.py`:

(a) Add `quarantine_note: str = ""` to `CatalogEntry` as the **last** field (a defaulted field; existing positional constructions stay valid because no positional caller passes more than the existing fields). The block becomes:

```python
@dataclass(frozen=True)
class CatalogEntry:
    path_id: str         # stable ID assigned by Reader: "f000", "f001", ...
    path: str            # absolute
    ext: str             # lowercase, including the dot
    size_bytes: int
    summary: str
    type_label: str
    is_junk: bool
    summary_failed: bool
    extract_status: ExtractStatus
    verbatim_head: str = ""
    sections: tuple[str, ...] = ()
    quarantine_note: str = ""
```

(b) Above `RoutingReport` (after the `Misfit` / `RoutedCategory` block), add:

```python
QUARANTINE_NO_EXTRACTOR = "_NoExtractor"
QUARANTINE_FAILED = "_ExtractionFailed"


def quarantine_bucket(entry: CatalogEntry) -> str | None:
    """Return the quarantine bucket name, or None when the entry is processed normally.

    None for junk files (DELETE path) and for `extract_status == "ok"`.
    `_NoExtractor` for `extract_status == "no_extractor"`.
    `_ExtractionFailed` for `extract_status in {"failed", "tool_missing"}`.
    """
    if entry.is_junk:
        return None
    if entry.extract_status == "ok":
        return None
    if entry.extract_status == "no_extractor":
        return QUARANTINE_NO_EXTRACTOR
    return QUARANTINE_FAILED


@dataclass(frozen=True)
class QuarantineEntry:
    relative_path: str
    size_bytes: int
    note: str


@dataclass(frozen=True)
class QuarantineGroup:
    bucket: str
    ext: str
    entries: tuple[QuarantineEntry, ...]
```

(c) Add `quarantine: tuple[QuarantineGroup, ...] = ()` as the **last** field of `RoutingReport`:

```python
@dataclass(frozen=True)
class RoutingReport:
    version: str
    generated_at: str
    target_root: str
    categories: tuple[RoutedCategory, ...]
    quarantine: tuple[QuarantineGroup, ...] = ()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_models.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm no regression from the new dataclass field**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS. (The new `quarantine_note` and `RoutingReport.quarantine` defaults keep every existing call site forward-compatible.)

- [ ] **Step 6: Commit**

```bash
git add fda/organize/models.py tests/test_organize_models.py
git commit -m "models(organize): add quarantine bucket constants, helper, and dataclasses

CatalogEntry gains an optional quarantine_note; RoutingReport gains
an optional quarantine tuple. New QuarantineEntry/QuarantineGroup
dataclasses and a pure quarantine_bucket(entry) discriminator land
the scaffolding the rest of the honesty layer builds on. No call
sites change yet. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 2: Reader short-circuits unextractable files

**Files:**
- Modify: `fda/organize/reader.py`
- Modify: `tests/test_organize_reader.py`

After the extractor returns a non-"ok" status, the reader must skip the per-file LLM call and emit a quarantine catalog entry instead. The existing `_build_user_message` branches that produce stub messages for `no_extractor` / `tool_missing` / `failed` are deleted — they become dead code with this change.

- [ ] **Step 1: Update existing `TestNoExtractorStub` and add new quarantine tests in `tests/test_organize_reader.py`**

The current `TestNoExtractorStub.test_unknown_extension_sends_stub` (around line 242) asserts the LLM is called with a stub message. After this task, the LLM is NOT called. Replace the entire class with the block below. Append the rest as new classes at the end of the file:

```python
class TestQuarantineShortCircuit:
    """Reader does NOT call backend.complete for unextractable files.

    `_extractors.extract` returning a non-"ok" status diverts the file
    into a quarantine CatalogEntry with empty summary/type_label and
    quarantine_note carrying the extractor's note.
    """

    def test_no_extractor_skips_backend_call(self, workspace, fake_backend, logger):
        from fda.organize import reader

        (workspace / "a.zzz").write_bytes(b"\x00")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.extract_status == "no_extractor"
        assert e.summary == ""
        assert e.type_label == ""
        assert e.is_junk is False
        assert e.summary_failed is False
        assert e.quarantine_note == ""
        # Backend never called for the quarantined file
        assert fake_backend.complete.call_count == 0

    def test_failed_extraction_skips_backend_call_and_keeps_note(
        self, workspace, fake_backend, logger, monkeypatch
    ):
        from fda.organize import _extractors, reader
        from fda.organize.models import ExtractionResult

        (workspace / "scan.pdf").write_bytes(b"%PDF-1.4 fake")

        def fake_pdf(_path):
            return ExtractionResult(
                text=None, status="failed",
                note="pdftotext produced no text (image-only PDF?)",
            )

        monkeypatch.setitem(_extractors.EXTRACTORS, ".pdf", fake_pdf)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.extract_status == "failed"
        assert e.summary == ""
        assert e.type_label == ""
        assert e.summary_failed is False
        assert e.quarantine_note == "pdftotext produced no text (image-only PDF?)"
        assert fake_backend.complete.call_count == 0

    def test_tool_missing_skips_backend_call(
        self, workspace, fake_backend, logger, monkeypatch
    ):
        from fda.organize import _extractors, reader
        from fda.organize.models import ExtractionResult

        (workspace / "scan.pdf").write_bytes(b"%PDF-1.4 fake")

        def fake_pdf(_path):
            return ExtractionResult(text=None, status="tool_missing", note="pdftotext")

        monkeypatch.setitem(_extractors.EXTRACTORS, ".pdf", fake_pdf)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.extract_status == "tool_missing"
        assert e.summary == ""
        assert e.quarantine_note == "pdftotext"
        assert fake_backend.complete.call_count == 0

    def test_ok_extraction_still_routes_through_backend(
        self, workspace, fake_backend, logger
    ):
        """Regression: status='ok' must still call the LLM summary path."""
        from fda.organize import reader

        (workspace / "a.txt").write_text("hello")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.extract_status == "ok"
        assert e.summary == "x"
        assert e.quarantine_note == ""
        assert fake_backend.complete.call_count == 1

    def test_junk_extractor_state_does_not_become_quarantine(
        self, workspace, fake_backend, logger
    ):
        """Junk files keep their is_junk=True/type_label='junk' identity,
        even though their extract_status is 'no_extractor'."""
        from fda.organize import reader
        from fda.organize.models import quarantine_bucket

        (workspace / ".DS_Store").write_bytes(b"\x00")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith(".DS_Store"))
        assert e.is_junk is True
        assert e.type_label == "junk"
        assert quarantine_bucket(e) is None


class TestReaderQuarantineLogEvent:
    def test_reader_quarantine_event_emitted(self, workspace, fake_backend, tmp_path):
        from fda.organize import reader
        from fda.organize._logger import OrganizeLogger

        log = OrganizeLogger(log_path=tmp_path / "r.log", target_basename="ws")
        (workspace / "a.zzz").write_bytes(b"\x00")
        reader.read(workspace, backend=fake_backend, logger=log)
        log_text = (tmp_path / "r.log").read_text(encoding="utf-8")
        assert "READER_QUARANTINE" in log_text
        assert "no_extractor" in log_text

    def test_reader_end_excludes_quarantine_from_ok_count(
        self, workspace, fake_backend, tmp_path
    ):
        from fda.organize import reader
        from fda.organize._logger import OrganizeLogger

        log = OrganizeLogger(log_path=tmp_path / "r.log", target_basename="ws")
        (workspace / "a.txt").write_text("hi")     # ok
        (workspace / "b.zzz").write_bytes(b"\x00")  # no_extractor
        reader.read(workspace, backend=fake_backend, logger=log)
        log_text = (tmp_path / "r.log").read_text(encoding="utf-8")
        assert "READER_END" in log_text
        # OrganizeLogger.log writes fields as `key=value` (see _logger.py:102),
        # not JSON. The ok count excludes the quarantine entry.
        assert "ok=1" in log_text
        assert "quarantine=1" in log_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q --tb=short -k "Quarantine"`
Expected: failures — the reader still calls the backend for non-ok extractions.

- [ ] **Step 3: Edit `fda/organize/reader.py` — short-circuit, helper, finalization, log event, deleted stub branches**

(a) Add a new helper above `_summarize_one`:

```python
def _quarantine_entry(path: Path, size: int, extraction: ExtractionResult) -> CatalogEntry:
    return CatalogEntry(
        path_id="",  # filled in by caller after global sort
        path=str(path),
        ext=path.suffix.lower(),
        size_bytes=size,
        summary="",
        type_label="",
        is_junk=False,
        summary_failed=False,
        extract_status=extraction.status,
        verbatim_head="",
        sections=(),
        quarantine_note=extraction.note or "",
    )
```

(b) Replace `_build_user_message` (lines 76-100 in the current file) with a version that handles only `status="ok"`, since the non-ok branches become unreachable:

```python
def _build_user_message(path: Path, ext_text: ExtractionResult, size_bytes: int) -> str:
    text = ext_text.text or ""
    if len(text.encode("utf-8")) > READER_TEXT_CAP_BYTES:
        encoded = text.encode("utf-8")[:READER_TEXT_CAP_BYTES]
        text = encoded.decode("utf-8", errors="replace")
        text = _TRUNCATE_MARKER + text
    return (
        f"PATH: {path}\n"
        f"EXT: {path.suffix.lower()}\n"
        f"SIZE_BYTES: {size_bytes}\n"
        f"---\n"
        f"{text}\n"
    )
```

(c) Modify `_summarize_one` (current line 103) — after `extraction = _extractors.extract(path)`, short-circuit before the `_verbatim_head` / `_build_user_message` / `backend.complete` block. The body becomes:

```python
def _summarize_one(
    path: Path,
    *,
    backend,
    skill: _skills.SkillConfig,
    timeout_seconds: float,
) -> tuple[CatalogEntry, str, str]:
    """Returns (entry, log_event_kind, detail).

    log_event_kind is 'done', 'timeout', 'fail', or 'quarantine'.
    'quarantine' is emitted when the extractor returned a non-'ok' status;
    no LLM call is made and detail carries the extractor's note (or '').
    """
    size = path.stat().st_size
    extraction = _extractors.extract(path)
    if extraction.status != "ok":
        return _quarantine_entry(path, size, extraction), "quarantine", extraction.note or ""
    head = _verbatim_head(extraction)
    user = _build_user_message(path, extraction, size)
    try:
        raw = backend.complete(
            system=skill.body,
            messages=[{"role": "user", "content": user}],
            model=skill.model,
            max_tokens=512,
            temperature=0.0,
            timeout=timeout_seconds,
        )
    except TimeoutError as e:
        return (
            _fail_entry(
                path, size, extraction.status, str(e),
                verbatim_head=head, sections=extraction.sections,
            ),
            "timeout",
            str(e),
        )
    except Exception as e:  # noqa: BLE001 — never abort a run because one file fails
        return (
            _fail_entry(
                path, size, extraction.status, str(e),
                verbatim_head=head, sections=extraction.sections,
            ),
            "fail",
            str(e),
        )

    try:
        parsed = json.loads(raw)
        type_label = str(parsed.get("type_label", ""))[:32]
        summary = str(parsed.get("summary", ""))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return (
            _fail_entry(
                path, size, extraction.status, "unparseable summary",
                verbatim_head=head, sections=extraction.sections,
            ),
            "fail",
            "unparseable summary",
        )

    return (
        CatalogEntry(
            path_id="",
            path=str(path),
            ext=path.suffix.lower(),
            size_bytes=size,
            summary=summary,
            type_label=type_label,
            is_junk=False,
            summary_failed=False,
            extract_status=extraction.status,
            verbatim_head=head,
            sections=extraction.sections,
        ),
        "done",
        "",
    )
```

(d) Modify the `read()` loop (currently around lines 282-302) so the new `"quarantine"` kind emits its own log event and is NOT counted as a fail. After the `if kind == "done":` block, add:

```python
            elif kind == "quarantine":
                logger.log(
                    "READER_QUARANTINE",
                    path=str(p),
                    extract_status=entry.extract_status,
                    note=detail,
                )
            else:
```

(In other words: change the existing `else:` that handles `timeout` / `fail` / `deadline` into `elif kind == "quarantine":` for the new case, and keep the existing `else:` for the three failure kinds.)

(e) The finalization loop (currently lines 306-321) must thread `quarantine_note` through the reconstructed `CatalogEntry`. Replace that loop with:

```python
    ordered = sorted(entries_by_path.values(), key=lambda e: e.path)
    finalized = tuple(
        CatalogEntry(
            path_id=f"f{idx:03d}",
            path=e.path,
            ext=e.ext,
            size_bytes=e.size_bytes,
            summary=e.summary,
            type_label=e.type_label,
            is_junk=e.is_junk,
            summary_failed=e.summary_failed,
            extract_status=e.extract_status,
            verbatim_head=e.verbatim_head,
            sections=e.sections,
            quarantine_note=e.quarantine_note,
        )
        for idx, e in enumerate(ordered)
    )
```

(f) Update the `READER_END` counts (currently lines 322-324). Replace with:

```python
    from fda.organize.models import quarantine_bucket
    quarantine_count = sum(1 for e in finalized if quarantine_bucket(e) is not None)
    ok = sum(
        1 for e in finalized
        if not e.summary_failed and not e.is_junk and quarantine_bucket(e) is None
    )
    failed = sum(1 for e in finalized if e.summary_failed)
    logger.log(
        "READER_END",
        ok=ok, failed=failed, quarantine=quarantine_count, total=len(finalized),
    )
```

(Add `quarantine_bucket` to the existing top-of-file import: `from fda.organize.models import Catalog, CatalogEntry, ExtractionResult, quarantine_bucket` and drop the local import in the function.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_reader.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite (other modules may import `quarantine_bucket` once Task 3+ land — for now verify nothing else broke)**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/reader.py tests/test_organize_reader.py
git commit -m "reader(organize): short-circuit unextractable files into quarantine entries

Files whose extractor returns a non-ok status now bypass the per-file
LLM summary call entirely and become catalog entries with empty
summary/type_label and quarantine_note carrying the extractor's note.
No fabricated content reasons are produced for files that were never
read. READER_END counts quarantine separately from ok/failed. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 3: Classifier filters quarantine before LLM stages

**Files:**
- Modify: `fda/organize/classifier.py`
- Modify: `tests/test_organize_classifier.py`

The classifier must never see quarantine entries — they have no `summary` / `type_label` / `verbatim_head` and would either crash sampling or produce garbage. Three changes: filter to extractable, early-return when extractable is empty, and scope the failed-summary threshold to extractable (not `real`).

- [ ] **Step 1: Add tests in `tests/test_organize_classifier.py`**

Append a new test class to the end of the file:

```python
class TestQuarantineFiltering:
    """Classifier never sees entries that the reader put into quarantine."""

    def _entry(self, idx, *, extract_status="ok", summary_failed=False, is_junk=False):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id=f"f{idx:03d}",
            path=f"/tmp/x/{idx}.txt",
            ext=".txt",
            size_bytes=10,
            summary="" if extract_status != "ok" else "doc summary",
            type_label="" if extract_status != "ok" else "doc",
            is_junk=is_junk,
            summary_failed=summary_failed,
            extract_status=extract_status,
        )

    def test_empty_extractable_subset_returns_empty_groupings_without_calling_backend(
        self, tmp_path
    ):
        """All-quarantine catalog → no skill load, no LLM call."""
        from unittest.mock import MagicMock

        from fda.organize import classifier
        from fda.organize._logger import OrganizeLogger
        from fda.organize.models import Catalog

        backend = MagicMock()
        catalog = Catalog(
            target=str(tmp_path),
            entries=(
                self._entry(0, extract_status="no_extractor"),
                self._entry(1, extract_status="failed"),
            ),
            git_repos_skipped=(),
        )
        log = OrganizeLogger(log_path=tmp_path / "c.log", target_basename="ws")
        groupings = classifier.classify(catalog, "", backend=backend, logger=log)
        assert groupings.items == ()
        assert groupings.overall_reason == ""
        assert backend.complete.call_count == 0

    def test_failed_summary_threshold_uses_extractable_denominator(self, tmp_path):
        """Threshold over extractable subset, not over all non-junk entries.

        Catalog: 1 extractable+summary_failed + 3 quarantine. The threshold
        is 25%; over `real` (4) the rate is 25% (≤ threshold, OK), but over
        the extractable subset (1) the rate is 100% — must raise.
        """
        from unittest.mock import MagicMock

        from fda.organize import classifier
        from fda.organize._logger import OrganizeLogger
        from fda.organize.models import Catalog

        backend = MagicMock()
        catalog = Catalog(
            target=str(tmp_path),
            entries=(
                self._entry(0, extract_status="ok", summary_failed=True),
                self._entry(1, extract_status="no_extractor"),
                self._entry(2, extract_status="no_extractor"),
                self._entry(3, extract_status="no_extractor"),
            ),
            git_repos_skipped=(),
        )
        log = OrganizeLogger(log_path=tmp_path / "c.log", target_basename="ws")
        with pytest.raises(classifier.ClassifierUnreliableInputError):
            classifier.classify(catalog, "", backend=backend, logger=log)
```

Make sure `pytest` is already imported at the top of `tests/test_organize_classifier.py` (it is); otherwise add `import pytest`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_classifier.py::TestQuarantineFiltering -x -q --tb=short`
Expected: failures — current classifier calls the backend for all non-junk entries.

- [ ] **Step 3: Edit `fda/organize/classifier.py` — filter + early-return + denominator**

Locate the top of `classify()` (around line 587). Replace the block from `real = [e for e in catalog.entries if not e.is_junk]` through the `ClassifierUnreliableInputError` raise (around lines 588-602) with:

```python
    from fda.organize.models import quarantine_bucket

    real = [e for e in catalog.entries if not e.is_junk]
    extractable = [e for e in real if quarantine_bucket(e) is None]

    if not extractable:
        # All non-junk files were unextractable — nothing for the LLM to
        # classify. Skip skill loading and the LLM call entirely.
        logger.log("CLASSIFIER_START", catalog_size=0)
        logger.log("CLASSIFIER_DONE", elapsed_ms=0)
        return Groupings(items=(), overall_reason="")

    failed = [e for e in extractable if e.summary_failed]
    if len(failed) / len(extractable) > READER_FAILED_SUMMARY_THRESHOLD:
        logger.log(
            "CLASSIFIER_UNRELIABLE",
            failed_pct=int(100 * len(failed) / len(extractable)),
        )
        raise ClassifierUnreliableInputError(
            f"{len(failed)} of {len(extractable)} extractable catalog entries "
            "have failed summaries"
        )
```

Then change every subsequent reference to `real` inside `classify()` (the `_sample_for_taxonomy(real, ...)` call, the `_run_stage_b(real, ...)` calls, the fallback-rate denominator `rate = fb_count / len(real)`, the refinement loop's `assignments.get` over `real`, etc.) to use `extractable` instead. Concretely:

```python
    sample = _sample_for_taxonomy(extractable, catalog.target)
    logger.log("CLASSIFIER_START", catalog_size=len(extractable))
    logger.log(
        "TAXONOMY_SAMPLE_DONE",
        size=len(sample),
        strategy="full" if len(extractable) <= TAXONOMY_SAMPLE_FULL_THRESHOLD else "stratified",
    )

    t0 = time.monotonic()
    taxonomy = _propose_taxonomy(
        sample, instructions, backend=backend, logger=logger, skill=proposer_skill,
    )
    assignments = _run_stage_b(
        extractable, taxonomy, instructions,
        backend=backend, logger=logger, skill=assigner_skill,
    )

    fallback_name = taxonomy.fallback_category.category_name
    fb_count = sum(1 for v in assignments.values() if v == fallback_name)

    if MAX_TAXONOMY_REFINEMENTS > 0 and extractable:
        rate = fb_count / len(extractable)
        if (rate > MAX_FALLBACK_RATE) and (fb_count >= MIN_FALLBACK_REFINE_COUNT):
            logger.log("CLASSIFIER_REFINE", fallback_count=fb_count, rate=int(rate * 100))
            exemplars = [e for e in extractable if assignments.get(e.path_id) == fallback_name]
            if len(exemplars) > TAXONOMY_SAMPLE_FALLBACK_BUDGET:
                exemplars = exemplars[:TAXONOMY_SAMPLE_FALLBACK_BUDGET]
            taxonomy_v2 = _propose_taxonomy(
                sample, instructions,
                backend=backend, logger=logger, skill=proposer_skill,
                extra_exemplars=exemplars,
            )
            assignments = _run_stage_b(
                extractable, taxonomy_v2, instructions,
                backend=backend, logger=logger, skill=assigner_skill,
            )
            taxonomy = taxonomy_v2
            fallback_name = taxonomy.fallback_category.category_name
            fb_count = sum(1 for v in assignments.values() if v == fallback_name)
            rate = fb_count / len(extractable)
            if rate > MAX_FALLBACK_RATE:
                logger.log("CLASSIFIER_FALLBACK_HIGH", rate=int(rate * 100))
```

(Existing `real`-handles in the *body* of `_sample_for_taxonomy` and `_run_stage_b` are local parameters named differently — they don't need changes. Just the `classify()` outer scope.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_classifier.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/classifier.py tests/test_organize_classifier.py
git commit -m "classifier(organize): filter quarantine entries before LLM stages

Classifier now operates over the extractable subset only. All-quarantine
catalogs short-circuit before skill load. The failed-summary threshold
denominator is scoped to extractable so quarantine entries (which carry
summary_failed=False) do not dilute a genuine summary-failure rate. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 4: Plan_builder emits quarantine MOVE+CREATE_DIR ops

**Files:**
- Modify: `fda/organize/plan_builder.py`
- Modify: `tests/test_organize_plan_builder.py`

`build()` gains a keyword-only `quarantine` parameter and produces deterministic MOVE+CREATE_DIR operations under `<target>/_NoExtractor/<ext>/` or `<target>/_ExtractionFailed/<ext>/`. Quarantine ops flow through the same sanitization, collision-resolution, and validation helpers as category moves.

- [ ] **Step 1: Add tests in `tests/test_organize_plan_builder.py`**

Append a new test class to the end of the file:

```python
class TestQuarantine:
    def _q(self, path, *, extract_status, note=""):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id="",
            path=str(path),
            ext=Path(path).suffix.lower(),
            size_bytes=10,
            summary="",
            type_label="",
            is_junk=False,
            summary_failed=False,
            extract_status=extract_status,
            quarantine_note=note,
        )

    def test_no_extractor_doc_moves_into_NoExtractor_doc(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        f = workspace / "old-quote.doc"
        f.write_bytes(b"fake")
        plan = plan_builder.build(
            target_dir=str(workspace),
            groupings=_groupings(),  # no categories
            path_by_id={},
            junk_paths=[],
            quarantine=[self._q(f, extract_status="no_extractor")],
        )
        moves = [op for op in plan.operations if op.kind == OperationKind.MOVE]
        create_dirs = [op for op in plan.operations if op.kind == OperationKind.CREATE_DIR]
        assert len(moves) == 1
        assert moves[0].destination == str(workspace / "_NoExtractor" / "doc" / "old-quote.doc")
        assert moves[0].reason == "no extractor registered for .doc"
        assert any(
            op.destination == str(workspace / "_NoExtractor" / "doc")
            for op in create_dirs
        )

    def test_failed_pdf_moves_into_ExtractionFailed_pdf_with_note_reason(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        f = workspace / "scan.pdf"
        f.write_bytes(b"%PDF")
        plan = plan_builder.build(
            target_dir=str(workspace),
            groupings=_groupings(),
            path_by_id={},
            junk_paths=[],
            quarantine=[self._q(
                f, extract_status="failed", note="image-only PDF?",
            )],
        )
        moves = [op for op in plan.operations if op.kind == OperationKind.MOVE]
        assert len(moves) == 1
        assert moves[0].destination == str(workspace / "_ExtractionFailed" / "pdf" / "scan.pdf")
        assert moves[0].reason == "image-only PDF?"

    def test_failed_without_note_falls_back_to_extract_status(self, workspace):
        from fda.organize import plan_builder

        f = workspace / "scan.pdf"
        f.write_bytes(b"%PDF")
        plan = plan_builder.build(
            target_dir=str(workspace), groupings=_groupings(),
            path_by_id={}, junk_paths=[],
            quarantine=[self._q(f, extract_status="failed", note="")],
        )
        moves = [op for op in plan.operations if op.kind.value == "move"]
        assert moves[0].reason == "failed"

    def test_no_extension_falls_back_to_no_ext_subfolder(self, workspace):
        from fda.organize import plan_builder

        f = workspace / "README"
        f.write_text("readme")
        plan = plan_builder.build(
            target_dir=str(workspace), groupings=_groupings(),
            path_by_id={}, junk_paths=[],
            quarantine=[self._q(f, extract_status="no_extractor")],
        )
        moves = [op for op in plan.operations if op.kind.value == "move"]
        assert moves[0].destination == str(workspace / "_NoExtractor" / "_no_ext" / "README")
        # The dot is genuinely absent — the synthesized reason is honest.
        assert moves[0].reason == "no extractor registered for "

    def test_collision_within_quarantine_bucket_resolves_deterministically(
        self, workspace
    ):
        """Two .doc files with the same basename from different source dirs
        land in the same bucket. After the deterministic sorted-by-source
        tiebreak in _resolve_basename, the lexicographically earlier source
        keeps the original basename and the other gets ' (2)'."""
        from fda.organize import plan_builder

        (workspace / "a").mkdir()
        (workspace / "b").mkdir()
        f_a = workspace / "a" / "quote.doc"
        f_b = workspace / "b" / "quote.doc"
        f_a.write_bytes(b"a")
        f_b.write_bytes(b"b")
        plan = plan_builder.build(
            target_dir=str(workspace), groupings=_groupings(),
            path_by_id={}, junk_paths=[],
            quarantine=[
                self._q(f_a, extract_status="no_extractor"),
                self._q(f_b, extract_status="no_extractor"),
            ],
        )
        destinations = sorted(
            op.destination for op in plan.operations if op.kind.value == "move"
        )
        # Sorted by source path: workspace/a/quote.doc < workspace/b/quote.doc
        # → the 'a/' source wins the original basename.
        assert destinations == [
            str(workspace / "_NoExtractor" / "doc" / "quote (2).doc"),
            str(workspace / "_NoExtractor" / "doc" / "quote.doc"),
        ]

    def test_quarantine_move_passes_fs_validation(self, workspace):
        """Quarantine MOVE goes through _fs.validate_operation — target-
        relative, no path traversal. We verify by passing _fs explicitly."""
        from fda.organize import _fs, plan_builder

        f = workspace / "a.doc"
        f.write_bytes(b"a")
        plan = plan_builder.build(
            target_dir=str(workspace), groupings=_groupings(),
            path_by_id={}, junk_paths=[],
            quarantine=[self._q(f, extract_status="no_extractor")],
        )
        for op in plan.operations:
            if op.kind.value == "move":
                _fs.validate_operation(op, workspace)  # must not raise

    def test_mixed_groupings_quarantine_junk_produces_all_three(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        (workspace / "a.txt").write_text("a")
        (workspace / "old.doc").write_bytes(b"d")
        (workspace / ".DS_Store").write_bytes(b"\x00")
        plan = plan_builder.build(
            target_dir=str(workspace),
            groupings=_groupings(_grouping("Texts", "Texts", ["f000"])),
            path_by_id={"f000": str(workspace / "a.txt")},
            junk_paths=[str(workspace / ".DS_Store")],
            quarantine=[self._q(workspace / "old.doc", extract_status="no_extractor")],
        )
        moves = [op for op in plan.operations if op.kind == OperationKind.MOVE]
        deletes = [op for op in plan.operations if op.kind == OperationKind.DELETE]
        create_dirs = [op for op in plan.operations if op.kind == OperationKind.CREATE_DIR]
        # 1 category move + 1 quarantine move
        assert len(moves) == 2
        # 1 junk delete
        assert len(deletes) == 1
        # CREATE_DIRs cover both destinations (Texts/ + _NoExtractor/doc/)
        dest_dirs = {op.destination for op in create_dirs}
        assert str(workspace / "Texts") in dest_dirs
        assert str(workspace / "_NoExtractor" / "doc") in dest_dirs

    def test_empty_groupings_with_quarantine_does_not_raise(self, workspace):
        """Plan with only quarantine MOVEs is valid — does not trip the
        'nothing to do' guard."""
        from fda.organize import plan_builder

        f = workspace / "x.doc"
        f.write_bytes(b"x")
        plan = plan_builder.build(
            target_dir=str(workspace), groupings=_groupings(),
            path_by_id={}, junk_paths=[],
            quarantine=[self._q(f, extract_status="no_extractor")],
        )
        assert any(op.kind.value == "move" for op in plan.operations)

    def test_all_dropped_quarantine_raises_nothing_to_do(self, workspace):
        """When the only input is a quarantine entry whose source vanished
        from disk, all ops are dropped → the 'nothing to do' guard fires.
        (Truly-empty input — no groupings, no junk, no quarantine — keeps
        its existing 'empty plan' behavior; see TestEmptyEverything at
        test_organize_plan_builder.py:157.)"""
        from fda.organize import plan_builder
        from fda.organize.models import CatalogEntry

        ghost = CatalogEntry(
            path_id="", path=str(workspace / "ghost.doc"),
            ext=".doc", size_bytes=0, summary="", type_label="",
            is_junk=False, summary_failed=False,
            extract_status="no_extractor", quarantine_note="",
        )
        with pytest.raises(plan_builder.PlanBuilderError):
            plan_builder.build(
                target_dir=str(workspace), groupings=_groupings(),
                path_by_id={}, junk_paths=[], quarantine=[ghost],
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_plan_builder.py::TestQuarantine -x -q --tb=short`
Expected: failures — `build()` does not accept `quarantine=`.

- [ ] **Step 3: Edit `fda/organize/plan_builder.py` — add the `quarantine` parameter and emit ops**

(a) Update the import block (around lines 17-23):

```python
from fda.organize.models import (
    CatalogEntry,
    Grouping,
    Groupings,
    Operation,
    OperationKind,
    Plan,
    QUARANTINE_FAILED,
    QUARANTINE_NO_EXTRACTOR,
    quarantine_bucket,
)
```

(b) Update `build()` signature (currently line 133):

```python
def build(
    target_dir: str,
    groupings: Groupings,
    path_by_id: Mapping[str, str],
    junk_paths: Sequence[str],
    *,
    quarantine: Sequence[CatalogEntry] = (),
) -> Plan:
```

(c) Before the final `operations = tuple(create_dir_ops + move_ops + delete_ops)` assembly, add a quarantine block. Insert it **after** the existing `move_ops` loop but **before** the `surviving_dirs` computation. The shape:

```python
    # ---- quarantine MOVEs --------------------------------------------------
    # Entries here had a non-ok extract_status; the reader skipped the LLM
    # call. We synthesize a reason from the bucket + extractor note and
    # route them under <target>/<bucket>/<ext>/<basename>.
    quarantine_candidates: list[tuple[Path, Path, str]] = []  # (src, dest_dir, reason)
    for entry in quarantine:
        bucket = quarantine_bucket(entry)
        if bucket is None:
            # Defensive: caller passed something that doesn't belong here.
            logger.info("dropping non-quarantine entry from quarantine list: %s", entry.path)
            continue
        src = Path(entry.path)
        if not src.is_file():
            logger.info("dropping quarantine source that is not a file: %s", src)
            continue
        ext_segment = entry.ext.lstrip(".") or "_no_ext"
        dest_dir = _resolve_destination_dir(target, f"{bucket}/{ext_segment}")
        if bucket == QUARANTINE_NO_EXTRACTOR:
            reason = f"no extractor registered for {entry.ext}"
        else:
            reason = entry.quarantine_note or entry.extract_status
        quarantine_candidates.append((src, dest_dir, reason))

    # Deterministic basename resolution: sort by source path so collisions
    # break ties alphabetically and reproducibly across runs.
    quarantine_candidates.sort(key=lambda t: str(t[0]))
    for src, dest_dir, reason in quarantine_candidates:
        basename = _resolve_basename(
            dest_dir, src, planned,
            on_disk_check=True, planned_sources=planned_sources,
        )
        key = (str(dest_dir), basename.casefold())
        planned.add(key)
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(src),
            destination=str(dest_dir / basename),
            reason=reason,
        )
        try:
            _fs.validate_operation(op, target)
        except ValueError as e:
            logger.info("dropping invalid quarantine move %s -> %s: %s", src, dest_dir / basename, e)
            planned.discard(key)
            continue
        move_ops.append(op)
```

`planned_sources` is currently computed from `candidates` (category moves) at line 207 *before* the move-ops loop. The quarantine block needs to participate in the same `planned_sources` snapshot, otherwise a category file and a quarantine file with the same source path (impossible by construction, but defensive) could collide. Recompute `planned_sources` to include both:

```python
    # (Replace the existing line 207 assignment with:)
    planned_sources = frozenset(
        list(src.resolve() for _, src, _, _ in candidates)
        + list(Path(e.path).resolve() for e in quarantine if Path(e.path).is_file())
    )
```

(d) Update the "nothing to do" guard (currently line 265). Replace the `had_input` line:

```python
    had_input = bool(groupings.items) or bool(junk_paths) or bool(quarantine)
```

The `surviving_dirs` computation (line 236) already includes the quarantine destinations because the quarantine ops were appended to `move_ops`. No further change there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_plan_builder.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/plan_builder.py tests/test_organize_plan_builder.py
git commit -m "plan_builder(organize): emit deterministic quarantine MOVE+CREATE_DIR ops

build() accepts quarantine=Sequence[CatalogEntry] and routes each entry
under <target>/_NoExtractor/<ext>/ or <target>/_ExtractionFailed/<ext>/
via the existing sanitization, collision, and validation helpers. Empty
groupings + non-empty quarantine is a valid plan. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 5: Router walks the plan, partitions MOVEs, and builds QuarantineGroups

**Files:**
- Modify: `fda/organize/router.py`
- Modify: `tests/test_organize_router.py`

`route()` gains a `plan: Plan` keyword argument. The router never calls the destination-router skill for quarantine moves; it groups them by `(bucket, ext)` and attaches them to `RoutingReport`.

- [ ] **Step 1: Add tests in `tests/test_organize_router.py`**

Append a new test class (the existing file has helpers `_entry`, `_grouping`, `_groupings`, `_Logger` already; use them directly):

```python
class TestQuarantineRouting:
    """Router never invokes the destination-router skill for quarantine
    moves. It partitions plan.operations by destination prefix and emits
    QuarantineGroups grouped by (bucket, ext)."""

    def _build_plan(self, target, *, category_moves=(), quarantine_moves=()):
        """Helper: build a minimal Plan with MOVE ops only."""
        from fda.organize.models import Operation, OperationKind, Plan
        ops = []
        for src, dst, reason in category_moves:
            ops.append(Operation(
                kind=OperationKind.MOVE,
                source=str(src), destination=str(dst), reason=reason,
            ))
        for src, dst, reason in quarantine_moves:
            ops.append(Operation(
                kind=OperationKind.MOVE,
                source=str(src), destination=str(dst), reason=reason,
            ))
        return Plan(target=str(target), instructions="", operations=tuple(ops), grouping_summary="")

    def test_quarantine_only_plan_does_not_call_skill(self, tmp_path):
        from unittest.mock import MagicMock

        from fda.organize import router
        from fda.organize.models import Catalog, Groupings

        backend = MagicMock()
        log = _Logger()
        target = tmp_path
        plan = self._build_plan(
            target,
            quarantine_moves=[
                (target / "a.doc", target / "_NoExtractor" / "doc" / "a.doc",
                 "no extractor registered for .doc"),
            ],
        )
        catalog = Catalog(target=str(target), entries=(), git_repos_skipped=())
        report = router.route(
            catalog=catalog, groupings=Groupings(items=(), overall_reason=""),
            target_path=target, backend=backend, logger=log, plan=plan,
        )
        assert backend.complete.call_count == 0
        assert report.categories == ()
        assert len(report.quarantine) == 1
        g = report.quarantine[0]
        assert g.bucket == "_NoExtractor"
        assert g.ext == "doc"
        assert g.entries[0].relative_path == "_NoExtractor/doc/a.doc"
        assert g.entries[0].note == "no extractor registered for .doc"

    def test_mixed_plan_skill_only_called_for_category_grouping(self, tmp_path):
        import json
        from unittest.mock import MagicMock

        from fda.organize import router
        from fda.organize.models import Catalog, CatalogEntry, Groupings

        backend = MagicMock()
        backend.complete.return_value = json.dumps(
            {"destination": "sharepoint", "reason": "ok", "misfits": []}
        )
        log = _Logger()
        target = tmp_path

        # One category file + one quarantine file
        entry_ok = CatalogEntry(
            path_id="f000",
            path=str(target / "Texts" / "a.txt"),
            ext=".txt", size_bytes=1, summary="ok", type_label="doc",
            is_junk=False, summary_failed=False, extract_status="ok",
        )
        catalog = Catalog(target=str(target), entries=(entry_ok,), git_repos_skipped=())
        plan = self._build_plan(
            target,
            category_moves=[
                (target / "a.txt", target / "Texts" / "a.txt", "text-shaped"),
            ],
            quarantine_moves=[
                (target / "b.doc", target / "_NoExtractor" / "doc" / "b.doc",
                 "no extractor registered for .doc"),
            ],
        )
        groupings = Groupings(
            items=(_grouping("Texts", ["f000"], subpath="Texts"),),
            overall_reason="",
        )
        report = router.route(
            catalog=catalog, groupings=groupings,
            target_path=target, backend=backend, logger=log, plan=plan,
        )
        # Skill called once for the single non-Misc category, never for quarantine
        assert backend.complete.call_count == 1
        assert len(report.categories) == 1
        assert len(report.quarantine) == 1

    def test_groups_keyed_by_bucket_and_ext_then_alphabetical(self, tmp_path):
        from unittest.mock import MagicMock

        from fda.organize import router
        from fda.organize.models import Catalog, Groupings

        log = _Logger()
        target = tmp_path
        # Quarantine: two .doc no_extractor + one .xls no_extractor + one .pdf failed
        plan = self._build_plan(
            target,
            quarantine_moves=[
                (target / "z.doc", target / "_NoExtractor" / "doc" / "z.doc", "n"),
                (target / "a.doc", target / "_NoExtractor" / "doc" / "a.doc", "n"),
                (target / "c.xls", target / "_NoExtractor" / "xls" / "c.xls", "n"),
                (target / "s.pdf", target / "_ExtractionFailed" / "pdf" / "s.pdf", "image-only"),
            ],
        )
        catalog = Catalog(target=str(target), entries=(), git_repos_skipped=())
        report = router.route(
            catalog=catalog, groupings=Groupings(items=(), overall_reason=""),
            target_path=target, backend=MagicMock(), logger=log, plan=plan,
        )
        # Groups: _NoExtractor first (doc, xls alphabetical), then _ExtractionFailed (pdf)
        assert [(g.bucket, g.ext) for g in report.quarantine] == [
            ("_NoExtractor", "doc"),
            ("_NoExtractor", "xls"),
            ("_ExtractionFailed", "pdf"),
        ]
        # Within the doc group: entries sorted by relative_path
        doc_group = report.quarantine[0]
        assert [e.relative_path for e in doc_group.entries] == [
            "_NoExtractor/doc/a.doc",
            "_NoExtractor/doc/z.doc",
        ]

    def test_fully_extractable_plan_emits_empty_quarantine_tuple(self, tmp_path):
        import json
        from unittest.mock import MagicMock

        from fda.organize import router
        from fda.organize.models import Catalog, CatalogEntry, Groupings

        backend = MagicMock()
        backend.complete.return_value = json.dumps(
            {"destination": "sharepoint", "reason": "ok", "misfits": []}
        )
        log = _Logger()
        target = tmp_path
        entry = CatalogEntry(
            path_id="f000",
            path=str(target / "Texts" / "a.txt"),
            ext=".txt", size_bytes=1, summary="ok", type_label="doc",
            is_junk=False, summary_failed=False, extract_status="ok",
        )
        catalog = Catalog(target=str(target), entries=(entry,), git_repos_skipped=())
        plan = self._build_plan(
            target,
            category_moves=[
                (target / "a.txt", target / "Texts" / "a.txt", "text-shaped"),
            ],
        )
        groupings = Groupings(
            items=(_grouping("Texts", ["f000"], subpath="Texts"),),
            overall_reason="",
        )
        report = router.route(
            catalog=catalog, groupings=groupings,
            target_path=target, backend=backend, logger=log, plan=plan,
        )
        assert report.quarantine == ()
```

The existing tests pass `plan=` as keyword `None` today (because they don't construct a plan). They'll break with `route()`'s new required `plan` kwarg unless we set a sensible default. Look at existing call sites for `route()` in `test_organize_router.py` (notably `TestRoute*` classes around line 296 onward) and add `plan=plan` to each call, constructing a minimal `Plan` from the test's existing operations. **For every existing `router.route(...)` call in the test file**, add `plan=<the same Plan the test sets up>` — most tests build a `Plan` already to pass through the executor; the ones that mock the executor must build a stub `Plan` via the `_build_plan` helper.

To minimize churn, also default `plan: Plan | None = None` in the production signature (Step 3 below) and synthesize an empty plan internally when `None`. That keeps existing tests passing without touching them.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py::TestQuarantineRouting -x -q --tb=short`
Expected: failures — `route()` doesn't accept `plan=`.

- [ ] **Step 3: Edit `fda/organize/router.py`**

(a) Update the imports (lines 21-33):

```python
from fda.organize.models import (
    Catalog,
    CatalogEntry,
    Destination,
    Groupings,
    Misfit,
    OperationKind,
    OperationOutcome,
    Plan,
    QUARANTINE_FAILED,
    QUARANTINE_NO_EXTRACTOR,
    QuarantineEntry,
    QuarantineGroup,
    RoutedCategory,
    RoutingReport,
    RoutingSignals,
)
```

(b) Add a helper above `route()` (after `_final_paths_from_outcomes`, before `route()`):

```python
_QUARANTINE_BUCKETS: frozenset[str] = frozenset({
    QUARANTINE_NO_EXTRACTOR, QUARANTINE_FAILED,
})


def _is_quarantine_dest(destination: str, target: Path) -> bool:
    try:
        rel = Path(destination).relative_to(target)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in _QUARANTINE_BUCKETS


def _build_quarantine_groups(
    plan: Plan, target: Path, logger: OrganizeLogger,
) -> tuple[QuarantineGroup, ...]:
    """Walk plan.operations, collect quarantine MOVEs, group by (bucket, ext)."""
    by_key: dict[tuple[str, str], list[QuarantineEntry]] = {}
    for op in plan.operations:
        if op.kind != OperationKind.MOVE or not op.destination:
            continue
        if not _is_quarantine_dest(op.destination, target):
            continue
        rel = Path(op.destination).relative_to(target)
        parts = rel.parts
        if len(parts) < 3:
            # Defensive: malformed quarantine destination (expected
            # bucket/ext/basename). Skip.
            continue
        bucket, ext = parts[0], parts[1]
        try:
            size = Path(op.destination).stat().st_size
        except OSError:
            # File may not be on disk in unit tests where the executor
            # didn't run — fall back to 0.
            size = 0
        by_key.setdefault((bucket, ext), []).append(QuarantineEntry(
            relative_path=str(rel),
            size_bytes=size,
            note=op.reason,
        ))

    bucket_order = {QUARANTINE_NO_EXTRACTOR: 0, QUARANTINE_FAILED: 1}
    groups: list[QuarantineGroup] = []
    for (bucket, ext) in sorted(
        by_key.keys(), key=lambda k: (bucket_order.get(k[0], 99), k[1]),
    ):
        entries = sorted(by_key[(bucket, ext)], key=lambda e: e.relative_path)
        groups.append(QuarantineGroup(bucket=bucket, ext=ext, entries=tuple(entries)))
        logger.log(
            "ROUTER_QUARANTINE_GROUP",
            bucket=bucket, ext=ext, count=len(entries),
        )
    return tuple(groups)
```

(c) Modify `route()` (line 273) to accept `plan` and build `QuarantineGroup`s. Replace the function head and the `RoutingReport(...)` construction at the bottom:

```python
def route(
    *,
    catalog: Catalog,
    groupings: Groupings,
    target_path: Path,
    backend,
    logger: OrganizeLogger,
    outcomes: tuple[OperationOutcome, ...] = (),
    plan: Plan | None = None,
) -> RoutingReport:
    """...existing docstring..."""
    skill = _skills.load_skill(_SKILL_DIR)
    entries_by_id = {e.path_id: e for e in catalog.entries}
    final_path_by_id = _final_paths_from_outcomes(catalog, outcomes)

    routed: list[RoutedCategory] = []
    logger.log("ROUTER_START", categories=len(groupings.items))

    # ... existing per-category loop unchanged ...

    quarantine_groups = (
        _build_quarantine_groups(plan, target_path, logger)
        if plan is not None else ()
    )

    report = RoutingReport(
        version="1.0",
        generated_at=_now_iso(),
        target_root=str(target_path),
        categories=tuple(routed),
        quarantine=quarantine_groups,
    )
    _write_json_report(report, target_path / "routing-report.json")
    _write_md_report(report, target_path / "routing-report.md")
    logger.log("ROUTER_DONE", categories=len(routed), quarantine=len(quarantine_groups))
    return report
```

(Defaulting `plan=None` keeps every existing test that calls `route()` without a plan working — they will simply see an empty `quarantine` tuple. Only the new TestQuarantineRouting tests rely on `plan=`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_router.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS. (Report writers are still pre-quarantine; they ignore the new field for now.)

- [ ] **Step 6: Commit**

```bash
git add fda/organize/router.py tests/test_organize_router.py
git commit -m "router(organize): partition plan MOVEs into category + quarantine groups

route() accepts plan=Plan | None, walks plan.operations, collects MOVEs
whose destinations live under _NoExtractor/ or _ExtractionFailed/ and
emits QuarantineGroup records keyed by (bucket, ext). The destination-
router skill is never invoked for quarantine moves. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 6: Report writers emit quarantine sections

**Files:**
- Modify: `fda/organize/router.py` (the `_write_json_report` and `_write_md_report` functions, around lines 398-481)
- Modify: `tests/test_routing_report.py`

JSON sidecar gains a top-level `quarantine` key (always present for schema stability, `[]` when empty). Markdown report appends two new sections — `## 건너뜀 — 추출기 없음 (No Extractor)` and `## 건너뜀 — 추출 실패 (Extraction Failed)` — when their bucket is non-empty, and the header summary line gains a skip count when any quarantine entries exist.

- [ ] **Step 1: Extend `tests/test_routing_report.py`**

Append new tests to the existing file:

```python
def test_json_report_always_includes_quarantine_key(tmp_path):
    """Even when empty, the quarantine key appears as []."""
    import json

    from fda.organize.models import RoutingReport
    from fda.organize.router import _report_to_dict, _write_json_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
    )
    out = tmp_path / "routing-report.json"
    _write_json_report(report, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["quarantine"] == []


def test_json_report_quarantine_shape_matches_spec(tmp_path):
    import json

    from fda.organize.models import (
        QuarantineEntry, QuarantineGroup, RoutingReport,
    )
    from fda.organize.router import _write_json_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
        quarantine=(
            QuarantineGroup(
                bucket="_NoExtractor", ext="doc",
                entries=(QuarantineEntry(
                    relative_path="_NoExtractor/doc/old-quote.doc",
                    size_bytes=24576,
                    note="no extractor registered for .doc",
                ),),
            ),
        ),
    )
    out = tmp_path / "routing-report.json"
    _write_json_report(report, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["quarantine"] == [
        {
            "bucket": "_NoExtractor",
            "ext": "doc",
            "entries": [
                {
                    "relative_path": "_NoExtractor/doc/old-quote.doc",
                    "size_bytes": 24576,
                    "note": "no extractor registered for .doc",
                },
            ],
        }
    ]


def test_md_report_omits_quarantine_sections_when_empty(tmp_path):
    from fda.organize.models import RoutingReport
    from fda.organize.router import _write_md_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")
    assert "건너뜀" not in md
    assert "건너뜀:" not in md  # header summary should not show skip count


def test_md_report_renders_both_quarantine_sections(tmp_path):
    from fda.organize.models import (
        QuarantineEntry, QuarantineGroup, RoutingReport,
    )
    from fda.organize.router import _write_md_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
        quarantine=(
            QuarantineGroup(
                bucket="_NoExtractor", ext="doc",
                entries=(
                    QuarantineEntry(
                        relative_path="_NoExtractor/doc/old-quote.doc",
                        size_bytes=24576,
                        note="no extractor registered for .doc",
                    ),
                ),
            ),
            QuarantineGroup(
                bucket="_ExtractionFailed", ext="pdf",
                entries=(
                    QuarantineEntry(
                        relative_path="_ExtractionFailed/pdf/scan.pdf",
                        size_bytes=180_000,
                        note="image-only PDF?",
                    ),
                ),
            ),
        ),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")
    # Header summary gains skip count
    assert "건너뜀: 2 (추출기 없음 1, 추출 실패 1)" in md
    # Sections present in fixed order
    assert "## 건너뜀 — 추출기 없음 (No Extractor)" in md
    assert "## 건너뜀 — 추출 실패 (Extraction Failed)" in md
    # Per-extension subheaders
    assert "### .doc (1개)" in md
    assert "### .pdf (1개)" in md
    # Per-file entries: backtick path + comma-grouped bytes + 바이트 + reason
    assert "`_NoExtractor/doc/old-quote.doc` (24,576 바이트) — no extractor registered for .doc" in md
    assert "`_ExtractionFailed/pdf/scan.pdf` (180,000 바이트) — image-only PDF?" in md


def test_md_report_header_summary_includes_skip_count_only_when_nonzero(tmp_path):
    from fda.organize.models import (
        CatalogEntry, RoutedCategory, RoutingReport,
    )
    from fda.organize.router import _aggregate_signals, _write_md_report

    entry = CatalogEntry(
        path_id="f000", path="/tmp/x/a.txt",
        ext=".txt", size_bytes=10, summary="s", type_label="doc",
        is_junk=False, summary_failed=False, extract_status="ok",
    )
    sig = _aggregate_signals([entry])
    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(
            RoutedCategory(
                name="Texts", subpath="Texts", destination="sharepoint",
                reason="r", low_confidence=False, signals=sig, misfits=(),
            ),
        ),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")
    assert "건너뜀:" not in md  # no quarantine → no skip count segment
    assert "총 카테고리: 1 · 총 파일: 1" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routing_report.py -x -q --tb=short`
Expected: failures — current writers don't know about `quarantine`.

- [ ] **Step 3: Edit `fda/organize/router.py` — extend `_report_to_dict` and `_write_md_report`**

(a) Update `_report_to_dict` (line 398) to emit the `quarantine` key:

```python
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
    }
```

(b) Update `_write_md_report` (line 439) to add the skip-count segment in the header and the two new sections at the bottom:

```python
def _write_md_report(report: RoutingReport, path: Path) -> None:
    total_files = sum(c.signals.file_count for c in report.categories)
    no_ext = next(
        (g for g in report.quarantine if g.bucket == QUARANTINE_NO_EXTRACTOR), None,
    )
    failed = next(
        (g for g in report.quarantine if g.bucket == QUARANTINE_FAILED), None,
    )
    no_ext_groups = [g for g in report.quarantine if g.bucket == QUARANTINE_NO_EXTRACTOR]
    failed_groups = [g for g in report.quarantine if g.bucket == QUARANTINE_FAILED]
    no_ext_count = sum(len(g.entries) for g in no_ext_groups)
    failed_count = sum(len(g.entries) for g in failed_groups)
    total_skipped = no_ext_count + failed_count

    lines: list[str] = []
    lines.append("# 라우팅 보고서")
    lines.append("")
    lines.append(f"생성 시각: {report.generated_at}")
    lines.append(f"대상 루트: {report.target_root}")
    summary = f"총 카테고리: {len(report.categories)} · 총 파일: {total_files}"
    if total_skipped > 0:
        summary += f" · 건너뜀: {total_skipped} (추출기 없음 {no_ext_count}, 추출 실패 {failed_count})"
    lines.append(summary)
    lines.append("")
    lines.append("## 카테고리별 라우팅")
    lines.append("")
    for c in report.categories:
        # ... existing per-category block unchanged ...
        lines.append(f"### {c.subpath}")
        lines.append("")
        suffix = " (낮은 신뢰도)" if c.low_confidence else ""
        lines.append(f"- 대상: {c.destination}{suffix}")
        lines.append(f"- 파일 수: {c.signals.file_count}")
        if c.reason:
            lines.append(f"- 이유: {c.reason}")
        ext_str = ", ".join(
            f"{ext} ({n})" for ext, n in c.signals.extension_distribution
        ) or "(없음)"
        lines.append(f"- 확장자 분포: {ext_str}")
        lines.append(f"- 총 용량(바이트): {c.signals.total_size_bytes:,}")
        lines.append(f"- 표 형식 일관성: {c.signals.tabular_schema_consistent}")
        lines.append(f"- 전체 추출 실패: {c.signals.all_extraction_failed}")
        lines.append("")
        if c.misfits:
            lines.append("### 불일치 파일")
            lines.append("")
            for m in c.misfits:
                lines.append(
                    f"- `{m.relative_path}` → "
                    f"{m.suggested_destination} — {m.reason}"
                )
            lines.append("")

    if no_ext_groups:
        lines.append("## 건너뜀 — 추출기 없음 (No Extractor)")
        lines.append("")
        lines.append(f"총 {no_ext_count}개 파일 · 추출기 등록 시 일반 카테고리로 흐름")
        lines.append("")
        for g in no_ext_groups:
            lines.append(f"### .{g.ext} ({len(g.entries)}개)")
            for e in g.entries:
                lines.append(
                    f"- `{e.relative_path}` ({e.size_bytes:,} 바이트) — {e.note}"
                )
            lines.append("")

    if failed_groups:
        lines.append("## 건너뜀 — 추출 실패 (Extraction Failed)")
        lines.append("")
        lines.append(f"총 {failed_count}개 파일 · 파일 자체 문제로 본문을 읽지 못함")
        lines.append("")
        for g in failed_groups:
            lines.append(f"### .{g.ext} ({len(g.entries)}개)")
            for e in g.entries:
                lines.append(
                    f"- `{e.relative_path}` ({e.size_bytes:,} 바이트) — {e.note}"
                )
            lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

(Delete the now-unused `no_ext` / `failed` `next(...)` lookups — they're replaced by the `no_ext_groups` / `failed_groups` filtered lists.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_routing_report.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/router.py tests/test_routing_report.py
git commit -m "router(organize): emit quarantine sections in JSON + Markdown reports

JSON sidecar always includes a top-level quarantine key (empty list when
no skips). Markdown report appends '## 건너뜀 — 추출기 없음 (No Extractor)'
and '## 건너뜀 — 추출 실패 (Extraction Failed)' sections in fixed order
when their bucket is non-empty, and the header summary line gains a
skip count when any files were quarantined. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 7: Orchestrator threads quarantine through plan_builder + router

**Files:**
- Modify: `fda/organize/__init__.py`
- Modify: `tests/test_organize_pipeline.py`

`organize()` is the only caller that wires the full pipeline. It must compute the extractable + quarantine subsets, narrow `path_by_id` to extractable, pass `quarantine=` to `plan_builder.build()`, and pass `plan=` to `router.route()`.

- [ ] **Step 1: Add integration tests in `tests/test_organize_pipeline.py`**

The existing file (line 16) defines a `workspace` fixture that ALREADY creates `.txt`, `.docx`, `.xlsx`, `.pptx`, `.csv`, `.hwpx`, and `.DS_Store` files — all extractable except the junk file. The existing tests use the `_scripted_backend(workspace)` helper (line 74), NOT a `mock_backend` fixture. The new tests must follow that pattern.

For the all-quarantine case, define a dedicated `quarantine_only_workspace` fixture inside the new test class (it must NOT reuse the shared `workspace` fixture, which would mix in extractable files and defeat the test).

Append a new test class to the end of the file:

```python
class TestQuarantineEndToEnd:
    """End-to-end: mixed extractable + quarantine corpus lands the
    quarantine files under _NoExtractor/<ext>/ and the report shows
    them as Skipped sections."""

    @pytest.fixture
    def quarantine_only_workspace(self, tmp_path):
        """Workspace whose every file lacks a registered extractor.

        Deliberately does NOT use the shared `workspace` fixture (which
        creates extractable .txt/.docx/.xlsx/.pptx/.csv/.hwpx) — this
        test needs the classifier's empty-extractable early-return path.
        """
        root = tmp_path / "ws-q"
        root.mkdir()
        (root / "a.doc").write_bytes(b"a")
        (root / "b.xls").write_bytes(b"b")
        return root

    def test_unsupported_extension_moved_to_NoExtractor_bucket(self, workspace):
        from fda.organize import organize

        # `workspace` fixture already created extractable files (.txt, .docx,
        # .xlsx, .pptx, .csv, .hwpx). Add one unsupported-extension file so
        # the test exercises both paths in the same run.
        (workspace / "old-quote.doc").write_bytes(b"fake doc bytes")
        backend = _scripted_backend(workspace)
        # Router is invoked because route=True (default) — chain a router
        # response onto the scripted backend, mirroring how TestOrganizeRouting
        # at test_organize_pipeline.py:178-192 does it.
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
            str(workspace),
            instructions="",
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        # The .doc goes to quarantine; extractable files keep flowing through
        # their normal categories (assertions for those already live in
        # TestOrganize.test_full_pipeline above).
        assert (workspace / "_NoExtractor" / "doc" / "old-quote.doc").exists()
        md = (workspace / "routing-report.md").read_text(encoding="utf-8")
        assert "## 건너뜀 — 추출기 없음 (No Extractor)" in md
        assert "`_NoExtractor/doc/old-quote.doc`" in md
        data = json.loads(
            (workspace / "routing-report.json").read_text(encoding="utf-8"),
        )
        assert any(g["bucket"] == "_NoExtractor" and g["ext"] == "doc"
                   for g in data["quarantine"])

    def test_all_quarantine_corpus_does_not_crash(self, quarantine_only_workspace):
        """No extractable files at all → classifier short-circuits, plan_builder
        emits only quarantine MOVEs, router emits only Skipped sections."""
        from fda.organize import organize

        workspace = quarantine_only_workspace
        # The classifier short-circuits before any LLM call, so the scripted
        # backend never sees a summarizer/Stage A/Stage B payload. A bare
        # MagicMock with a never-called .complete is enough here.
        backend = MagicMock()
        organize(
            str(workspace),
            instructions="",
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert backend.complete.call_count == 0
        assert (workspace / "_NoExtractor" / "doc" / "a.doc").exists()
        assert (workspace / "_NoExtractor" / "xls" / "b.xls").exists()
        md = (workspace / "routing-report.md").read_text(encoding="utf-8")
        assert "건너뜀: 2 (추출기 없음 2, 추출 실패 0)" in md
```

Note: `_scripted_backend`, `MagicMock`, `json`, and `pytest` are already imported at the top of `tests/test_organize_pipeline.py` (lines 6-13). Do NOT duplicate those imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_organize_pipeline.py::TestQuarantineEndToEnd -x -q --tb=short`
Expected: failures — `organize()` doesn't thread quarantine yet.

- [ ] **Step 3: Edit `fda/organize/__init__.py` — partition catalog + thread quarantine**

Locate the catalog → path_by_id → plan_builder block (currently around lines 64-79). Replace it with:

```python
        catalog = reader.read(target_path, backend=backend, logger=olog)
        groupings = classifier.classify(
            catalog, instructions, backend=backend, logger=olog,
        )
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

And update the `router.route(...)` call (around lines 159-167) to pass `plan=`:

```python
        if route:
            try:
                router.route(
                    catalog=catalog,
                    groupings=groupings,
                    target_path=target_path,
                    backend=backend,
                    logger=olog,
                    outcomes=result.outcomes,
                    plan=plan,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("router stage failed: %s", e, exc_info=True)
                olog.log("ROUTER_FAIL_FATAL", error=str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_organize_pipeline.py -x -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/__init__.py tests/test_organize_pipeline.py
git commit -m "organize: thread quarantine catalog entries through plan_builder + router

organize() now partitions catalog.entries into extractable, quarantine,
and junk subsets. path_by_id covers only the extractable subset (matching
what the classifier saw); quarantine entries flow as a separate kwarg
to plan_builder.build() and the produced plan flows into router.route()
so the router can emit Skipped sections in the report. Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

---

## Task 8: Regression sweep + manual eyeball

**Files:**
- Modify: any existing test asserting `.doc/.xls/.ppt/.html` land in `Misc/` (likely `tests/test_organize_pipeline.py`)
- No code changes expected — this is a verification task.

The 2026-05-12 mixed-corpus fixture under `/private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12/` is the integration eyeball.

- [ ] **Step 1: Sweep for legacy-format expectations**

Run: `grep -nr "\.doc\b\|\.xls\b\|\.ppt\b\|\.html\b\|Misc.*\(doc\|xls\|ppt\|html\)" tests/ | grep -v "\.docx\|\.xlsx\|\.pptx" | head -40`

For each match, decide whether the test asserted Misc/ placement for an unsupported format. If so, update it to expect `_NoExtractor/<ext>/`. If the test fixture only used `.docx` / `.xlsx` / `.pptx` (extractable formats), it doesn't need changes.

- [ ] **Step 2: Run the full suite again to confirm green after sweep**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS, 113+ tests (113 baseline + new tests added across Tasks 1–7).

- [ ] **Step 3: Manual eyeball on the 2026-05-12 fixture**

The fixture is read-only by convention — make a working copy first.

```bash
cp -R /private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12 \
      /private/tmp/fda-test-sets/honesty-eyeball-2026-05-13
.venv/bin/python -m fda.cli organize /private/tmp/fda-test-sets/honesty-eyeball-2026-05-13
```

Open `/private/tmp/fda-test-sets/honesty-eyeball-2026-05-13/routing-report.md` and verify:

- The header summary line includes `· 건너뜀: N (...)` if any legacy-format or extraction-failed files were in the fixture.
- A `## 건너뜀 — 추출기 없음 (No Extractor)` section exists with `### .doc`, `### .xls`, `### .ppt`, `### .html` subheaders depending on what was in the fixture.
- Each file line uses `` `_NoExtractor/<ext>/<name>` (N,NNN 바이트) — no extractor registered for .<ext>`` format.
- The previously-fabricated content reasons that landed legacy-format files in `Misc/` are gone — there is no `Misc/` bucket containing `.doc` files with a content-sounding reason.
- The corresponding files exist on disk under `_NoExtractor/<ext>/`.

If anything doesn't match, fix the underlying code and update tests — do not just patch the fixture.

- [ ] **Step 4: Commit any regression-sweep edits (if Step 1 found tests to update)**

```bash
git add tests/
git commit -m "tests(organize): update Misc-targeted assertions to _NoExtractor/

Tests that previously expected legacy-format files (.doc/.xls/.ppt/.html)
to land in Misc/ now expect the honesty layer to route them under
_NoExtractor/<ext>/. Companion to the extractor-coverage-honesty change.
Spec:
docs/superpowers/specs/2026-05-13-extractor-coverage-honesty-design.md"
```

(If Step 1 found nothing to update, skip this commit.)

- [ ] **Step 5: Update memory**

Once the eyeball passes, the next session's "what's next on FDA" lookup should point at item #3 (system/manifest files), not still #2. Update memory:

- Edit `~/.claude/projects/-Users-hogyeongkim-Desktop-Projects-FDA-FDA/memory/project_organize_test_followups.md`. Move item #2 from "Pending" to "Status:" with a SHIPPED line citing the spec, plan, and commits. Renumber pending items as needed.

This is bookkeeping, not code — no commit required (memory files are outside the repo).

---

## Notes on order

Tasks 1–7 are TDD: each adds tests that fail, then the production code that makes them pass, then a commit. Task 8 is a regression sweep + manual eyeball. The tasks are independently committable — every commit boundary leaves the test suite green.

The only subtle ordering constraint is that Task 5 (router) defaults `plan=None` so existing tests that pass `route()` without a plan keep working; Task 7 then routes a real `plan` through the orchestrator. Task 6 (report writers) can technically land before Task 5 — but Task 5's tests rely on the writers handling a non-empty `quarantine` tuple, so 6 after 5 keeps that simpler.

Per memory `feedback_chunk_scope_binding`: execute every task. Do not silently narrow.
