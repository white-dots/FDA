# fda/metadata/classifier.py
"""Batched Sonnet classifier for the metadata layer.

Public surface (this file):
- classify_batch(files, backend, skill, business_context) -> list[Classification]
  One Claude call. Raises ClassifierResponseError on any structural problem.
- apply_fail_closed_override(record) -> (Classification, bool)
  Deterministic post-process; sets confidentiality='restricted' when
  confidence < FAIL_CLOSED_THRESHOLD and confidentiality is not already
  'restricted'. Returns the (possibly-replaced) record and a boolean
  indicating whether the override fired.

Task 11 will add classify_with_retry_and_bisect() on top of this.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from fda.metadata.schema import Classification

logger = logging.getLogger(__name__)

FAIL_CLOSED_THRESHOLD = 0.3
MAX_CLAUDE_TOKENS = 4096


class ClassifierResponseError(Exception):
    """Raised when the classifier returns an unparseable or wrong-shaped JSON response."""


def _build_prompt_payload(
    files: list[dict[str, Any]], business_context: str,
) -> str:
    return json.dumps(
        {"business_context": business_context, "files": files},
        ensure_ascii=False,
    )


def _parse_response(raw: str, expected_count: int) -> list[Classification]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClassifierResponseError(
            f"classifier returned invalid JSON: {e}"
        ) from e
    if not isinstance(parsed, list):
        raise ClassifierResponseError(
            f"classifier response is not a JSON array (got {type(parsed).__name__})"
        )
    if len(parsed) != expected_count:
        raise ClassifierResponseError(
            f"classifier response count mismatch: got {len(parsed)}, "
            f"expected {expected_count}"
        )
    out: list[Classification] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ClassifierResponseError(
                f"classifier item {i} is not an object (got {type(item).__name__})"
            )
        try:
            out.append(Classification(**item))
        except ValidationError as e:
            raise ClassifierResponseError(
                f"classifier item {i} failed schema validation: {e}"
            ) from e
    return out


def classify_batch(
    *,
    files: list[dict[str, Any]],
    backend,
    skill,
    business_context: str,
) -> list[Classification]:
    """Run one Claude call over `files` (≤10). Returns validated records."""
    payload = _build_prompt_payload(files, business_context)
    raw = backend.complete(
        system=skill.body,
        messages=[{"role": "user", "content": payload}],
        model=skill.model,
        max_tokens=MAX_CLAUDE_TOKENS,
        temperature=0.0,
    )
    return _parse_response(raw, expected_count=len(files))


def apply_fail_closed_override(
    record: Classification,
) -> tuple[Classification, bool]:
    """Force confidentiality='restricted' when confidence < threshold.

    Returns (record, fired). Idempotent: a record already marked
    `restricted` is left alone, and `fail_closed_override` is preserved
    if the model emitted it (but typically the model returns False here).
    """
    if (
        record.confidence < FAIL_CLOSED_THRESHOLD
        and record.confidentiality != "restricted"
    ):
        replaced = record.model_copy(update={
            "confidentiality": "restricted",
            "fail_closed_override": True,
        })
        return replaced, True
    return record, False


@dataclass
class BisectResult:
    """Aggregated outcome of one classify_with_retry_and_bisect call.

    records_by_path_id: successful classifications keyed by path_id.
    failed_path_ids: files whose single-file probe failed twice — these
        get fail-closed rows downstream (extract_status='failed',
        confidentiality='restricted', confidence=0.0).
    batches_retried: count of top-level retries (each batch's second
        attempt counts once regardless of bisect depth).
    batches_total: count of distinct batches attempted, including
        bisect sub-batches. Useful for cost accounting.
    """
    records_by_path_id: dict[str, Classification] = field(default_factory=dict)
    failed_path_ids: list[str] = field(default_factory=list)
    batches_retried: int = 0
    batches_total: int = 0


def classify_with_retry_and_bisect(
    *,
    files: list[dict[str, Any]],
    backend,
    skill,
    business_context: str,
) -> BisectResult:
    """Classify `files` with: first-attempt → retry-whole-batch → bisect.

    Algorithm per spec § Retry + bisect policy:
    1. Attempt the batch.
    2. On ClassifierResponseError, retry once.
    3. On second failure, split in half and recurse.
    4. On single-file double-failure, mark the file failed and return.

    Returns a BisectResult with successful records keyed by path_id and
    a list of path_ids that failed both attempts at single-file
    granularity.
    """
    result = BisectResult()
    _bisect(files=files, backend=backend, skill=skill,
            business_context=business_context, out=result)
    return result


def _bisect(*, files, backend, skill, business_context, out: BisectResult) -> None:
    """Recursive worker for classify_with_retry_and_bisect."""
    if not files:
        # Defensive: empty list would infinite-recurse on double failure.
        return
    out.batches_total += 1
    # Attempt 1
    try:
        records = classify_batch(
            files=files, backend=backend, skill=skill,
            business_context=business_context,
        )
        for f, r in zip(files, records):
            out.records_by_path_id[f["path_id"]] = r
        return
    except ClassifierResponseError as e:
        logger.warning("metadata classifier attempt 1 failed (n=%d): %s",
                       len(files), e)
    # Attempt 2 (whole-batch retry)
    out.batches_retried += 1
    try:
        records = classify_batch(
            files=files, backend=backend, skill=skill,
            business_context=business_context,
        )
        for f, r in zip(files, records):
            out.records_by_path_id[f["path_id"]] = r
        return
    except ClassifierResponseError as e:
        logger.warning("metadata classifier attempt 2 failed (n=%d): %s",
                       len(files), e)
    # Both attempts failed.
    if len(files) == 1:
        out.failed_path_ids.append(files[0]["path_id"])
        return
    mid = len(files) // 2
    _bisect(files=files[:mid], backend=backend, skill=skill,
            business_context=business_context, out=out)
    _bisect(files=files[mid:], backend=backend, skill=skill,
            business_context=business_context, out=out)
