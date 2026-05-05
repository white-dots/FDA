"""Immutable data models for the organize pipeline.

Plans flow planner -> executor -> verifier; immutability prevents a
downstream phase from quietly mutating an artifact a previous phase
produced.
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
