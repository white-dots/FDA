"""Integration tests for fda.organize.organize() — full planner+executor+verifier."""

import pytest
from pathlib import Path

from fda.organize import organize, apply_plan
from fda.organize.models import Plan, PlanResult


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


def _planner_script(workspace):
    return [("submit_plan", {
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
            {
                "kind": "move",
                "source": str(workspace / "b.txt"),
                "destination": str(workspace / "Texts" / "b.txt"),
                "reason": "text file",
            },
            {
                "kind": "delete",
                "source": str(workspace / ".DS_Store"),
                "reason": "macOS junk",
            },
        ],
        "grouping_summary": "All texts grouped",
    })]


class TestOrganize:
    def test_full_pipeline_executes_plan(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        result = organize(
            str(workspace),
            "sort",
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert isinstance(result, PlanResult)
        assert (workspace / "Texts" / "a.txt").exists()
        assert (workspace / "Texts" / "b.txt").exists()
        assert not (workspace / ".DS_Store").exists()
        assert "All texts grouped" in result.summary
        assert "text file" in result.summary

    def test_preview_returns_plan_without_executing(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        plan = organize(
            str(workspace),
            "",
            preview=True,
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert isinstance(plan, Plan)
        # Filesystem unchanged
        assert (workspace / "a.txt").exists()
        assert (workspace / ".DS_Store").exists()

    def test_invalid_target_raises(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        with pytest.raises(ValueError, match="not in allowed"):
            organize(
                "/etc",
                "",
                backend=backend,
                allowed_roots=[workspace.parent],
            )

    def test_progress_callback_receives_phase_prefixed_events(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        events: list[str] = []
        organize(
            str(workspace),
            "",
            backend=backend,
            allowed_roots=[workspace.parent],
            progress_callback=events.append,
        )
        prefixes = {e.split(":", 1)[0] for e in events}
        assert "planner" in prefixes
        assert "executor" in prefixes


class TestApplyPlan:
    def test_apply_after_preview(self, workspace, stub_claude_backend):
        backend = stub_claude_backend(_planner_script(workspace))
        plan = organize(
            str(workspace),
            "",
            preview=True,
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        # Filesystem still untouched
        assert (workspace / "a.txt").exists()

        result = apply_plan(plan)
        assert isinstance(result, PlanResult)
        assert (workspace / "Texts" / "a.txt").exists()
        assert not (workspace / "a.txt").exists()
