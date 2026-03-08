"""
Configuration and constants for the FDA system.

Defines model names, file paths, and system defaults.

Cross-platform support:
- Set FDA_ROOT environment variable to override the default project root
- On macOS: defaults to ~/Documents/agenthub/fda-system
- On Linux: defaults to ~/.fda
- On Windows: defaults to ~/.fda
"""

import os
import sys
from pathlib import Path
from typing import Final


def _get_default_project_root() -> Path:
    """Get the default project root based on platform."""
    # Allow override via environment variable
    if env_root := os.environ.get("FDA_ROOT"):
        return Path(env_root).expanduser()

    # Platform-specific defaults
    if sys.platform == "darwin":
        # macOS: use Documents folder (traditional location)
        return Path.home() / "Documents" / "agenthub" / "fda-system"
    else:
        # Linux/Windows: use hidden directory in home
        return Path.home() / ".fda"


# Model names — used by the API backend; ignored when running via Claude Code CLI
# (Max subscription). The CLI always uses your subscription's model.
# Set FDA_CLAUDE_BACKEND=cli (or just install `claude`) to use Max subscription.
# Set FDA_CLAUDE_BACKEND=api  to use the Anthropic API (pay-per-token).
MODEL_FDA: Final[str] = "claude-sonnet-4-5-20250929"  # Sonnet for quality responses
MODEL_EXECUTOR: Final[str] = "claude-sonnet-4-5-20250929"  # Fast for command execution
MODEL_LIBRARIAN: Final[str] = "claude-sonnet-4-5-20250929"  # Fast for file indexing/search
MODEL_WORKER: Final[str] = "claude-sonnet-4-5-20250929"  # Fast for file identification

# Use Sonnet for quality-critical tasks (code generation, meeting summaries)
MODEL_MEETING_SUMMARY: Final[str] = "claude-sonnet-4-20250514"  # Quality for code gen + summaries
MODEL_CODE_GEN: Final[str] = "claude-sonnet-4-20250514"  # Quality code generation

# Project root and directory structure
PROJECT_ROOT: Final[Path] = _get_default_project_root()
JOURNAL_DIR: Final[Path] = PROJECT_ROOT / "journal"
STATE_DB_PATH: Final[Path] = PROJECT_ROOT / "state.db"
MESSAGE_BUS_PATH: Final[Path] = PROJECT_ROOT / "message_bus.json"
RESTART_MARKER_PATH: Final[Path] = PROJECT_ROOT / ".restart_requested"
INDEX_PATH: Final[Path] = JOURNAL_DIR / "index.json"

# System defaults
DEFAULT_DAILY_CHECKIN_TIME: Final[str] = "09:00"  # 9 AM
DEFAULT_CHECK_INTERVAL_MINUTES: Final[int] = 15  # Check tasks every 15 minutes
DEFAULT_MEETING_PREP_LEAD_TIME_MINUTES: Final[int] = 30  # Prep 30 min before
DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES: Final[int] = 5  # Check calendar every 5 min

# Retrieval constants
DEFAULT_RETRIEVAL_TOP_N: Final[int] = 5
RELEVANCE_WEIGHT: Final[float] = 0.6
RECENCY_WEIGHT: Final[float] = 0.4
DECAY_RATES: Final[dict[str, float]] = {
    "fast": 0.1,      # Decays quickly
    "medium": 0.05,   # Moderate decay
    "slow": 0.01,     # Slow decay
}

# Microsoft Graph API
OUTLOOK_API_ENDPOINT: Final[str] = "https://graph.microsoft.com/v1.0"

# Data directory (for token caches, etc.)
# Always under PROJECT_ROOT for consistency
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"

# Telegram configuration
TELEGRAM_BOT_TOKEN_ENV: Final[str] = "TELEGRAM_BOT_TOKEN"

# Discord configuration
DISCORD_BOT_TOKEN_ENV: Final[str] = "DISCORD_BOT_TOKEN"
DISCORD_CLIENT_ID_ENV: Final[str] = "DISCORD_CLIENT_ID"

# Slack configuration
SLACK_BOT_TOKEN_ENV: Final[str] = "SLACK_BOT_TOKEN"
SLACK_APP_TOKEN_ENV: Final[str] = "SLACK_APP_TOKEN"
SLACK_CHANNEL_ID_ENV: Final[str] = "SLACK_CHANNEL_ID"

