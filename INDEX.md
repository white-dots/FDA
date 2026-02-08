# FDA System - Complete Index

## Quick Navigation

### Getting Started
1. **README.md** - Start here for overview and quick start
2. **SCAFFOLD_SUMMARY.md** - Detailed architecture and components
3. **IMPLEMENTATION_EXAMPLES.md** - Code patterns and examples
4. **FILE_MANIFEST.txt** - Complete file listing with purposes

### Installation & Setup
```bash
cd /sessions/kind-optimistic-bell/mnt/jaeheukjung/Documents/agenthub/fda-system
pip install -e .
```

## Package Contents

### Configuration Layer
- **pyproject.toml** - Package metadata, dependencies, entry points
- **fda/config.py** - Global constants, model names, paths, defaults

### CLI Layer  
- **fda/cli.py** - Command-line interface with argparse
  - Commands: init, start, ask, status, meeting-prep, report, journal, config

### Agent Layer (AI Decision Making)
- **fda/fda_agent.py** - FDA (Facilitating Director Agent)
- **fda/executor_agent.py** - Task execution agent
- **fda/librarian_agent.py** - Knowledge management agent
- **fda/scheduler.py** - Event scheduling using threading
- **fda/outlook.py** - Outlook calendar integration via Microsoft Graph

### Subsystems

#### Communication (fda/comms/)
- **message_bus.py** - Inter-agent message passing with fcntl locking

#### Data Integration (fda/data/)
- **base.py** - Abstract DataAdapter interface
- **api_adapter.py** - REST API integration
- **excel_adapter.py** - Excel/CSV file handling
- **db_adapter.py** - Database adapter (week 2)

#### Knowledge Management (fda/journal/)
- **writer.py** - Journal entry creation with markdown
- **index.py** - Entry indexing and search
- **retriever.py** - Two-pass retrieval with decay ranking

#### State Management (fda/state/)
- **project_state.py** - SQLite-based persistent state

## Architecture Overview

```
fda-system/
├── Core Package (fda/)
│   ├── Agent classes (3 agents)
│   ├── CLI interface
│   ├── Scheduler
│   ├── Calendar integration
│   └── Configuration
├── Communication (fda/comms/)
│   └── Message bus
├── Data Adapters (fda/data/)
│   ├── Abstract base
│   ├── REST API adapter
│   ├── Excel/CSV adapter
│   └── Database adapter
├── Knowledge (fda/journal/)
│   ├── Entry writer
│   ├── Index manager
│   └── Retriever
├── State (fda/state/)
│   └── Project state (SQLite)
└── Documentation
    ├── README.md
    ├── SCAFFOLD_SUMMARY.md
    ├── IMPLEMENTATION_EXAMPLES.md
    ├── FILE_MANIFEST.txt
    └── INDEX.md (this file)
```

## Key Concepts

### Three-Agent Design
1. **FDA Agent** - Strategic oversight, decisions, KPI monitoring
2. **Executor Agent** - Task execution, tracking, blocker management
3. **Librarian Agent** - Knowledge organization, reporting, insight

### Data Flow
```
Outlook Calendar → Scheduler → FDA/Executor/Librarian → Message Bus
                       ↓                                      ↓
                    Event Loop                          Project State
                       ↓
              Data Adapters (API/Excel/DB)
```

### Persistence
- **Project State**: SQLite database with tables for context, tasks, KPIs, alerts, decisions
- **Message Bus**: JSON file with fcntl locking
- **Journal**: Markdown files with YAML frontmatter + JSON index

### Communication Pattern
- File-based message bus for inter-agent messaging
- Message types: task, alert, request
- Priority levels: low, medium, high
- Thread-safe with file locking

## Implementation Roadmap

### Week 1: Core Infrastructure
- [ ] Implement ProjectState.init_db()
- [ ] Implement basic task CRUD
- [ ] Implement MessageBus.send() and get_pending()
- [ ] Set up Scheduler basic functionality

