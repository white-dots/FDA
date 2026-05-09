---
name: fda-remote
description: Set up or use the FDA (Facilitating Director Agent) over MCP from a remote Claude Code session. Trigger when the user wants to connect their laptop/VM/other-machine to the FDA orchestrator running on John's Mac mini, register the FDA MCP server via SSH, query FDA tasks/journal/decisions/alerts from another machine, send messages to the orchestrator, troubleshoot FDA MCP connection failures, or invoke any `mcp__fda__*` tool. Also use when the user says "connect to FDA", "FDA from laptop", "remote FDA", "set up FDA MCP", "FDA over SSH", or asks how to query FDA state from outside the Mac mini.
---

# FDA Remote Access

This skill guides the user through connecting a remote Claude Code session to the FDA orchestrator running on John's Mac mini.

## How it works (1-paragraph mental model)

FDA is a daemon (always-on under launchd) on a Mac mini at `hello.local` / `192.168.45.70`. It exposes its state — open tasks, journal entries, alerts, decisions, message bus — through an **MCP stdio server** (`fda-mcp` console script, defined in `fda/mcp_server.py`). To use it from another machine, register the server with `claude mcp add` using `ssh` as the transport: Claude Code spawns `ssh user@host 'python -m fda.mcp_server'`, and the MCP JSON-RPC just rides over the SSH stdio. **No ports, no tokens, no daemon to start.** Auth is regular SSH keys.

```
┌──────────────────┐   ssh stdio   ┌──────────────────────────┐
│  Your laptop     │ ────────────► │  Mac mini "hello"        │
│  Claude Code     │               │  python -m fda.mcp_server│
│  ─ mcp__fda__*   │ ◄──────────── │  reads SQLite + journal  │
└──────────────────┘   JSON-RPC    └──────────────────────────┘
```

## Available tools after setup

Once registered, these `mcp__fda__*` tools are loaded automatically in any Claude Code session:

| Tool | What it does |
|---|---|
| `state_summary` | Snapshot: task counts, alerts, agent statuses, recent decisions |
| `list_tasks` | List/filter tasks by status, owner, client, priority |
| `submit_task` | Create a new task in FDA's tracker |
| `recent_decisions` | Last N architectural/business decisions logged |
| `recent_alerts` | Open alerts (errors, warnings, system events) |
| `journal_search` | Tag + text search across journal entries (with relevance decay) |
| `journal_read` | Read a specific journal entry by id |
| `send_message_to_orchestrator` | Post a message to FDA's bus (e.g., ad-hoc questions) |

## Setup playbook

Follow these steps in order. Stop and ask the user before each step that requires their input or external action.

### Step 1 — Determine hostname

Ask the user how they reach the Mac mini:
- **Same Wi-Fi/LAN**: use `hello.local` (mDNS Bonjour) — works without configuration
- **Tailscale / WireGuard**: use the Tailscale IP or magic-DNS hostname
- **Public internet**: use the public IP `175.117.246.118` AND require port-forwarding on the router (port 22 → 192.168.45.70). Recommend Tailscale instead for safety.

For the rest of this guide, treat the chosen value as `<MAC_HOST>`.

### Step 2 — Verify SSH connectivity

Run this from the user's machine:

```bash
ssh -o ConnectTimeout=5 john@<MAC_HOST> 'echo ok'
```

Three possible outcomes:

- **Prints `ok`** → SSH works and a key is already trusted. Skip to Step 4.
- **Asks for a password** → SSH works but no key is set up. Go to Step 3.
- **`Permission denied (publickey)` or connection timeout** → diagnose. See Troubleshooting.

### Step 3 — Set up SSH key auth (one-time, on the user's machine)

```bash
# 3a. Generate a key if they don't have one
test -f ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)-fda-mcp"

# 3b. Copy the public key to the Mac mini
ssh-copy-id john@<MAC_HOST>
# (this prompts for the Mac password ONCE, then never again)

# 3c. Verify keyless access
ssh john@<MAC_HOST> 'echo ok'
```

If `ssh-copy-id` isn't available, the manual equivalent is:

