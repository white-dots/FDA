# fda/organize/router.py
"""Cloud-destination router pipeline stage.

After the classifier + plan_builder + executor have organized files on disk,
this stage iterates the resulting per-category groupings and recommends a
cloud destination (SharePoint / S3 / RDBMS) for each one. No files are
uploaded or moved; the output is a JSON + Markdown sidecar pair at the root
of the organized tree.
"""
from __future__ import annotations

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
