# Organize: Korean Structural-Fingerprint Coverage in `_sections.py` — Design

**Date:** 2026-05-08
**Branch:** dev_branch
**Predecessor:** `.pptx` extractor — shipped 2026-05-08 (commit `352b72c`); `.csv` extractor in flight (separate Claude session, plan at `docs/superpowers/plans/2026-05-08-organize-csv.md`).
**Tracker:** `Future Plan - Structural Sections Across Formats.md` (Obsidian, Lion_Chemtech/FDA) — section "Korean structural-fingerprint coverage in `_sections.py`" was parked corpus-blocked; this spec unparks it now that a real Korean corpus is available.

## Goal

Extend `fda/organize/_sections.py` so its line-by-line regex pass detects Korean section headers in addition to English. Today the regex is `[A-Z]`-anchored and never matches Korean lines like `[발주서]`, `이름:`, or `■ 회사 정보`, which means every Korean PDF and Korean plaintext file currently flows into the classifier with `sections=()` regardless of how clearly the underlying document is divided.

The fix is additive: keep both existing English regexes unchanged; add three Korean-specific patterns (bracketed banner, colon label, bullet banner). Add one new min-length constant for Korean labels (2 chars; Korean labels are syllable-dense and 2-syllable labels like `이름`, `직위`, `부서` are routine and meaningful). The post-match length guard becomes pattern-aware so the new min applies only to Korean patterns and English keeps its existing `SECTION_HEADER_MIN_CHARS = 3`.

This is sub-project A of the larger Korean+HWP track. Sub-projects B (`.hwpx` extractor) and C (`.hwp` extractor) are **separate future brainstorms** that depend on `.csv` merging first, since both touch `fda/organize/_extractors.py`.

## Non-goals

- `.hwp` extractor (sub-project C) — separate brainstorm; corpus-blocked-no-more but file-conflict-blocked on the in-flight CSV work. Ships later.
- `.hwpx` extractor (sub-project B) — separate brainstorm; format-native (XML), language-agnostic, doesn't require this spec's regex changes.
- `.doc`, `.xls`, `.eml`, `.msg`, `.tsv` — separate sub-projects unrelated to Korean coverage.
- Numbered Korean headers (`1. 제품 정보`, `1) 회사 정보`, `제 1 장`, `제1조`) — high false-positive risk against ordered-list bodies; revisit only if real corpus shows numbered banners are dominant.
- Hanja / CJK Unified Ideograph (`一-鿿`) support — out for v1. The assigned Hangul Syllables range (`가-힣`) covers modern Korean business-doc text. Revisit if corpus shows Hanja in headers.
- Hangul Jamo (`ᄀ-ᇿ`, `㄰-㆏`) — out. Modern Korean text uses precomposed syllables; Jamo in headers would be unusual.
- English bracketed banners (`[INVOICE]`, `[Q&A]`) — out. Adding bracket support for English would be a generalization unrelated to Korean coverage; keep the new bracket regex Korean-only by requiring at least one Hangul character in the inner content.
- Format-native extractor changes for `.docx`, `.xlsx`, `.pptx`, `.csv`, `.hwpx` — none **in this spec**. Those paths read structure from format APIs (heading style names, sheet/cell values, slide titles, XML elements, `csv.reader`), so the regex layer in `_sections.py` doesn't need to learn their structure. **Caveat:** the format-native pipelines are language-agnostic at the API layer, but their **downstream length-guard post-processing is not**. The in-flight `.csv` extractor (separate Claude session) is making a parallel adjustment inside `_extractors.py` so that 2-syllable Korean column headers (`이름`, `나이`, `도시`) survive the per-cell length guard — mirroring this spec's `KOREAN_LABEL_MIN_CHARS` approach. That coordination is between the two sessions; A continues to leave `_extractors.py` untouched. The same caveat may apply to future format extractors (B, C, etc.) — each is responsible for its own per-script guard if it post-processes captured labels.
- Korean text-pattern fallback for `.docx` (manually-styled headings without proper heading styles) — separate concern, language-orthogonal, out of scope.
- Skill prompt revisions to `taxonomy-proposer` / `taxonomy-assigner` — they already consume `sections` as opaque `tuple[str, ...]`. Korean labels flow through the same shape.
- Reader, classifier, plan_builder, executor, verifier, models — no changes.
- New runtime dependency — none. Stdlib `re` only.

## Architecture

Pure-functions module unchanged in shape. `extract_sections_from_text` still:

1. Returns `()` on empty input.
2. Slices the head of the text via `SECTION_SCAN_CHARS`.
3. Walks lines once in source order.
4. Tries each compiled regex per line; first match wins; `break` after a match.
5. Strips, collapses whitespace, title-cases ALL-CAPS, ordered-set dedupes.
6. Caps at `MAX_SECTIONS_PER_FILE`.

What changes:

