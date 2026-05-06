# fda/organize/_logger.py
"""Structured grep-friendly logger for organize runs.

Produces a single line per event:
    [HH:MM:SS.mmm] EVENT_TYPE key1=value1 key2="value with spaces"

Strings are emitted via json.dumps so embedded newlines/quotes can't break the
one-line-per-event contract. Numbers and booleans are emitted bare. The logger
also forwards each rendered line to an optional progress_callback so the live
UI feed shows the same events the file does.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_LOG_ROOT = Path.home() / ".fda" / "logs" / "organize"
LONG_VALUE_TRUNCATE_CHARS = 200

_LONG_VALUE_KEYS = frozenset({"summary", "instructions", "fallback", "reason"})


def _now_stamp() -> str:
    """YYYYMMDD-HHMMSS-mmm in local time. Indirection lets tests freeze it."""
    now = datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


def _format_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = value if isinstance(value, str) else str(value)
    if key in _LONG_VALUE_KEYS and len(text) > LONG_VALUE_TRUNCATE_CHARS:
        text = text[:LONG_VALUE_TRUNCATE_CHARS] + "…"
    return json.dumps(text, ensure_ascii=False)


class OrganizeLogger:
    """One-line-per-event structured logger.

    log_path semantics:
        None  -> auto-pick under DEFAULT_LOG_ROOT
        Path  -> use exactly that path
        False -> disable file logging; progress_callback still fires.
    """

    def __init__(
        self,
        log_path: Path | bool | None,
        *,
        target_basename: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._progress = progress_callback
        self._fh = None
        self.path: Path | None = None

        if log_path is False:
            return
        chosen = self._resolve_path(log_path, target_basename)
        try:
            chosen.parent.mkdir(parents=True, exist_ok=True)
            # If something already exists at the auto-picked path, append the PID.
            if log_path is None and chosen.exists():
                chosen = chosen.with_name(chosen.stem + f"-pid{os.getpid()}.log")
            self._fh = open(chosen, "w", encoding="utf-8", buffering=1)
        except OSError as e:
            logger.warning("could not open organize log %s: %s", chosen, e)
            self._fh = None
            self.path = None
            return
        self.path = chosen
        # Surface the log path via progress_callback right after open, so
        # CLI / Telegram / web feeds see it before the first event lands.
        if self._progress is not None:
            try:
                self._progress(f"📝 logging to {chosen}")
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)

    @staticmethod
    def _resolve_path(log_path: Path | None, target_basename: str) -> Path:
        if isinstance(log_path, Path):
            return log_path
        stem = f"{_now_stamp()}-{target_basename}"
        return DEFAULT_LOG_ROOT / f"{stem}.log"

    def log(self, event: str, **fields: object) -> None:
        now = datetime.now()
        ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        parts = [f"[{ts}] {event}"]
        for key, value in fields.items():
            parts.append(f"{key}={_format_value(key, value)}")
        line = " ".join(parts)
        if self._fh is not None:
            try:
                self._fh.write(line + "\n")
            except OSError as e:
                logger.warning("write to organize log failed: %s", e)
        if self._progress is not None:
            try:
                self._progress(line)
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
