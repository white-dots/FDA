# fda/metadata/enrich.py
"""Deterministic per-file enrichment: sha256, mime, language, mtime.

No LLM. No network. Pure functions of file content + path.
"""
from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

_HANGUL_RANGES = ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F))


def sha256_of(path: Path | str, *, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of the file's content, hex-encoded."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def mime_of(path: Path | str) -> str:
    """Best-effort MIME type. Falls back to application/octet-stream."""
    guess, _ = mimetypes.guess_type(str(path))
    if guess:
        return guess
    # mimetypes returns None for many text-y files; return a generic fallback.
    return "application/octet-stream"


def language_of(text: str) -> str:
    """Tiny language heuristic: ko if any Hangul codepoint, else en, else unknown.

    Deliberately simple — the metadata layer just needs a 'which surface
    label set should I default to' hint. Real per-document language is a
    Phase 2 concern.
    """
    if not text:
        return "unknown"
    for ch in text:
        cp = ord(ch)
        for lo, hi in _HANGUL_RANGES:
            if lo <= cp <= hi:
                return "ko"
    return "en"


def mtime_iso(path: Path | str) -> str:
    """File mtime as ISO-8601 UTC ending in Z, with microsecond resolution.

    Uses `timespec="microseconds"` so the string format is constant — every
    output has exactly 6 fractional digits. This matters for lexicographic
    sorting (the DB stores mtime as a string; SQLite ORDER BY uses string
    comparison) and for detecting same-second file changes.
    """
    ts = Path(path).stat().st_mtime
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
