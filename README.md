# FDA System - Multi-Agent Project Coordination Platform

A sophisticated Python package implementing a distributed multi-agent system for project delivery and coordination using Claude AI models.

## Overview

The FDA System consists of three specialized AI agents:

1. **FDA (Facilitating Director Agent)** - Strategic oversight, KPI monitoring, and decision-making
2. **Executor Agent** - Task execution, delivery tracking, and blocker management
3. **Librarian Agent** - Knowledge management, reporting, and organizational memory

Plus integrations for:
- **Telegram** - Bot for queries and proactive notifications
- **Discord** - Voice channel participation with speech-to-text and text-to-speech
- **Office 365** - Calendar integration via device code login

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/white-dots/FDA.git
cd FDA

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install the package
pip install -e .

# Install optional dependencies for communication features
pip install -e ".[all]"  # Everything
# Or individually:
pip install -e ".[telegram]"  # Telegram bot
pip install -e ".[discord]"   # Discord voice bot
pip install -e ".[web]"       # Web setup UI
```

### Web-Based Setup (Recommended)

The easiest way to configure FDA is through the web interface:

```bash
# Start the setup server
fda setup

# Then open http://localhost:9999 in your browser
```

The web UI lets you:
- Configure all API tokens (Anthropic, Telegram, Discord, OpenAI)
- Test connections
- Generate Discord bot invite links
- View system health status

### CLI Setup (Alternative)

```bash
# Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# Initialize a new project
fda init /path/to/project

# Configure Telegram (interactive)
fda telegram setup

# Configure Discord (interactive)
fda discord setup

# Connect Office 365 calendar
fda calendar login
```

### Basic Usage

```bash
# Start the system
fda start --daemon

# Ask the FDA agent
fda ask "What are our current blockers?"

# Show project status
fda status

# Prepare for a meeting
fda meeting-prep --id event_12345

# Generate reports
fda report daily

# Search the project journal
fda journal search "kubernetes deployment"
```

### Start Communication Bots

```bash
# Start Telegram bot (run in separate terminal)
fda telegram start

# Start Discord bot (run in separate terminal)
fda discord start

# Get Discord bot invite link
fda discord invite
```

## VM Deployment

For deploying FDA on a virtual machine:

```bash
# 1. SSH into your VM
ssh user@your-vm-ip

# 2. Install Python 3.9+
sudo apt update && sudo apt install python3.9 python3.9-venv python3-pip

# 3. Clone and install
git clone https://github.com/white-dots/FDA.git
cd FDA
python3.9 -m venv venv
source venv/bin/activate
pip install -e ".[all]"

# 4. Start the web setup (accessible from your browser)
fda setup --host 0.0.0.0 --port 9999

# 5. Open http://your-vm-ip:9999 to configure

# 6. Start the bots (use screen/tmux for persistence)
screen -S telegram
fda telegram start
# Ctrl+A, D to detach

screen -S discord
fda discord start
# Ctrl+A, D to detach
```

## Architecture

### Package Structure

```
fda/
├── __init__.py              # Package exports
├── config.py                # Global configuration and constants
├── cli.py                   # Command-line interface
├── setup_server.py          # Web-based setup UI (port 9999)
├── base_agent.py            # Base class for all agents
├── fda_agent.py             # FDA agent implementation
├── executor_agent.py        # Executor agent implementation
├── librarian_agent.py       # Librarian agent implementation
├── telegram_bot.py          # Telegram bot integration
├── discord_bot.py           # Discord voice bot integration
├── scheduler.py             # Event and task scheduling
├── outlook.py               # Microsoft Outlook calendar integration
├── comms/                   # Inter-agent communication
│   └── message_bus.py       # File-based message bus with locking
├── data/                    # Data source adapters
│   ├── api_adapter.py       # REST API adapter
│   ├── excel_adapter.py     # Excel/CSV file adapter
│   └── db_adapter.py        # Database adapter
├── journal/                 # Project knowledge management
│   ├── writer.py            # Markdown entry writing
│   ├── index.py             # Entry indexing and search
│   └── retriever.py         # Two-pass retrieval with decay
└── state/                   # Project state management
    └── project_state.py     # SQLite state persistence
