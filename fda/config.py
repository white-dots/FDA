"""
Configuration and constants for the FDA system.

Defines model names, file paths, and system defaults.
"""

from pathlib import Path
from typing import Final

# Model names for Claude API
MODEL_FDA: Final[str] = "claude-opus-4-5-20251101"  # Full power for FDA agent
MODEL_EXECUTOR: Final[str] = "claude-3-5-sonnet-20241022"  # Balanced for executor
MODEL_LIBRARIAN: Final[str] = "claude-3-5-sonnet-20241022"  # Balanced for librarian

# Project root and directory structure
PROJECT_ROOT: Final[Path] = Path.home() / "Documents" / "agenthub" / "fda-system"
JOURNAL_DIR: Final[Path] = PROJECT_ROOT / "journal"
STATE_DB_PATH: Final[Path] = PROJECT_ROOT / "state.db"
MESSAGE_BUS_PATH: Final[Path] = PROJECT_ROOT / "message_bus.json"
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
DATA_DIR: Final[Path] = Path.home() / ".fda" / "data"

# Telegram configuration
TELEGRAM_BOT_TOKEN_ENV: Final[str] = "TELEGRAM_BOT_TOKEN"

# Discord configuration
DISCORD_BOT_TOKEN_ENV: Final[str] = "DISCORD_BOT_TOKEN"
DISCORD_CLIENT_ID_ENV: Final[str] = "DISCORD_CLIENT_ID"

# OpenAI configuration (for Whisper STT and TTS)
OPENAI_API_KEY_ENV: Final[str] = "OPENAI_API_KEY"

# Anthropic configuration
ANTHROPIC_API_KEY_ENV: Final[str] = "ANTHROPIC_API_KEY"

# Logging
LOG_DIR: Final[Path] = PROJECT_ROOT / "logs"
LOG_LEVEL: Final[str] = "INFO"
