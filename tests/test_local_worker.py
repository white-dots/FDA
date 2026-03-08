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
    """Tests for the file organization tools."""

    def test_is_inside_git_repo(self, local_worker, local_worker_dir):
        git_file = local_worker_dir / "my-project" / "main.py"
        non_git_file = local_worker_dir / "readme.txt"

        assert local_worker._is_inside_git_repo(git_file) is True
        assert local_worker._is_inside_git_repo(non_git_file) is False

    def test_orgtool_list_directory_shows_metadata(self, local_worker, local_worker_dir):
        result = local_worker._orgtool_list_directory(
            local_worker_dir, {"path": "."},
        )
        # Should show sizes and dates
        assert "modified:" in result
        # Should flag git repos
        assert "[GIT REPO]" in result

    def test_orgtool_get_file_info(self, local_worker, local_worker_dir):
        result = local_worker._orgtool_get_file_info(
            local_worker_dir, {"path": "readme.txt"},
        )
        info = json.loads(result)
        assert info["name"] == "readme.txt"
        assert info["type"] == "file"
        assert info["in_git_repo"] is False
        assert "size" in info
        assert "modified" in info

    def test_orgtool_get_file_info_git_repo(self, local_worker, local_worker_dir):
        result = local_worker._orgtool_get_file_info(
            local_worker_dir, {"path": "my-project"},
        )
        info = json.loads(result)
        assert info["type"] == "directory"
        assert info["is_git_repo"] is True

    def test_orgtool_get_file_info_inside_repo(self, local_worker, local_worker_dir):
        result = local_worker._orgtool_get_file_info(
            local_worker_dir, {"path": "my-project/main.py"},
        )
        info = json.loads(result)
        assert info["in_git_repo"] is True

    def test_orgtool_create_directory(self, local_worker, local_worker_dir):
        local_worker._organize_dirs_created = []
        result = local_worker._orgtool_create_directory(
            local_worker_dir, {"path": "Documents/PDFs"},
        )
        assert "Created" in result
        assert (local_worker_dir / "Documents" / "PDFs").is_dir()
        assert len(local_worker._organize_dirs_created) == 1

    def test_orgtool_move_file(self, local_worker, local_worker_dir):
        local_worker._organize_moves = []
        # Create target dir first
        (local_worker_dir / "organized").mkdir()

        result = local_worker._orgtool_move_file(
            local_worker_dir,
            {"source": "readme.txt", "destination": "organized/readme.txt"},
        )
        assert "Moved" in result
        assert not (local_worker_dir / "readme.txt").exists()
        assert (local_worker_dir / "organized" / "readme.txt").exists()
        assert len(local_worker._organize_moves) == 1

    def test_orgtool_move_blocks_git_repo(self, local_worker, local_worker_dir):
        local_worker._organize_moves = []
        result = local_worker._orgtool_move_file(
            local_worker_dir,
            {"source": "my-project/main.py", "destination": "moved.py"},
        )
        assert "BLOCKED" in result
        assert len(local_worker._organize_moves) == 0

    def test_orgtool_move_nonexistent_source(self, local_worker, local_worker_dir):
        result = local_worker._orgtool_move_file(
            local_worker_dir,
            {"source": "ghost.txt", "destination": "somewhere.txt"},
        )
        assert "Error" in result

    def test_orgtool_delete_junk(self, local_worker, local_worker_dir):
        local_worker._organize_deletions = []
        result = local_worker._orgtool_delete_file(
            local_worker_dir, {"path": ".DS_Store"},
        )
        assert "Deleted" in result
        assert not (local_worker_dir / ".DS_Store").exists()
        assert len(local_worker._organize_deletions) == 1

    def test_orgtool_delete_blocks_user_files(self, local_worker, local_worker_dir):
        local_worker._organize_deletions = []
        result = local_worker._orgtool_delete_file(
            local_worker_dir, {"path": "readme.txt"},
        )
        assert "BLOCKED" in result
        assert (local_worker_dir / "readme.txt").exists()

    def test_orgtool_delete_blocks_inside_git(self, local_worker, local_worker_dir):
        # Create a .DS_Store inside the git repo
        (local_worker_dir / "my-project" / ".DS_Store").write_bytes(b"\x00")
        result = local_worker._orgtool_delete_file(
            local_worker_dir, {"path": "my-project/.DS_Store"},
        )
        assert "BLOCKED" in result

    def test_orgtool_delete_allows_empty_files(self, local_worker, local_worker_dir):
        local_worker._organize_deletions = []
        (local_worker_dir / "empty.tmp").write_text("")
        result = local_worker._orgtool_delete_file(
            local_worker_dir, {"path": "empty.tmp"},
        )
        assert "Deleted" in result

    def test_human_size(self):
        from fda.local_worker_agent import LocalWorkerAgent
        assert LocalWorkerAgent._human_size(0) == "0.0 B"
        assert LocalWorkerAgent._human_size(1024) == "1.0 KB"
        assert LocalWorkerAgent._human_size(1048576) == "1.0 MB"
        assert LocalWorkerAgent._human_size(1073741824) == "1.0 GB"

    def test_organize_files_validates_path(self, local_worker):
        result = local_worker.organize_files("/nonexistent/path")
        assert result["success"] is False

    def test_execute_organize_tool_dispatch(self, local_worker, local_worker_dir):
        """Verify the tool dispatcher routes to the right methods."""
        local_worker._organize_moves = []
        local_worker._organize_deletions = []
        local_worker._organize_dirs_created = []
        local_worker._repos_skipped = []

        # list_directory
        result = local_worker._execute_organize_tool(
            "list_directory", {"path": "."},
        )
        assert "modified:" in result

        # get_file_info
        result = local_worker._execute_organize_tool(
            "get_file_info", {"path": "readme.txt"},
        )
        info = json.loads(result)
        assert info["name"] == "readme.txt"

        # unknown tool
        result = local_worker._execute_organize_tool(
            "nonexistent_tool", {},
        )
        assert "Unknown tool" in result


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