- **Three new compiled regexes** for Korean bracket / colon / bullet shapes, all requiring at least one Hangul syllable in the captured label so they cannot match English-only lines.
- **One new module-level constant** `KOREAN_LABEL_MIN_CHARS = 2`, registered in `tests/test_organize_constraints.py`.
- **One new public module-level helper** `HANGUL_RANGE = "가-힣"` (U+AC00–U+D7A3, the assigned Hangul Syllables range) reused inside the three Korean regexes (single source of truth). **Public** rather than private (no leading underscore) so other modules can import it for Hangul-aware logic. The in-flight `.csv` extractor session has flagged a follow-up cleanup PR that replaces its private `_CSV_KOREAN_LABEL_MIN_CHARS` and `_contains_hangul` with imports from this module once A ships.
- **One new public helper function** `contains_hangul(text: str) -> bool` returning `True` iff `text` contains at least one Hangul Syllables character. Implemented via a precompiled `re.search` against `HANGUL_RANGE`. Intended for cross-module reuse by format-native extractors that need per-script guards (e.g., csv's per-cell length policy).
- **The pattern-iteration loop** in `extract_sections_from_text` becomes `for rx, min_chars, normalize_case in _PATTERNS:` where `_PATTERNS` is a module-level tuple pairing each compiled regex with its min-chars guard *and* a `normalize_case` flag. English patterns pair with `(SECTION_HEADER_MIN_CHARS, True)` (allows the existing ALL-CAPS-to-Title-case rule to fire). Korean patterns pair with `(KOREAN_LABEL_MIN_CHARS, False)` — Korean labels must NEVER be title-cased because Python's `"KPI 지표".isupper()` returns `True` (Hangul is uncased; cased characters K, P, I are uppercase), so an untagged title-case pass would mutate `KPI 지표` into `Kpi 지표`. The flag isolates the title-case rule to English patterns where it belongs. The post-match length guard becomes `min_chars <= len(label) <= SECTION_HEADER_MAX_CHARS`. `SECTION_HEADER_MAX_CHARS = 40` stays global.

**Files touched:**
- `fda/organize/_sections.py` — modified.
- `tests/test_organize_sections.py` — extended with new test classes (`TestKoreanBracketBanners`, `TestKoreanColonLabels`, `TestKoreanBulletBanners`, `TestMixedLanguage`, `TestKoreanLengthGuard`, `TestKoreanRegexBacktrackingBound`, `TestContainsHangul`).
- `tests/test_organize_constraints.py` — register `KOREAN_LABEL_MIN_CHARS` in the `CONSTS` dict.
- `tests/test_organize_reader.py` — *optional* one Korean-plaintext-fixture test asserting Korean sections flow through Reader to `CatalogEntry.sections`.

**Files explicitly NOT touched** (so this work merges cleanly in parallel with the in-flight CSV implementation):
- `fda/organize/_extractors.py` — CSV is editing this file. A doesn't need to.
- `tests/test_organize_extractors.py` — CSV is adding test classes here. A doesn't need to.
- `tests/test_organize_pipeline.py` — CSV is editing this file. A doesn't need to.
- `pyproject.toml` — no new dependency.

## Pattern definitions

### Existing patterns (unchanged)

```python
_HEADER_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Za-z0-9 \-/&]{1,38}[A-Za-z0-9]):[ \t]*$"
)

_ALLCAPS_LINE_RE = re.compile(
    r"^[ \t]*([A-Z][A-Z0-9 \-/&]{2,38}[A-Z0-9])[ \t]*$"
)
```

These continue to handle English colon labels (`Shipping Details:`) and English ALL-CAPS banners (`INVOICE`). No edits.

### New pattern: `_KOREAN_BRACKET_RE`

Bracketed banner lines. Opener set: `[`, `【`, `《`, `〔`, `「`. Closer set: `]`, `】`, `》`, `〕`, `」`. The regex tolerates mismatched opener/closer pairs (low false-positive risk in practice; the alternative — five separate paired regexes — adds surface area for marginal benefit).

The captured group requires at least one Hangul syllable so English-only bracketed lines like `[INVOICE]` and `[Q&A]` do not match. Inner content cannot contain any of the ten bracket characters (prevents matching nested-bracket noise across multiple labels on one line).

```python
# Hangul Syllables block (U+AC00..U+D7A3 — assigned range).
# Modern Korean business-doc text is precomposed Hangul; Jamo
# (U+1100..U+11FF, U+3130..U+318F) and CJK Hanja (U+4E00..U+9FFF)
# are intentionally excluded from v1 — see Non-goals.
HANGUL_RANGE = "가-힣"

# Bounded char-class repetition ({0,40} / {2,40}) caps regex work at
# ~SECTION_HEADER_MAX_CHARS per match attempt, eliminating the
# catastrophic-backtracking risk on long malformed Hangul lines (e.g.,
# a 16K all-Hangul bracketed line missing its closer would otherwise
# cost ~6.7s with an unbounded *? capture; bounded is ~10µs). The
# lookahead ensures at least one Hangul appears within the bounded
# window so English-only lines fail the lookahead immediately. ASCII
# space, tab, and U+3000 ideographic space are all accepted at line
# leading/trailing/separator positions; the latter is common in Korean
# templates.

_KOREAN_BRACKET_RE = re.compile(
    r"^[ \t　]*[\[【《〔「][ \t　]*"
    rf"(?=[^\[\]【】《》〔〕「」]{{0,40}}[{HANGUL_RANGE}])"
    rf"([^\[\]【】《》〔〕「」]{{2,40}})"
    r"[ \t　]*[\]】》〕」][ \t　]*$"
)
```

Examples that match: `[발주서]`, `【계약서】`, `《제품 정보》`, `〔공지〕`, `「인용」`, `[발주서 - 2024]`.
Examples that don't: `[INVOICE]` (no Hangul), `[Q&A]` (no Hangul), `[발주서` (missing closer), `발주서]` (missing opener), `[발주서] 2024` (content after closer).

Post-match: label is the captured group, stripped, internal whitespace collapsed. Length guard with `KOREAN_LABEL_MIN_CHARS = 2`.

### New pattern: `_KOREAN_COLON_RE`

Korean colon-suffixed labels — analog of `_HEADER_LINE_RE` but Hangul-anchored. The line must end with a colon (with optional trailing whitespace including U+3000); content after the colon disqualifies, which is why mid-line `회사: 한화` does not match (the line ends with `한화`, not with `:`). The captured group must contain at least one Hangul syllable. Internal colons are additionally excluded from the captured group as a defense-in-depth check.

```python
# Char class excludes bullet markers so a line that mixes bullet + colon
# (e.g., "■ 회사 정보:") does not match either Korean regex (the bullet
# regex separately excludes ":"). v1 limitation; bullet+colon ambiguity
# is rare in real Korean docs.
_KOREAN_COLON_RE = re.compile(
    rf"^[ \t　]*"
    rf"(?=[^:\n■◆●□◇○]{{0,40}}[{HANGUL_RANGE}])"
    rf"([^:\n■◆●□◇○]{{2,40}})"
    rf"[ \t　]*:[ \t　]*$"
)
```

Examples that match: `이름:`, `회사 정보:`, `담당자 :` (trailing space before colon), `2024년 매출:`, `제품 정보:`.
Examples that don't: `Order ID: 10488` (no Hangul), `회사: 한화` (line doesn't end with `:`; `한화` is content after colon), `회사정보` (no colon), `: 회사정보` (regex requires Hangul before the colon, in the captured group).

