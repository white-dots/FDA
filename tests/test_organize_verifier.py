"""Tests for fda.organize.verifier — diff + empty-dir cleanup + summary."""

import pytest
from pathlib import Path

from fda.organize import verifier
from fda.organize.models import (
    Operation,
    OperationKind,
    OperationOutcome,
    Plan,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _plan(workspace, *ops, summary=""):
    return Plan(
        target=str(workspace),
        instructions="",
        operations=tuple(ops),
        grouping_summary=summary,
    )


def _outcome(idx, op, status="applied", error=None):
    return OperationOutcome(
        operation_index=idx, operation=op, status=status, error=error,
    )


class TestNoDiscrepancies:
    def test_all_applied_filesystem_matches(self, workspace):
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("a")
        op_dir = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(workspace / "Texts"),
            reason="r",
        )
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "old" / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_dir, op_move)
        outcomes = (_outcome(0, op_dir), _outcome(1, op_move))
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert result.discrepancies == ()


class TestDiscrepancies:
    def test_applied_move_but_destination_missing(self, workspace):
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        # Marked applied but the file doesn't exist anywhere.
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert any("a.txt" in d for d in result.discrepancies)


class TestEmptyDirCleanup:
    def test_source_parent_emptied_by_plan_is_cleaned(self, workspace):
        old = workspace / "old"
        old.mkdir()
        # File was already moved out before verifier sees it.
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(old / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("a")
        plan = _plan(workspace, op_move)
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert str(old) in result.leftover_empty_dirs
        assert not old.exists()

    def test_source_parent_with_unrelated_files_not_cleaned(self, workspace):
        old = workspace / "old"
        old.mkdir()
        (old / "unrelated.txt").write_text("keep me")
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(old / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert str(old) not in result.leftover_empty_dirs
        assert old.exists()

    def test_source_parent_in_git_repo_not_cleaned(self, workspace):
        repo = workspace / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        sub = repo / "sub"
        sub.mkdir()
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(sub / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        # Even if verifier wouldn't normally see this op (planner would
        # reject it), guard the cleanup loop against it anyway.
        outcomes = (_outcome(0, op_move, status="failed", error="git repo"),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert str(sub) not in result.leftover_empty_dirs
        assert sub.exists()


class TestSummaryRendering:
    def test_summary_includes_grouping_and_reasons(self, workspace):
        (workspace / "Texts").mkdir()
        (workspace / "Texts" / "a.txt").write_text("a")
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "old" / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="grouped with other text files",
        )
        plan = _plan(workspace, op_move, summary="Two groups")
        outcomes = (_outcome(0, op_move),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert "grouped with other text files" in result.summary
        assert "a.txt" in result.summary

    def test_summary_lists_failures(self, workspace):
        op_move = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "a.txt"),
            destination=str(workspace / "Texts" / "a.txt"),
            reason="r",
        )
        plan = _plan(workspace, op_move)
        outcomes = (_outcome(0, op_move, status="failed", error="permission denied"),)
        result = verifier.verify_plan(plan, outcomes, workspace)
        assert "Couldn't" in result.summary or "couldn't" in result.summary.lower()
        assert "permission denied" in result.summary
