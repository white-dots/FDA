# FDA — Your Always-On AI Team Member

FDA (Facilitating Director Agent) is a persistent multi-agent system built on Claude that runs as a background service. Unlike chat-based AI tools you open and close, FDA runs 24/7 — monitoring your projects, remembering past work, posting daily briefings, and meeting you across every chat platform you use.

Built for software teams and consultancies managing multiple client codebases.

## Why Not Just Use Claude?

| Vanilla Claude / ChatGPT | FDA |
|---|---|
| Forgets everything between sessions | Persistent journal with relevance-ranked memory |
| You go to it | It comes to you — same brain across Telegram, Discord, Slack, KakaoTalk |
| Only works when you're chatting | Runs 24/7 — morning briefings, daily notetaking, health monitoring |
| Can't touch your infrastructure | SSHs into VMs, explores codebases, proposes changes with approval workflow |
| Single conversation context | Manages multiple client projects with separate configs |

## Key Features

### Multi-Channel Presence
One AI brain across all your chat platforms. Ask a question on Telegram, get a code fix approved on Discord, review daily notes on Slack — FDA keeps context across all of them.

- **Telegram** — mobile-friendly queries and notifications
- **Discord** — team collaboration with voice support
- **Slack** — workspace integration with threading
- **KakaoTalk** — automated client chat monitoring

### Persistent Memory
A journal system that never forgets. Every investigation, code change, deployment, and decision is logged with tags and relevance decay. FDA searches its own memory before re-running expensive tasks.

### Autonomous Daily Operations
- **9 AM Morning Briefing** — summarizes yesterday's journal entries and posts to Discord/Slack
- **9 PM Daily Notetaking** — auto-summarizes conversations from designated chat channels into journal entries
- **Health Monitoring** — checks bot threads every hour, auto-restarts crashed services

### Agentic Code Workers
Two worker agents that autonomously explore and modify codebases using Claude's tool-use API:

- **Remote Worker** — SSHs into client Azure VMs, uses tools (`list_directory`, `read_file`, `search_files`, `write_file`, `run_command`) to explore and generate fixes
- **Local Worker** — same agentic pattern on the local filesystem

Both workers let Claude decide what to explore (like a developer would) instead of dumping all files upfront. Changes go through an approval workflow before deployment.

### Guided Setup
A 7-step onboarding wizard (`fda onboard`) that walks through:
1. System check
2. API key validation
3. Chat channel setup (Telegram, Discord, Slack)
4. Daily notetaking channel selection
5. User profile
6. Daemon installation (launchd on macOS, systemd on Linux)
7. Completion summary

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
pip install -e ".[web]"        # Web setup UI only
```

## Usage

```bash
# Start the system (runs all agents + bots)
fda start

# Interactive setup
fda onboard

# Ask FDA a question
fda ask "What are our current blockers?"

# Check system status
fda status

# Journal
fda journal search "deployment issue"

# Notetaking channels
fda config notetaking list
fda config notetaking add telegram <channel_id> --label "Client Chat"
fda config notetaking remove telegram <channel_id>

# Web-based setup UI
fda setup
```

### Bot Commands

**Telegram / Discord / Slack:**
- Ask any question in natural language — FDA uses tools to search journal, read chats, check tasks, run commands on local/remote machines
- `!approve <id>` / `!reject <id>` — approve or reject code changes
- `!details <id>` — view full diff of proposed changes

## Architecture

```
fda/
├── orchestrator.py          # Central coordinator — starts all agents + bots,
│                            #   schedules daily operations, routes messages
├── fda_agent.py             # Core FDA agent — onboarding, check-ins, Q&A
├── worker_agent.py          # Remote worker — agentic code ops via SSH
├── local_worker_agent.py    # Local worker — agentic code ops on local filesystem
├── claude_backend.py        # Claude abstraction — CLI (Max sub) or API backend
├── daemon.py                # Daemon installer — launchd (macOS) / systemd (Linux)
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
│   └── project_state.py     #   Tasks, alerts, context, chat history
│
├── clients/                 # Multi-client project management
│   └── client_config.py     #   Per-client VM, repo, and context configs
│
├── remote/                  # Remote VM operations
│   ├── ssh_manager.py       #   SSH with ControlMaster multiplexing
│   └── deploy.py            #   File deployment with backup + rollback
│
└── comms/                   # Inter-agent communication
    └── message_bus.py       #   SQLite message bus with locking
```

### How It Works

1. **Orchestrator** starts all agent threads and bot threads, then enters its main polling loop
2. **Chat bots** (Telegram/Discord/Slack) receive user messages and use Claude's tool-use API to autonomously call tools: search journal, read chats, check tasks, run local/remote commands, dispatch worker tasks
3. **Worker agents** receive task briefs and use tools (`list_directory`, `read_file`, `search_files`, `write_file`, `run_command`) to explore codebases and generate fixes
4. **Approval workflow** — code changes require explicit user approval before deployment
5. **Journal** — all investigations, changes, and deployments are automatically logged
6. **Scheduled tasks** — morning briefings (9 AM) and daily notetaking (9 PM) run automatically

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
OPENAI_API_KEY=sk-...                 # For voice (TTS/STT)
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
- **SQLite** — state persistence, message bus, chat history
- **SSH** (ControlMaster) — remote VM operations with connection multiplexing
- **launchd / systemd** — daemon management

### Dependencies

Core: `anthropic`, `pandas`, `requests`, `flask`
Optional: `python-telegram-bot`, `discord.py[voice]`, `slack-bolt`, `openai`, `msal`

## License

Proprietary - Jae Heuk Jung

## Links

- GitHub: [white-dots/FDA](https://github.com/white-dots/FDA)
- Issues: [github.com/white-dots/FDA/issues](https://github.com/white-dots/FDA/issues)
