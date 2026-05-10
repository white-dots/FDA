# FDA — Your Always-On AI Helper

FDA (Facilitating Director Agent) is a persistent, multi-agent assistant built on Claude that runs as a background service on your Mac. Unlike chat tools you open and close, FDA stays running — handling your day-to-day tasks, remembering past work, posting daily briefings, and meeting you wherever you already are: Telegram, Discord, Slack, KakaoTalk, or directly inside Claude Code.

It's a general-purpose day-to-day helper first, with deep capabilities (code workers, calendar integration, multi-client project tracking) layered on top for when you need them.

## Why Not Just Use Claude?

| Vanilla Claude / ChatGPT | FDA |
|---|---|
| Forgets everything between sessions | Persistent journal with relevance-ranked memory |
| You go to it | It comes to you — same brain across Telegram, Discord, Slack, KakaoTalk |
| Only works when you're chatting | Runs 24/7 — morning briefings, daily notetaking, health monitoring |
| Can't see your calendar or files | Reads Outlook calendar, indexes local files semantically |
| Isolated per chat session | Same memory + state shared across every interface, including Claude Code |

## What It Does Day-to-Day

### Multi-Channel Presence
One AI brain across all your chat platforms. Ask a question on Telegram, get a code change approved on Discord, review notes on Slack — same memory, same context.

- **Telegram** — mobile-friendly queries and notifications
- **Discord** — team collaboration with voice support (OpenAI Realtime API)
- **Slack** — workspace integration with threading
- **KakaoTalk** — automated client chat monitoring
- **Claude Code (via MCP)** — your IDE-side Claude sessions get tools to query FDA's state, journal, and tasks

### Persistent Memory
A journal system that never forgets. Every investigation, decision, deployment, and chat summary is logged with tags and relevance decay. FDA searches its own memory before re-running expensive tasks.

### Autonomous Daily Routines
- **9 AM Morning Briefing** — summarizes yesterday's journal entries, posts to Discord/Slack
- **9 PM Daily Notetaking** — auto-summarizes conversations from designated channels into journal entries
- **Calendar awareness** — reads Outlook calendar, prepares meeting briefs from journal context
- **Health monitoring** — checks bot threads every hour, auto-restarts crashed services

### Task & State Tracking
- Lightweight task list (pending / in_progress / completed / blocked) with owner and priority
- Decision log with rationale and impact
- Alerts surfaced through every chat channel
- KPI snapshots over time

### Semantic File Index
Local embedding-based file indexer for fast "where did I put that doc?" queries — no cloud upload.

### Agentic Code Workers (Optional)
For when day-to-day work bleeds into actual coding:

- **Local Worker** — agentic file ops on your local filesystem
- **Remote Worker** — SSHs into VMs, explores codebases, generates fixes

Both let Claude decide what to explore (like a developer would). Changes go through an approval workflow before deployment.

### Claude Code Integration (MCP)
A built-in MCP server exposes FDA's state, journal, and message bus to any Claude Code session — local on the Mac or remote over SSH. See [MCP_SETUP.md](MCP_SETUP.md).

## Quick Start

```bash
# Clone and install
git clone https://github.com/white-dots/FDA.git
cd FDA
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"

# Run the guided setup
fda onboard

# Or start directly
fda start
```

### Install Options

```bash
pip install -e ".[all]"        # Everything
pip install -e ".[telegram]"   # Telegram bot only
pip install -e ".[discord]"    # Discord bot only
pip install -e ".[slack]"      # Slack bot only
pip install -e ".[mcp]"        # MCP server for Claude Code
pip install -e ".[web]"        # Web setup UI only
```

## Usage

```bash
# Start the system (runs all agents + bots)
fda start

# Interactive setup
fda onboard

# Ask FDA a question
fda ask "What are my open tasks?"

# Check system status
fda status

# Journal
fda journal search "deployment issue"

# Notetaking channels
fda config notetaking list
fda config notetaking add telegram <channel_id> --label "Daily Standup"
fda config notetaking remove telegram <channel_id>

# Web-based setup UI
fda setup
```

### Bot Commands

**Telegram / Discord / Slack:**
- Ask any question in natural language — FDA uses tools to search journal, read chats, check tasks, run commands on local/remote machines
- `!approve <id>` / `!reject <id>` — approve or reject code changes
- `!details <id>` — view full diff of proposed changes

### Claude Code Tools (via MCP)

Once registered (`claude mcp add fda -- python -m fda.mcp_server`), Claude Code sessions get:

```
list_tasks            submit_task            recent_decisions   recent_alerts
journal_search        journal_read           state_summary      send_message_to_orchestrator
```

