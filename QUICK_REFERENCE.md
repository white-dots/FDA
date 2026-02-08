# FDA System - Quick Reference Card

## Package Location
```
/sessions/kind-optimistic-bell/mnt/jaeheukjung/Documents/agenthub/fda-system/
```

## Installation
```bash
cd /path/to/fda-system
pip install -e .
```

## CLI Commands

| Command | Purpose | Example |
|---------|---------|---------|
| `fda init` | Initialize project | `fda init /path` |
| `fda start` | Start system | `fda start --daemon` |
| `fda ask` | Ask FDA agent | `fda ask "What's the status?"` |
| `fda status` | Show status | `fda status` |
| `fda meeting-prep` | Prepare meeting | `fda meeting-prep --id evt_123` |
| `fda report` | Generate report | `fda report daily` |
| `fda journal` | Journal operations | `fda journal search "keyword"` |
| `fda config` | Show config | `fda config` |

## Core Classes & Methods

### FDAAgent
```python
from fda.fda_agent import FDAAgent

agent = FDAAgent(project_state_path)
agent.onboard()                           # Initialize
agent.daily_checkin()                     # Health check
agent.ask("question")                     # Ask
agent.review_task(task_id)               # Review
agent.check_kpis()                       # Monitor KPIs
agent.prepare_meeting(event_id)          # Meeting brief
```

### ExecutorAgent
```python
from fda.executor_agent import ExecutorAgent

executor = ExecutorAgent(project_state_path)
executor.run_event_loop()                # Start execution
executor.pick_up_task()                  # Get next task
executor.execute_task(task)              # Execute
executor.request_review(task_id)         # Get feedback
executor.report_blocker(task_id, reason) # Report blocker
```

### LibrarianAgent
```python
from fda.librarian_agent import LibrarianAgent

librarian = LibrarianAgent(project_state_path)
librarian.run_event_loop()               # Start processing
librarian.generate_report(type)          # Generate report
librarian.generate_meeting_brief(event)  # Meeting brief
librarian.write_journal_entry(entry)     # Write entry
librarian.update_index()                 # Update index
librarian.alert_fda(message)             # Send alert
```

### ProjectState
```python
from fda.state import ProjectState

state = ProjectState()
state.set_context("key", value)          # Set context
state.get_context("key")                 # Get context
state.add_task(title, desc, owner)       # Add task
state.update_task(id, status="done")     # Update task
state.get_tasks(status="pending")        # Get tasks
state.add_kpi_snapshot(metric, value)    # Record KPI
state.get_latest_kpi(metric)             # Get latest KPI
state.add_alert(level, message, source)  # Add alert
state.add_decision(title, rationale, ...)# Record decision
state.record_meeting_prep(...)           # Record prep
```

### MessageBus
```python
from fda.comms import MessageBus

bus = MessageBus()
msg_id = bus.send(from_agent, to_agent, msg_type, subject, body)
messages = bus.get_pending(agent_name)
bus.mark_read(msg_id)
thread = bus.get_thread(msg_id)
```

### JournalWriter
```python
from fda.journal import JournalWriter

writer = JournalWriter()
path = writer.write_entry(
    author="agent_name",
    tags=["tag1", "tag2"],
    summary="Summary line",
    content="# Markdown content",
    relevance_decay="medium"
)
```

### JournalRetriever
```python
from fda.journal import JournalRetriever

retriever = JournalRetriever()
results = retriever.retrieve(
    query_tags=["tag1"],
    query_text="search text",
    top_n=5
)
```

### Data Adapters
```python
from fda.data import APIAdapter, ExcelAdapter

# REST API
api = APIAdapter({"base_url": "...", "endpoints": {...}})
api.test_connection()
api.pull_latest(metric="revenue")
api.get_schema()

# Excel/CSV
excel = ExcelAdapter(Path("watch_dir"))
excel.test_connection()
excel.pull_latest()
excel.get_schema()
```

### Scheduler
```python
from fda.scheduler import Scheduler

scheduler = Scheduler()
scheduler.register_daily_checkin("09:00")
scheduler.register_calendar_watcher(5)    # every 5 minutes
scheduler.run()                          # Start
scheduler.stop()                         # Stop
```

### OutlookCalendar
```python
from fda.outlook import OutlookCalendar

cal = OutlookCalendar(client_id, tenant_id, client_secret)
cal.authenticate()
cal.get_events_today()
cal.get_upcoming_events(within_minutes=45)
cal.get_event_details(event_id)
```

