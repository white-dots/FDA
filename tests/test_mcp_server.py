"""
Smoke tests for the FDA MCP server.

Exercises tool functions directly (FastMCP's @tool decorator leaves them callable)
against temp state/bus/journal so the tests don't touch the real daemon.
"""

import json
import pytest

pytest.importorskip("mcp")

from fda.comms.message_bus import MessageBus, MessageTypes
from fda.state.project_state import ProjectState


@pytest.fixture
def patched_mcp(monkeypatch, tmp_state_db, tmp_bus_path, tmp_journal_dir, tmp_index_path):
    """Import mcp_server with helpers redirected to temp resources."""
    from fda import mcp_server

    state = ProjectState(db_path=tmp_state_db)
    bus = MessageBus(bus_path=tmp_bus_path)

    monkeypatch.setattr(mcp_server, "_state", lambda: state)
    monkeypatch.setattr(mcp_server, "_bus", lambda: bus)
    monkeypatch.setattr(mcp_server, "INDEX_PATH", tmp_index_path)
    monkeypatch.setattr(mcp_server, "JOURNAL_DIR", tmp_journal_dir)

    return mcp_server, state, bus


def test_list_and_submit_tasks(patched_mcp):
    mcp_server, state, _ = patched_mcp

    assert mcp_server.list_tasks() == []

    result = mcp_server.submit_task(
        title="Ship MCP server",
        description="Wire FDA orchestrator into Claude Code",
        owner="executor",
    )
    assert result["task_id"].startswith("task_")
    assert result["status"] == "pending"

    tasks = mcp_server.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Ship MCP server"

    pending = mcp_server.list_tasks(status="pending")
    assert len(pending) == 1
    assert mcp_server.list_tasks(status="completed") == []


def test_journal_search_and_read(patched_mcp, tmp_journal_dir, tmp_index_path):
    mcp_server, _, _ = patched_mcp

    assert mcp_server.journal_search("anything") == []

    fname = "2026-04-26_first.md"
    (tmp_journal_dir / fname).write_text("# First entry\n\nbody")
    tmp_index_path.write_text(json.dumps({
        "entries": [{
            "filename": fname,
            "summary": "first entry about onboarding",
            "tags": ["onboarding"],
            "created_at": "2026-04-26T00:00:00",
        }],
        "count": 1,
    }))

    hits = mcp_server.journal_search("onboarding")
    assert len(hits) == 1 and hits[0]["filename"] == fname

    body = mcp_server.journal_read(fname)
    assert "First entry" in body["content"]

    missing = mcp_server.journal_read("does_not_exist.md")
    assert "error" in missing


def test_state_summary(patched_mcp):
    mcp_server, state, _ = patched_mcp

    state.add_task("a", "x", "executor", status="pending")
    state.add_task("b", "y", "executor", status="completed")
    state.add_decision("pick mcp", "simplest path", "John", "high")

    summary = mcp_server.state_summary()
    assert summary["task_total"] == 2
    assert summary["task_counts"]["pending"] == 1
    assert summary["task_counts"]["completed"] == 1
    assert len(summary["recent_decisions"]) == 1


def test_send_message_to_orchestrator(patched_mcp):
    mcp_server, _, bus = patched_mcp

    res = mcp_server.send_message_to_orchestrator(
        subject="hello",
        body="from claude code",
        msg_type=MessageTypes.REQUEST,
    )
    assert "message_id" in res

    pending = bus.get_pending("fda")
    assert len(pending) == 1
    assert pending[0]["from"] == "claude_code"
    assert pending[0]["subject"] == "hello"
