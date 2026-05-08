# Organize: Korean Structural-Fingerprint Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `fda/organize/_sections.py` with three Korean-aware regex patterns (bracketed banners, colon labels, bullet banners), a public `HANGUL_RANGE` + `contains_hangul()` helper, and a per-pattern min-length / case-normalization gate so Korean section headers (e.g., `[발주서]`, `이름:`, `■ 회사 정보`) flow through the structural-fingerprint signal alongside the existing English patterns.

**Architecture:** Pure-functions module unchanged in shape. Add three new compiled regexes (each with bounded `{0,40}` lookahead + `{2,40}` capture for backtracking safety), one public constant `KOREAN_LABEL_MIN_CHARS = 2`, one public helper constant `HANGUL_RANGE = "가-힣"`, and one public function `contains_hangul()`. Refactor the iteration loop to consume `_PATTERNS = ((rx, min_chars, normalize_case), ...)` so per-pattern length and title-case behavior is enforceable. English regexes and existing English tests remain untouched.

**Tech Stack:** Python 3.12+, stdlib `re` only. pytest. Spec at `docs/superpowers/specs/2026-05-08-organize-korean-sections-design.md` (committed as `88d0779`).

**Pretest:** every task ends with running the full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` (CLAUDE.md command). If that exact path is unavailable on the executing host, fall back to whichever Python 3.12 the test fixtures' conftest works under.

**Parallel-safe with the in-flight CSV implementation:** the CSV session edits `fda/organize/_extractors.py`, `tests/test_organize_extractors.py`, `tests/test_organize_pipeline.py`, and adds entries to `tests/test_organize_constraints.py` `CONSTS` and `tests/test_organize_reader.py`. This plan touches `fda/organize/_sections.py`, `tests/test_organize_sections.py`, `tests/test_organize_constraints.py` (different keys), and optionally `tests/test_organize_reader.py` (different test class). Whichever PR lands second resolves the two textual conflicts mechanically.

---

## File Structure

| File | Role |
|---|---|
| `fda/organize/_sections.py` | Owns regexes, constants, helpers, and `extract_sections_from_text`. **Modified** in this plan. |
| `tests/test_organize_sections.py` | Unit tests for `_sections.py`. **Extended** with seven new test classes. |
| `tests/test_organize_constraints.py` | Lint-style constants test. **Extended** by appending two entries to the `CONSTS` dict. |
| `tests/test_organize_reader.py` | Reader-layer integration tests. **Optionally extended** with a new `TestKoreanSectionsThroughReader` class. |

Each task in the plan changes only the files listed above; `_extractors.py`, `_skills/`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, `pyproject.toml`, and skill `SKILL.md` files are **not touched**.

---

## Task 1: Reserve `KOREAN_LABEL_MIN_CHARS` and `HANGUL_RANGE` constants

Lock the two new public constants in the constants test first (TDD). Failing test → add constants → test passes. No regex changes yet.

**Files:**
- Modify: `tests/test_organize_constraints.py:51-90` (append two entries to `CONSTS`)
- Modify: `fda/organize/_sections.py:13-16` (add two constants below the existing section constants)

- [ ] **Step 1: Append two entries to the `CONSTS` dict**

Edit `tests/test_organize_constraints.py`. Inside `class TestEachConstantHasOneHome:`, find the existing `_sections.py` entries:

```python
        "MAX_SECTIONS_PER_FILE": ("_sections.py", "15"),
        "SECTION_HEADER_MAX_CHARS": ("_sections.py", "40"),
        "SECTION_SCAN_CHARS": ("_sections.py", "16 * 1024"),
```

Below the existing `SECTION_HEADER_MIN_CHARS` line, append:

```python
        "KOREAN_LABEL_MIN_CHARS": ("_sections.py", "2"),
        "HANGUL_RANGE": ("_sections.py", '"가-힣"'),
```

- [ ] **Step 2: Run the constants test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py::TestEachConstantHasOneHome::test_constant_defined_in_owning_module -v`
Expected: FAIL — `KOREAN_LABEL_MIN_CHARS missing from _sections.py`.

- [ ] **Step 3: Add the two constants to `_sections.py`**

Edit `fda/organize/_sections.py`. Just below the existing `SECTION_SCAN_CHARS = 16 * 1024` line (~line 16), insert:

```python

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
```

- [ ] **Step 4: Run the constants test to verify it passes**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py -v`
Expected: PASS (all entries in `CONSTS` find their owning module).

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_sections.py tests/test_organize_constraints.py
git commit -m "organize(sections): reserve korean structural-fingerprint constants"
```

