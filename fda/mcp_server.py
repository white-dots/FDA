"""
MCP server exposing the FDA orchestrator's state and journal to Claude Code.

Run locally:    python -m fda.mcp_server
Run remotely:   ssh mac.local python -m fda.mcp_server   (used as MCP stdio command)

The server reads the same SQLite state DB, journal directory, and message bus
that the running FDA daemon uses. Read tools query files directly; write tools
post to the message bus or insert directly via ProjectState.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from fda.config import INDEX_PATH, JOURNAL_DIR
from fda.comms.message_bus import Agents, MessageBus, MessageTypes
from fda.state.project_state import ProjectState


mcp = FastMCP("fda")


def _state() -> ProjectState:
    return ProjectState()


def _bus() -> MessageBus:
    return MessageBus()


@mcp.tool()
def list_tasks(status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """List FDA tasks, optionally filtered by status (pending, in_progress, completed, blocked)."""
    tasks = _state().get_tasks(status=status)
    return tasks[:limit]


@mcp.tool()
def submit_task(
    title: str,
    description: str,
    owner: str = "executor",
    priority: str = "medium",
    due_date: Optional[str] = None,
) -> dict[str, str]:
    """Create a new task in the FDA state DB. The orchestrator picks it up on its next loop."""
    task_id = _state().add_task(
        title=title,
        description=description,
        owner=owner,
        priority=priority,
        due_date=due_date,
    )
    return {"task_id": task_id, "status": "pending"}


@mcp.tool()
def recent_decisions(limit: int = 20) -> list[dict[str, Any]]:
    """Get recent decisions logged in the project state."""
    return _state().get_decisions(limit=limit)


@mcp.tool()
def recent_alerts(
    level: Optional[str] = None,
    only_unacknowledged: bool = True,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get alerts. level: info|warning|critical. Defaults to unacknowledged only."""
    alerts = _state().get_alerts(
        level=level,
        acknowledged=False if only_unacknowledged else None,
    )
    return alerts[:limit]


@mcp.tool()
def journal_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search journal entries by substring match against summary, tags, and filename. Case-insensitive."""
    if not INDEX_PATH.exists():
        return []
    data = json.loads(INDEX_PATH.read_text())
    entries = data.get("entries", [])
    q = query.lower()
    matches = []
    for entry in entries:
        haystack = " ".join([
            entry.get("summary", ""),
            " ".join(entry.get("tags") or []),
            entry.get("filename", ""),
        ]).lower()
        if q in haystack:
            matches.append(entry)
    matches.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return matches[:limit]


@mcp.tool()
def journal_read(filename: str) -> dict[str, Any]:
    """Read the full markdown body of a journal entry by filename (as listed in journal_search)."""
    safe = Path(filename).name
    path = JOURNAL_DIR / safe
    if not path.exists():
        return {"error": f"Journal entry not found: {safe}"}
    return {"filename": safe, "content": path.read_text()}


@mcp.tool()
def state_summary() -> dict[str, Any]:
    """High-level snapshot: task counts by status, recent alerts, recent decisions."""
    state = _state()
    tasks = state.get_tasks()
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "task_counts": counts,
        "task_total": len(tasks),
        "open_alerts": len(state.get_alerts(acknowledged=False)),
        "recent_decisions": state.get_decisions(limit=5),
    }


@mcp.tool()
def send_message_to_orchestrator(
    subject: str,
    body: str,
    msg_type: str = MessageTypes.REQUEST,
    priority: str = "medium",
) -> dict[str, str]:
    """Post a fire-and-forget message to the FDA orchestrator's inbox via the message bus. The running daemon must be active to pick it up."""
    msg_id = _bus().send(
        from_agent="claude_code",
        to_agent=Agents.FDA,
        msg_type=msg_type,
        subject=subject,
        body=body,
        priority=priority,
    )
    return {"message_id": msg_id, "to": Agents.FDA}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
