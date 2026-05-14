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
