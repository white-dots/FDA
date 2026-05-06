# fda/organize/classifier.py
"""Two-stage Sonnet classifier.

Public:
    classify(catalog, instructions, *, backend, logger) -> Groupings

Internal flow:
  1. Validate input (catalog, failed-summary threshold).
  2. Stage A: Taxonomy Proposer (one Sonnet call) on a stratified sample.
  3. Stage B: Assigner — token-bounded batches, ≤4 concurrent, with retry.
  4. Optional refinement: if fallback rate is too high, re-run Stage A and
     re-run Stage B over the entire catalog with the refined taxonomy.
  5. Deterministic merge of assignments into Groupings.

The model never emits absolute paths; Stage A returns categories only and
Stage B returns `path_id` strings.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fda.organize import _skills
from fda.organize._logger import OrganizeLogger
from fda.organize.models import (
    Catalog,
    CatalogEntry,
    Grouping,
    Groupings,
    Taxonomy,
    TaxonomyCategory,
)

logger = logging.getLogger(__name__)

TAXONOMY_SAMPLE_FULL_THRESHOLD = 150
TAXONOMY_SAMPLE_TARGET_SIZE = 100
TAXONOMY_SAMPLE_FALLBACK_BUDGET = 100
ASSIGNER_BATCH_TARGET_TOKENS = 45_000
MAX_ASSIGNER_INPUT_TOKENS = 50_000
MAX_CLASSIFIER_CONCURRENCY = 4
MAX_FALLBACK_RATE = 0.20
MIN_FALLBACK_REFINE_COUNT = 25
MAX_TAXONOMY_REFINEMENTS = 1
ASSIGNER_BAD_RESPONSE_FALLBACK_RATE = 0.02
ASSIGNER_BAD_RESPONSE_FALLBACK_MAX = 10
CLASSIFIER_INPUT_TOKEN_BUDGET = 60_000
READER_FAILED_SUMMARY_THRESHOLD = 0.25
SUMMARY_TRUNCATE_CHARS = 200

_PROPOSER_SKILL_DIR = Path(__file__).parent / "skills" / "taxonomy-proposer"
_ASSIGNER_SKILL_DIR = Path(__file__).parent / "skills" / "taxonomy-assigner"


class ClassifierError(Exception):
    """Raised when Stage A produces an invalid taxonomy after retry."""


class ClassifierBatchError(Exception):
    """Raised when Stage B produces unrecoverable batch errors."""


class ClassifierOverflowError(Exception):
    """Raised when the Stage A serialized payload exceeds the input budget."""


class ClassifierUnreliableInputError(Exception):
    """Raised when too many catalog entries have failed summaries to classify."""


# ---- helpers -----------------------------------------------------------

def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _truncate_summary(s: str) -> str:
    if len(s) > SUMMARY_TRUNCATE_CHARS:
        return s[:SUMMARY_TRUNCATE_CHARS] + "…"
    return s


def _entry_dict(e: CatalogEntry) -> dict[str, Any]:
    return {
        "path_id": e.path_id,
        "path": e.path,
        "ext": e.ext,
        "size_bytes": e.size_bytes,
        "summary": _truncate_summary(e.summary) if e.summary
                   else "summary unavailable",
        "type_label": e.type_label,
        "extract_status": e.extract_status,
        "verbatim_head": e.verbatim_head,
    }


def _is_rate_limit(e: BaseException) -> bool:
    code = getattr(e, "status_code", None)
    if code == 429:
        return True
    return "rate" in str(e).lower() and "limit" in str(e).lower()


# ---- sampling ---------------------------------------------------------

def _sample_for_taxonomy(
    entries: list[CatalogEntry], target: str,
) -> list[CatalogEntry]:
    if len(entries) <= TAXONOMY_SAMPLE_FULL_THRESHOLD:
        return list(entries)

    target_path = Path(target)
    chosen: dict[str, CatalogEntry] = {}

    def _add(e: CatalogEntry) -> bool:
        """Add `e` if not already chosen and budget allows. Returns True if added."""
        if len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
            return False
        if e.path_id in chosen:
            return False
        chosen[e.path_id] = e
        return True

    # 1) Up to 2 entries per unique extension.
    by_ext: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        by_ext.setdefault(e.ext, []).append(e)
    for ext, group in sorted(by_ext.items()):
        if len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
            break
        for e in group[:2]:
            _add(e)

    # 2) Up to 2 entries per unique top-level directory UNDER TARGET.
    def _top_level(e: CatalogEntry) -> str:
        try:
            rel = Path(e.path).relative_to(target_path)
        except ValueError:
            return ""
        parts = rel.parts
        return parts[0] if len(parts) > 1 else ""
    by_top: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        by_top.setdefault(_top_level(e), []).append(e)
    for top, group in sorted(by_top.items()):
        if len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
            break
        for e in group[:2]:
            _add(e)

    # 3) Up to 1 per unique leaf directory until 70 slots filled.
    by_leaf: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        by_leaf.setdefault(str(Path(e.path).parent), []).append(e)
    for leaf, group in sorted(by_leaf.items()):
        if len(chosen) >= 70:
            break
        _add(group[0])

    # 4) Up to 10 failed summaries.
    failed = [e for e in entries if e.summary_failed or e.extract_status != "ok"]
    failed_added = 0
    for e in failed:
        if failed_added >= 10 or len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
            break
        if _add(e):
            failed_added += 1

    # 5) Up to 5 largest files.
    largest = sorted(entries, key=lambda e: -e.size_bytes)[:5]
    for e in largest:
        if len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
            break
        _add(e)

    # 6) Fill remainder by evenly-spaced indexes across the sorted catalog.
    if len(chosen) < TAXONOMY_SAMPLE_TARGET_SIZE:
        remaining = [e for e in entries if e.path_id not in chosen]
        need = TAXONOMY_SAMPLE_TARGET_SIZE - len(chosen)
        if remaining and need > 0:
            step = max(1, len(remaining) // need)
            for i in range(0, len(remaining), step):
                if len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
                    break
                _add(remaining[i])

    return sorted(chosen.values(), key=lambda e: e.path_id)


# ---- Stage A -----------------------------------------------------------

def _build_proposer_prompt(
    sample: list[CatalogEntry], instructions: str, extra_exemplars: list[CatalogEntry] | None = None
) -> str:
    payload = {
        "USER_INSTRUCTIONS": instructions,
        "CATALOG": [_entry_dict(e) for e in sample],
    }
    if extra_exemplars:
        payload["FALLBACK_EXEMPLARS"] = [_entry_dict(e) for e in extra_exemplars]
    return json.dumps(payload, ensure_ascii=False)


def _validate_taxonomy_payload(raw: str) -> Taxonomy:
    parsed = json.loads(raw)
    cats_raw = parsed.get("categories")
    fb_raw = parsed.get("fallback_category")
    if not isinstance(cats_raw, list) or not cats_raw:
        raise ClassifierError("taxonomy must include at least one category")
    if not isinstance(fb_raw, dict):
        raise ClassifierError("taxonomy missing fallback_category")
    seen_names: set[str] = set()
    cats: list[TaxonomyCategory] = []
    for c in cats_raw:
        name = c.get("category_name")
        sub = c.get("subpath")
        if not isinstance(name, str) or not name:
            raise ClassifierError("category missing category_name")
        if name in seen_names:
            raise ClassifierError(f"duplicate category_name: {name!r}")
        seen_names.add(name)
        if not isinstance(sub, str) or not sub:
            raise ClassifierError(f"category {name!r} missing subpath")
        if ".." in Path(sub).parts:
            raise ClassifierError(f"subpath traversal: {sub!r}")
        cats.append(TaxonomyCategory(
            category_name=name,
            subpath=sub,
            description=str(c.get("description", "")),
            criteria=str(c.get("criteria", "")),
        ))
    fb_name = fb_raw.get("category_name")
    fb_sub = fb_raw.get("subpath")
    if not isinstance(fb_name, str) or not fb_name:
        raise ClassifierError("fallback_category missing category_name")
    if not isinstance(fb_sub, str) or not fb_sub:
        raise ClassifierError("fallback_category missing subpath")
    if ".." in Path(fb_sub).parts:
        raise ClassifierError(f"fallback subpath traversal: {fb_sub!r}")
    if fb_name in seen_names:
        raise ClassifierError(
            f"fallback_category name {fb_name!r} collides with a regular category"
        )
    fallback = TaxonomyCategory(
        category_name=fb_name,
        subpath=fb_sub,
        description=str(fb_raw.get("description", "")),
        criteria=str(fb_raw.get("criteria", "")),
    )
    return Taxonomy(categories=tuple(cats), fallback_category=fallback)


def _propose_taxonomy(
    sample: list[CatalogEntry],
    instructions: str,
    *,
    backend,
    logger: OrganizeLogger,
    skill: _skills.SkillConfig,
    extra_exemplars: list[CatalogEntry] | None = None,
) -> Taxonomy:
    payload = _build_proposer_prompt(sample, instructions, extra_exemplars)
    if _est_tokens(payload) > CLASSIFIER_INPUT_TOKEN_BUDGET:
        logger.log("CLASSIFIER_OVERFLOW", stage="A", est_tokens=_est_tokens(payload))
        raise ClassifierOverflowError(
            f"Stage A payload est. {_est_tokens(payload)} tokens exceeds budget"
        )

    last_error: str | None = None
    for attempt in range(2):
        sys_prompt = skill.body
        if attempt == 1 and last_error:
            sys_prompt = (
                skill.body
                + f"\n\nThe previous attempt failed JSON parsing: {last_error}\n"
                "Output ONLY the JSON object."
            )
        try:
            t0 = time.monotonic()
            raw = _backend_call_with_backoff(
                backend, skill, payload, logger,
                override_system=sys_prompt,
                retry_event="CLASSIFIER_RETRY",
            )
            elapsed = int((time.monotonic() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            logger.log("CLASSIFIER_FAIL", reason=str(e), stage="A")
            raise
        try:
            tax = _validate_taxonomy_payload(raw)
            logger.log(
                "TAXONOMY_PROPOSED",
                categories=len(tax.categories),
                fallback=tax.fallback_category.category_name,
                elapsed_ms=elapsed,
            )
            return tax
        except json.JSONDecodeError as e:
            last_error = str(e)
            continue
    raise ClassifierError(f"Stage A JSON parse failed twice: {last_error}")


# ---- Stage B -----------------------------------------------------------

def _batch_entries(entries: list[CatalogEntry]) -> list[list[CatalogEntry]]:
    """Pack entries into batches whose serialized size estimate is bounded."""
    batches: list[list[CatalogEntry]] = []
    current: list[CatalogEntry] = []
    current_tokens = 0
    for e in entries:
        est = _est_tokens(json.dumps(_entry_dict(e), ensure_ascii=False))
        if current and current_tokens + est > ASSIGNER_BATCH_TARGET_TOKENS:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(e)
        current_tokens += est
    if current:
        batches.append(current)
    return batches


def _build_assigner_prompt(
    batch: list[CatalogEntry], taxonomy: Taxonomy, instructions: str,
) -> str:
    payload = {
        "USER_INSTRUCTIONS": instructions,
        "TAXONOMY": {
            "categories": [
                {"category_name": c.category_name, "subpath": c.subpath,
                 "description": c.description, "criteria": c.criteria}
                for c in taxonomy.categories
            ],
            "fallback_category": {
                "category_name": taxonomy.fallback_category.category_name,
                "subpath": taxonomy.fallback_category.subpath,
                "description": taxonomy.fallback_category.description,
                "criteria": taxonomy.fallback_category.criteria,
            },
        },
        "BATCH": [_entry_dict(e) for e in batch],
    }
    return json.dumps(payload, ensure_ascii=False)


def _validate_assignment_payload(
    raw: str, batch_ids: set[str], allowed_names: frozenset[str],
) -> tuple[dict[str, str], list[str]]:
    """Returns (path_id -> category_name, list of violations)."""
    parsed = json.loads(raw)
    items = parsed.get("assignments")
    if not isinstance(items, list):
        return {}, ["assignments must be a list"]
    seen: dict[str, str] = {}
    violations: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            violations.append(f"non-dict assignment item: {type(it).__name__}")
            continue
        pid = it.get("path_id")
        cat = it.get("category_name")
        if pid not in batch_ids:
            violations.append(f"unknown path_id {pid!r}")
            continue
        if cat not in allowed_names:
            violations.append(f"unknown category_name {cat!r} for {pid!r}")
            continue
        if pid in seen:
            violations.append(f"duplicate path_id {pid!r}")
            continue
        seen[pid] = cat
    missing = batch_ids - set(seen.keys())
    for m in sorted(missing):
        violations.append(f"missing assignment for {m!r}")
    return seen, violations


def _run_stage_b(
    entries: list[CatalogEntry],
    taxonomy: Taxonomy,
    instructions: str,
    *,
    backend,
    logger: OrganizeLogger,
    skill: _skills.SkillConfig,
) -> dict[str, str]:
    raw_batches = _batch_entries(entries)
    # _batch_entries budgets entry tokens only; here we enforce the per-call
    # MAX_ASSIGNER_INPUT_TOKENS cap on the full serialized assigner prompt
    # (which also includes the taxonomy block and JSON envelope).
    batches: list[list[CatalogEntry]] = []
    stack: list[list[CatalogEntry]] = list(reversed(raw_batches))
    while stack:
        b = stack.pop()
        if not b:
            continue
        est = _est_tokens(_build_assigner_prompt(b, taxonomy, instructions))
        if est <= MAX_ASSIGNER_INPUT_TOKENS or len(b) == 1:
            batches.append(b)
            continue
        mid = len(b) // 2
        stack.append(b[mid:])
        stack.append(b[:mid])
    if not batches:
        return {}

    sem = threading.Semaphore(MAX_CLASSIFIER_CONCURRENCY)
    allowed = taxonomy.category_name_set()
    fallback_name = taxonomy.fallback_category.category_name
    n_batches = len(batches)

    results: dict[str, str] = {}
    results_lock = threading.Lock()

    def run_one(idx: int, batch: list[CatalogEntry]) -> None:
        sem.acquire()
        try:
            payload = _build_assigner_prompt(batch, taxonomy, instructions)
            ids = {e.path_id for e in batch}
            t0 = time.monotonic()
            logger.log(
                "ASSIGNER_BATCH_START", batch=idx, of=n_batches, files=len(batch),
            )
            raw = _backend_call_with_backoff(backend, skill, payload, logger)
            try:
                mapping, violations = _validate_assignment_payload(raw, ids, allowed)
            except json.JSONDecodeError as e:
                mapping, violations = {}, [f"invalid JSON: {e}"]
            if violations:
                logger.log(
                    "ASSIGNER_RETRY", batch=idx, reason="; ".join(violations[:3]),
                )
                retry_skill_body = (
                    skill.body
                    + "\n\nYour previous response had violations:\n"
                    + "\n".join(f"- {v}" for v in violations[:50])
                    + "\nProduce a corrected JSON object."
                )
                raw = _backend_call_with_backoff(
                    backend, skill, payload, logger, override_system=retry_skill_body,
                )
                try:
                    mapping, violations = _validate_assignment_payload(raw, ids, allowed)
                except json.JSONDecodeError as e:
                    mapping, violations = {}, [f"invalid JSON: {e}"]

            if violations:
                bad_count = len(violations)
                if (bad_count <= ASSIGNER_BAD_RESPONSE_FALLBACK_MAX
                    and bad_count <= max(1, len(ids) * ASSIGNER_BAD_RESPONSE_FALLBACK_RATE)):
                    # Coerce missing / bad entries to the fallback.
                    coerced = 0
                    for pid in ids:
                        if pid not in mapping:
                            mapping[pid] = fallback_name
                            coerced += 1
                    logger.log("ASSIGNER_COERCE_FALLBACK", batch=idx, count=coerced)
                else:
                    logger.log("CLASSIFIER_FAIL", reason="; ".join(violations[:3]),
                               stage="B")
                    raise ClassifierBatchError(
                        f"batch {idx}: {bad_count} unresolved violation(s)"
                    )

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.log(
                "ASSIGNER_BATCH_DONE",
                batch=idx, assigned=len(mapping), elapsed_ms=elapsed_ms,
            )
            with results_lock:
                results.update(mapping)
        finally:
            sem.release()

    with ThreadPoolExecutor(max_workers=MAX_CLASSIFIER_CONCURRENCY) as ex:
        futures = [ex.submit(run_one, i, b) for i, b in enumerate(batches)]
        try:
            for fut in as_completed(futures):
                fut.result()  # propagate
        except BaseException:
            for f in futures:
                f.cancel()
            raise

    return results


def _backend_call_with_backoff(
    backend,
    skill: _skills.SkillConfig,
    payload: str,
    logger: OrganizeLogger,
    *,
    override_system: str | None = None,
    max_attempts: int = 4,
    retry_event: str = "ASSIGNER_RETRY",
) -> str:
    delay = 1.0
    last: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return backend.complete(
                system=override_system or skill.body,
                messages=[{"role": "user", "content": payload}],
                model=skill.model,
                max_tokens=4096,
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_rate_limit(e) or attempt == max_attempts - 1:
                raise
            logger.log(retry_event, reason="rate_limit", delay=delay)
            time.sleep(delay)
            delay *= 2
    raise last  # type: ignore[misc]


# ---- merge -------------------------------------------------------------

def _merge(
    taxonomy: Taxonomy,
    assignments: dict[str, str],
) -> Groupings:
    by_cat: dict[str, list[str]] = {}
    for pid, cat in assignments.items():
        by_cat.setdefault(cat, []).append(pid)
    cat_lookup = {c.category_name: c for c in taxonomy.categories}
    cat_lookup[taxonomy.fallback_category.category_name] = taxonomy.fallback_category
    items: list[Grouping] = []
    for cat_name in [c.category_name for c in taxonomy.categories] \
                    + [taxonomy.fallback_category.category_name]:
        ids = by_cat.get(cat_name, [])
        if not ids:
            continue
        cat = cat_lookup[cat_name]
        items.append(Grouping(
            category=cat.category_name,
            subpath=cat.subpath,
            file_ids=tuple(sorted(ids)),
            reason=cat.criteria,
        ))
    return Groupings(items=tuple(items), overall_reason="")


# ---- public entry point ------------------------------------------------

def classify(
    catalog: Catalog,
    instructions: str,
    *,
    backend,
    logger: OrganizeLogger,
) -> Groupings:
    real = [e for e in catalog.entries if not e.is_junk]
    if not real:
        logger.log("CLASSIFIER_START", catalog_size=0)
        logger.log("CLASSIFIER_DONE", elapsed_ms=0)
        return Groupings(items=(), overall_reason="")

    failed = [e for e in real if e.summary_failed]
    if real and len(failed) / len(real) > READER_FAILED_SUMMARY_THRESHOLD:
        logger.log(
            "CLASSIFIER_UNRELIABLE",
            failed_pct=int(100 * len(failed) / len(real)),
        )
        raise ClassifierUnreliableInputError(
            f"{len(failed)} of {len(real)} catalog entries have failed summaries"
        )

    proposer_skill = _skills.load_skill(_PROPOSER_SKILL_DIR)
    assigner_skill = _skills.load_skill(_ASSIGNER_SKILL_DIR)

    sample = _sample_for_taxonomy(real, catalog.target)
    logger.log(
        "CLASSIFIER_START", catalog_size=len(real),
    )
    logger.log(
        "TAXONOMY_SAMPLE_DONE",
        size=len(sample),
        strategy="full" if len(real) <= TAXONOMY_SAMPLE_FULL_THRESHOLD else "stratified",
    )

    t0 = time.monotonic()
    taxonomy = _propose_taxonomy(
        sample, instructions, backend=backend, logger=logger, skill=proposer_skill,
    )
    assignments = _run_stage_b(
        real, taxonomy, instructions,
        backend=backend, logger=logger, skill=assigner_skill,
    )

    fallback_name = taxonomy.fallback_category.category_name
    fb_count = sum(1 for v in assignments.values() if v == fallback_name)

    if MAX_TAXONOMY_REFINEMENTS > 0 and real:
        rate = fb_count / len(real)
        if (rate > MAX_FALLBACK_RATE) and (fb_count >= MIN_FALLBACK_REFINE_COUNT):
            logger.log("CLASSIFIER_REFINE", fallback_count=fb_count, rate=int(rate * 100))
            exemplars = [e for e in real if assignments.get(e.path_id) == fallback_name]
            if len(exemplars) > TAXONOMY_SAMPLE_FALLBACK_BUDGET:
                exemplars = exemplars[:TAXONOMY_SAMPLE_FALLBACK_BUDGET]
            taxonomy_v2 = _propose_taxonomy(
                sample, instructions,
                backend=backend, logger=logger, skill=proposer_skill,
                extra_exemplars=exemplars,
            )
            assignments = _run_stage_b(
                real, taxonomy_v2, instructions,
                backend=backend, logger=logger, skill=assigner_skill,
            )
            taxonomy = taxonomy_v2
            # The v2 taxonomy may have a different fallback category name
            # than v1 (the model picks the name). Recompute to count
            # against the *current* taxonomy.
            fallback_name = taxonomy.fallback_category.category_name
            fb_count = sum(1 for v in assignments.values() if v == fallback_name)
            rate = fb_count / len(real)
            if rate > MAX_FALLBACK_RATE:
                logger.log("CLASSIFIER_FALLBACK_HIGH", rate=int(rate * 100))

    groupings = _merge(taxonomy, assignments)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    for g in groupings.items:
        logger.log(
            "CLASSIFIER_GROUP",
            category=g.category, files=len(g.file_ids), reason=g.reason,
        )
    logger.log("CLASSIFIER_DONE", elapsed_ms=elapsed_ms)
    return groupings
