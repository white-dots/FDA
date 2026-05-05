"""Tests for fda.organize.executor — Phase A happy path (no rescue)."""

import pytest
from pathlib import Path

from fda.organize import executor
from fda.organize.models import Operation, OperationKind, Plan


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


def _plan(workspace, *operations):
    return Plan(
        target=str(workspace),
        instructions="",
        operations=tuple(operations),
        grouping_summary="",
    )


def _move(workspace, src_name, dest_rel, reason="r"):
    return Operation(
        kind=OperationKind.MOVE,
        source=str(workspace / src_name),
        destination=str(workspace / dest_rel),
        reason=reason,
    )


def _create_dir(workspace, dest_rel, reason="r"):
    return Operation(
        kind=OperationKind.CREATE_DIR,
        source=None,
        destination=str(workspace / dest_rel),
        reason=reason,
    )


def _delete(workspace, src_name, reason="r"):
    return Operation(
        kind=OperationKind.DELETE,
        source=str(workspace / src_name),
        destination=None,
        reason=reason,
    )


class TestHappyPath:
    def test_all_ops_apply(self, workspace):
        plan = _plan(
            workspace,
            _create_dir(workspace, "Texts"),
            _move(workspace, "a.txt", "Texts/a.txt"),
            _move(workspace, "b.txt", "Texts/b.txt"),
            _delete(workspace, ".DS_Store"),
        )
        outcomes = executor.execute_plan(plan)
        assert all(o.status == "applied" for o in outcomes)
        assert (workspace / "Texts" / "a.txt").exists()
        assert (workspace / "Texts" / "b.txt").exists()
        assert not (workspace / ".DS_Store").exists()

    def test_outcome_indexes_match_plan_order(self, workspace):
        # Plan order: move, create_dir, delete (NOT execution order)
        plan = _plan(
            workspace,
            _move(workspace, "a.txt", "Texts/a.txt"),  # index 0
            _create_dir(workspace, "Texts"),           # index 1
            _delete(workspace, ".DS_Store"),           # index 2
        )
        outcomes = executor.execute_plan(plan)
        # Outcomes are returned in the original plan order, regardless of
        # the executor's internal CREATE_DIR -> MOVE -> DELETE reordering.
        assert [o.operation_index for o in outcomes] == [0, 1, 2]
        assert outcomes[0].operation.kind == OperationKind.MOVE
        assert outcomes[1].operation.kind == OperationKind.CREATE_DIR


class TestFailureSemantics:
    def test_destination_collision_records_failed(self, workspace):
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("collision")
        plan = _plan(workspace, _move(workspace, "a.txt", "Texts/a.txt"))
        outcomes = executor.execute_plan(plan)
        assert outcomes[0].status == "failed"
        assert "exists" in outcomes[0].error.lower()
        # Source untouched on failure
        assert (workspace / "a.txt").read_text() == "aaa"

    def test_filesystem_race_into_git_repo(self, workspace):
        # Simulate: planner planned a move, but a git repo appeared at the
        # destination's parent before execution.
        (workspace / "subdir").mkdir()
        plan = _plan(workspace, _move(workspace, "a.txt", "subdir/a.txt"))
        # Convert subdir into a repo after planning, before executing.
        (workspace / "subdir" / ".git").mkdir()
        outcomes = executor.execute_plan(plan)
        assert outcomes[0].status == "failed"
        assert "git" in outcomes[0].error.lower()


class TestIdempotency:
    def test_rerun_same_plan(self, workspace):
        plan = _plan(
            workspace,
            _create_dir(workspace, "Texts"),
            _move(workspace, "a.txt", "Texts/a.txt"),
            _delete(workspace, ".DS_Store"),
        )
        first = executor.execute_plan(plan)
        assert all(o.status == "applied" for o in first)
        second = executor.execute_plan(plan)
        # Second run: create_dir is no-op ("applied"), move is "skipped"
        # (source gone, destination present), delete is no-op ("applied").
        statuses = {o.operation.kind: o.status for o in second}
        assert statuses[OperationKind.CREATE_DIR] == "applied"
        assert statuses[OperationKind.MOVE] == "skipped"
        assert statuses[OperationKind.DELETE] == "applied"
