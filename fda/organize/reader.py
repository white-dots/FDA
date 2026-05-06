# fda/organize/reader.py
"""Catalog-building stage.

Walks `target` once, skips git worktrees and symlinks, dispatches one
file-summarizer Haiku call per non-junk file in a thread pool, and returns
a deterministically-ordered Catalog with stable path_ids.

Per-file timeout and total wall-clock cap are enforced at the LLM-backend
HTTP layer (request timeouts), not via Future.cancel(): cancel is unreliable
for already-running threads. The backend timeout is what severs in-flight
calls.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from fda.organize import _extractors, _fs, _skills
from fda.organize._logger import OrganizeLogger
from fda.organize.models import Catalog, CatalogEntry, ExtractionResult

logger = logging.getLogger(__name__)

READER_TEXT_CAP_BYTES = 64 * 1024
READER_PER_FILE_TIMEOUT_SECONDS = 30
READER_TOTAL_TIMEOUT_SECONDS = 300
READER_WORKER_COUNT = 8

_TRUNCATE_MARKER = "[TRUNCATED at 64KB]\n"
_SKILL_DIR = Path(__file__).parent / "skills" / "file-summarizer"


def _walk(target: Path) -> tuple[list[Path], list[str]]:
    """Walk `target` once. Returns (files, git_repos_skipped)."""
    files: list[Path] = []
    skipped: list[str] = []
    stack = [target]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError as e:
            logger.debug("could not iterdir %s: %s", d, e)
            continue
        if any(c.name == ".git" for c in entries):
            skipped.append(str(d))
            continue
        for c in entries:
            if c.is_symlink():
                logger.debug("skipping symlink: %s", c)
                continue
            if c.is_dir():
                stack.append(c)
            elif c.is_file():
                files.append(c)
    return files, skipped


def _build_user_message(path: Path, ext_text: ExtractionResult, size_bytes: int) -> str:
    if ext_text.status == "ok" and ext_text.text is not None:
        text = ext_text.text
        if len(text.encode("utf-8")) > READER_TEXT_CAP_BYTES:
            # Truncate by bytes, then redecode safely.
            encoded = text.encode("utf-8")[:READER_TEXT_CAP_BYTES]
            text = encoded.decode("utf-8", errors="replace")
            text = _TRUNCATE_MARKER + text
        body = text
    elif ext_text.status == "no_extractor":
        body = (
            f"(stub: no_extractor; classify by filename/extension/size — "
            f"binary or unsupported format, size={size_bytes} bytes)"
        )
    elif ext_text.status == "tool_missing":
        body = f"(stub: tool_missing — {ext_text.note}; size={size_bytes} bytes)"
    else:
        body = f"(stub: extraction failed — {ext_text.note}; size={size_bytes} bytes)"
    return (
        f"PATH: {path}\n"
        f"EXT: {path.suffix.lower()}\n"
        f"SIZE_BYTES: {size_bytes}\n"
        f"---\n"
        f"{body}\n"
    )


def _summarize_one(
    path: Path,
    *,
    backend,
    skill: _skills.SkillConfig,
    timeout_seconds: float,
) -> tuple[CatalogEntry, str, str]:
    """Returns (entry, log_event_kind, detail).

    log_event_kind is 'done', 'timeout', or 'fail'.
    detail carries a human-readable description for failure logs.
    """
    size = path.stat().st_size
    extraction = _extractors.extract(path)
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
        return _fail_entry(path, size, extraction.status, str(e)), "timeout", str(e)
    except Exception as e:  # noqa: BLE001 — never abort a run because one file fails
        return _fail_entry(path, size, extraction.status, str(e)), "fail", str(e)

    try:
        parsed = json.loads(raw)
        type_label = str(parsed.get("type_label", ""))[:32]
        summary = str(parsed.get("summary", ""))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return (
            _fail_entry(path, size, extraction.status, "unparseable summary"),
            "fail",
            "unparseable summary",
        )

    return (
        CatalogEntry(
            path_id="",  # filled in by caller after global sort
            path=str(path),
            ext=path.suffix.lower(),
            size_bytes=size,
            summary=summary,
            type_label=type_label,
            is_junk=False,
            summary_failed=False,
            extract_status=extraction.status,
        ),
        "done",
        "",
    )


def _fail_entry(path: Path, size: int, extract_status: str, _why: str) -> CatalogEntry:
    return CatalogEntry(
        path_id="",
        path=str(path),
        ext=path.suffix.lower(),
        size_bytes=size,
        summary="",
        type_label="",
        is_junk=False,
        summary_failed=True,
        extract_status=extract_status,
    )


def _junk_entry(path: Path) -> CatalogEntry:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return CatalogEntry(
        path_id="",
        path=str(path),
        ext=path.suffix.lower(),
        size_bytes=size,
        summary="",
        type_label="junk",
        is_junk=True,
        summary_failed=False,
        extract_status="no_extractor",
    )


def read(
    target: Path,
    *,
    backend,
    logger: OrganizeLogger,
) -> Catalog:
    target = Path(target).resolve()
    files, skipped = _walk(target)

    skill = _skills.load_skill(_SKILL_DIR)

    junks = [p for p in files if _fs.is_junk_file(p)]
    real = [p for p in files if not _fs.is_junk_file(p)]

    logger.log("READER_START", files=len(files), real=len(real), junk=len(junks))

    entries_by_path: dict[str, CatalogEntry] = {}
    for j in junks:
        entries_by_path[str(j)] = _junk_entry(j)

    deadline = time.monotonic() + READER_TOTAL_TIMEOUT_SECONDS

    def _worker(p: Path) -> tuple[CatalogEntry, str, int, str]:
        """Per-file worker. Recomputes the timeout when the worker
        actually picks up the task — so a file that sat in the queue past
        the deadline returns a deadline-failed entry rather than running.

        Returns (entry, kind, elapsed_ms, detail).
        """
        t0 = time.monotonic()
        remaining = max(0.0, deadline - t0)
        if remaining <= 0:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return _fail_entry(p, size, "failed", "deadline"), "deadline", elapsed_ms, "deadline"
        timeout_for_call = min(float(READER_PER_FILE_TIMEOUT_SECONDS), remaining)
        entry, kind, detail = _summarize_one(
            p, backend=backend, skill=skill, timeout_seconds=timeout_for_call,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return entry, kind, elapsed_ms, detail

    with ThreadPoolExecutor(max_workers=READER_WORKER_COUNT) as ex:
        futures = {ex.submit(_worker, p): p for p in real}

        for fut in as_completed(futures):
            p = futures[fut]
            try:
                entry, kind, elapsed_ms, detail = fut.result()
            except Exception as e:  # TOCTOU defense (F1) — e.g., file vanished mid-walk
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                entry = _fail_entry(p, size, "failed", str(e))
                kind = "fail"
                elapsed_ms = 0
                detail = f"worker raised: {e}"
            entries_by_path[str(p)] = entry
            if kind == "done":
                logger.log(
                    "READER_FILE_DONE",
                    path=str(p),
                    summary=entry.summary,
                    extract_status=entry.extract_status,
                    elapsed_ms=elapsed_ms,
                )
            else:
                if kind == "deadline":
                    logger.log("READER_DEADLINE", path=str(p), remaining=0)
                logger.log(
                    "READER_FILE_FAIL",
                    path=str(p),
                    reason=kind,
                    detail=detail,
                    extract_status=entry.extract_status,
                    elapsed_ms=elapsed_ms,
                )

    # Deterministic ordering by absolute path; assign stable path_ids.
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
        )
        for idx, e in enumerate(ordered)
    )
    ok = sum(1 for e in finalized if not e.summary_failed and not e.is_junk)
    failed = sum(1 for e in finalized if e.summary_failed)
    logger.log("READER_END", ok=ok, failed=failed, total=len(finalized))
    return Catalog(
        target=str(target),
        entries=finalized,
        git_repos_skipped=tuple(skipped),
    )