```

## Core Components

### Agents

#### FDAAgent
Strategic oversight and decision making.
- `onboard()` - Initialize new projects
- `daily_checkin()` - Health check and alert generation
- `ask(question)` - Interactive question answering
- `review_task(task_id)` - Task feedback and coaching
- `check_kpis()` - Monitor key performance indicators
- `prepare_meeting(event_id)` - Generate meeting briefs

#### ExecutorAgent
Task delivery and execution.
- `run_event_loop()` - Main execution loop
- `pick_up_task()` - Get next task from queue
- `execute_task(task)` - Execute task with tracking
- `request_review(task_id)` - Request FDA feedback
- `report_blocker(task_id, reason)` - Alert on blockers

#### LibrarianAgent
Knowledge organization and reporting.
- `run_event_loop()` - Main processing loop
- `generate_report(type)` - Create reports (daily/weekly/monthly/project)
- `generate_meeting_brief(event)` - Generate meeting materials
- `write_journal_entry(entry)` - Write markdown with frontmatter
- `update_index()` - Maintain searchable index

### Communication Integrations

#### Telegram Bot
- `/start` - Register for notifications
- `/ask <question>` - Ask FDA about the project
- `/status` - Get project status
- `/tasks` - List current tasks
- `/alerts` - Show unacknowledged alerts
- Proactive alert notifications to registered users

#### Discord Voice Bot
- `!join` - Join your voice channel
- `!leave` - Leave voice channel
- `!ask <question>` - Ask FDA (responds in voice)
- `!status` - Show project status
- `!say <text>` - Speak text in voice channel
- Automatic session transcripts logged to journal

### Data Adapters

Pluggable adapters for data sources:

- **APIAdapter** - REST API integration with configurable endpoints
- **ExcelAdapter** - Excel/CSV file watching and reading with pandas
- **DBAdapter** - Database connectivity

### State Management

SQLite-based persistent state with tables for:
- **context** - Key-value project configuration
- **tasks** - Task tracking with status and ownership
- **kpi_snapshots** - Historical metric values
- **alerts** - System alerts and notifications
- **decisions** - Recorded decisions with rationale
- **meeting_prep** - Meeting preparation records
- **telegram_users** - Registered Telegram users
- **discord_sessions** - Voice session history

### Journal System

Markdown-based project knowledge repository with:
- Automatic YAML frontmatter generation
- Tag-based organization
- Two-pass ranked retrieval (relevance + recency scoring)
- Exponential decay for aging entries

## Command-Line Interface

```bash
# Project management
fda init <path>              # Initialize project
fda start [--daemon]         # Start system
fda status                   # Show status
fda config                   # Show configuration

# Interaction
fda ask <question>           # Ask FDA
fda meeting-prep --id <id>   # Prepare for meeting
fda report {daily|weekly|monthly|project}

# Task management
fda task add <title> --owner <name>
fda task list [--status pending|in_progress|completed|blocked]
fda task update <id> --status <status>

# Journal
fda journal search <query>
fda journal write --author <name> --tags <tags> --summary <text> --content <text>

# Telegram
fda telegram setup           # Configure bot token
fda telegram status          # Show bot status
fda telegram start           # Start bot
fda telegram test            # Send test message

# Discord
fda discord setup            # Configure bot token
fda discord status           # Show bot status
fda discord start            # Start bot
fda discord invite           # Get invite link

# Calendar
fda calendar login           # Log in to Office 365
fda calendar logout          # Log out
fda calendar status          # Check connection
fda calendar today           # Show today's events
fda calendar upcoming        # Show upcoming events

# Setup
fda setup [--port 9999]      # Start web setup UI
```

## Dependencies

### Core
- **anthropic** - Claude API client
- **pandas** - Data manipulation
- **openpyxl** - Excel file handling
- **msal** - Microsoft authentication
- **requests** - HTTP library

### Optional
- **python-telegram-bot** - Telegram integration
- **discord.py[voice]** - Discord integration
- **openai** - Whisper STT and TTS for voice
- **pynacl** - Voice encryption for Discord
- **flask** - Web setup UI

Install with:
```bash
pip install -e ".[all]"  # Everything
pip install -e ".[telegram]"  # Just Telegram
pip install -e ".[discord]"  # Just Discord
pip install -e ".[web]"  # Just web UI
```

## Configuration

### Environment Variables

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Telegram (optional - can use fda telegram setup instead)
TELEGRAM_BOT_TOKEN=123456789:ABCDefGHI...

# Discord (optional - can use fda discord setup instead)
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_CLIENT_ID=your_client_id

# OpenAI for voice features (optional)
OPENAI_API_KEY=sk-...
```

### Model Configuration

Defined in `fda/config.py`:
- `MODEL_FDA`: Claude Opus 4.5 for FDA agent
- `MODEL_EXECUTOR`: Claude 3.5 Sonnet for executor
- `MODEL_LIBRARIAN`: Claude 3.5 Sonnet for librarian

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Check code quality
black . && ruff check .
```

## License

Proprietary - Jae Heuk Jung

## Support

- GitHub Issues: https://github.com/white-dots/FDA/issues
- Documentation files: `SCAFFOLD_SUMMARY.md`, `IMPLEMENTATION_EXAMPLES.md`
