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
MODEL_FDA: Final[str] = "claude-3-5-haiku-20241022"  # Fast for routing/classification
MODEL_EXECUTOR: Final[str] = "claude-3-5-haiku-20241022"  # Fast for command execution
MODEL_LIBRARIAN: Final[str] = "claude-3-5-haiku-20241022"  # Fast for file indexing/search
MODEL_WORKER: Final[str] = "claude-3-5-haiku-20241022"  # Fast for file identification

# Use Sonnet for quality-critical tasks (code generation, meeting summaries)
MODEL_MEETING_SUMMARY: Final[str] = "claude-sonnet-4-20250514"  # Quality for code gen + summaries
MODEL_CODE_GEN: Final[str] = "claude-sonnet-4-20250514"  # Quality code generation

# Project root and directory structure
PROJECT_ROOT: Final[Path] = _get_default_project_root()
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
# Always under PROJECT_ROOT for consistency
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"

# Telegram configuration
TELEGRAM_BOT_TOKEN_ENV: Final[str] = "TELEGRAM_BOT_TOKEN"

# Discord configuration
DISCORD_BOT_TOKEN_ENV: Final[str] = "DISCORD_BOT_TOKEN"
DISCORD_CLIENT_ID_ENV: Final[str] = "DISCORD_CLIENT_ID"

# OpenAI configuration (for Whisper STT and TTS)
OPENAI_API_KEY_ENV: Final[str] = "OPENAI_API_KEY"

# OpenAI Realtime API configuration (voice)
OPENAI_REALTIME_MODEL: Final[str] = "gpt-4o-realtime-preview-2025-06-03"
OPENAI_REALTIME_VOICE: Final[str] = "alloy"  # alloy, ash, ballad, coral, echo, sage, shimmer, verse
OPENAI_REALTIME_URL: Final[str] = "wss://api.openai.com/v1/realtime"

# Anthropic configuration
ANTHROPIC_API_KEY_ENV: Final[str] = "ANTHROPIC_API_KEY"

# KakaoTalk configuration
KAKAOTALK_EXPORT_DIR: Final[Path] = Path.home() / "Documents" / "fda-exports" / "kakaotalk"
KAKAOTALK_POLL_INTERVAL_SECONDS: Final[int] = 60  # Check for new messages every minute

# Client configuration
CLIENTS_CONFIG_DIR: Final[Path] = Path(__file__).parent / "clients" / "configs"

# Logging
LOG_DIR: Final[Path] = PROJECT_ROOT / "logs"
LOG_LEVEL: Final[str] = "INFO"
