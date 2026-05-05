"""
Tests for LocalWorkerAgent — tool execution + file organization.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from fda.claude_backend import ToolLoopTimeoutError


class TestLocalWorkerTools:
    """Tests for individual local worker tools."""

    def test_list_directory(self, local_worker, local_worker_dir):
        result = local_worker._tool_list_directory(
            local_worker_dir, {"path": "."},
        )
        assert "readme.txt" in result
        assert "script.py" in result
        assert "subdir/" in result
        # .DS_Store is hidden (starts with .), should be excluded
        assert ".DS_Store" not in result

    def test_list_directory_excludes_git(self, local_worker, local_worker_dir):
        result = local_worker._tool_list_directory(
            local_worker_dir, {"path": "."},
        )
        # .git dir itself should not appear (hidden)
        # my-project/ should appear (it's a normal dir from listing perspective)
        assert "my-project/" in result

    def test_list_directory_outside_project(self, local_worker, local_worker_dir):
        result = local_worker._tool_list_directory(
            local_worker_dir, {"path": "../../"},
        )
        assert "Error" in result

    def test_read_file(self, local_worker, local_worker_dir):
        result = local_worker._tool_read_file(
            local_worker_dir, {"path": "readme.txt"},
        )
        assert result == "Hello world"

    def test_read_file_stores_for_diff(self, local_worker, local_worker_dir):
        local_worker._files_read = {}
        local_worker._tool_read_file(
            local_worker_dir, {"path": "readme.txt"},
        )
        assert "readme.txt" in local_worker._files_read

    def test_read_file_not_found(self, local_worker, local_worker_dir):
        result = local_worker._tool_read_file(
            local_worker_dir, {"path": "nonexistent.txt"},
        )
        assert "Error" in result

    def test_write_file_records_pending(self, local_worker, local_worker_dir):
        local_worker._pending_changes = {}
        result = local_worker._tool_write_file(
            local_worker_dir, {"path": "new.py", "content": "print('hi')"},
        )
        assert "Recorded change" in result
        assert "new.py" in local_worker._pending_changes

    def test_search_files(self, local_worker, local_worker_dir):
        result = local_worker._tool_search_files(
            local_worker_dir, {"pattern": "hello", "file_pattern": "*.py"},
        )
        assert "script.py" in result or "No matches" in result

    def test_run_command(self, local_worker, local_worker_dir):
        result = local_worker._tool_run_command(
            local_worker_dir, {"command": "echo test123"},
        )
        assert "test123" in result

    def test_run_command_blocks_dangerous(self, local_worker, local_worker_dir):
        result = local_worker._tool_run_command(
            local_worker_dir, {"command": "rm -rf /"},
        )
        assert "dangerous" in result.lower() or "blocked" in result.lower()

    def test_run_command_timeout(self, local_worker, local_worker_dir):
        result = local_worker._tool_run_command(
            local_worker_dir, {"command": "sleep 60"},
        )
        assert "timed out" in result.lower()

    def test_validate_project_allowed(self, local_worker, local_worker_dir):
        path = local_worker._validate_project(str(local_worker_dir))
        assert path == local_worker_dir

    def test_validate_project_blocked(self, local_worker):
        with pytest.raises(ValueError, match="not in allowed"):
            local_worker._validate_project("/etc/passwd")

    def test_detect_tech_stack_python(self, local_worker, local_worker_dir):
        stack = local_worker._detect_tech_stack(local_worker_dir)
        assert "Python" in stack


class TestFileOrganization:
    """Tests retained for git-repo detection, human-size formatting, and
    the still-public organize_files() validation entry point. The detailed
    per-tool tests have moved into tests/test_organize_*.py now that
    file organization lives in fda.organize."""

    def test_is_inside_git_repo(self, local_worker, local_worker_dir):
        git_file = local_worker_dir / "my-project" / "main.py"
        non_git_file = local_worker_dir / "readme.txt"

        assert local_worker._is_inside_git_repo(git_file) is True
        assert local_worker._is_inside_git_repo(non_git_file) is False

    def test_human_size(self):
        from fda.local_worker_agent import LocalWorkerAgent
        assert LocalWorkerAgent._human_size(0) == "0.0 B"
        assert LocalWorkerAgent._human_size(1024) == "1.0 KB"
        assert LocalWorkerAgent._human_size(1048576) == "1.0 MB"
        assert LocalWorkerAgent._human_size(1073741824) == "1.0 GB"

    def test_organize_files_validates_path(self, local_worker):
        result = local_worker.organize_files("/nonexistent/path")
        assert result["success"] is False


class TestOrganizeFilesBackCompat:
    """Phase A: organize_files() returns the same dict shape as today,
    plus per-move `reason`, `discrepancies`, and `leftover_empty_dirs`."""

    def test_dict_shape_preserved(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import (
            Operation, OperationKind, Plan, PlanResult, OperationOutcome,
        )

        op = Operation(
            kind=OperationKind.MOVE,
            source=str(local_worker_dir / "readme.txt"),
            destination=str(local_worker_dir / "Texts" / "readme.txt"),
            reason="text file",
        )
        plan = Plan(
            target=str(local_worker_dir),
            instructions="",
            operations=(op,),
            grouping_summary="g",
        )
        outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
        fake_result = PlanResult(
            plan=plan,
            outcomes=(outcome,),
            leftover_empty_dirs=(),
            discrepancies=(),
            repos_skipped=(),
            summary="rendered summary",
        )

        def fake_organize(target, instructions, **kwargs):
            return fake_result

        monkeypatch.setattr("fda.organize.organize", fake_organize)

        result = local_worker.organize_files(str(local_worker_dir), "")
        assert result["success"] is True
        assert "summary" in result
        assert "moves" in result
        assert "deletions" in result
        assert "dirs_created" in result
        assert "repos_skipped" in result
        assert "discrepancies" in result
        assert "leftover_empty_dirs" in result
        assert result["moves"][0]["reason"] == "text file"
        assert result["moves"][0]["from"].endswith("readme.txt")
        assert result["moves"][0]["to"].endswith("Texts/readme.txt")

    def test_success_false_when_any_outcome_failed(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import (
            Operation, OperationKind, Plan, PlanResult, OperationOutcome,
        )

        op = Operation(
            kind=OperationKind.MOVE,
            source=str(local_worker_dir / "readme.txt"),
            destination=str(local_worker_dir / "Texts" / "readme.txt"),
            reason="r",
        )
        plan = Plan(target=str(local_worker_dir), instructions="", operations=(op,), grouping_summary="")
        outcome = OperationOutcome(
            operation_index=0, operation=op, status="failed", error="permission denied",
        )
        fake_result = PlanResult(
            plan=plan, outcomes=(outcome,), leftover_empty_dirs=(),
            discrepancies=(), repos_skipped=(), summary="s",
        )
        monkeypatch.setattr("fda.organize.organize", lambda t, i, **kw: fake_result)

        result = local_worker.organize_files(str(local_worker_dir), "")
        assert result["success"] is False
        # Failed outcomes must be surfaced so the orchestrator can render the
        # spec's `## Couldn't Complete` section. They must NOT appear in moves.
        assert result["moves"] == []
        assert len(result["failures"]) == 1
        assert result["failures"][0]["kind"] == "move"
        assert result["failures"][0]["error"] == "permission denied"
        assert result["failures"][0]["source"].endswith("readme.txt")

    def test_success_false_when_discrepancies(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Plan, PlanResult

        plan = Plan(target=str(local_worker_dir), instructions="", operations=(), grouping_summary="")
        fake_result = PlanResult(
            plan=plan, outcomes=(), leftover_empty_dirs=(),
            discrepancies=("a.txt missing",), repos_skipped=(), summary="s",
        )
        monkeypatch.setattr("fda.organize.organize", lambda t, i, **kw: fake_result)

        result = local_worker.organize_files(str(local_worker_dir), "")
        assert result["success"] is False

    def test_validation_error_returns_dict_not_raises(self, local_worker):
        result = local_worker.organize_files("/etc", "")
        assert result["success"] is False
        assert "error" in result


class TestOrganizeFilesPreviewAndApply:
    def test_preview_returns_plan(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Plan

        fake_plan = Plan(target=str(local_worker_dir), instructions="", operations=(), grouping_summary="g")
        monkeypatch.setattr("fda.organize.organize", lambda t, i, **kw: fake_plan)

        result = local_worker.organize_files_preview(str(local_worker_dir), "")
        assert isinstance(result, Plan)
        assert result.grouping_summary == "g"

    def test_apply_returns_dict(self, local_worker, local_worker_dir, monkeypatch):
        from fda.organize.models import Plan, PlanResult

        plan = Plan(target=str(local_worker_dir), instructions="", operations=(), grouping_summary="")
        fake_result = PlanResult(
            plan=plan, outcomes=(), leftover_empty_dirs=(),
            discrepancies=(), repos_skipped=(), summary="done",
        )
        monkeypatch.setattr("fda.organize.apply_plan", lambda p, **kw: fake_result)

        result = local_worker.organize_files_apply(plan)
        assert result["success"] is True
        assert result["summary"] == "done"


class TestOrganizeConcurrencyRegression:
    """Two back-to-back organize_files() calls on different targets must not
    corrupt each other. Today's instance-field mutation made this unsafe;
    Phase A removes the shared mutable state."""

    def test_two_targets_dont_share_state(self, local_worker, tmp_path, monkeypatch):
        from fda.organize.models import (
            Operation, OperationKind, Plan, PlanResult, OperationOutcome,
        )

        ws_a = tmp_path / "a"
        ws_a.mkdir()
        (ws_a / "x.txt").write_text("x")
        ws_b = tmp_path / "b"
        ws_b.mkdir()
        (ws_b / "y.txt").write_text("y")

        local_worker.projects = [Path(tmp_path)]

        captured_targets: list[str] = []

        def fake_organize(target, instructions, **kwargs):
            captured_targets.append(target)
            t = Path(target)
            entry = list(t.iterdir())[0]
            op = Operation(
                kind=OperationKind.MOVE,
                source=str(t / entry.name),
                destination=str(t / "Sorted" / entry.name),
                reason="r",
            )
            plan = Plan(target=str(t), instructions=instructions, operations=(op,), grouping_summary="")
            outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
            return PlanResult(
                plan=plan, outcomes=(outcome,), leftover_empty_dirs=(),
                discrepancies=(), repos_skipped=(), summary="s",
            )

        monkeypatch.setattr("fda.organize.organize", fake_organize)

        r1 = local_worker.organize_files(str(ws_a), "first")
        r2 = local_worker.organize_files(str(ws_b), "second")

        assert r1["moves"][0]["from"].endswith("x.txt")
        assert r2["moves"][0]["from"].endswith("y.txt")
        assert captured_targets == [str(ws_a), str(ws_b)]


class TestDeployment:
    """Tests for deploy_approved_changes with backup/rollback."""

    def test_deploy_creates_backup(self, local_worker, local_worker_dir, tmp_path):
        local_worker._backup_dir = tmp_path / "backups"
        result = local_worker.deploy_approved_changes(
            str(local_worker_dir),
            {"readme.txt": "Updated content"},
        )
        assert result["success"] is True
        assert "backup_path" in result
        assert (local_worker_dir / "readme.txt").read_text() == "Updated content"

    def test_deploy_blocked_path(self, local_worker):
        result = local_worker.deploy_approved_changes(
            "/etc/shadow",
            {"test.txt": "content"},
        )
        assert result["success"] is False
        assert "not in allowed" in result["error"]


class TestTimeouts:
    """Tests for timeout behavior in the tool-use loop."""

    def test_analyze_returns_timeout_error(self, local_worker, local_worker_dir):
        local_worker._backend.complete_with_tools.side_effect = ToolLoopTimeoutError(
            elapsed=301.0, budget=300.0, iterations=5,
        )
        result = local_worker.analyze_and_fix(
            project_path=str(local_worker_dir),
            task_brief="some task",
        )
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_organize_returns_timeout_error(self, local_worker, local_worker_dir):
        local_worker._backend.complete_with_tools.side_effect = ToolLoopTimeoutError(
            elapsed=601.0, budget=600.0, iterations=10,
        )
        result = local_worker.organize_files(target_path=str(local_worker_dir))
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_timeout_error_attributes(self):
        err = ToolLoopTimeoutError(elapsed=120.5, budget=100.0, iterations=3)
        assert err.elapsed == 120.5
        assert err.budget == 100.0
        assert err.iterations == 3
        assert "120s" in str(err)


class TestRepoDiscovery:
    """Tests for auto-discovery of git repositories."""

    def test_discover_finds_git_dirs(self, local_worker, local_worker_dir):
        """Verify that directories with .git are discovered."""
        local_worker.state = MagicMock()
        local_worker.state.get_all_projects.return_value = []
        local_worker.state.add_project.return_value = "proj_123"

        repos = local_worker.discover_repos()
        assert len(repos) >= 1
        names = [r["name"] for r in repos]
        assert "my-project" in names

    def test_discover_skips_known(self, local_worker, local_worker_dir):
        """Already-known repos are not re-reported as new."""
        known_path = str((local_worker_dir / "my-project").resolve())
        local_worker.state = MagicMock()
        local_worker.state.get_all_projects.return_value = [
            {"path": known_path},
        ]

        repos = local_worker.discover_repos()
        names = [r["name"] for r in repos]
        assert "my-project" not in names

    def test_discover_skips_excluded_dirs(self, local_worker, local_worker_dir):
        """Directories in REPO_DISCOVERY_SKIP_DIRS are not scanned."""
        nm = local_worker_dir / "node_modules" / "fake-repo"
        nm.mkdir(parents=True)
        (nm / ".git").mkdir()

        local_worker.state = MagicMock()
        local_worker.state.get_all_projects.return_value = []

        repos = local_worker.discover_repos()
        paths = [r["path"] for r in repos]
        assert str(nm.resolve()) not in paths

    def test_extract_repo_metadata(self, local_worker, local_worker_dir):
        repo_path = local_worker_dir / "my-project"
        info = local_worker._extract_repo_metadata(repo_path)
        assert info["name"] == "my-project"
        assert info["path"] == str(repo_path.resolve())

    def test_get_repo_shortcuts(self, local_worker):
        local_worker.state = MagicMock()
        local_worker.state.get_all_projects.return_value = [
            {"name": "FDA", "path": "/Users/john/Documents/FDA"},
            {"name": "my-app", "path": "/Users/john/Documents/my-app"},
        ]
        shortcuts = local_worker.get_repo_shortcuts()
        assert shortcuts["fda"] == "/Users/john/Documents/FDA"
        assert shortcuts["my-app"] == "/Users/john/Documents/my-app"
        assert shortcuts["myapp"] == "/Users/john/Documents/my-app"

    def test_resolve_project_path_shortcut(self, local_worker):
        local_worker.state = MagicMock()
        local_worker.state.get_all_projects.return_value = [
            {"name": "FDA", "path": "/Users/john/Documents/FDA"},
        ]
        assert local_worker.resolve_project_path("FDA") == "/Users/john/Documents/FDA"
        assert local_worker.resolve_project_path("fda") == "/Users/john/Documents/FDA"

    def test_resolve_project_path_absolute(self, local_worker):
        local_worker.state = MagicMock()
        result = local_worker.resolve_project_path("/tmp/something")
        assert result == "/private/tmp/something" or result == "/tmp/something"
