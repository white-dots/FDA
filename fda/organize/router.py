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
