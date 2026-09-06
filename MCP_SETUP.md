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

---

## Remote HTTP MCP server: `aonebnh`

A second MCP server, reached over HTTP through a Cloudflare tunnel rather than
stdio-over-SSH. Registered at **project scope** so every session in this repo
picks it up.

### Files

- `.mcp.json` — server definition (committed). The bearer token is *not* in it;
  the header interpolates `${AONEBNH_TOKEN}`.
- `.claude/settings.json` — pre-approves `aonebnh` via `enabledMcpjsonServers`,
  so sessions don't prompt for the project-scoped server (committed).
- `.claude/settings.local.json` — holds `AONEBNH_TOKEN` in its `env` block.
  Gitignored (`.gitignore:52`); the token never reaches the repo.

```jsonc
// .mcp.json
{
  "mcpServers": {
    "aonebnh": {
      "type": "http",
      "url": "https://pulse-meeting-phone-vista.trycloudflare.com/mcp",
      "headers": { "Authorization": "Bearer ${AONEBNH_TOKEN}" }
    }
  }
}
```

```jsonc
// .claude/settings.local.json  (gitignored — create on each machine)
{
  "env": { "AONEBNH_TOKEN": "aonebnh_…" }
}
```

Alternative to the settings file: export the variable in your shell profile
(`export AONEBNH_TOKEN=aonebnh_…`) before launching `claude`. Either source
satisfies the `${AONEBNH_TOKEN}` expansion.

### Verify

```bash
claude mcp get aonebnh      # confirms scope, URL, header
claude mcp list             # health check — should report ✓ Connected
# then inside a session:
/mcp                        # lists the server and its tools
```

First run in the repo may ask once to trust the project-scoped server; approve it.

### Caveats

- `trycloudflare.com` hostnames are **ephemeral** — a quick tunnel gets a new
  subdomain every time `cloudflared` restarts. When the URL changes, edit the
  `url` in `.mcp.json`; the token stays put.
- The token is a long-lived bearer credential. Rotate it on the server side if
  it leaks, and keep it out of `.mcp.json`, commits, and screenshots.
- Cloud/remote Claude Code sessions may not reach the tunnel at all: their
  egress proxy denies non-allowlisted hosts (`connect_rejected`). This server is
  effectively local-machine only unless the host is allowlisted.
