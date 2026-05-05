"""Tests for fda.organize.models — dataclass invariants."""

import pytest
from dataclasses import FrozenInstanceError

from fda.organize.models import (
    OperationKind,
    Operation,
    Plan,
    OperationOutcome,
    PlanResult,
)


def _make_op(kind=OperationKind.MOVE, source="/t/a.txt", destination="/t/b/a.txt"):
    return Operation(kind=kind, source=source, destination=destination, reason="test")


class TestOperationKind:
    def test_values_are_lowercase_strings(self):
        assert OperationKind.CREATE_DIR.value == "create_dir"
        assert OperationKind.MOVE.value == "move"
        assert OperationKind.DELETE.value == "delete"


class TestOperation:
    def test_construct(self):
        op = _make_op()
        assert op.kind == OperationKind.MOVE
        assert op.source == "/t/a.txt"
        assert op.destination == "/t/b/a.txt"
        assert op.reason == "test"

    def test_frozen(self):
        op = _make_op()
        with pytest.raises(FrozenInstanceError):
            op.reason = "changed"  # type: ignore[misc]


class TestPlan:
    def test_construct_with_tuple_operations(self):
        plan = Plan(
            target="/t",
            instructions="sort",
            operations=(_make_op(),),
            grouping_summary="one move",
        )
        assert isinstance(plan.operations, tuple)
        assert len(plan.operations) == 1

    def test_frozen(self):
        plan = Plan(target="/t", instructions="", operations=(), grouping_summary="")
        with pytest.raises(FrozenInstanceError):
            plan.target = "/x"  # type: ignore[misc]


class TestOperationOutcome:
    def test_defaults(self):
        op = _make_op()
        outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
        assert outcome.error is None
        assert outcome.rescue_note is None

    def test_status_values(self):
        op = _make_op()
        for status in ("applied", "rescued", "skipped", "failed"):
            OperationOutcome(operation_index=0, operation=op, status=status)


class TestPlanResult:
    def test_construct(self):
        plan = Plan(target="/t", instructions="", operations=(), grouping_summary="")
        result = PlanResult(
            plan=plan,
            outcomes=(),
            leftover_empty_dirs=(),
            discrepancies=(),
            repos_skipped=(),
            summary="nothing to do",
        )
        assert result.summary == "nothing to do"
        assert isinstance(result.outcomes, tuple)
        assert isinstance(result.leftover_empty_dirs, tuple)
