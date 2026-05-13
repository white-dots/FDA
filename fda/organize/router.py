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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fda.organize import _skills
from fda.organize._logger import OrganizeLogger
from fda.organize.models import (
    Catalog,
    CatalogEntry,
    Destination,
    Groupings,
    Misfit,
    OperationKind,
    OperationOutcome,
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
    raw: str, *, sample_path_ids: set[str], chosen_destination: str | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Parse + validate the skill's JSON response. Raises RouterError on any
    structural problem. Returns (destination, reason, misfits).

    `sample_path_ids` must be the IDs of files actually shown to the model
    (i.e. the output of `_sample_entries`), not the full category. The model
    cannot validly reference path_ids it never saw.
    """
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
        if pid not in sample_path_ids:
            raise RouterError(f"misfit path_id {pid!r} not in sample")
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
    sample_ids = {e.path_id for e in _sample_entries(entries)}
    return _validate_router_response(raw, sample_path_ids=sample_ids)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


def _final_paths_from_outcomes(
    catalog: Catalog,
    outcomes: tuple[OperationOutcome, ...],
) -> dict[str, str]:
    """Map path_id → on-disk path after the executor ran.

    The catalog's `CatalogEntry.path` records the pre-move location. For files
    the executor successfully moved, the post-move location lives on the
    matching MOVE outcome's `destination`. Files that were skipped or failed
    keep their original path (the executor left them alone).
    """
    final: dict[str, str] = {e.path_id: e.path for e in catalog.entries}
    if not outcomes:
        return final
    id_by_source: dict[str, str] = {e.path: e.path_id for e in catalog.entries}
    for o in outcomes:
        op = o.operation
        if op.kind != OperationKind.MOVE:
            continue
        if o.status not in ("applied", "rescued"):
            continue
        if not op.source or not op.destination:
            continue
        pid = id_by_source.get(op.source)
        if pid is not None:
            final[pid] = op.destination
    return final


def route(
    *,
    catalog: Catalog,
    groupings: Groupings,
    target_path: Path,
    backend,
    logger: OrganizeLogger,
    outcomes: tuple[OperationOutcome, ...] = (),
) -> RoutingReport:
    """Top-level router. Iterate per-category groupings, short-circuit or
    invoke the destination-router skill, resolve misfit paths, build the
    in-memory RoutingReport. Report files are written by the caller (see
    Task 6).

    `outcomes` carries executor results so misfit `relative_path` reflects
    the post-move on-disk layout. When empty (e.g. unit tests that bypass
    the executor), the router falls back to the catalog's pre-move paths.
    """
    skill = _skills.load_skill(_SKILL_DIR)
    entries_by_id = {e.path_id: e for e in catalog.entries}
    final_path_by_id = _final_paths_from_outcomes(catalog, outcomes)

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
            # Note: Grouping carries `category`, `subpath`, `file_ids`,
            # `reason` (== TaxonomyCategory.criteria). It does NOT carry
            # the TaxonomyCategory.description. Routing v1 sends an empty
            # description; the skill prompt still has category_name +
            # subpath + criteria, which carry most of the routing signal.
            # If Task 9 (corpus eyeballing) reveals decisions suffer from
            # the missing description, the v2 follow-up is to thread
            # Taxonomy through classify() → organize() → route().
            destination, reason, raw_misfits = _route_one_category(
                category_name=g.category,
                description="",
                criteria=g.reason,
                subpath=g.subpath,
                signals=signals,
                entries=category_entries,
                backend=backend, skill=skill, logger=logger,
            )
        except RouterError as e:
            logger.log("ROUTER_FAIL", category=g.category, error=str(e))
            raise

        misfit_records: list[Misfit] = []
        for m in raw_misfits:
            entry_path = final_path_by_id.get(
                m["path_id"], entries_by_id[m["path_id"]].path,
            )
            try:
                rel = str(Path(entry_path).relative_to(target_path))
            except ValueError:
                # Entry path isn't under target_path (file's MOVE was
                # skipped/failed and its source lives elsewhere, symlink
                # resolved outside the tree, etc.). Skip this misfit but
                # keep the category — losing one annotation is better than
                # losing the entire routing stage.
                logger.log(
                    "ROUTER_MISFIT_SKIP",
                    category=g.category, path_id=m["path_id"], path=entry_path,
                )
                continue
            misfit_records.append(Misfit(
                path_id=m["path_id"],
                relative_path=rel,
                suggested_destination=m["suggested_destination"],
                reason=m["reason"],
            ))
        misfits = tuple(misfit_records)
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
    _write_json_report(report, target_path / "routing-report.json")
    _write_md_report(report, target_path / "routing-report.md")
    logger.log("ROUTER_DONE", categories=len(routed))
    return report


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
    total_files = sum(c.signals.file_count for c in report.categories)
    lines: list[str] = []
    lines.append("# 라우팅 보고서")
    lines.append("")
    lines.append(f"생성 시각: {report.generated_at}")
    lines.append(f"대상 루트: {report.target_root}")
    lines.append(
        f"총 카테고리: {len(report.categories)} · 총 파일: {total_files}"
    )
    lines.append("")
    lines.append("## 카테고리별 라우팅")
    lines.append("")
    for c in report.categories:
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
        lines.append(
            f"- 표 형식 일관성: {c.signals.tabular_schema_consistent}"
        )
        lines.append(
            f"- 전체 추출 실패: {c.signals.all_extraction_failed}"
        )
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