# OpenAI configuration (for Whisper STT and TTS)
OPENAI_API_KEY_ENV: Final[str] = "OPENAI_API_KEY"

# OpenAI Realtime API configuration (voice)
OPENAI_REALTIME_MODEL: Final[str] = "gpt-realtime-mini"
OPENAI_REALTIME_VOICE: Final[str] = "alloy"  # alloy, ash, ballad, coral, echo, sage, shimmer, verse
OPENAI_REALTIME_URL: Final[str] = "wss://api.openai.com/v1/realtime"

# Anthropic configuration
ANTHROPIC_API_KEY_ENV: Final[str] = "ANTHROPIC_API_KEY"

# KakaoTalk configuration
KAKAOTALK_EXPORT_DIR: Final[Path] = Path.home() / "Documents" / "fda-exports" / "kakaotalk"
KAKAOTALK_POLL_INTERVAL_SECONDS: Final[int] = 60  # Check for new messages every minute

# Client configuration
CLIENTS_CONFIG_DIR: Final[Path] = Path(__file__).parent / "clients" / "configs"

# Local Worker configuration
# List of local project directories the local worker can read/modify
LOCAL_WORKER_PROJECTS: Final[list[str]] = [
    str(Path.home() / "Documents"),
    str(Path.home() / "Downloads"),
    str(Path.home() / "Desktop"),
]
LOCAL_WORKER_BACKUP_DIR: Final[Path] = DATA_DIR / "local_backups"

# Timeout constants (seconds) for agentic tool-use loops
ANALYZE_TIMEOUT_SECONDS: Final[int] = 300   # 5 minutes for analyze_and_fix
ORGANIZE_TIMEOUT_SECONDS: Final[int] = 600  # 10 minutes for organize_files

# Repository auto-discovery configuration
REPO_DISCOVERY_SKIP_DIRS: Final[frozenset[str]] = frozenset({
    ".Trash", ".Trashes", "node_modules", ".venv", "venv",
    "__pycache__", ".git", "dist", "build", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "htmlcov", ".eggs", "site-packages", ".cache", ".npm",
    ".yarn", "Library", ".local", ".config", ".claude",
    "Applications", "Music", "Movies", "Pictures",
})
REPO_DISCOVERY_MAX_DEPTH: Final[int] = 3
REPO_DISCOVERY_INTERVAL_MINUTES: Final[int] = 60

# Extended thinking
ENABLE_EXTENDED_THINKING: Final[bool] = True
EXTENDED_THINKING_BUDGET: Final[int] = 10000  # max tokens for thinking

# File upload limits
MAX_IMAGE_UPLOAD_MB: Final[int] = 10
MAX_DOCUMENT_UPLOAD_MB: Final[int] = 30
SUPPORTED_IMAGE_TYPES: Final[frozenset[str]] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})
SUPPORTED_DOC_TYPES: Final[frozenset[str]] = frozenset({
    "application/pdf",
})
SUPPORTED_TEXT_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md", ".csv",
    ".xml", ".html", ".css", ".sh", ".sql", ".go", ".rs", ".java", ".c",
    ".cpp", ".h", ".rb", ".php", ".log", ".conf", ".toml", ".ini", ".env",
})

# Conversation history
HISTORY_MESSAGE_LIMIT: Final[int] = 50     # messages to load for LLM context
HISTORY_CHAR_LIMIT: Final[int] = 4000      # per-message character truncation
HISTORY_HOURS_CUTOFF: Final[int] = 72      # hours before messages are considered stale

# Daemon configuration
FDA_DAEMON_LABEL: Final[str] = "com.fda.agent"
FDA_SYSTEMD_NAME: Final[str] = "fda"

# Notetaking
DEFAULT_NOTETAKING_TIME: Final[str] = "21:00"  # 9 PM daily summary

# Daily journal review — morning briefing posted to Discord/Slack
DEFAULT_JOURNAL_REVIEW_TIME: Final[str] = "09:00"  # 9 AM morning briefing

# Logging
LOG_DIR: Final[Path] = PROJECT_ROOT / "logs"
LOG_LEVEL: Final[str] = "INFO"
