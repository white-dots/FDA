"""
Shared pytest fixtures for FDA system tests.

Provides temp directories, mock backends, and pre-configured components
so tests run in isolation without touching real state or APIs.
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime


# ---------------------------------------------------------------------------
# Temp directory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project_root(tmp_path):
    """Isolated project root for all FDA components."""
    root = tmp_path / "fda-test"
    root.mkdir()
    return root


@pytest.fixture
def tmp_journal_dir(tmp_project_root):
    """Temp journal directory."""
    d = tmp_project_root / "journal"
    d.mkdir()
    return d


@pytest.fixture
def tmp_index_path(tmp_journal_dir):
    """Temp index.json path (inside journal dir)."""
    return tmp_journal_dir / "index.json"


@pytest.fixture
def tmp_state_db(tmp_project_root):
    """Temp SQLite DB path for ProjectState."""
    return tmp_project_root / "state.db"


@pytest.fixture
def tmp_bus_path(tmp_project_root):
    """Temp message_bus.json path."""
    return tmp_project_root / "message_bus.json"


# ---------------------------------------------------------------------------
# Component fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def journal_writer(tmp_journal_dir, tmp_index_path):
    """JournalWriter backed by temp directory."""
    with patch("fda.journal.writer.INDEX_PATH", tmp_index_path):
        from fda.journal.writer import JournalWriter
        return JournalWriter(journal_dir=tmp_journal_dir)


@pytest.fixture
def journal_index(tmp_index_path):
    """JournalIndex backed by temp file."""
    from fda.journal.index import JournalIndex
    return JournalIndex(index_path=tmp_index_path)


@pytest.fixture
def journal_retriever(journal_writer, tmp_journal_dir):
    """JournalRetriever that shares the writer's index for test visibility."""
    from fda.journal.retriever import JournalRetriever
    retriever = JournalRetriever.__new__(JournalRetriever)
    retriever.journal_dir = Path(tmp_journal_dir)
    retriever.index = journal_writer.index
    return retriever


@pytest.fixture
def project_state(tmp_state_db):
    """ProjectState backed by temp SQLite DB."""
    from fda.state.project_state import ProjectState
    return ProjectState(db_path=tmp_state_db)


@pytest.fixture
def message_bus(tmp_bus_path):
    """MessageBus backed by temp JSON file."""
    from fda.comms.message_bus import MessageBus
    return MessageBus(bus_path=tmp_bus_path)


# ---------------------------------------------------------------------------
# Mock backend fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_claude_backend():
    """Mock ClaudeBackend that returns canned responses."""
    backend = MagicMock()
    backend.complete.return_value = "mock response"
    backend.complete_with_tools.return_value = "mock tool response"
    return backend


# ---------------------------------------------------------------------------
# Local worker fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def local_worker_dir(tmp_path):
    """Temp directory tree for local worker tests."""
    root = tmp_path / "workspace"
    root.mkdir()

    # Create some test files
    (root / "readme.txt").write_text("Hello world")
    (root / "notes.md").write_text("# Notes\nSome notes here")
    (root / "script.py").write_text("print('hello')")

    # Create a sub-directory
    sub = root / "subdir"
    sub.mkdir()
    (sub / "data.csv").write_text("a,b,c\n1,2,3")

    # Create a fake git repo directory (should be skipped by organize)
    repo = root / "my-project"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "main.py").write_text("# git-tracked file")

    # Create a junk file
    (root / ".DS_Store").write_bytes(b"\x00\x00")

    return root


@pytest.fixture
def local_worker(local_worker_dir, mock_claude_backend):
    """LocalWorkerAgent with mocked backend and temp project dir."""
    with patch("fda.local_worker_agent.get_claude_backend", return_value=mock_claude_backend), \
         patch("fda.base_agent.get_claude_backend", return_value=mock_claude_backend), \
         patch("fda.base_agent.ProjectState"), \
         patch("fda.base_agent.MessageBus"), \
         patch("fda.base_agent.JournalWriter"), \
         patch("fda.base_agent.JournalRetriever"):
        from fda.local_worker_agent import LocalWorkerAgent
        agent = LocalWorkerAgent(
            projects=[str(local_worker_dir)],
        )
        agent._backend = mock_claude_backend
        agent._current_project = local_worker_dir
        return agent
