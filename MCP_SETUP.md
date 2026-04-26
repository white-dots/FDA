# FDA MCP Server — Setup & Continuation Notes

Spike of an MCP server that exposes the FDA orchestrator's state/journal/message bus
to Claude Code sessions. Architecture: stdio MCP server, transported over SSH for
remote sessions. Same binary, same code — SSH is just the stdio pipe.

## What was built

- `fda/mcp_server.py` — FastMCP stdio server, 8 tools.
- `tests/test_mcp_server.py` — 4 smoke tests (full suite: 142 passed).
- `pyproject.toml` — added `mcp>=1.0.0` (optional `[mcp]` extra) + console script `fda-mcp`.

Tools registered:

```
list_tasks            submit_task            recent_decisions   recent_alerts
journal_search        journal_read           state_summary      send_message_to_orchestrator
```

All read tools hit the same SQLite DB / journal files the daemon uses (read-only,
fcntl-safe). Writes go through `ProjectState.add_task` and the message bus.

## Install on the Mac

```bash
cd ~/Documents/agenthub/fda-system
/Users/john/.pyenv/versions/3.12.8/bin/pip install -e '.[mcp]'
```

## Register with Claude Code

Local sessions on the Mac:

```bash
claude mcp add fda -- /Users/john/.pyenv/versions/3.12.8/bin/python -m fda.mcp_server
```

Remote sessions (laptop, VM, anywhere with SSH access):

```bash
claude mcp add fda -- ssh your-mac.local /Users/john/.pyenv/versions/3.12.8/bin/python -m fda.mcp_server
```

Auth = SSH keys. No new ports, no tokens, nothing exposed beyond port 22.

## Verify

```bash
claude mcp list                    # should show "fda"
# then in a session:
/mcp                               # lists connected servers + tools
```

Quick functional check from a session:

```
> use the fda tool to show state_summary
```

## Run the tests

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_mcp_server.py -x -q --tb=short
```

## Open question — next iteration

Most useful upgrade is a real round-trip `ask_orchestrator(question)` tool:

1. MCP server posts a `MCP_REQUEST` (new message type) to the bus addressed to FDA.
2. Orchestrator's inbox loop picks it up, runs it through the FDA agent's Claude
   loop with full state/journal/kakao context.
3. Orchestrator posts the answer back to the bus with `reply_to=request_id`.
4. MCP server uses `MessageBus.wait_for_response()` (already exists) and returns
   the answer to Claude Code.

Needs:
- New `MCP_REQUEST` / `MCP_RESULT` constants in `fda/comms/message_bus.py`.
- Handler hooked into `FDAOrchestrator` inbox processing (mirror the
  Telegram/Slack bot patterns — `_handle_*` methods around line 220+ of
  `fda/orchestrator.py`).
- New `ask_orchestrator` tool in `fda/mcp_server.py` that calls
  `bus.send(...)` then `bus.wait_for_response(...)` with a configurable timeout.

Build that next session — current 8 tools are useful as-is.
