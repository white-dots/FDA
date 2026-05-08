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

# Precompiled once for cheap reuse by contains_hangul().
_HANGUL_SEARCH_RE = re.compile(rf"[{HANGUL_RANGE}]")


def contains_hangul(text: str) -> bool:
    """True iff `text` contains at least one Hangul Syllables character.

    Public so other modules (e.g., the csv extractor's per-cell length
    guard) can apply per-script logic without re-deriving the regex char
    class from HANGUL_RANGE. Excludes Jamo and CJK Hanja by construction
    of HANGUL_RANGE itself — see spec Non-goals.
    """
    return bool(_HANGUL_SEARCH_RE.search(text))


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

# Bounded char-class repetition ({0,40} / {2,40}) caps regex work at
# ~SECTION_HEADER_MAX_CHARS per match attempt, eliminating the
# catastrophic-backtracking risk on long malformed Hangul lines (e.g.,
# a 16K all-Hangul bracketed line missing its closer would otherwise
# cost ~6.7s with an unbounded *? capture; bounded is ~10µs). The
# lookahead ensures at least one Hangul appears within the bounded
# window so English-only bracketed lines fail the lookahead immediately.
# ASCII space, tab, and U+3000 ideographic space are accepted at line
# leading/trailing/inside-bracket positions; the latter is common in
# Korean templates.
_KOREAN_BRACKET_RE = re.compile(
    r"^[ \t　]*[\[【《〔「][ \t　]*"
    rf"(?=[^\[\]【】《》〔〕「」]{{0,40}}[{HANGUL_RANGE}])"
    rf"([^\[\]【】《》〔〕「」]{{2,40}})"
    r"[ \t　]*[\]】》〕」][ \t　]*$"
)

# (regex, min_chars, normalize_case)
# Tuple-driven iteration so each pattern carries its own length guard and
# whether the existing ALL-CAPS-to-Title-case rule should fire. Korean
# patterns will be appended in subsequent tasks with normalize_case=False
# (Hangul has no case; "KPI 지표".isupper() is True because cased K, P, I
# are uppercase, so an untagged .title() pass would mutate the label).
#
# IMPORTANT: every regex referenced here must be defined ABOVE this
# tuple in the file. Tasks 4/5/6 each insert a new Korean regex between
# _ALLCAPS_LINE_RE and _PATTERNS — never below _PATTERNS — so this tuple
# remains the last definition before extract_sections_from_text. Putting
# a regex below _PATTERNS would cause a module-import-time NameError.
_PATTERNS: tuple[tuple[re.Pattern[str], int, bool], ...] = (
    (_HEADER_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_ALLCAPS_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_KOREAN_BRACKET_RE, KOREAN_LABEL_MIN_CHARS, False),
)


def extract_sections_from_text(text: str) -> tuple[str, ...]:
    """Pure function: text → ordered tuple of unique section labels.

    Walks lines ONCE in source order, trying each compiled regex per line
    via _PATTERNS. Each entry pairs a regex with its min-chars guard and a
    normalize_case flag (English ALL-CAPS labels get title-cased; Korean
    labels do NOT — Hangul is uncased so str.isupper() over mixed labels
    misbehaves). First match wins per line; ordered-set dedupe; cap at
    MAX_SECTIONS_PER_FILE.

    Deterministic, side-effect-free, no LLM.
    """
    if not text:
        return ()
    head = text[:SECTION_SCAN_CHARS]
    seen: dict[str, None] = {}
    for line in head.splitlines():
        for rx, min_chars, normalize_case in _PATTERNS:
            m = rx.match(line)
            if not m:
                continue
            label = m.group(1).strip()
            if not (min_chars <= len(label) <= SECTION_HEADER_MAX_CHARS):
                continue
            label = " ".join(label.split())
            if normalize_case and label.isupper():
                label = label.title()
            if label not in seen:
                seen[label] = None
                if len(seen) >= MAX_SECTIONS_PER_FILE:
                    return tuple(seen)
            break
    return tuple(seen)
