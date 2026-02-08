# FDA System - Multi-Agent Project Coordination Platform

A sophisticated Python package implementing a distributed multi-agent system for project delivery and coordination using Claude AI models.

## Overview

The FDA System consists of three specialized AI agents:

1. **FDA (Facilitating Director Agent)** - Strategic oversight, KPI monitoring, and decision-making
2. **Executor Agent** - Task execution, delivery tracking, and blocker management
3. **Librarian Agent** - Knowledge management, reporting, and organizational memory

## Quick Start

### Installation

```bash
# Clone and install in development mode
cd /path/to/fda-system
pip install -e .
```

### Basic Usage

```bash
# Initialize a new project
fda init /path/to/project

# Start the system
fda start --daemon

# Ask the FDA agent
fda ask "What are our current blockers?"

# Prepare for a meeting
fda meeting-prep --id event_12345

# Generate reports
fda report daily

# Search the project journal
fda journal search "kubernetes deployment"
```

## Architecture

### Package Structure

```
fda/
├── __init__.py              # Package exports
├── config.py                # Global configuration and constants
├── cli.py                   # Command-line interface
├── fda_agent.py            # FDA agent implementation
├── executor_agent.py       # Executor agent implementation
├── librarian_agent.py      # Librarian agent implementation
├── scheduler.py            # Event and task scheduling
├── outlook.py              # Microsoft Outlook calendar integration
├── comms/                  # Inter-agent communication
│   ├── __init__.py
│   └── message_bus.py      # File-based message bus with locking
├── data/                   # Data source adapters
│   ├── __init__.py
│   ├── base.py            # Abstract DataAdapter
│   ├── api_adapter.py     # REST API adapter
│   ├── excel_adapter.py   # Excel/CSV file adapter
│   └── db_adapter.py      # Database adapter (week 2)
├── journal/               # Project knowledge management
│   ├── __init__.py
│   ├── writer.py          # Markdown entry writing
│   ├── index.py           # Entry indexing and search
│   └── retriever.py       # Two-pass retrieval with decay
└── state/                 # Project state management
    ├── __init__.py
    └── project_state.py   # SQLite state persistence
```

## Core Components

### Configuration (config.py)

Centralized configuration with typed constants:

- **Models**: Claude Opus 4.5 for FDA, Claude 3.5 Sonnet for other agents
- **Paths**: Project root, journal directory, state database
- **Defaults**: Check intervals, meeting prep timing, retrieval settings
- **Decay Rates**: Relevance scoring weights (fast/medium/slow)

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
- `alert_fda(message)` - Send alerts to FDA

### Data Adapters

Pluggable adapters for data sources:

- **APIAdapter** - REST API integration with configurable endpoints
- **ExcelAdapter** - Excel/CSV file watching and reading with pandas
- **DBAdapter** - Database connectivity (week 2)

All adapters implement the `DataAdapter` abstract base class with:
- `test_connection()` - Validate data source access
- `pull_latest(metric)` - Fetch latest metrics
- `get_schema()` - Describe available data

### Communication (message_bus.py)

File-based inter-agent message bus:
- **Persistence**: JSON-based with atomic file locking (fcntl)
- **Features**: Priority levels (low/medium/high), message types, threading
- **Methods**: `send()`, `get_pending()`, `mark_read()`, `get_thread()`

### State Management (project_state.py)

SQLite-based persistent state with tables for:
- **context** - Key-value project configuration
- **tasks** - Task tracking with status and ownership
- **kpi_snapshots** - Historical metric values
- **alerts** - System alerts and notifications
- **decisions** - Recorded decisions with rationale
- **meeting_prep** - Meeting preparation records

### Journal System

Markdown-based project knowledge repository:

**Writer**: Creates entries with YAML frontmatter
- Automatic filename generation from timestamps
- Tag-based organization
- Relevance decay settings

**Index**: JSON-based searchable index
- Full-text search support
- Tag filtering
- Date range queries

**Retriever**: Two-pass ranked retrieval
- Pass 1: Filter by tags and keywords
- Pass 2: Score using (0.6 × relevance + 0.4 × recency)
- Exponential decay for aging entries

### Scheduling (scheduler.py)

Threading-based event scheduler:
- Daily checkin at specified times
- Periodic calendar monitoring
- Generic task registration
- Graceful shutdown with timer cancellation

### Calendar Integration (outlook.py)

Microsoft Outlook calendar via Graph API:
- OAuth authentication (MSAL)
- Event retrieval and filtering
- Meeting time extraction
- Integration with meeting preparation

## Command-Line Interface

Comprehensive CLI with subcommands:

```bash
# Project initialization
fda init <path>

# System control
fda start [--daemon]

# Interaction
fda ask <question>

# Monitoring
fda status

# Meeting management
fda meeting-prep --id <event_id>

# Reporting
fda report {daily|weekly|monthly|project}

# Knowledge management
fda journal search <query>

# Configuration
fda config
```

## Implementation Notes

### Type Hints
All functions use Python 3.9+ type annotations with proper imports.

### Docstrings
Comprehensive docstrings for modules, classes, and all public methods.

### Error Handling
Placeholder implementations raise `NotImplementedError` with TODO comments.

### Dependencies

- **anthropic** - Claude API client
- **pandas** - Data manipulation and analysis
- **openpyxl** - Excel file handling
- **msal** - Microsoft authentication
- **requests** - HTTP library

## Development

### File Locations

All files are located at:
```
/sessions/kind-optimistic-bell/mnt/jaeheukjung/Documents/agenthub/fda-system/
```

### Key Files by Purpose

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package metadata and dependencies |
| `fda/config.py` | Constants and configuration |
| `fda/cli.py` | Command-line interface |
| `fda/state/project_state.py` | Persistent state storage |
| `fda/journal/` | Knowledge management |
| `fda/data/` | Data source integration |
| `fda/comms/message_bus.py` | Inter-agent communication |

### Documentation

- **SCAFFOLD_SUMMARY.md** - Detailed component overview
- **IMPLEMENTATION_EXAMPLES.md** - Code patterns and implementation samples
- **README.md** - This file

## Next Steps

### Week 1
- Implement core agent loops
- Set up database schema and state management
- Create basic task queue

### Week 2
- Implement database adapter
- Add full Claude API integration
- Build scheduler functionality

### Week 3
- Implement all agent methods
- Add Outlook calendar integration
- Build comprehensive tests

### Testing
```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests (once implemented)
pytest

# Check code quality
black . && ruff check .
```

## Configuration

Environment variables and defaults are defined in `fda/config.py`. Key settings:

- `MODEL_FDA`: Claude model for FDA agent (Opus 4.5)
- `MODEL_EXECUTOR`: Claude model for executor (Sonnet 3.5)
- `MODEL_LIBRARIAN`: Claude model for librarian (Sonnet 3.5)
- `DEFAULT_DAILY_CHECKIN_TIME`: "09:00" (9 AM)
- `DEFAULT_CHECK_INTERVAL_MINUTES`: 15
- `DEFAULT_MEETING_PREP_LEAD_TIME_MINUTES`: 30

## License

Proprietary - Jae Heuk Jung

## Support

For implementation questions, refer to:
1. **SCAFFOLD_SUMMARY.md** - Architecture overview
2. **IMPLEMENTATION_EXAMPLES.md** - Code patterns
3. Class docstrings in source files