## Configuration Constants

All in `fda/config.py`:

```python
# Models
MODEL_FDA = "claude-opus-4-5-20251101"
MODEL_EXECUTOR = "claude-3-5-sonnet-20241022"
MODEL_LIBRARIAN = "claude-3-5-sonnet-20241022"

# Paths
PROJECT_ROOT = Path.home() / "Documents" / "agenthub" / "fda-system"
JOURNAL_DIR = PROJECT_ROOT / "journal"
STATE_DB_PATH = PROJECT_ROOT / "state.db"
MESSAGE_BUS_PATH = PROJECT_ROOT / "message_bus.json"

# Timing
DEFAULT_DAILY_CHECKIN_TIME = "09:00"
DEFAULT_CHECK_INTERVAL_MINUTES = 15
DEFAULT_MEETING_PREP_LEAD_TIME_MINUTES = 30
DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES = 5

# Scoring
DEFAULT_RETRIEVAL_TOP_N = 5
RELEVANCE_WEIGHT = 0.6
RECENCY_WEIGHT = 0.4
DECAY_RATES = {"fast": 0.1, "medium": 0.05, "slow": 0.01}
```

## File Reference

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package metadata |
| `fda/config.py` | Configuration |
| `fda/cli.py` | CLI interface |
| `fda/fda_agent.py` | FDA agent |
| `fda/executor_agent.py` | Executor agent |
| `fda/librarian_agent.py` | Librarian agent |
| `fda/scheduler.py` | Event scheduler |
| `fda/outlook.py` | Calendar integration |
| `fda/comms/message_bus.py` | Message bus |
| `fda/data/base.py` | Data adapter base |
| `fda/data/api_adapter.py` | REST API adapter |
| `fda/data/excel_adapter.py` | Excel/CSV adapter |
| `fda/journal/writer.py` | Journal writer |
| `fda/journal/index.py` | Journal index |
| `fda/journal/retriever.py` | Journal retriever |
| `fda/state/project_state.py` | Project state |

## Database Tables

SQLite tables in `state.db`:

- `context` - Key-value project configuration
- `tasks` - Task tracking (id, title, status, owner, priority, due_date)
- `kpi_snapshots` - Historical metrics (metric, value, timestamp)
- `alerts` - System alerts (level, message, source, timestamp)
- `decisions` - Recorded decisions (title, rationale, decision_maker, impact)
- `meeting_prep` - Meeting preparation records (event_id, brief, created_by)

## JSON Files

- `message_bus.json` - Inter-agent messages with metadata
- `journal/index.json` - Journal entry index with metadata

## Markdown Files

- `journal/*.md` - Journal entries with YAML frontmatter

## Type Annotations

All code uses Python 3.9+ type hints:

```python
# Common patterns
def func(param: str) -> None: ...
def func(items: list[str]) -> dict[str, Any]: ...
def func(path: Path) -> Optional[dict]: ...
def func(callback: Callable[[int], None]) -> None: ...
```

## Error Handling

Placeholder methods raise `NotImplementedError`:

```python
# TODO: Implement feature
raise NotImplementedError("Feature not yet implemented")
```

## Dependencies

```
anthropic>=0.7.0
pandas>=1.5.0
openpyxl>=3.9.0
msal>=1.20.0
requests>=2.28.0
```

## Documentation Files

- **INDEX.md** - Navigation guide
- **README.md** - Overview and quick start
- **SCAFFOLD_SUMMARY.md** - Architecture details
- **IMPLEMENTATION_EXAMPLES.md** - Code examples
- **FILE_MANIFEST.txt** - File listing
- **QUICK_REFERENCE.md** - This file

## Implementation Roadmap

**Week 1**: Core infrastructure
- ProjectState tables and methods
- MessageBus basic operations
- Scheduler foundation

**Week 2**: Data integration
- APIAdapter full implementation
- ExcelAdapter methods
- DBAdapter setup

**Week 3**: Agent logic
- FDAAgent Claude integration
- ExecutorAgent event loop
- LibrarianAgent reporting
- Outlook calendar integration

## Testing Installation

```bash
# Install in development mode
pip install -e .

# Verify installation
python -c "import fda; print(fda.__version__)"

# Test CLI
fda --help

# Test imports
python -c "from fda.fda_agent import FDAAgent; print(FDAAgent)"
```

---

For more details, see the full documentation files in the package directory.
