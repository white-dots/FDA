"""Tests for fda.organize.planner — happy path, validation rejection, missed submit."""

import pytest
from pathlib import Path

from fda.organize import planner
from fda.organize.models import OperationKind


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / "b.txt").write_text("world")
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return root


def _valid_submit(workspace):
    return ("submit_plan", {
        "operations": [
            {
                "kind": "create_dir",
                "destination": str(workspace / "Texts"),
                "reason": "group text files",
            },
            {
                "kind": "move",
                "source": str(workspace / "a.txt"),
                "destination": str(workspace / "Texts" / "a.txt"),
                "reason": "text file",
            },
        ],
        "grouping_summary": "grouped texts",
    })


class TestBuildPlanHappyPath:
    def test_returns_plan_when_submit_called(self, workspace, stub_claude_backend):
        backend = stub_claude_backend([_valid_submit(workspace)])
        plan = planner.build_plan(workspace, "sort", backend=backend)
        assert plan.target == str(workspace)
        assert plan.instructions == "sort"
        assert plan.grouping_summary == "grouped texts"
        assert len(plan.operations) == 2
        assert plan.operations[0].kind == OperationKind.CREATE_DIR
        assert plan.operations[1].kind == OperationKind.MOVE
        assert plan.operations[1].reason == "text file"


class TestBuildPlanValidationRejection:
    def test_invalid_then_valid_submission_succeeds(self, workspace, stub_claude_backend):
        bad = ("submit_plan", {
            "operations": [
                {
                    "kind": "delete",
                    "source": str(workspace / "a.txt"),  # not junk, not empty
                    "reason": "want to delete",
                },
            ],
            "grouping_summary": "bad",
        })
        good = _valid_submit(workspace)
        backend = stub_claude_backend([bad], [good])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert len(plan.operations) == 2  # the second submission was accepted

    def test_all_or_nothing_on_partial_invalid(self, workspace, stub_claude_backend):
        # One valid op + one invalid op in the same submission -> entire
        # submission rejected; planner can resubmit.
        mixed = ("submit_plan", {
            "operations": [
                {
                    "kind": "create_dir",
                    "destination": str(workspace / "Texts"),
                    "reason": "ok",
                },
                {
                    "kind": "move",
                    "source": str(workspace / "a.txt"),
                    "destination": str(workspace / "repo" / "a.txt"),  # into git repo
                    "reason": "bad",
                },
            ],
            "grouping_summary": "mixed",
        })
        good = _valid_submit(workspace)
        backend = stub_claude_backend([mixed], [good])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert plan.grouping_summary == "grouped texts"


class TestBuildPlanMissedSubmit:
    def test_raises_when_submit_never_called(self, workspace, stub_claude_backend):
        # Stub does nothing; the planner's _submitted flag stays False.
        backend = stub_claude_backend([])  # zero iterations
        with pytest.raises(planner.PlannerDidNotSubmitError):
            planner.build_plan(workspace, "", backend=backend)
