# fda/metadata/context.py
"""Loader for ~/.fda/business_context.md.

Rules (per spec § Business context injection):
- Missing file: return empty text, info message, sha=None.
- Oversize file (> 50KB): truncate to 50KB on a UTF-8 char boundary,
  warn via info_message, SHA over the truncated payload.
- Cap is on the BYTES we send to the model (token budget), not on the
  user's file on disk.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 50 * 1024  # 50 KB


@dataclass(frozen=True)
class BusinessContext:
    text: str
    sha256: str | None
    was_truncated: bool
    info_message: str | None


def _truncate_utf8(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # Truncate to max_bytes, then back off to a valid UTF-8 boundary.
    trimmed = encoded[:max_bytes]
    while trimmed:
        try:
            return trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return ""


def load_business_context(path: Path | str) -> BusinessContext:
    p = Path(path)
    if not p.exists():
        return BusinessContext(
            text="",
            sha256=None,
            was_truncated=False,
            info_message=(
                f"ℹ no business_context.md at {p}; using generic classification"
            ),
        )
    raw = p.read_text(encoding="utf-8")
    raw_bytes = raw.encode("utf-8")
    truncated = len(raw_bytes) > MAX_BYTES
    text = _truncate_utf8(raw, MAX_BYTES) if truncated else raw
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    info = None
    if truncated:
        info = (
            f"⚠ business_context.md is {len(raw_bytes)}B; "
            f"truncated to {MAX_BYTES}B for prompt budget"
        )
    return BusinessContext(
        text=text, sha256=sha, was_truncated=truncated, info_message=info,
    )
