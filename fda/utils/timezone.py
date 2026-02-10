"""
Timezone utilities for FDA system.

Provides timezone-aware date/time handling for the personal assistant.
Cross-platform support for macOS, Linux, and Windows.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore


# Common timezone abbreviations to full IANA names
TIMEZONE_ABBREVIATIONS: dict[str, str] = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "GMT": "Europe/London",
    "UTC": "UTC",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland",
}


def get_full_timezone_name(abbrev: str) -> Optional[str]:
    """
    Convert a timezone abbreviation to full IANA timezone name.

    Args:
        abbrev: Timezone abbreviation (e.g., "PST", "EST", "JST")

    Returns:
        Full IANA timezone name or None if not recognized
    """
    return TIMEZONE_ABBREVIATIONS.get(abbrev.upper())


def validate_timezone(tz_str: str) -> Optional[str]:
    """
    Validate and normalize a timezone string.

    Args:
        tz_str: Timezone string (abbreviation or IANA name)

    Returns:
        Valid IANA timezone name or None if invalid
    """
    if not tz_str:
        return None

    tz_str = tz_str.strip()

    # First, check if it's an abbreviation
    full_name = get_full_timezone_name(tz_str)
    if full_name:
        return full_name

    # Try as IANA name directly
    try:
        ZoneInfo(tz_str)
        return tz_str
    except Exception:
        return None


def get_user_timezone(state) -> Optional[str]:
    """
    Get user's timezone from state.

    Args:
        state: ProjectState instance

    Returns:
        User's timezone string or None
    """
    return state.get_context("user_timezone")


def get_timezone_info(timezone_str: Optional[str]) -> Optional[ZoneInfo]:
    """
    Get ZoneInfo object for a timezone string.

    Args:
        timezone_str: IANA timezone name

    Returns:
        ZoneInfo object or None for system default
    """
    if timezone_str:
        try:
            return ZoneInfo(timezone_str)
        except Exception:
            return None
    return None


def get_local_today(timezone_str: Optional[str]) -> tuple[datetime, datetime]:
    """
    Get start and end of today in user's timezone.

    Args:
        timezone_str: User's timezone (IANA name)

    Returns:
        Tuple of (start_of_day, end_of_day) as datetime objects
    """
    tz = get_timezone_info(timezone_str)

    if tz:
        now = datetime.now(tz)
    else:
        now = datetime.now()

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    return start_of_day, end_of_day


def get_current_time_for_user(timezone_str: Optional[str]) -> datetime:
    """
    Get current time in user's timezone.

    Args:
        timezone_str: User's timezone (IANA name)

    Returns:
        Current datetime in user's timezone (or local if None)
    """
    tz = get_timezone_info(timezone_str)

    if tz:
        return datetime.now(tz)
    return datetime.now()


def format_time_for_user(dt: datetime, timezone_str: Optional[str], fmt: str = "%I:%M %p") -> str:
    """
    Format a datetime for display in user's timezone.

    Args:
        dt: Datetime to format
        timezone_str: User's timezone
        fmt: strftime format string

    Returns:
        Formatted time string
    """
    tz = get_timezone_info(timezone_str)

    if tz and dt.tzinfo is None:
        # Naive datetime, assume UTC and convert
        dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    elif tz:
        dt = dt.astimezone(tz)

    return dt.strftime(fmt)


def detect_system_timezone() -> Optional[str]:
    """
    Detect the system timezone.

    Works cross-platform:
    - macOS: Uses systemsetup or /etc/localtime
    - Linux: Checks /etc/timezone, /etc/localtime, or timedatectl
    - Windows: Uses tzlocal if available, otherwise returns None

    Returns:
        IANA timezone name or None if detection fails
    """
    # Try TZ environment variable first
    if tz_env := os.environ.get("TZ"):
        if validate_timezone(tz_env):
            return tz_env

    if sys.platform == "darwin":
        # macOS
        return _detect_macos_timezone()
    elif sys.platform.startswith("linux"):
        # Linux
        return _detect_linux_timezone()
    else:
        # Windows or other - try tzlocal package
        return _detect_tzlocal()


def _detect_macos_timezone() -> Optional[str]:
    """Detect timezone on macOS."""
    try:
        result = subprocess.run(
            ["systemsetup", "-gettimezone"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Output: "Time Zone: America/Los_Angeles"
            line = result.stdout.strip()
            if ":" in line:
                tz = line.split(":", 1)[1].strip()
                if validate_timezone(tz):
                    return tz
    except Exception:
        pass

    # Fallback: read /etc/localtime symlink
    return _detect_from_localtime()


def _detect_linux_timezone() -> Optional[str]:
    """Detect timezone on Linux."""
    # Method 1: /etc/timezone (Debian/Ubuntu)
    timezone_file = Path("/etc/timezone")
    if timezone_file.exists():
        try:
            tz = timezone_file.read_text().strip()
            if validate_timezone(tz):
                return tz
        except Exception:
            pass

    # Method 2: timedatectl (systemd)
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            tz = result.stdout.strip()
            if validate_timezone(tz):
                return tz
    except Exception:
        pass

    # Method 3: /etc/localtime symlink
    return _detect_from_localtime()


def _detect_from_localtime() -> Optional[str]:
    """Detect timezone from /etc/localtime symlink."""
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        try:
            target = os.readlink(localtime)
            # Target like: /usr/share/zoneinfo/America/Los_Angeles
            if "zoneinfo" in target:
                parts = target.split("zoneinfo/")
                if len(parts) > 1:
                    tz = parts[1]
                    if validate_timezone(tz):
                        return tz
        except Exception:
            pass
    return None


def _detect_tzlocal() -> Optional[str]:
    """Try to detect timezone using tzlocal package."""
    try:
        import tzlocal
        tz = tzlocal.get_localzone_name()
        if validate_timezone(tz):
            return tz
    except Exception:
        pass
    return None
