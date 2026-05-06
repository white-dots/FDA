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
