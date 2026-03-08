# FDA System — Claude Code Guidelines

## Testing Policy

**All code changes must pass the test suite before being committed.**

Run tests after any modification to `fda/` source files:

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short
```

- 113 tests across 6 test files covering: journal, state, message bus, local worker, remote worker, bots
- A pre-commit hook enforces this automatically — commits are blocked if tests fail
- Never use `--no-verify` to skip tests unless explicitly asked

## Project Structure

- `fda/` — core package (agents, bots, journal, state, comms, config)
- `tests/` — pytest suite with shared fixtures in `conftest.py`
- `pyproject.toml` — pytest config under `[tool.pytest.ini_options]`

## Key Conventions

- Python 3.12+ (pyenv: `/Users/john/.pyenv/versions/3.12.8/bin/python`)
- All agents extend `BaseAgent` (in `fda/base_agent.py`)
- Claude backend abstracted via `get_claude_backend()` — mock in tests
- Journal entries: markdown with YAML frontmatter, indexed in `index.json`
- State: SQLite via `ProjectState`
- Inter-agent comms: `MessageBus` (JSON file with fcntl locking)
- Git repos are never modified by the file organization tools
