"""
Tests for the remote WorkerAgent — SSH tool execution with mocked connections.
"""

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from fda.claude_backend import ToolLoopTimeoutError


@dataclass
class MockSSHResult:
    """Mock SSH command result."""
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0

    @property
    def success(self):
        return self.return_code == 0


@pytest.fixture
def mock_ssh():
    """Mock SSHManager."""
    ssh = MagicMock()
    ssh.execute.return_value = MockSSHResult(stdout="ok")
    ssh.read_files.return_value = {"/app/test.py": "print('hello')"}
    ssh.test_connection.return_value = True
    ssh.warmup.return_value = True
    return ssh


@pytest.fixture
def mock_client_config():
    """Mock ClientConfig."""
    client = MagicMock()
    client.client_id = "test-client"
    client.name = "Test Client"
    client.vm.host = "10.0.0.1"
    client.vm.ssh_user = "deploy"
    client.vm.ssh_key = ""
    client.vm.port = 22
    client.project.repo_path = "/app"
    client.project.extra_repo_paths = []
    client.get_context_for_prompt.return_value = "Test client context"
    return client


@pytest.fixture
def mock_client_manager(mock_client_config):
    """Mock ClientManager."""
    mgr = MagicMock()
    mgr.get_client.return_value = mock_client_config
    mgr.list_clients.return_value = [mock_client_config]
    return mgr


@pytest.fixture
def remote_worker(mock_client_manager, mock_claude_backend):
    """WorkerAgent with all dependencies mocked."""
    with patch("fda.worker_agent.get_claude_backend", return_value=mock_claude_backend), \
         patch("fda.worker_agent.SSHManager") as mock_ssh_cls, \
         patch("fda.base_agent.get_claude_backend", return_value=mock_claude_backend), \
         patch("fda.base_agent.ProjectState"), \
         patch("fda.base_agent.MessageBus"), \
         patch("fda.base_agent.JournalWriter"), \
         patch("fda.base_agent.JournalRetriever"):
        mock_ssh_cls.return_value = MagicMock()
        mock_ssh_cls.return_value.warmup.return_value = True

        from fda.worker_agent import WorkerAgent
        agent = WorkerAgent(client_manager=mock_client_manager)
        agent._backend = mock_claude_backend
        return agent


class TestRemoteWorkerTools:
    """Tests for remote worker tool execution via SSH."""

    def test_resolve_path_relative(self, remote_worker):
        assert remote_worker._resolve_path("/app", "src/main.py") == "/app/src/main.py"

    def test_resolve_path_absolute(self, remote_worker):
        assert remote_worker._resolve_path("/app", "/var/log/app.log") == "/var/log/app.log"

    def test_resolve_path_dot(self, remote_worker):
        assert remote_worker._resolve_path("/app", ".") == "/app"

    def test_resolve_path_tilde(self, remote_worker):
        assert remote_worker._resolve_path("/app", "~/data") == "~/data"

    def test_tool_list_directory(self, remote_worker, mock_ssh):
        remote_worker._current_ssh = mock_ssh
        remote_worker._current_repo_path = "/app"
        mock_ssh.execute.return_value = MockSSHResult(stdout="src/\nmain.py\nREADME.md\n")

        result = remote_worker._tool_list_directory(mock_ssh, "/app", {"path": "."})
        assert "src/" in result
        mock_ssh.execute.assert_called_once()

    def test_tool_read_file(self, remote_worker, mock_ssh):
        remote_worker._current_ssh = mock_ssh
        remote_worker._current_repo_path = "/app"
        remote_worker._files_read = {}

        result = remote_worker._tool_read_file(mock_ssh, "/app", {"path": "test.py"})
        assert "print" in result
        assert "test.py" in remote_worker._files_read

    def test_tool_search_files(self, remote_worker, mock_ssh):
        remote_worker._current_ssh = mock_ssh
        remote_worker._current_repo_path = "/app"
        mock_ssh.execute.return_value = MockSSHResult(
            stdout="./src/main.py:5:def hello():"
        )

        result = remote_worker._tool_search_files(
            mock_ssh, "/app", {"pattern": "def hello"},
        )
        assert "main.py" in result

    def test_tool_write_file_records_change(self, remote_worker):
        remote_worker._pending_changes = {}
        remote_worker._files_read = {}

        result = remote_worker._tool_write_file(
            {"path": "src/fix.py", "content": "fixed code"},
        )
        assert "Recorded" in result
        assert "src/fix.py" in remote_worker._pending_changes

    def test_tool_run_command(self, remote_worker, mock_ssh):
        mock_ssh.execute.return_value = MockSSHResult(stdout="running")

        result = remote_worker._tool_run_command(
            mock_ssh, "/app", {"command": "systemctl status app"},
        )
        assert "running" in result

    def test_tool_run_command_blocks_dangerous(self, remote_worker, mock_ssh):
        result = remote_worker._tool_run_command(
            mock_ssh, "/app", {"command": "rm -rf /"},
        )
        assert "dangerous" in result.lower() or "blocked" in result.lower()

    def test_tool_run_command_failure(self, remote_worker, mock_ssh):
        mock_ssh.execute.return_value = MockSSHResult(
            stderr="command not found", return_code=127,
        )
        result = remote_worker._tool_run_command(
            mock_ssh, "/app", {"command": "nonexistent"},
        )
        assert "command not found" in result


class TestRemoteWorkerIntegration:
    """Tests for the analyze_and_fix flow."""

    def test_analyze_unknown_client(self, remote_worker):
        remote_worker.client_manager.get_client.return_value = None
        result = remote_worker.analyze_and_fix("nonexistent", "fix bug")
        assert result["success"] is False
        assert "Unknown client" in result["error"]

    def test_analyze_calls_backend(self, remote_worker, mock_claude_backend):
        mock_claude_backend.complete_with_tools.return_value = "Analysis done"
        result = remote_worker.analyze_and_fix("test-client", "investigate logs")
        assert result["success"] is True
        assert mock_claude_backend.complete_with_tools.called

    def test_analyze_investigation_no_changes(self, remote_worker, mock_claude_backend):
        mock_claude_backend.complete_with_tools.return_value = "Logs look fine"
        result = remote_worker.analyze_and_fix("test-client", "check logs")
        assert result["success"] is True
        assert result["investigation"] is True
        assert result["changes"] == {}

    def test_invalidate_structure_cache_noop(self, remote_worker):
        # Should not raise — it's a no-op for backward compat
        remote_worker.invalidate_structure_cache()
        remote_worker.invalidate_structure_cache("test-client")

    def test_test_all_connections(self, remote_worker):
        results = remote_worker.test_all_connections()
        assert "test-client" in results


class TestRemoteWorkerTimeouts:
    """Tests for timeout behavior in remote worker."""

    def test_analyze_returns_timeout_error(self, remote_worker, mock_claude_backend):
        mock_claude_backend.complete_with_tools.side_effect = ToolLoopTimeoutError(
            elapsed=301.0, budget=300.0, iterations=5,
        )
        result = remote_worker.analyze_and_fix("test-client", "investigate logs")
        assert result["success"] is False
        assert "timed out" in result["error"].lower()