See [MCP_SETUP.md](MCP_SETUP.md) for local + SSH-tunneled remote setup.

## Architecture

```
fda/
├── orchestrator.py          # Central coordinator — starts all agents + bots,
│                            #   schedules daily routines, routes messages
├── fda_agent.py             # Core FDA agent — onboarding, check-ins, Q&A
├── worker_agent.py          # Remote worker — agentic code ops via SSH
├── local_worker_agent.py    # Local worker — agentic code ops on local filesystem
├── claude_backend.py        # Claude abstraction — CLI (Max sub) or API backend
├── daemon.py                # Daemon installer — launchd (macOS) / systemd (Linux)
├── scheduler.py             # Recurring tasks (briefings, notetaking, calendar)
├── file_indexer.py          # Semantic file index using local embeddings
├── outlook.py               # Outlook calendar via Microsoft Graph
├── realtime_voice.py        # OpenAI Realtime API voice for Discord
├── mcp_server.py            # MCP server exposing FDA to Claude Code
│
├── telegram_bot.py          # Telegram bot with tool-use
├── discord_bot.py           # Discord bot with voice + tool-use
├── slack_bot.py             # Slack bot with tool-use
│
├── journal/                 # Persistent memory system
│   ├── writer.py            #   Markdown entries with YAML frontmatter
│   ├── index.py             #   Tag-based indexing and search
│   └── retriever.py         #   Two-pass retrieval with relevance decay
│
├── state/                   # SQLite state persistence
│   └── project_state.py     #   Tasks, alerts, decisions, chat history, file index
│
├── clients/                 # Optional multi-client project management
│   └── client_config.py     #   Per-client VM, repo, and context configs
│
├── remote/                  # Remote VM operations
│   ├── ssh_manager.py       #   SSH with ControlMaster multiplexing
│   └── deploy.py            #   File deployment with backup + rollback
│
└── comms/                   # Inter-agent communication
    └── message_bus.py       #   File-based message bus with fcntl locking
```

### How It Works

1. **Orchestrator** starts all agent threads and bot threads, then enters its main polling loop
2. **Chat bots** (Telegram/Discord/Slack) receive user messages and use Claude's tool-use API to autonomously call tools: search journal, read chats, check tasks, run local/remote commands
3. **MCP server** exposes the same state to Claude Code sessions over stdio (local) or SSH (remote)
4. **Worker agents** receive task briefs and explore filesystems / codebases with Claude tool-use
5. **Approval workflow** — any code change requires explicit user approval before deployment
6. **Journal** — investigations, changes, and chat summaries are automatically logged
7. **Scheduled routines** — morning briefings (9 AM) and daily notetaking (9 PM) run automatically

### Claude Backend

FDA supports two Claude backends:

- **Claude Code CLI** (`claude --print`) — uses your Max/Pro subscription, no API cost
- **Anthropic API** — pay-per-token, supports tool-use and streaming

Auto-detected at startup. Set `FDA_CLAUDE_BACKEND=api` or `FDA_CLAUDE_BACKEND=cli` to force.

## Configuration

### Environment Variables

```bash
# Claude (one of these)
ANTHROPIC_API_KEY=sk-ant-...          # For API backend
# or just have `claude` CLI on PATH   # For CLI backend (Max subscription)

# Chat bots (or configure via fda onboard)
TELEGRAM_BOT_TOKEN=123456789:ABC...
DISCORD_BOT_TOKEN=your_token
DISCORD_CLIENT_ID=your_client_id
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Optional
OPENAI_API_KEY=sk-...                 # For voice (TTS/STT) and embeddings
```

### Daemon Installation

FDA can run as a system service that starts at boot:

```bash
# Installed automatically during `fda onboard`, or manually:
# macOS — creates ~/Library/LaunchAgents/com.fda.agent.plist
# Linux — creates ~/.config/systemd/user/fda.service
```

## Tech Stack

- **Python 3.10+**
- **Claude API** (Anthropic) — tool-use, streaming, extended thinking
- **MCP** — Model Context Protocol for Claude Code integration
- **SQLite** — state persistence, file index, chat history
- **SSH** (ControlMaster) — remote VM operations with connection multiplexing
- **launchd / systemd** — daemon management

### Dependencies

Core: `anthropic`, `pandas`, `requests`, `pyyaml`
Optional: `python-telegram-bot`, `discord.py[voice]`, `slack-bolt`, `openai`, `msal`, `mcp`, `flask`

## License

Proprietary - Jae Heuk Jung

## Links

- GitHub: [white-dots/FDA](https://github.com/white-dots/FDA)
- Issues: [github.com/white-dots/FDA/issues](https://github.com/white-dots/FDA/issues)
