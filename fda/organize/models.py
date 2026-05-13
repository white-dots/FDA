# fda/organize/models.py
"""Immutable data models for the organize pipeline.

Plans flow reader -> classifier -> plan_builder -> executor -> verifier;
immutability prevents a downstream phase from quietly mutating an artifact
a previous phase produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class OperationKind(str, Enum):
    CREATE_DIR = "create_dir"
    MOVE = "move"
    DELETE = "delete"


@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    source: str | None
    destination: str | None
    reason: str


@dataclass(frozen=True)
class Plan:
    target: str
    instructions: str
    operations: tuple[Operation, ...]
    grouping_summary: str
    log_path: str | None = None


OutcomeStatus = Literal["applied", "rescued", "skipped", "failed"]


@dataclass(frozen=True)
class OperationOutcome:
    operation_index: int
    operation: Operation
    status: OutcomeStatus
    error: str | None = None
    rescue_note: str | None = None


@dataclass(frozen=True)
class PlanResult:
    plan: Plan
    outcomes: tuple[OperationOutcome, ...]
    leftover_empty_dirs: tuple[str, ...]
    discrepancies: tuple[str, ...]
    repos_skipped: tuple[str, ...]
    summary: str
    log_path: str | None = None


# ---- new pipeline models -----------------------------------------------------


ExtractStatus = Literal["ok", "no_extractor", "tool_missing", "failed"]


@dataclass(frozen=True)
class ExtractionResult:
    text: str | None
    status: ExtractStatus
    note: str = ""
    sections: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class Catalog:
    target: str
    entries: tuple[CatalogEntry, ...]
    git_repos_skipped: tuple[str, ...]


@dataclass(frozen=True)
class TaxonomyCategory:
    category_name: str
    subpath: str
    description: str
    criteria: str


@dataclass(frozen=True)
class Taxonomy:
    categories: tuple[TaxonomyCategory, ...]
    fallback_category: TaxonomyCategory

    def category_name_set(self) -> frozenset[str]:
        return frozenset(
            {c.category_name for c in self.categories}
            | {self.fallback_category.category_name}
        )


@dataclass(frozen=True)
class Grouping:
    category: str
    subpath: str
    file_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Groupings:
    items: tuple[Grouping, ...]
    overall_reason: str


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


@dataclass(frozen=True)
class RoutingReport:
    version: str
    generated_at: str
    target_root: str
    categories: tuple[RoutedCategory, ...]
    quarantine: tuple[QuarantineGroup, ...] = ()