Post-match: label = captured group, stripped, collapsed. Length guard with `KOREAN_LABEL_MIN_CHARS = 2`. The line-end-colon anchor is what distinguishes a Korean section header from a Korean labeled-field-with-value, mirroring the existing English `_HEADER_LINE_RE` discipline.

### New pattern: `_KOREAN_BULLET_RE`

Bullet-prefixed Korean banners. Marker set: `■`, `◆`, `●`, `□`, `◇`, `○`. Marker must be followed by at least one whitespace character. Captured label must contain at least one Hangul syllable.

```python
# Char class excludes ":" so a line that mixes bullet + colon (e.g.,
# "■ 회사 정보:") does not match either Korean regex. The bullet regex
# would otherwise greedy-capture through to the trailing colon and
# leave it embedded in the label. v1 limitation matching the colon
# regex's parallel exclusion of bullet markers.
_KOREAN_BULLET_RE = re.compile(
    r"^[ \t　]*[■◆●□◇○][ \t　]+"
    rf"(?=[^\n:]{{0,40}}[{HANGUL_RANGE}])"
    rf"([^\n:]{{2,40}})"
    r"[ \t　]*$"
)
```

Examples that match: `■ 회사 정보`, `◆ 계약 조건`, `● 주의사항`, `□ 부속 합의`, `◇ 변경 사항`, `○ 비고`.
Examples that don't: `■회사 정보` (no whitespace after marker), `■ Company Info` (no Hangul), `▶ 회사 정보` (marker not in the v1 set; revisit if corpus shows triangle markers).

Post-match: label = captured group, stripped, collapsed. Length guard with `KOREAN_LABEL_MIN_CHARS = 2`.

## `_PATTERNS` table and the iteration change

```python
# (regex, min_chars, normalize_case)
_PATTERNS: tuple[tuple[re.Pattern[str], int, bool], ...] = (
    (_HEADER_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_ALLCAPS_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_KOREAN_BRACKET_RE, KOREAN_LABEL_MIN_CHARS, False),
    (_KOREAN_COLON_RE, KOREAN_LABEL_MIN_CHARS, False),
    (_KOREAN_BULLET_RE, KOREAN_LABEL_MIN_CHARS, False),
)
```

The body of `extract_sections_from_text` becomes:

```python
def extract_sections_from_text(text: str) -> tuple[str, ...]:
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
```