### Week 2: Data Integration
- [ ] Complete DBAdapter implementation
- [ ] Implement ExcelAdapter.pull_latest()
- [ ] Complete APIAdapter with authentication
- [ ] Add data source testing

### Week 3: Agent Logic
- [ ] Implement FDAAgent methods with Claude
- [ ] Implement ExecutorAgent event loop
- [ ] Implement LibrarianAgent reporting
- [ ] Add Outlook calendar integration

## File Locations

**Base Directory:**
```
/sessions/kind-optimistic-bell/mnt/jaeheukjung/Documents/agenthub/fda-system/
```

**Key Files by Responsibility:**

| Task | File |
|------|------|
| Package setup | pyproject.toml |
| Configuration | fda/config.py |
| CLI | fda/cli.py |
| State management | fda/state/project_state.py |
| Agent logic | fda/*_agent.py |
| Data integration | fda/data/*.py |
| Message passing | fda/comms/message_bus.py |
| Knowledge mgmt | fda/journal/*.py |
| Scheduling | fda/scheduler.py |
| Calendars | fda/outlook.py |

## Code Statistics

- **Total Files**: 26 (21 Python + 5 documentation)
- **Total Lines**: ~1,744 (excluding documentation)
- **Type Coverage**: 100% (all functions typed)
- **Docstring Coverage**: 100% (all public methods documented)
- **Package Size**: 136 KB

## Dependencies

### Required
- anthropic >= 0.7.0
- pandas >= 1.5.0
- openpyxl >= 3.9.0
- msal >= 1.20.0
- requests >= 2.28.0

### Optional (Development)
- pytest >= 7.0
- pytest-cov >= 4.0
- black >= 23.0
- ruff >= 0.0.250

## Usage Examples

### Command Line
```bash
# Start the system
fda start --daemon

# Ask a question
fda ask "What are current blockers?"

# Prepare for meeting
fda meeting-prep --id event_12345

# Generate report
fda report daily

# Search journal
fda journal search "kubernetes"
```

### Python API
```python
from fda.fda_agent import FDAAgent
from fda.state.project_state import ProjectState

# Initialize
state = ProjectState()
fda = FDAAgent(state.db_path)

# Use agents
response = fda.ask("Project status?")
kpis = fda.check_kpis()
meeting_brief = fda.prepare_meeting("event_123")
```

## Documentation Files

- **README.md** (3.2 KB) - Quick start and architecture
- **SCAFFOLD_SUMMARY.md** (7.4 KB) - Detailed component breakdown
- **IMPLEMENTATION_EXAMPLES.md** (9.1 KB) - 10 code examples
- **FILE_MANIFEST.txt** (4.8 KB) - Complete file listing
- **INDEX.md** (this file) - Navigation and overview

## Standards & Conventions

### Type Hints
All functions use Python 3.9+ type hints:
```python
def method(param: str, items: list[dict[str, Any]]) -> Optional[Path]:
```

### Docstrings
Google-style docstrings with sections:
```python
"""Short description.

Longer description if needed.

Args:
    param: Description.

Returns:
    Description of return value.
"""
```

### Configuration
Centralized in config.py with Final[] types:
```python
DEFAULT_CHECK_INTERVAL_MINUTES: Final[int] = 15
```

### Error Handling
Placeholder methods raise NotImplementedError with TODO comments:
```python
# TODO: Implement feature description
raise NotImplementedError("Feature not yet implemented")
```

## Next Steps

1. **Read README.md** for overview
2. **Review SCAFFOLD_SUMMARY.md** for architecture details
3. **Check IMPLEMENTATION_EXAMPLES.md** for code patterns
4. **Reference FILE_MANIFEST.txt** for file purposes
5. **Start implementing** following the roadmap

## Support & Questions

Refer to:
1. Class and method docstrings in source files
2. IMPLEMENTATION_EXAMPLES.md for code patterns
3. SCAFFOLD_SUMMARY.md for architecture decisions
4. config.py for configuration options

---

**Created**: February 7, 2025
**Status**: Complete scaffold ready for implementation
**Total Size**: 136 KB, 26 files
