# fda/organize/_sections.py
"""Deterministic structural-fingerprint extraction from document text.

Pure functions, no LLM, no I/O. Used by text-based extractors (PDF,
plaintext, future .hwp) to populate ExtractionResult.sections.
"""

from __future__ import annotations

import re

# Constants — single home; covered by the constants-test pattern.
MAX_SECTIONS_PER_FILE = 15
SECTION_HEADER_MIN_CHARS = 3
SECTION_HEADER_MAX_CHARS = 40
SECTION_SCAN_CHARS = 16 * 1024   # only scan first ~16 K characters; structure tops most docs

# Korean structural-fingerprint coverage. Korean syllables are morpheme-dense:
# 2-syllable labels like 이름 (name), 직위 (position), 부서 (department) are
# routine and meaningful. English keeps min 3 via SECTION_HEADER_MIN_CHARS.
KOREAN_LABEL_MIN_CHARS = 2

# Assigned Hangul Syllables range (U+AC00..U+D7A3). Public so other modules
# (e.g., the csv extractor's per-cell length guard) can reuse it via
# contains_hangul() instead of duplicating the regex char class. Modern
# Korean business-doc text uses precomposed Hangul; Jamo and CJK Hanja are
# intentionally excluded — see spec Non-goals.
HANGUL_RANGE = "가-힣"

# Pattern A: line that is *just* a section header followed by a colon.
#   "Shipping Details:" / "Bill To:" / "Order Details:"
_HEADER_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Za-z0-9 \-/&]{1,38}[A-Za-z0-9]):[ \t]*$"
)

# Pattern B: short ALL-CAPS line that looks like a section divider.
#   "INVOICE" / "TOTAL DUE" / "PURCHASE ORDER"
_ALLCAPS_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Z0-9 \-/&]{2,38}[A-Z0-9])[ \t]*$"
)


def extract_sections_from_text(text: str) -> tuple[str, ...]:
    """Pure function: text → ordered tuple of unique section labels.

    Walks lines ONCE in source order, trying each pattern per line. This
    preserves intra-document order in mixed-pattern documents (e.g., a
    file whose first matching line is ALL-CAPS and whose second is a
    colon-header). Caps at MAX_SECTIONS_PER_FILE.

    Deterministic, side-effect-free, no LLM.
    """
    if not text:
        return ()
    head = text[:SECTION_SCAN_CHARS]
    seen: dict[str, None] = {}   # ordered set
    for line in head.splitlines():
        for rx in (_HEADER_LINE_RE, _ALLCAPS_LINE_RE):
            m = rx.match(line)
            if not m:
                continue
            label = m.group(1).strip()
            if not (SECTION_HEADER_MIN_CHARS <= len(label) <= SECTION_HEADER_MAX_CHARS):
                continue
            # Normalize: collapse internal whitespace; title-case ALL-CAPS for
            # stable keys (so "INVOICE" and "Invoice" don't both appear).
            label = " ".join(label.split())
            if label.isupper():
                label = label.title()
            if label not in seen:
                seen[label] = None
                if len(seen) >= MAX_SECTIONS_PER_FILE:
                    return tuple(seen)
            break  # one match per line is enough; don't double-count
    return tuple(seen)