---

## Task 2: Refactor iteration loop with `_PATTERNS` triples (no behavior change)

Convert the inline `for rx in (_HEADER_LINE_RE, _ALLCAPS_LINE_RE)` loop into a tuple-driven loop that carries per-pattern `min_chars` and `normalize_case` flags. Existing English tests must continue to pass unchanged. This refactor is invisible behavior-wise; subsequent tasks add Korean entries to `_PATTERNS`.

**Files:**
- Modify: `fda/organize/_sections.py:31-62` (the body of `extract_sections_from_text` and the new `_PATTERNS` tuple above it)

- [ ] **Step 1: Add the `_PATTERNS` tuple between the regexes and `extract_sections_from_text`**

Edit `fda/organize/_sections.py`. Just above `def extract_sections_from_text(text: str) -> tuple[str, ...]:` (~line 31), insert:

```python
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
)
```

- [ ] **Step 2: Replace the body of `extract_sections_from_text`**

Edit `fda/organize/_sections.py`. Replace the existing body of `extract_sections_from_text` with:

```python
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
```

- [ ] **Step 3: Run the existing sections tests to confirm no regression**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py -v`
Expected: PASS — all 11 existing tests in `TestExtractSectionsFromText` continue to pass under the new iteration shape.

- [ ] **Step 4: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add fda/organize/_sections.py
git commit -m "organize(sections): tuple-driven pattern iteration with per-pattern flags"
```

---

## Task 3: Add `contains_hangul()` public helper

Public helper that other modules import for per-script logic without re-deriving the regex. Drives a simple precompiled `re.search` against `HANGUL_RANGE`.

