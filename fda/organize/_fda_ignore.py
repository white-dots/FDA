"""Root-only file-pinning via .fda-ignore.

Reader stage utility: decide which root-level files FDA must leave untouched
during organization. Patterns combine built-in defaults with the user's
optional target/.fda-ignore file (additive only — no negations).
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BUILTIN_DEFAULTS: frozenset[str] = frozenset({
    ".fda-ignore",
    "manifest.csv",
    "README.md",
    "README.*",
    "LICENSE",
    "LICENSE.*",
})


def load_patterns(target: Path) -> tuple[str, ...]:
    """Return defaults ∪ user patterns. Order: defaults first, then user.

    Reads `target/.fda-ignore` if present. Strips `# comments` and blank
    lines. Unreadable file (permission error, decode error) → defaults only,
    with a warning to the logger. Never raises.
    """
    user: list[str] = []
    ignore_file = target / ".fda-ignore"
    if ignore_file.is_file():
        try:
            for raw in ignore_file.read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].strip()
                if line:
                    user.append(line)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(".fda-ignore unreadable at %s: %s", ignore_file, e)
    return tuple(sorted(BUILTIN_DEFAULTS)) + tuple(user)