```bash
cat ~/.ssh/id_ed25519.pub | ssh john@<MAC_HOST> 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
```

### Step 4 — Register the FDA MCP server

```bash
claude mcp add fda --scope user -- ssh john@<MAC_HOST> /Users/john/.pyenv/versions/3.12.8/bin/python -m fda.mcp_server
```

Notes:
- `--scope user` makes it available in every Claude Code session on this machine (writes to `~/.claude.json`)
- `--scope project` instead if they only want it for the current repo
- The Python path is hardcoded because the daemon runs from pyenv 3.12.8; the `fda-mcp` console script also exists but the explicit `python -m` form is most reliable across SSH

### Step 5 — Verify

```bash
claude mcp list
# expect: fda: ssh john@<MAC_HOST> ... - ✓ Connected
```

Inside a Claude Code session, the tools should appear as `mcp__fda__*`. Quick smoke test:

> use mcp__fda__state_summary to show me current FDA state

If it returns a JSON snapshot with task counts and agent statuses, end-to-end is working.

## Common workflows

After setup, these are the typical things the user wants to do:

### "What's FDA up to right now?"
Call `mcp__fda__state_summary` — gives task counts, agent statuses, recent activity.

### "Are there open issues with project X?"
Call `mcp__fda__list_tasks` filtered by status=`pending|in_progress` and `client=X`.
Follow up with `mcp__fda__recent_alerts` if the user wants errors/warnings too.

### "What did FDA learn yesterday?"
Call `mcp__fda__journal_search` with relevant tags (e.g., `["meeting", "decision"]`) or text query.
Use `mcp__fda__journal_read` to fetch a specific entry id from the search results.

### "Tell FDA to do X" / "Ask FDA about Y"
Call `mcp__fda__send_message_to_orchestrator` — posts to the bus. **Note**: today this is fire-and-forget; the orchestrator processes the message but doesn't reply back through MCP yet (round-trip `ask_orchestrator` is in the roadmap per `MCP_SETUP.md`).

### "Add a task to FDA"
Call `mcp__fda__submit_task` with title/description/owner/priority. The task lands in the same SQLite table the daemon and bots use.

## Troubleshooting

### `Permission denied (publickey)` after `claude mcp add`
SSH key isn't authorized on the Mac. Re-run Step 3.

### `command not found: python` over SSH
The user's `ssh` session has a stripped PATH. Use the explicit pyenv path: `/Users/john/.pyenv/versions/3.12.8/bin/python -m fda.mcp_server`.

### `ModuleNotFoundError: fda` over SSH
The `fda` package isn't installed in that Python. On the Mac mini:
```bash
cd /Users/john/Documents/FDA
/Users/john/.pyenv/versions/3.12.8/bin/pip install -e '.[mcp]'
```

### `ModuleNotFoundError: mcp`
Same fix as above — the `[mcp]` extra wasn't installed.

### `claude mcp list` shows `fda: ✗ Failed to connect`
Run the underlying SSH command manually to see the actual error:
```bash
ssh john@<MAC_HOST> /Users/john/.pyenv/versions/3.12.8/bin/python -m fda.mcp_server <<<''
```
The first line of stderr usually pinpoints the problem.

### Tools appear but every call returns stale data
Possible causes:
- The FDA daemon isn't running on the Mac (`launchctl list | grep fda` should show a PID)
- The MCP server reads the SQLite DB directly; if the daemon's state is fresh but the file isn't being updated, that's a daemon-side bug, not an MCP issue

### Connection works, but `send_message_to_orchestrator` "doesn't do anything"
Expected — see "Common workflows" above. The bus message is delivered but no MCP-side reply mechanism exists yet. Look for the daemon's response in its logs or via `journal_search`.

## Reference

- FDA repo: <https://github.com/white-dots/FDA>
- MCP server source: `fda/mcp_server.py`
- Setup notes (Mac side): `MCP_SETUP.md` in the repo
- Daemon path on Mac: `/Users/john/Documents/FDA`, launched by `~/Library/LaunchAgents/com.john.fda.plist`