Two functional changes vs. today: (1) `min_chars` replaces the hardcoded `SECTION_HEADER_MIN_CHARS` reference, taking its value from the per-pattern tuple; (2) the ALL-CAPS-to-Title-case normalization is now gated by the per-pattern `normalize_case` flag, preventing it from mutating Korean labels like `KPI 지표` (which Python's `str.isupper()` reports as uppercase because Hangul is uncased and `KPI` is the only cased substring). Everything else (collapse, ordered-set dedupe, cap, break-on-match) is preserved.

Pattern iteration order is declaration order. The Korean regexes are tightened so that no two **Korean** patterns can match the same line:
- `_KOREAN_COLON_RE` excludes bullet markers from its capture char class.
- `_KOREAN_BULLET_RE` excludes `:` from its capture char class.
- `_KOREAN_BRACKET_RE` excludes opener/closer brackets from its capture char class — and any line wrapped in brackets has no leading bullet or trailing colon outside the brackets, since the regex anchors require the brackets to be at the line's leading/trailing positions.

The English patterns and Korean patterns are mutually exclusive because the English regexes' middle char classes (`[A-Za-z0-9 \-/&]` etc.) admit no Hangul, and the Korean regexes' lookaheads require Hangul. So a line containing Hangul never matches an English pattern, and a line without Hangul never matches a Korean pattern.

**Documented v1 limitation:** lines that combine bullet ornament and trailing colon (e.g., `■ 회사 정보:`) match **neither** Korean regex under the tightened char classes. Such lines yield no section. Bullet+colon ambiguity is rare in real Korean docs — most templates pick one convention per line. Revisit if corpus shows mixed patterns are common.

Order is therefore presentation-only; pinning English first keeps regression diffs minimal in the test file.

## Normalization

Existing rules retained:
- `label.strip()` → trim outer whitespace (Python's no-arg `strip` removes Unicode whitespace including U+3000).
- `" ".join(label.split())` → collapse internal whitespace.
- `if normalize_case and label.isupper(): label = label.title()` → title-case English ALL-CAPS labels **only** for English patterns.

The `normalize_case` gate is the single behavioral change. Background: Python's `str.isupper()` returns `True` for any string whose **cased** characters are all uppercase, ignoring uncased characters. Hangul is uncased. Therefore `"KPI 지표".isupper()` is `True`, and an untagged `.title()` pass would mutate the label to `"Kpi 지표"`. Pattern-aware gating ensures the title-case pass runs only for `_HEADER_LINE_RE` and `_ALLCAPS_LINE_RE` matches, leaving Korean-pattern labels (which carry Hangul by construction of the regex lookahead) untouched.

No other new normalization rules. Three precise statements about non-ASCII edge cases:
- **Full-width Roman characters mixed with Hangul** (e.g., `ＫＰＩ 지표:`) — **DO match** the Korean patterns. The capture char class admits any non-excluded character; the lookahead requires only that some Hangul appears within the bounded window. The captured label preserves full-width chars verbatim with `normalize_case=False`. This is intentional: stripping/transliterating full-width Roman is out of scope for v1.
- **Pure full-width Roman labels** (no Hangul, e.g., `ＫＰＩ:`) — do **NOT** match the Korean patterns (lookahead fails) and do **NOT** match the English patterns either (English char classes admit only ASCII). Acceptable v1 limitation.
- **Zero-width space (U+200B), bidi/RTL marks, and other invisible/control whitespace** — not handled. If they appear inside a captured label, they survive into the final `sections` tuple verbatim; if they appear at line leading/trailing positions, they break the regex anchors. Acceptable v1 limitation; revisit only if corpus shows real coverage gap.

## Failure modes

Pure functions, no exceptions to handle. The new regexes can fail to match a malformed line (no failure surface), and they can produce a captured group that fails the length guard (existing post-match drop logic handles this). `SECTION_SCAN_CHARS` cap and `MAX_SECTIONS_PER_FILE` cap unchanged.

**Backtracking risk:** all three Korean regexes use bounded char-class repetition (`{0,40}` lookahead + `{2,40}` capture) so each match attempt is O(`SECTION_HEADER_MAX_CHARS`) regardless of input line length. A pathological 16K-char line of all-Hangul missing its closer/colon/marker terminates in microseconds. The earlier draft's unbounded `*?` capture would have made the same input cost seconds (Codex-measured at ~6.7s for a 16K malformed bracketed line); the bounded version is the canonical form and is pinned by `TestKoreanRegexBacktrackingBound`.

Re-running the same text through `extract_sections_from_text` is deterministic and side-effect-free; the function does not mutate `_PATTERNS` or any other module-level state.

## Constants

| Name | Value | Module | Purpose |
|---|---|---|---|
| `KOREAN_LABEL_MIN_CHARS` | `2` | `_sections.py` (new) | Min-chars guard for Korean patterns only. Korean syllables are ~1 morpheme each; 2-syllable labels (`이름`, `직위`, `부서`) are common and meaningful. English keeps min 3 via `SECTION_HEADER_MIN_CHARS`. |
| `HANGUL_RANGE` | `"가-힣"` (U+AC00..U+D7A3) | `_sections.py` (new, public) | Assigned Hangul Syllables range as a regex char-class fragment. Single source of truth used inside the three Korean regexes; also exported for cross-module use (e.g., the in-flight csv extractor's per-script length guard will import it via the `contains_hangul()` helper after a follow-up cleanup PR). Registered in the CONSTS check. |
| `contains_hangul` | n/a (function) | `_sections.py` (new, public) | `(text: str) -> bool` — `True` iff `text` contains at least one Hangul Syllables character. Implemented via a precompiled `re.search` against `HANGUL_RANGE`. Public so other modules can use it for per-script logic without re-deriving the regex char class. |
| `MAX_SECTIONS_PER_FILE` | `15` | `_sections.py` (existing) | Reused as global cap. |
| `SECTION_HEADER_MIN_CHARS` | `3` | `_sections.py` (existing) | Reused length guard for English patterns. |
| `SECTION_HEADER_MAX_CHARS` | `40` | `_sections.py` (existing) | Reused length guard, applies to all patterns. |
| `SECTION_SCAN_CHARS` | `16 * 1024` | `_sections.py` (existing) | Reused scan-window cap. |

`KOREAN_LABEL_MIN_CHARS` and `HANGUL_RANGE` must both be registered in `tests/test_organize_constraints.py` `CONSTS` dict alongside `SECTION_HEADER_MIN_CHARS`.

`contains_hangul` is a public function, not a constant, so it is **not** registered in `CONSTS`. Its tests live in `tests/test_organize_sections.py` (new `TestContainsHangul` class — see Testing plan).

The bracket-character literal sets (`[\[【《〔「]`, `[\]】》〕」]`) and the bullet marker set (`[■◆●□◇○]`) appear inline in their respective regexes and are not exported as constants. They are scoped to their owning regex; if a future format extractor needs them, lift them then.

## Skill prompts

Unchanged. The v1 prompts already consume `sections` as opaque `tuple[str, ...]`. Korean labels flow through the same shape. The prompts' descriptions of "structural fingerprint" and "section divergence" apply to Korean labels exactly as they do to English ones.

(Pre-existing aside, not addressed here: the `taxonomy-proposer` / `taxonomy-assigner` SKILL.md docs still describe `sections` as a regex-derived signal. That description is already stale for docx/xlsx/pptx and will be staler with csv. The same separate documentation cleanup PR called out in the pptx and csv specs applies; it isn't part of A.)

## Testing plan

Synthetic Korean strings written inline in tests (no fixture files required for the unit layer). Optional sanitized real-corpus validation at the integration layer.

### Unit tests in `tests/test_organize_sections.py`

Add seven new test classes alongside the existing `TestExtractSectionsFromText`. Existing English tests must continue to pass unmodified.

**`TestKoreanBracketBanners`:**
- Each opener/closer pair (`[..]`, `【..】`, `《..》`, `〔..〕`, `「..」`) with a Hangul label → label appears in `sections` without brackets, in declaration order.
- Mixed-pair `[발주서》` (square-open + angle-close) → matches under the v1 "tolerate mismatched pairs" stance.
- English-only `[INVOICE]` → does **not** match `_KOREAN_BRACKET_RE` (Hangul lookalike absent).
- Unbalanced `[발주서` (missing closer) → does **not** match.
- Content after closer `[발주서] 2024-Q1` → does **not** match (`$` anchor).
- Bracket-with-internal-dash `[발주서 - 2024]` → matches; label = `발주서 - 2024` after collapse.
- Bracket label > 40 chars → dropped by length guard (`SECTION_HEADER_MAX_CHARS`).
- Bracket label = 1 Hangul char (e.g., `[가]`) → dropped by `KOREAN_LABEL_MIN_CHARS = 2`.
- Bracket label = 2 Hangul chars (e.g., `[발주]`) → matches.

**`TestKoreanColonLabels`:**
- 2-syllable label `이름:` → matches; appears as `이름`.
- 4-syllable label `회사 정보:` (4 Hangul + 1 space = 5 chars after trim) → matches; appears as `회사 정보`.
- Trailing-space-before-colon `담당자 :` → matches; trailing space consumed by the pre-colon `[ \t　]*` segment.
- Year-prefixed label `2024년 매출:` → matches.
- Mid-line colon `회사: 한화` → does **not** match (line doesn't end with `:`).
- English label `Order ID:` → matches under the existing `_HEADER_LINE_RE`, NOT `_KOREAN_COLON_RE` (no Hangul).
- 1-syllable Korean colon label `가:` → dropped by `KOREAN_LABEL_MIN_CHARS = 2`.
- Long Korean colon label > 40 chars → dropped by `SECTION_HEADER_MAX_CHARS`.
- Pure-ASCII line that ends with a colon (`order details:` lowercase) → matches NEITHER existing English nor new Korean (no `[A-Z]` start, no Hangul). Acceptable existing limitation; not regressed.
- **Mixed-case label preservation** `KPI 지표:` → matches `_KOREAN_COLON_RE`; appears as `KPI 지표` (NOT `Kpi 지표`). Pins the `normalize_case=False` gate for Korean patterns.
- **U+3000 leading whitespace** `　이름:` (ideographic space then label) → matches; appears as `이름`.
- **U+3000 between marker and label is irrelevant here** (no marker in colon pattern).

**`TestKoreanBulletBanners`:**
- Each marker (`■ ◆ ● □ ◇ ○`) followed by space + Korean label → label appears in `sections` without bullet.
- Bullet without space `■회사` → does **not** match (whitespace required).
- Marker with English-only content `■ Company Info` → does **not** match (no Hangul).
- 1-syllable Korean after bullet `■ 가` → dropped (label is 1 char, below min 2).
- Out-of-set marker `▶ 회사 정보` → does **not** match (triangle not in v1 set; documented limitation).
- Bullet label > 40 chars → dropped.
- **U+3000 separator** `■　회사 정보` (ideographic space between marker and label) → matches; appears as `회사 정보`.

**`TestMixedLanguage`:**
- Document with `Order Details:`, `[발주서]`, `INVOICE` → all three appear in `sections` in source order, after title-casing of `INVOICE` → `Invoice`.
- Document with intermixed Korean colon and English colon labels → both detected, source order preserved.
- Same Korean label appearing twice → ordered-set dedupe, single entry.
- Same Korean label appearing once + same English-translation label appearing once (e.g., `이름:` and `Name:`) → both appear (treated as distinct labels; we do not translate).
- Korean label containing English uppercase acronym `[KPI 지표]` → captured as `KPI 지표`, NOT title-cased (`Kpi 지표` would be a regression). Same assertion for `■ KPI 지표` and `KPI 지표:`. Pins the `normalize_case=False` gate against Codex blocker 1.
- **Bullet+colon ambiguous line** `■ 회사 정보:` → matches **NEITHER** Korean regex under the tightened char-class exclusions (colon regex excludes bullet markers; bullet regex excludes `:`). Yields no section. Pins the documented v1 limitation against accidental relaxation of the char classes.
- **Mixed full-width Roman + Hangul label** `ＫＰＩ 지표:` → matches `_KOREAN_COLON_RE`; captured verbatim as `ＫＰＩ 지표`. Pins the precise full-width policy from the Normalization section.

**`TestKoreanLengthGuard`:**
- Min-2 enforcement on each Korean pattern (one test per pattern that crosses the 1↔2-char boundary).
- Max-40 enforcement on each Korean pattern (one test per pattern that crosses the 40↔41-char boundary).
- Cap at `MAX_SECTIONS_PER_FILE`: produce 30 distinct Korean labels via repeated bracket lines → result length equals `MAX_SECTIONS_PER_FILE`.

**`TestKoreanRegexBacktrackingBound`:**
- Each Korean regex run against a 16K-character malformed line (e.g., 16K `한` characters with no closing bracket / no terminating colon / no proper bullet line). Test wraps the call in a wall-clock budget (e.g., `time.monotonic()` before and after, asserting elapsed < 100ms). Pins the bounded `{0,40}` lookahead + `{2,40}` capture structure against accidental regression to unbounded `*?` form, which Codex measured at ~6.7s on the same input. The 100ms budget is generous (bounded version is ~10µs in measurement) but tight enough to fail loudly if a future edit reintroduces unbounded backtracking.

**`TestContainsHangul`:**
- `contains_hangul("이름")` → `True`.
- `contains_hangul("KPI 지표")` → `True` (mixed ASCII + Hangul).
- `contains_hangul("name")` → `False`.
- `contains_hangul("")` → `False`.
- `contains_hangul("[Q&A]")` → `False`.
- `contains_hangul("ＫＰＩ")` → `False` (full-width Roman is not Hangul; pins the precise Hangul-only intent).
- `contains_hangul("漢字")` → `False` (CJK Hanja excluded per v1 Non-goals).
- `contains_hangul("ＫＰＩ 지표")` → `True` (mixed full-width Roman + Hangul; the Hangul presence is what matters).

### English regression coverage

The existing tests in `TestExtractSectionsFromText` (`test_colon_only_on_line_headers`, `test_labeled_field_with_value_on_same_line_does_not_match`, `test_allcaps_short_lines_are_title_cased`, `test_source_order_preserved_across_patterns`, `test_duplicates_deduplicated_in_first_seen_order`, `test_capped_at_max_sections_per_file`, `test_empty_input_returns_empty_tuple`, `test_no_headers_returns_empty_tuple`, `test_too_short_label_rejected`, `test_too_long_label_rejected`, `test_scan_chars_cap_bounds_work`) must pass unchanged. They are the primary defense against the iteration-loop refactor accidentally regressing English behavior.

### Integration (optional but recommended)

Add one Korean plaintext fixture test in `tests/test_organize_reader.py` proving the wire is intact end-to-end:

- Write a `.txt` file containing Korean section banners to the reader workspace.
- Run `reader.read(workspace, backend=fake_backend, logger=logger)`.
- Assert `CatalogEntry.sections` for that file contains the expected Korean labels.
- Optionally assert the new patterns don't break the existing PDF/text/docx/xlsx/pptx integration tests in the same file.

The integration test belongs in a new test class (e.g., `TestKoreanSectionsThroughReader`) rather than added to the in-flight CSV's `TestSectionsPropagationDocxXlsx`, so the two PRs don't collide on the same class body.

PDF Korean integration testing is **deferred** to manual validation against the real corpus — `pdftotext` output formatting is hard to fixture deterministically (output depends on local poppler version, locale, and Korean font handling), and the unit-level coverage in `test_organize_sections.py` already pins the regex behavior on the post-extraction text.

### Real-corpus validation step (pre-merge gate)

The Future Plan flagged "no Korean corpus" as the reason this work was parked. Now that a corpus is available, the implementation plan must include a manual validation pass before merge:

1. Run the new code over a representative subset of the real Korean corpus (PDFs, plaintext, anything that routes through `_sections.py`).
2. Inspect the resulting `sections` tuples for: false negatives (real banner lines that didn't match), false positives (non-banner lines that did match), and length-guard surprises (real labels filtered out).
3. If false-negative rate is high for a specific shape that v1 chose to skip (e.g., numbered headers `1. 제품 정보`), document the gap for v2; do **not** widen v1 patterns mid-implementation without a brainstorm round.
4. If real-corpus output is acceptable, merge. If not, revise patterns in the spec, re-run unit tests, re-validate.

This step is a manual quality gate, not an automated test; it goes in the implementation plan's done-condition checklist rather than the pytest suite.

### Constants test

`tests/test_organize_constraints.py` `CONSTS` dict gains two entries:

```python
        "KOREAN_LABEL_MIN_CHARS": ("_sections.py", "2"),
        "HANGUL_RANGE": ("_sections.py", '"가-힣"'),
```

inserted alongside the existing `_sections.py` entries. No `DISTINCTIVE_LITERALS` change (the literal `2` is too generic to be meaningfully tracked, and the Hangul range string is already pinned by the CONSTS lookup itself).

The csv session is appending its own `_CSV_KOREAN_LABEL_MIN_CHARS` entry to `CONSTS` independently. Both PRs append to the same dict; merge resolution is mechanical (different keys, no semantic conflict).

### Full-suite gate

`/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` must pass green. Exact test count is not the acceptance criterion — green is.

## Sequencing context

This spec is sub-project A of the Korean+HWP track. Position in the rollout:

1. ✅ `.docx` + `.xlsx` — shipped 2026-05-07.
2. ✅ `.pptx` — shipped 2026-05-08 (commit `352b72c`).
3. **In flight (separate Claude session):** `.csv` extractor — spec at `docs/superpowers/specs/2026-05-08-organize-csv-design.md`, plan at `docs/superpowers/plans/2026-05-08-organize-csv.md`. Touches `fda/organize/_extractors.py`, `tests/test_organize_extractors.py`, `tests/test_organize_pipeline.py`, `tests/test_organize_constraints.py`, `tests/test_organize_reader.py`.
4. **This spec — sub-project A:** Korean structural-fingerprint coverage in `_sections.py`. Touches `fda/organize/_sections.py`, `tests/test_organize_sections.py`, `tests/test_organize_constraints.py`, optionally `tests/test_organize_reader.py`. **Parallel-safe with (3):** the `_sections.py` source file and `test_organize_sections.py` test file are not touched by the CSV work, so A can be coded, tested, and merged in parallel without rebase pain. Two test files both specs touch:
    - `tests/test_organize_constraints.py` — both PRs append entries to the `CONSTS` dict at the same region. Different keys (CSV adds `_CSV_KOREAN_LABEL_MIN_CHARS`; A adds `KOREAN_LABEL_MIN_CHARS`, `HANGUL_RANGE`). Expect a minor textual conflict for whichever PR lands second; resolution is mechanical.
    - `tests/test_organize_reader.py` — A's optional integration test goes in a **new** test class `TestKoreanSectionsThroughReader`, NOT inside CSV's `TestSectionsPropagationDocxXlsx`. This avoids a class-body collision; the two test classes live independently and merge cleanly.
5. **Future brainstorm — sub-project B:** `.hwpx` extractor (XML-based, language-agnostic, similar to docx). Conflicts with CSV on `_extractors.py`; must wait for CSV merge. Does not depend on A's regex changes.
6. **Future brainstorm — sub-project C:** `.hwp` extractor (binary OLE, needs `hwp5txt` or `pyhwp`). Conflicts with CSV on `_extractors.py` and adds a runtime dep in `pyproject.toml`; must wait for CSV merge. Pairs with A — without A's Korean regex, `.hwp` Korean files yield empty `sections` because `.hwp` extracts to plaintext and routes through `_sections.py`.
7. **Possible v2 follow-ups (corpus-driven, not blocking):** numbered Korean headers, Hanja support, English bracketed banners (would generalize `_KOREAN_BRACKET_RE` and require renaming).

Tracked in Obsidian: `Future Plan - Structural Sections Across Formats.md`. The note's "Korean structural-fingerprint coverage" and "`.hwp`" parked sections will be unparked / split / updated as A, B, C ship.

## Done condition

- `_sections.py` defines `KOREAN_LABEL_MIN_CHARS`, `HANGUL_RANGE`, `contains_hangul()`, the three new compiled regexes, and the `_PATTERNS` tuple. `extract_sections_from_text` iterates `_PATTERNS` with pattern-aware min-length and pattern-aware case-normalization.
- All new tests in the seven new test classes pass.
- All existing tests in `TestExtractSectionsFromText` pass unchanged.
- `tests/test_organize_constraints.py` registers `KOREAN_LABEL_MIN_CHARS` and `HANGUL_RANGE`.
- Optional Korean plaintext reader integration test passes (or is intentionally omitted with a one-line spec note in the implementation plan).
- Real-corpus validation pass complete; results recorded in the implementation plan's done-condition checklist.
- Full pytest suite green.
- One commit per task; merged independently of the in-flight CSV work; the minor append-style conflict in `tests/test_organize_constraints.py` resolves mechanically (different keys). Optional integration test goes in a new `TestKoreanSectionsThroughReader` class in `tests/test_organize_reader.py` so there's no class-body collision with CSV's `TestSectionsPropagationDocxXlsx`.

## Real-corpus validation results

**Corpus:** Lion Chemtech client documents (8 Korean PDFs sampled across three document types: production logs `성형일지` / `가공일지`, R&R organizational sheets `개인별 업무 리스트_*`, and a proposal deck). Cross-referenced against representative `.docx` (`01_생산팀_질문지_완.docx`, `02_품질관리팀_질문지_업데이트.docx`) and `.xlsx` (`수출260410.xlsx`) shapes via direct text inspection.

**Method:** Loaded `_sections.py` standalone, extracted PDF text with `pdftotext -layout -nopgbrk`, ran `extract_sections_from_text` per file. Probed individual representative lines from the docx/xlsx corpus directly through the same function.

**Quantitative result:** Across 8 PDFs — 3 extract failures (scanned image PDFs returning empty text; not a regex concern), 5 successfully extracted (~28K Hangul characters total). 0 sections detected, 0 false positives.

**Line-level probes (from real corpus):**

| Line | v1 Result | Reason |
|---|---|---|
| `[ 작업 확인사항 ]` (xlsx cell) | ✅ matches → `("작업 확인사항",)` | bracket + U+3000 padding handled |
| `목 재 :`, `코드스트랩:`, `에어백 :` (xlsx cells) | ✅ matches | colon-label pattern |
| `1. 데이터 관리 현황`, `2. MES화 기초작업 현황 (핵심)` (docx) | ❌ no match | numbered headers — deferred to v2 |
| `📋 안내사항` (docx) | ❌ no match | emoji prefix — not in v1 marker set |
| `대상 팀`, `작성자` (docx) | ❌ no match | bare label, no banner shape |
| `AI 전환(AIX) 사전 진단을 위한 현황 파악` (docx) | ❌ no match | prose with parens, not a banner |

**Why the PDF corpus produced zero hits:** the sampled PDFs are tabular role/responsibility sheets (`개인별 업무 리스트`) — they consist of column headers (`구분 | 업무명 | 업무 상세 내용`) plus rows of names and tasks. None of those lines have v1's bracket / colon / bullet shape. The bracket and colon regexes correctly fire on the xlsx/docx cells that *do* use those shapes, confirming the patterns work; the PDF samples just don't contain that style of banner.

**Decision:** MERGE v1 as-is. False-positive rate is 0%; the v1-chosen patterns (bracket / colon / bullet) work correctly when their shapes appear. The dominant false-negative source is numbered-header style (`1. 제품 정보`) — exactly the v2 follow-up the plan anticipated. The plan's directive — "do not widen v1 patterns mid-implementation; open a new brainstorm" — applies cleanly.

**v2 priority queue (corpus-driven, ranked by frequency in this corpus):**

1. Numbered Korean headers (`1. 데이터 관리 현황`, `2. MES화 기초작업 현황`) — the highest-impact gap. Brainstorm a separate sub-project, mindful of false-positive risk against ordered-list bodies.
2. Emoji/icon-prefixed headers (`📋 안내사항`) — corpus-frequent in modern Korean templates. Either widen the bullet marker set or add a lightweight emoji-banner pattern.
3. Bare Korean labels with no marker (`대상 팀`, `작성자`) — likely too ambiguous for regex; defer indefinitely.

Hanja / CJK-Unified-Ideographs in headers and English bracketed banners (`[INVOICE]`) saw zero corpus signal here; remain Non-goals.