**Files:**
- Modify: `fda/organize/_sections.py` (add a precompiled `_HANGUL_SEARCH_RE` and the public function)
- Modify: `tests/test_organize_sections.py` (new `TestContainsHangul` class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# contains_hangul — public helper for cross-module per-script logic
# ---------------------------------------------------------------------------


class TestContainsHangul:
    def test_pure_hangul_returns_true(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("이름") is True

    def test_mixed_ascii_and_hangul_returns_true(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("KPI 지표") is True

    def test_pure_ascii_returns_false(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("name") is False

    def test_empty_string_returns_false(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("") is False

    def test_punctuation_only_returns_false(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("[Q&A]") is False

    def test_full_width_roman_returns_false(self):
        """Full-width Roman characters are not Hangul. Pins the precise
        Hangul-only intent of HANGUL_RANGE = U+AC00..U+D7A3."""
        from fda.organize._sections import contains_hangul

        assert contains_hangul("ＫＰＩ") is False

    def test_hanja_returns_false(self):
        """CJK Hanja excluded per v1 Non-goals."""
        from fda.organize._sections import contains_hangul

        assert contains_hangul("漢字") is False

    def test_full_width_roman_plus_hangul_returns_true(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("ＫＰＩ 지표") is True
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestContainsHangul -v`
Expected: FAIL — `cannot import name 'contains_hangul'`.

- [ ] **Step 3: Add the helper to `_sections.py`**

Edit `fda/organize/_sections.py`. Just below the `HANGUL_RANGE = "가-힣"` line added in Task 1, insert:

```python

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
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestContainsHangul -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_sections.py tests/test_organize_sections.py
git commit -m "organize(sections): contains_hangul public helper"
```

---

## Task 4: Add `_KOREAN_BRACKET_RE` and wire it into `_PATTERNS`

**Prerequisites:** Tasks 1, 2, 3 complete. Task 4 references `KOREAN_LABEL_MIN_CHARS`, `HANGUL_RANGE`, and the `_PATTERNS` tuple introduced earlier.

Bracketed-banner pattern: `[발주서]`, `【계약서】`, `《제품 정보》`, `〔공지〕`, `「인용」`. Bounded char-class repetition keeps backtracking O(40) per match attempt; lookahead requires at least one Hangul so English-only bracketed lines like `[INVOICE]` don't match.

**Files:**
- Modify: `fda/organize/_sections.py` (add the regex and append to `_PATTERNS`)
- Modify: `tests/test_organize_sections.py` (new `TestKoreanBracketBanners` class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# Korean bracketed banners — [발주서] / 【계약서】 / 《제품 정보》 / 〔공지〕 / 「인용」
# ---------------------------------------------------------------------------


class TestKoreanBracketBanners:
    def test_square_bracket_banner_matches(self, ):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주서]\n") == ("발주서",)

    def test_lenticular_bracket_banner_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("【계약서】\n") == ("계약서",)

    def test_double_angle_bracket_banner_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("《제품 정보》\n") == ("제품 정보",)

    def test_tortoise_bracket_banner_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("〔공지〕\n") == ("공지",)

    def test_corner_bracket_banner_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("「인용」\n") == ("인용",)

    def test_mixed_pair_brackets_match(self):
        """Spec stance: tolerate mismatched opener/closer pairs (the
        alternative — five paired regexes — adds surface area for marginal
        benefit; mismatched pairs in real Korean docs are rare but harmless
        when caught)."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주서》\n") == ("발주서",)

    def test_u3000_inside_brackets_handled(self):
        """U+3000 ideographic space immediately after opener and before closer
        — common in Korean templates that pad banner content for visual
        alignment. Leading and trailing U+3000 are consumed by the
        whitespace-tolerant inner anchors (or stripped post-match)."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[　발주서　]\n") == ("발주서",)

    def test_inline_bracket_in_middle_of_line_does_not_match(self):
        """Anchored to line start/end — a bracketed phrase embedded in
        prose must not produce a section."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("가나 [다라] 마바\n") == ()

    def test_internal_dash_preserved_in_label(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주서 - 2024]\n") == ("발주서 - 2024",)

    def test_two_syllable_label_passes_min_2(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주]\n") == ("발주",)

    def test_one_syllable_label_dropped_by_min_2(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[가]\n") == ()

    def test_label_over_max_chars_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        long_label = "한" * 41
        assert extract_sections_from_text(f"[{long_label}]\n") == ()

    def test_english_only_bracketed_does_not_match(self):
        """English bracketed lines like [INVOICE] are explicitly out of v1
        scope — the lookahead requires Hangul somewhere in the inner content."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[INVOICE]\n") == ()

    def test_q_and_a_does_not_match(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[Q&A]\n") == ()

    def test_unbalanced_open_does_not_match(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주서\n") == ()

    def test_unbalanced_close_does_not_match(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("발주서]\n") == ()

    def test_content_after_closer_does_not_match(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주서] 2024\n") == ()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanBracketBanners -v`
Expected: FAIL — `extract_sections_from_text` returns `()` for the matching cases (no Korean regex registered yet).

- [ ] **Step 3: Add `_KOREAN_BRACKET_RE` and register it in `_PATTERNS`**

Edit `fda/organize/_sections.py`. Insert the new regex **between `_ALLCAPS_LINE_RE` and `_PATTERNS`** (i.e., after the last existing regex definition and before the tuple introduced in Task 2). The tuple references `_KOREAN_BRACKET_RE` so the regex must be defined before the tuple at module import time — placing it below `_PATTERNS` would raise `NameError`. Add:

```python


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
```

Then update the `_PATTERNS` tuple to include the new regex:

```python
_PATTERNS: tuple[tuple[re.Pattern[str], int, bool], ...] = (
    (_HEADER_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_ALLCAPS_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_KOREAN_BRACKET_RE, KOREAN_LABEL_MIN_CHARS, False),
)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanBracketBanners -v`
Expected: PASS (17 tests).

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green (existing English tests still pass; new Korean bracket tests pass).

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_sections.py tests/test_organize_sections.py
git commit -m "organize(sections): _KOREAN_BRACKET_RE bounded bracketed-banner pattern"
```

---

## Task 5: Add `_KOREAN_COLON_RE` and wire it into `_PATTERNS`

**Prerequisites:** Tasks 1, 2, 3, 4 complete.

Korean colon-suffixed labels (`이름:`, `회사 정보:`, `2024년 매출:`). Char class **excludes bullet markers** so a line that combines bullet + colon (e.g., `■ 회사 정보:`) doesn't match this regex (paired with Task 6's bullet exclusion of `:`, the ambiguous line is dropped — documented v1 limitation).

**Files:**
- Modify: `fda/organize/_sections.py` (add the regex and append to `_PATTERNS`)
- Modify: `tests/test_organize_sections.py` (new `TestKoreanColonLabels` class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# Korean colon labels — 이름: / 회사 정보: / 2024년 매출:
# ---------------------------------------------------------------------------


class TestKoreanColonLabels:
    def test_two_syllable_label_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("이름:\n") == ("이름",)

    def test_four_syllable_label_with_space_matches(self):
        """`회사 정보` is 4 Hangul syllables + 1 space = 5 chars after trim."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("회사 정보:\n") == ("회사 정보",)

    def test_trailing_space_before_colon_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("담당자 :\n") == ("담당자",)

    def test_year_prefixed_label_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("2024년 매출:\n") == ("2024년 매출",)

    def test_mid_line_colon_does_not_match(self):
        """Line must end with `:` — `회사: 한화` ends with `한화`, not `:`."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("회사: 한화\n") == ()

    def test_english_label_routes_to_english_pattern_not_korean(self):
        """`Order ID:` must match _HEADER_LINE_RE (existing English regex),
        NOT _KOREAN_COLON_RE — the Korean lookahead requires Hangul."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("Order ID:\n") == ("Order ID",)

    def test_one_syllable_korean_dropped_by_min_2(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("가:\n") == ()

    def test_label_over_max_chars_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        long_label = "한" * 41
        assert extract_sections_from_text(f"{long_label}:\n") == ()

    def test_u3000_leading_whitespace_handled(self):
        """U+3000 ideographic space at line start — common in Korean templates."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("　이름:\n") == ("이름",)

    def test_full_width_roman_plus_hangul_matches_verbatim(self):
        """Mixed full-width Roman + Hangul: lookahead succeeds (Hangul present),
        capture preserves full-width chars verbatim, normalize_case=False
        leaves casing alone."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("ＫＰＩ 지표:\n") == ("ＫＰＩ 지표",)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanColonLabels -v`
Expected: FAIL on the Korean cases (matching tests return `()`); the English `Order ID:` case may already pass via `_HEADER_LINE_RE`.

- [ ] **Step 3: Add `_KOREAN_COLON_RE` and register it in `_PATTERNS`**

Edit `fda/organize/_sections.py`. Insert **between `_KOREAN_BRACKET_RE` (Task 4) and `_PATTERNS` (Task 2)** — i.e., still above the tuple. Same import-order constraint as Task 4: any regex referenced by `_PATTERNS` must be defined before the tuple. Add:

```python


# Char class excludes ":" (line-end anchor handles the terminal colon)
# AND bullet markers so a line that mixes bullet + colon (e.g.,
# "■ 회사 정보:") does not match either Korean regex (the bullet regex
# separately excludes ":"). v1 limitation; bullet+colon ambiguity is
# rare in real Korean docs.
_KOREAN_COLON_RE = re.compile(
    rf"^[ \t　]*"
    rf"(?=[^:\n■◆●□◇○]{{0,40}}[{HANGUL_RANGE}])"
    rf"([^:\n■◆●□◇○]{{2,40}})"
    rf"[ \t　]*:[ \t　]*$"
)
```

Then update `_PATTERNS`:

```python
_PATTERNS: tuple[tuple[re.Pattern[str], int, bool], ...] = (
    (_HEADER_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_ALLCAPS_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_KOREAN_BRACKET_RE, KOREAN_LABEL_MIN_CHARS, False),
    (_KOREAN_COLON_RE, KOREAN_LABEL_MIN_CHARS, False),
)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanColonLabels -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_sections.py tests/test_organize_sections.py
git commit -m "organize(sections): _KOREAN_COLON_RE bounded colon-label pattern"
```

---

## Task 6: Add `_KOREAN_BULLET_RE` and wire it into `_PATTERNS`

**Prerequisites:** Tasks 1, 2, 3, 4, 5 complete.

Bullet-prefixed Korean banners (`■ 회사 정보`, `◆ 계약 조건`, `● 주의사항`). Marker set: `■ ◆ ● □ ◇ ○`. Char class **excludes `:`** so a bullet+colon line doesn't have its trailing colon glued into the captured label.

**Files:**
- Modify: `fda/organize/_sections.py` (add the regex and append to `_PATTERNS`)
- Modify: `tests/test_organize_sections.py` (new `TestKoreanBulletBanners` class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# Korean bullet banners — ■ 회사 정보 / ◆ 계약 조건 / ● 주의사항
# ---------------------------------------------------------------------------


class TestKoreanBulletBanners:
    def test_filled_square_bullet_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ 회사 정보\n") == ("회사 정보",)

    def test_filled_diamond_bullet_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("◆ 계약 조건\n") == ("계약 조건",)

    def test_filled_circle_bullet_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("● 주의사항\n") == ("주의사항",)

    def test_hollow_square_bullet_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("□ 부속 합의\n") == ("부속 합의",)

    def test_hollow_diamond_bullet_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("◇ 변경 사항\n") == ("변경 사항",)

    def test_hollow_circle_bullet_matches(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("○ 비고\n") == ("비고",)

    def test_bullet_without_whitespace_does_not_match(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■회사 정보\n") == ()

    def test_bullet_with_english_only_does_not_match(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ Company Info\n") == ()

    def test_one_syllable_after_bullet_dropped_by_min_2(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ 가\n") == ()

    def test_out_of_set_marker_does_not_match(self):
        """Triangles aren't in the v1 marker set — documented limitation."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("▶ 회사 정보\n") == ()

    def test_label_over_max_chars_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        long_label = "한" * 41
        assert extract_sections_from_text(f"■ {long_label}\n") == ()

    def test_u3000_separator_handled(self):
        """U+3000 ideographic space between marker and label — common in Korean
        templates that align text via ideographic-width spaces."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■　회사 정보\n") == ("회사 정보",)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanBulletBanners -v`
Expected: FAIL — matching cases return `()` (no bullet regex registered yet).

- [ ] **Step 3: Add `_KOREAN_BULLET_RE` and register it in `_PATTERNS`**

Edit `fda/organize/_sections.py`. Insert **between `_KOREAN_COLON_RE` (Task 5) and `_PATTERNS` (Task 2)** — i.e., still above the tuple. Same import-order constraint. Add:

```python


# Char class excludes ":" so a line that mixes bullet + colon (e.g.,
# "■ 회사 정보:") does not match either Korean regex. The bullet regex
# would otherwise greedy-capture through the trailing colon and leave
# it embedded in the label. v1 limitation matching the colon regex's
# parallel exclusion of bullet markers.
_KOREAN_BULLET_RE = re.compile(
    r"^[ \t　]*[■◆●□◇○][ \t　]+"
    rf"(?=[^\n:]{{0,40}}[{HANGUL_RANGE}])"
    rf"([^\n:]{{2,40}})"
    r"[ \t　]*$"
)
```

Then update `_PATTERNS`:

```python
_PATTERNS: tuple[tuple[re.Pattern[str], int, bool], ...] = (
    (_HEADER_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_ALLCAPS_LINE_RE, SECTION_HEADER_MIN_CHARS, True),
    (_KOREAN_BRACKET_RE, KOREAN_LABEL_MIN_CHARS, False),
    (_KOREAN_COLON_RE, KOREAN_LABEL_MIN_CHARS, False),
    (_KOREAN_BULLET_RE, KOREAN_LABEL_MIN_CHARS, False),
)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanBulletBanners -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_sections.py tests/test_organize_sections.py
git commit -m "organize(sections): _KOREAN_BULLET_RE bounded bullet-banner pattern"
```

---

## Task 7: Mixed-language coexistence + `KPI 지표` regression pin

Locks in two contracts: (a) Korean and English patterns coexist source-order-preserving in the same document; (b) `normalize_case=False` for Korean patterns prevents `.title()` from mutating mixed-case labels like `KPI 지표` into `Kpi 지표`. Both are spec contracts that must not regress.

**Files:**
- Modify: `tests/test_organize_sections.py` (new `TestMixedLanguage` class)

- [ ] **Step 1: Write the tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# Mixed-language documents — Korean and English patterns coexist
# ---------------------------------------------------------------------------


class TestMixedLanguage:
    def test_english_colon_korean_bracket_english_allcaps_in_source_order(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Order Details:\n"
            "...\n"
            "[발주서]\n"
            "...\n"
            "INVOICE\n"
            "...\n"
        )
        assert extract_sections_from_text(text) == (
            "Order Details",
            "발주서",
            "Invoice",
        )

    def test_korean_and_english_colon_labels_intermixed(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Bill To:\n"
            "...\n"
            "이름:\n"
            "...\n"
            "Ship To:\n"
            "...\n"
            "회사 정보:\n"
        )
        assert extract_sections_from_text(text) == (
            "Bill To",
            "이름",
            "Ship To",
            "회사 정보",
        )

    def test_duplicate_korean_labels_deduped(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "[발주서]\n"
            "...\n"
            "[계약서]\n"
            "...\n"
            "[발주서]\n"
        )
        assert extract_sections_from_text(text) == ("발주서", "계약서")

    def test_korean_label_and_english_translation_are_distinct(self):
        """We do not translate; same-meaning labels in different languages
        appear as distinct entries."""
        from fda.organize._sections import extract_sections_from_text

        text = "이름:\nName:\n"
        assert extract_sections_from_text(text) == ("이름", "Name")

    def test_kpi_jipyo_in_bracket_not_title_cased(self):
        """KPI 지표 must NOT become Kpi 지표. Pins the normalize_case=False gate
        for _KOREAN_BRACKET_RE — Python's str.isupper() returns True on
        "KPI 지표" because Hangul is uncased and KPI is uppercase."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[KPI 지표]\n") == ("KPI 지표",)

    def test_kpi_jipyo_in_colon_not_title_cased(self):
        """Pins the normalize_case=False gate for _KOREAN_COLON_RE."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("KPI 지표:\n") == ("KPI 지표",)

    def test_kpi_jipyo_after_bullet_not_title_cased(self):
        """Pins the normalize_case=False gate for _KOREAN_BULLET_RE."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ KPI 지표\n") == ("KPI 지표",)

    def test_bullet_plus_colon_ambiguous_line_yields_no_section(self):
        """v1 limitation: a line combining bullet ornament and trailing colon
        matches NEITHER Korean regex under the tightened char-class
        exclusions. Pins the limitation against accidental relaxation."""
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ 회사 정보:\n") == ()
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestMixedLanguage -v`
Expected: PASS (8 tests). If any fails, the implementation in Tasks 2/4/5/6 deviated from spec — fix the implementation, do not loosen the test.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_sections.py
git commit -m "organize(sections): mixed-language coexistence + KPI 지표 regression pin"
```

---

## Task 8: Length-guard + cap regression pins

The length guard (`KOREAN_LABEL_MIN_CHARS = 2` for Korean, `SECTION_HEADER_MIN_CHARS = 3` for English) and the global `MAX_SECTIONS_PER_FILE = 15` cap are already pinned per-pattern in earlier tasks; this class concentrates the boundary cases in one place so a future regression on the iteration-loop refactor fails loudly.

**Files:**
- Modify: `tests/test_organize_sections.py` (new `TestKoreanLengthGuard` class)

- [ ] **Step 1: Write the tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# Length-guard + cap regression pins
# ---------------------------------------------------------------------------


class TestKoreanLengthGuard:
    def test_korean_bracket_at_min_2_passes(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[발주]\n") == ("발주",)

    def test_korean_bracket_at_min_minus_1_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("[가]\n") == ()

    def test_korean_colon_at_min_2_passes(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("이름:\n") == ("이름",)

    def test_korean_colon_at_min_minus_1_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("가:\n") == ()

    def test_korean_bullet_at_min_2_passes(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ 비고\n") == ("비고",)

    def test_korean_bullet_at_min_minus_1_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("■ 가\n") == ()

    def test_korean_bracket_at_max_40_passes(self):
        from fda.organize._sections import extract_sections_from_text

        label = "한" * 40
        assert extract_sections_from_text(f"[{label}]\n") == (label,)

    def test_korean_bracket_at_max_plus_1_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        label = "한" * 41
        assert extract_sections_from_text(f"[{label}]\n") == ()

    def test_korean_colon_at_max_40_passes(self):
        from fda.organize._sections import extract_sections_from_text

        label = "한" * 40
        assert extract_sections_from_text(f"{label}:\n") == (label,)

    def test_korean_colon_at_max_plus_1_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        label = "한" * 41
        assert extract_sections_from_text(f"{label}:\n") == ()

    def test_korean_bullet_at_max_40_passes(self):
        from fda.organize._sections import extract_sections_from_text

        label = "한" * 40
        assert extract_sections_from_text(f"■ {label}\n") == (label,)

    def test_korean_bullet_at_max_plus_1_dropped(self):
        from fda.organize._sections import extract_sections_from_text

        label = "한" * 41
        assert extract_sections_from_text(f"■ {label}\n") == ()

    def test_korean_labels_capped_at_max_sections_per_file(self):
        from fda.organize._sections import (
            MAX_SECTIONS_PER_FILE,
            extract_sections_from_text,
        )

        # 30 distinct Korean labels via repeated bracket lines.
        # Use 2-syllable labels constructed from distinct Hangul digits to
        # stay simple and unambiguously distinct.
        labels = [f"발{i:02d}주" for i in range(30)]
        text = "\n".join(f"[{lbl}]" for lbl in labels) + "\n"
        result = extract_sections_from_text(text)
        assert len(result) == MAX_SECTIONS_PER_FILE
        assert result[0] == labels[0]
        assert result[-1] == labels[MAX_SECTIONS_PER_FILE - 1]
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanLengthGuard -v`
Expected: PASS (13 tests). Failures indicate length-guard or cap drift in earlier tasks — fix the implementation.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_sections.py
git commit -m "organize(sections): korean length-guard and cap regression pins"
```

---

## Task 9: Backtracking-bound regression pin

Locks in the bounded `{0,40}` lookahead + `{2,40}` capture structure of all three Korean regexes against accidental regression to unbounded `*?` form. Codex measured ~6.7s for a 16K all-Hangul malformed bracketed line under the unbounded form; bounded form is ~10µs. The 500ms wall-clock budget is loose enough to absorb noisy CI runner pauses but still ~50,000× tighter than the unbounded regression (~6.7s), so it fails loudly if backtracking is reintroduced.

**Files:**
- Modify: `tests/test_organize_sections.py` (new `TestKoreanRegexBacktrackingBound` class)

- [ ] **Step 1: Write the tests**

Append to `tests/test_organize_sections.py`:

```python
# ---------------------------------------------------------------------------
# Backtracking-bound regression pin
# ---------------------------------------------------------------------------


class TestKoreanRegexBacktrackingBound:
    """Pins the bounded char-class repetition in all three Korean regexes.

    A 16K-character malformed line of all-Hangul triggers catastrophic
    backtracking under unbounded `*?` capture (~6.7s in Codex's measurement)
    but completes in microseconds under the bounded `{0,40}` lookahead +
    `{2,40}` capture. The 500ms budget is loose enough to absorb noisy
    CI runner pauses (the bounded version is ~10µs in measurement) but
    still ~13× tighter than the unbounded regression, so it fails loudly
    if a future edit reintroduces unbounded backtracking.
    """

    BUDGET_MS = 500

    def _run_with_budget(self, fn):
        import time
        t0 = time.monotonic()
        result = fn()
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < self.BUDGET_MS, (
            f"regex took {elapsed_ms:.1f}ms, budget {self.BUDGET_MS}ms — "
            "bounded char-class repetition may have been reverted to "
            "unbounded *? form (catastrophic backtracking risk)."
        )
        return result

    def test_bracket_no_closer_terminates_fast(self):
        from fda.organize._sections import extract_sections_from_text

        line = "[" + ("한" * 16000)
        result = self._run_with_budget(lambda: extract_sections_from_text(line))
        assert result == ()

    def test_colon_no_terminator_terminates_fast(self):
        from fda.organize._sections import extract_sections_from_text

        line = "한" * 16000
        result = self._run_with_budget(lambda: extract_sections_from_text(line))
        assert result == ()

    def test_bullet_no_terminator_terminates_fast(self):
        from fda.organize._sections import extract_sections_from_text

        line = "■ " + ("한" * 16000)
        result = self._run_with_budget(lambda: extract_sections_from_text(line))
        assert result == ()
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_sections.py::TestKoreanRegexBacktrackingBound -v`
Expected: PASS (3 tests). If a test fails with "regex took … ms, budget 100 ms", the implementation deviated from the bounded-regex spec — fix the regex back to the bounded form.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_sections.py
git commit -m "organize(sections): backtracking-bound regression pin for korean regexes"
```

---

## Task 10: Reader integration — `TestKoreanSectionsThroughReader`

Optional but recommended: prove Korean labels flow end-to-end through Reader to `CatalogEntry.sections`. New test class — **not** added inside CSV's `TestSectionsPropagationDocxXlsx` — so the two PRs don't collide on the same class body.

**Files:**
- Modify: `tests/test_organize_reader.py` (append a new top-level test class)

- [ ] **Step 1: Locate where to append the new test class**

Open `tests/test_organize_reader.py`. Find the last top-level class definition (e.g., `class TestSectionsPropagationDocxXlsx`). Append the new class after that class's body, at the same indentation level (top-level).

- [ ] **Step 2: Append the new test class**

```python
class TestKoreanSectionsThroughReader:
    """Korean structural-fingerprint coverage: a plaintext file containing
    Korean banners flows through Reader, and CatalogEntry.sections holds
    the expected Korean labels. Separate top-level class (not appended to
    TestSectionsPropagationDocxXlsx) so this PR doesn't collide with the
    in-flight CSV PR's edits to that class.
    """

    def test_korean_plaintext_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        from fda.organize import reader

        f = workspace / "korean_doc.txt"
        f.write_text(
            "[발주서]\n"
            "회사 정보:\n"
            "■ 주의사항\n"
            "본문 내용...\n"
        )

        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("korean_doc.txt"))
        assert e.extract_status == "ok"
        assert e.sections == ("발주서", "회사 정보", "주의사항")
```

- [ ] **Step 3: Run the new test**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_reader.py::TestKoreanSectionsThroughReader -v`
Expected: PASS — the `_sections.py` regex output flows through Reader's `_read_text` extractor and into `CatalogEntry.sections`.

- [ ] **Step 4: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_organize_reader.py
git commit -m "organize(reader): korean sections flow through reader"
```

---

## Task 11: Real-corpus validation pass (manual gate)

Manual quality gate before merging. The Future Plan's original block on Korean coverage was "no corpus to validate against"; that block lifted when the user provided sanitized samples. This task runs the new code over a representative subset of the real Korean corpus, inspects the outputs, and (Step 5–6) records findings as a final spec edit.

**Files:**
- Modify: `docs/superpowers/specs/2026-05-08-organize-korean-sections-design.md` (Step 5 appends a `## Real-corpus validation results` section).

- [ ] **Step 1: Identify a representative subset of the real Korean corpus**

Pick at least:
- 2–3 Korean PDFs (each a different document type — e.g., 발주서, 계약서, 회의록).
- 1–2 Korean plaintext files if any exist.
- 1–2 Korean `.docx`, `.xlsx`, or `.pptx` files for cross-reference (these go through format-native extractors, but they exercise the full pipeline including the Korean-naming pass-through).

- [ ] **Step 2: Run the organize pipeline (or just the reader stage) over the subset**

A targeted way: run `fda/organize/reader.py`'s `read()` against the directory containing the samples, with logging enabled, and capture each `CatalogEntry.sections`. A pytest-driven path is also fine — write a one-off scratch test that walks the directory and prints sections for each file.

- [ ] **Step 3: Inspect the output for three failure modes**

For each file:
- **False negatives:** real banner lines in the document (visible to a human reader) that did NOT produce a section. Note which header convention the line uses (bracket / colon / bullet / something else).
- **False positives:** non-banner lines that DID produce a section. Note what the line actually was.
- **Length-guard surprises:** real labels that got dropped because they hit min/max chars.

- [ ] **Step 4: Make a go/no-go decision**

- If false-positive rate is acceptably low (< 5% of returned sections) AND false-negative rate is acceptably low for v1's chosen patterns (bracketed banners + colon labels + bullet banners) → proceed to merge.
- If a specific shape v1 chose to skip (e.g., numbered headers `1. 제품 정보`) accounts for a large slice of false negatives → **document the gap as a v2 follow-up**, do **not** widen v1 patterns mid-implementation. Open a new brainstorm for the additional pattern.
- If a v1 pattern produces unexpected false positives → revise the pattern in the spec, re-run unit tests, re-validate.

- [ ] **Step 5: Record findings**

Add a brief paragraph to `docs/superpowers/specs/2026-05-08-organize-korean-sections-design.md` (under a new `## Real-corpus validation results` section at the bottom) summarizing: corpus size, false-positive count, false-negative count, decision (merge / revise / defer), notable findings.

This is the only intentional spec edit during implementation. Commit it as part of the merge-readiness commit.

- [ ] **Step 6: Commit the validation summary**

```bash
git add docs/superpowers/specs/2026-05-08-organize-korean-sections-design.md
git commit -m "organize(spec): record korean real-corpus validation results"
```

---

## Done condition

- `fda/organize/_sections.py` defines `KOREAN_LABEL_MIN_CHARS`, `HANGUL_RANGE`, `contains_hangul()`, the three new compiled regexes (`_KOREAN_BRACKET_RE`, `_KOREAN_COLON_RE`, `_KOREAN_BULLET_RE`), and the `_PATTERNS` tuple. `extract_sections_from_text` iterates `_PATTERNS` with pattern-aware min-length and pattern-aware case-normalization.
- All seven new test classes added in Tasks 3–9 are green: `TestContainsHangul`, `TestKoreanBracketBanners`, `TestKoreanColonLabels`, `TestKoreanBulletBanners`, `TestMixedLanguage`, `TestKoreanLengthGuard`, `TestKoreanRegexBacktrackingBound`.
- All 11 existing tests in `TestExtractSectionsFromText` pass unchanged.
- `tests/test_organize_constraints.py` registers `KOREAN_LABEL_MIN_CHARS` and `HANGUL_RANGE`.
- Either (a) `tests/test_organize_reader.py` includes a new `TestKoreanSectionsThroughReader` class (Task 10 done), OR (b) Task 10 is intentionally skipped and the spec carries a one-line note explaining the omission. Spec section "Integration (optional but recommended)" anticipates either outcome.
- Real-corpus validation pass complete; results recorded in the spec via Task 11 Step 5–6.
- Full pytest suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`.
- One commit per task; all on `dev_branch`. Independent of the in-flight CSV work; if both land in parallel, the second-merger resolves the textual conflict in `tests/test_organize_constraints.py` (different keys) mechanically.
