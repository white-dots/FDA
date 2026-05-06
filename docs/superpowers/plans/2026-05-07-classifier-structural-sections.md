# Classifier Structural-Sections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover ≥95% per-class classifier accuracy on documents that share topic but differ in field/section structure (e.g., simple "Purchase Orders" vs detailed shipping orders) by adding a deterministic per-file `sections: tuple[str, ...]` signal that both Classifier stages can use as a structural fingerprint.

**Architecture:** A new pure-function module `fda/organize/_sections.py` extracts a list of section labels from text using two regex patterns (colon-only-on-line headers, short ALL-CAPS dividers) in a single source-order-preserving pass. The PDF and plain-text extractors call it; the result rides on `ExtractionResult.sections` → `CatalogEntry.sections` → both Classifier prompts. Stage A's stratified sample reserves slots per distinct sections-shape so rare structural types reach the proposer; Stage B's per-file payload gains the field plus an `extract_status`-aware fallback rule.

**Tech Stack:** Python 3.9+, stdlib `re`/`dataclasses`, existing `fda.claude_backend` (Haiku for Reader, Sonnet for Classifier), `pytest`. No new dependencies.

**Spec source:** `docs/superpowers/specs/2026-05-07-classifier-structural-sections-design.md` (commits `dc581b4`, `372821d` on `dev_branch`). Read it if any task is ambiguous.

**Python interpreter:** `python3`. The project's `requires-python = ">=3.9"`. On this checkout `/Users/john/.pyenv/versions/3.12.8/bin/python` does not exist; use `python3` (resolves to `/opt/anaconda3/bin/python3` for this user).

**Test command (run after every code-changing step):**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

A pre-commit hook runs the same suite — never use `--no-verify`.

---

## How to read this plan

The plan is **11 tasks**. Steps inside each task are bite-sized (2-5 min). Dependency order is strict: do not start task N+1 until task N is committed and the suite is green.

**Constraints reaffirmed for every task:**

- Never modify `fda/organize/executor.py` or `fda/organize/verifier.py`.
- Never modify `fda/organize/plan_builder.py`.
- New constants are defined exactly once in their owning module and added to the constraints test (`CONSTS` mapping).
- No hardcoded model IDs in `.py` files — model IDs only appear in SKILL.md frontmatter.
- One task ≈ one commit. Commit message follows the project style: `organize: <short verb>...` or `organize(<scope>): <short verb>...`.

---

## Chunks (for Codex review between chunks)

The 11 tasks are grouped into **4 review chunks plus 1 manual measurement step**. Implement one chunk at a time; ask Codex to review at each chunk boundary before starting the next.

| Chunk | Tasks | Scope | Codex review focus |
|-------|-------|-------|--------------------|
| **Chunk 1 — Data plumbing + helper** | 1, 2, 3, 4 | `models.py` fields, new `_sections.py` module + tests, `_extractors.py` wiring | Helper correctness (single-pass source-order, cap, length filter, scan budget); `sections` survives extraction failure as `()`; CSV/JSON/XML v1 behavior pinned |
| **Chunk 2 — Reader** | 5 | `reader.py`: `_summarize_one` populates field, `_fail_entry` preserves it through summarizer failure, final-rebuild loop threads it through global sort | `sections` survives summary timeout / exception / unparseable JSON, mirroring the existing `verbatim_head` preservation pattern |
| **Chunk 3 — Classifier internals + prompts** | 6, 7, 8 | `classifier.py`: `_entry_dict`, `_sample_for_taxonomy` (new shape-budget step), constant; `taxonomy-proposer/SKILL.md`; `taxonomy-assigner/SKILL.md` | Field reaches both Stage A and Stage B payloads; new sampling step is purely additive; Stage A prompt instructs criteria to enumerate discriminating section names; Stage B priority hierarchy is consistent and `extract_status` disambiguation is unambiguous |
| **Chunk 4 — Regression tests + constraints** | 9, 10 | `tests/test_organize_classifier.py` (Stage B conflict / empty-sections-by-status / filename-vs-sections / Stage A diversity); `tests/test_organize_constraints.py` (`CONSTS` extension) | Tests fail loudly if `sections` is dropped from the wire format or the priority hierarchy is misordered; pinned fixture text matches single-pass regex output exactly |
| **Manual — Northwind validation** | 11 | Fresh fixture + `--apply` run + manifest scoring | Not a Codex review chunk — measurement against live Sonnet. Run after Chunk 4 lands; report numbers to the user. |

**Chunk boundaries are marked inline below** with `### CHUNK N START` / `### CHUNK N END` markers around the task groups.

---

## File map

### Modified files

```
fda/organize/models.py                                  # Tasks 1, 2
fda/organize/_extractors.py                             # Task 4
fda/organize/reader.py                                  # Task 5
fda/organize/classifier.py                              # Task 6
fda/organize/skills/taxonomy-proposer/SKILL.md          # Task 7
fda/organize/skills/taxonomy-assigner/SKILL.md          # Task 8

tests/test_organize_models.py                           # Tasks 1, 2
tests/test_organize_extractors.py                       # Task 4
tests/test_organize_reader.py                           # Task 5
tests/test_organize_classifier.py                       # Tasks 6, 9
tests/test_organize_constraints.py                      # Task 10
```

### New files

```
fda/organize/_sections.py                               # Task 3
tests/test_organize_sections.py                         # Task 3
```

---

<!-- ===================== CHUNK 1 START — Data plumbing + helper (Tasks 1–4) ===================== -->

### Task 1: Add `sections` field to `ExtractionResult`

**Files:**
- Modify: `fda/organize/models.py` (the existing `ExtractionResult` dataclass at lines 68-72)
- Modify: `tests/test_organize_models.py` (append a new test class at end of file)

The field must default to `()` so existing constructors in `_extractors.py` (and in any test fixture) keep working without changes.

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_organize_models.py`:

```python
class TestExtractionResultSections:
    def test_default_is_empty_tuple(self):
        from fda.organize.models import ExtractionResult

        r = ExtractionResult(text=None, status="ok")
        assert r.sections == ()

    def test_accepts_explicit_value(self):
        from fda.organize.models import ExtractionResult

        r = ExtractionResult(
            text="ignored",
            status="ok",
            sections=("Shipping Details", "Customer Details"),
        )
        assert r.sections == ("Shipping Details", "Customer Details")
```

- [ ] **Step 2: Run the test to verify it fails.**

```bash
python3 -m pytest tests/test_organize_models.py::TestExtractionResultSections -v
```

Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'sections'` for the second test, and the first test's assertion fails because `r.sections` doesn't exist.

- [ ] **Step 3: Add the field to `ExtractionResult`.**

Edit `fda/organize/models.py`. Find the existing `ExtractionResult` dataclass and add `sections: tuple[str, ...] = ()` as the last field:

```python
@dataclass(frozen=True)
class ExtractionResult:
    text: str | None
    status: ExtractStatus
    note: str = ""
    sections: tuple[str, ...] = ()
```

- [ ] **Step 4: Run the test to verify it passes.**

```bash
python3 -m pytest tests/test_organize_models.py::TestExtractionResultSections -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite to catch regressions.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. The default `()` keeps every existing `ExtractionResult(...)` construction backward-compatible.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/models.py tests/test_organize_models.py
git commit -m "$(cat <<'EOF'
organize(models): add ExtractionResult.sections with default ()

Backing field for the upcoming structural-fingerprint signal that
text-based extractors will populate (regex helper to be added next)
and the Classifier will consume. Defaults to empty tuple so existing
constructors remain backward-compatible.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add `sections` field to `CatalogEntry`

**Files:**
- Modify: `fda/organize/models.py` (the existing `CatalogEntry` dataclass at lines 75-86)
- Modify: `tests/test_organize_models.py` (append another new test class)

The field must default to `()` so existing constructors in `reader.py` (`_summarize_one`, `_fail_entry`, `_junk_entry`, the final rebuild loop in `read()`) and in test fixtures (`tests/test_organize_classifier.py:_entry`) keep working without arg changes.

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_organize_models.py`:

```python
class TestCatalogEntrySections:
    def test_default_is_empty_tuple(self):
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/a.txt",
            ext=".txt",
            size_bytes=10,
            summary="text",
            type_label="text",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
        )
        assert e.sections == ()

    def test_accepts_explicit_value(self):
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/a.txt",
            ext=".txt",
            size_bytes=10,
            summary="text",
            type_label="text",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            sections=("Shipping Details", "Products"),
        )
        assert e.sections == ("Shipping Details", "Products")
```

- [ ] **Step 2: Run the test to verify it fails.**

```bash
python3 -m pytest tests/test_organize_models.py::TestCatalogEntrySections -v
```

Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'sections'`.

- [ ] **Step 3: Add the field to `CatalogEntry`.**

Edit `fda/organize/models.py`. Find the existing `CatalogEntry` dataclass and add `sections: tuple[str, ...] = ()` as the last field, **after** `verbatim_head`:

```python
@dataclass(frozen=True)
class CatalogEntry:
    path_id: str         # stable ID assigned by Reader: "f000", "f001", ...
    path: str            # absolute
    ext: str             # lowercase, including the dot
    size_bytes: int
    summary: str
    type_label: str
    is_junk: bool
    summary_failed: bool
    extract_status: ExtractStatus
    verbatim_head: str = ""
    sections: tuple[str, ...] = ()
```

- [ ] **Step 4: Run the test to verify it passes.**

```bash
python3 -m pytest tests/test_organize_models.py::TestCatalogEntrySections -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite to catch regressions.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/models.py tests/test_organize_models.py
git commit -m "$(cat <<'EOF'
organize(models): add CatalogEntry.sections with default ()

Catalog-side carrier for the structural-fingerprint signal. Reader
will copy this from ExtractionResult.sections; both Classifier stages
will consume it via _entry_dict. Defaults to empty tuple to preserve
backward compatibility with existing constructors and test fixtures.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Create `fda/organize/_sections.py` with `extract_sections_from_text`

**Files:**
- Create: `fda/organize/_sections.py`
- Create: `tests/test_organize_sections.py`

This module is pure-function, no I/O, no LLM. It exists as its own file (not inside `_extractors.py`) because (a) its responsibility is structural extraction, distinct from extension-dispatch + format conversion; (b) the future `.hwp` extractor will reuse it after converting to text. The constants live here as their single home.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_organize_sections.py`:

```python
"""Tests for fda.organize._sections.extract_sections_from_text."""

import pytest


class TestExtractSectionsFromText:
    def test_colon_only_on_line_headers(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Some preamble.\n"
            "\n"
            "Shipping Details:\n"
            "  ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "  Hanna Moos\n"
        )
        assert extract_sections_from_text(text) == (
            "Shipping Details",
            "Customer Details",
        )

    def test_labeled_field_with_value_on_same_line_does_not_match(self):
        """Lines like 'Order ID: 10488' have content after the colon and
        deliberately don't match — that's how we distinguish section
        dividers from labeled fields."""
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Order ID: 10488\n"
            "Order Date: 2024-03-15\n"
            "\n"
            "Products:\n"
        )
        assert extract_sections_from_text(text) == ("Products",)

    def test_allcaps_short_lines_are_title_cased(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "INVOICE\n"
            "\n"
            "Some content here.\n"
            "\n"
            "TOTAL DUE\n"
        )
        assert extract_sections_from_text(text) == ("Invoice", "Total Due")

    def test_source_order_preserved_across_patterns(self):
        """ALL-CAPS line precedes colon-headers — pins the single-pass
        algorithm. A two-pass implementation would emit colon-headers
        first and break this test."""
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Some intro paragraph\n"
            "\n"
            "INVOICE\n"
            "\n"
            "Bill To:\n"
            "Acme Corp\n"
            "\n"
            "Ship To:\n"
            "Customer Address\n"
        )
        assert extract_sections_from_text(text) == (
            "Invoice",
            "Bill To",
            "Ship To",
        )

    def test_duplicates_deduplicated_in_first_seen_order(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Shipping Details:\n"
            "...\n"
            "Customer Details:\n"
            "...\n"
            "Shipping Details:\n"  # duplicate; should not re-appear
            "...\n"
        )
        assert extract_sections_from_text(text) == (
            "Shipping Details",
            "Customer Details",
        )

    def test_capped_at_max_sections_per_file(self):
        from fda.organize._sections import (
            MAX_SECTIONS_PER_FILE,
            extract_sections_from_text,
        )

        # 30 distinct colon-headers — well past the cap of 15.
        lines = [f"Section {i}:\n  body\n" for i in range(30)]
        result = extract_sections_from_text("\n".join(lines))
        assert len(result) == MAX_SECTIONS_PER_FILE
        # First MAX_SECTIONS_PER_FILE in source order.
        assert result[0] == "Section 0"
        assert result[-1] == f"Section {MAX_SECTIONS_PER_FILE - 1}"

    def test_empty_input_returns_empty_tuple(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("") == ()
        assert extract_sections_from_text("   \n  \t\n") == ()

    def test_no_headers_returns_empty_tuple(self):
        from fda.organize._sections import extract_sections_from_text

        text = "just a sentence with a colon: see?\nno headers anywhere here.\n"
        assert extract_sections_from_text(text) == ()

    def test_too_short_label_rejected(self):
        from fda.organize._sections import extract_sections_from_text

        # "Ab" is shorter than SECTION_HEADER_MIN_CHARS (3), so the line
        # "Ab:" is rejected even though the regex matches.
        # (The regex's own minimum is 3 chars including the leading capital,
        # so "Ab:" doesn't match the regex anyway — this test pins the
        # SECTION_HEADER_MIN_CHARS guard as a defense in depth.)
        text = "Ab:\n\nLonger Header:\n"
        assert extract_sections_from_text(text) == ("Longer Header",)

    def test_too_long_label_rejected(self):
        from fda.organize._sections import (
            SECTION_HEADER_MAX_CHARS,
            extract_sections_from_text,
        )

        long = "A" + "b" * (SECTION_HEADER_MAX_CHARS + 5)
        text = f"{long}:\n\nShort Header:\n"
        # Long label is rejected by the SECTION_HEADER_MAX_CHARS guard.
        assert extract_sections_from_text(text) == ("Short Header",)

    def test_scan_bytes_cap_bounds_work(self):
        from fda.organize._sections import (
            SECTION_SCAN_BYTES,
            extract_sections_from_text,
        )

        # Header inside the scan window, decoy header past the cap.
        head = "Real Header:\n" + ("x\n" * 10)
        padding = "y\n" * (SECTION_SCAN_BYTES + 1024)
        decoy = "Decoy Header:\n"
        result = extract_sections_from_text(head + padding + decoy)
        assert "Real Header" in result
        assert "Decoy Header" not in result
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
python3 -m pytest tests/test_organize_sections.py -v
```

Expected: FAIL — `ImportError: No module named 'fda.organize._sections'` for every test.

- [ ] **Step 3: Create the module.**

Create `fda/organize/_sections.py`:

```python
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
SECTION_SCAN_BYTES = 16 * 1024   # only scan first ~16 KB; structure tops most docs

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
    head = text[:SECTION_SCAN_BYTES]
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
```

- [ ] **Step 4: Run the tests to verify they all pass.**

```bash
python3 -m pytest tests/test_organize_sections.py -v
```

Expected: PASS for all 11 tests.

- [ ] **Step 5: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/_sections.py tests/test_organize_sections.py
git commit -m "$(cat <<'EOF'
organize(sections): add deterministic regex-based section extractor

New module fda/organize/_sections.py exports extract_sections_from_text
plus four constants (MAX_SECTIONS_PER_FILE, SECTION_HEADER_MIN_CHARS,
SECTION_HEADER_MAX_CHARS, SECTION_SCAN_BYTES). Single-pass line walk
preserves source order across both patterns (colon-only-on-line headers
and short ALL-CAPS dividers), with normalization to stable title-cased
keys. The module is the single home for the structural-extraction
helper; PDF / plaintext extractors and the future .hwp extractor will
all import from it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire `_extractors.py` to populate `sections` via the helper

**Files:**
- Modify: `fda/organize/_extractors.py` (add import; populate `sections` in `_read_text` and `_extract_pdf_text`)
- Modify: `tests/test_organize_extractors.py` (add a test class at end of file)

`_read_text` covers `.txt`, `.md`, `.csv`, `.log`, `.json`, `.xml`. `_extract_pdf_text` covers `.pdf`. Both go through the same helper. CSV/JSON/XML rarely have colon-headers or ALL-CAPS dividers; an empty `sections` tuple for them is the expected v1 outcome (format-native extractors are deferred per the spec's "Future format coverage" section).

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_organize_extractors.py`:

```python
# ---------------------------------------------------------------------------
# Section extraction is wired into the text-based extractors
# ---------------------------------------------------------------------------


class TestSectionsWiredIntoExtractors:
    def test_read_text_populates_sections(self, tmp_path):
        from fda.organize._extractors import _read_text

        f = tmp_path / "doc.txt"
        f.write_text(
            "Order ID: 10488\n"
            "\n"
            "Shipping Details:\n"
            "ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "Hanna Moos\n"
        )
        result = _read_text(f)
        assert result.status == "ok"
        assert result.sections == ("Shipping Details", "Customer Details")

    def test_read_text_empty_file_yields_empty_sections(self, tmp_path):
        from fda.organize._extractors import _read_text

        f = tmp_path / "empty.txt"
        f.write_text("")
        result = _read_text(f)
        assert result.status == "ok"
        assert result.sections == ()

    def test_v1_csv_with_no_colon_headers_yields_empty_sections(self, tmp_path):
        """Pin v1 CSV behavior: regex runs, finds no colon-headers or
        ALL-CAPS dividers in typical CSV content, returns (). Format-native
        column-header extraction is v2 (see spec's Future format coverage)."""
        from fda.organize._extractors import _read_text

        f = tmp_path / "data.csv"
        f.write_text(
            "customer_id,order_date,amount,status\n"
            "1,2024-01-01,100.00,paid\n"
            "2,2024-01-02,200.00,pending\n"
        )
        result = _read_text(f)
        assert result.status == "ok"
        assert result.sections == ()

    def test_extract_pdf_text_populates_sections(self, tmp_path, monkeypatch):
        """Mock pdftotext to emit a known structural document; assert
        sections are populated from its text."""
        from fda.organize import _extractors
        from fda.organize._extractors import _extract_pdf_text

        # Make _which return a real-looking path so the early bailout
        # doesn't trip; stub _run_pdftotext to return the canned text.
        monkeypatch.setattr(_extractors, "_which", lambda name: "/usr/bin/pdftotext")
        canned = (
            "Order ID: 10488\n"
            "\n"
            "Shipping Details:\n"
            "ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "Hanna Moos\n"
        ).encode("utf-8")
        monkeypatch.setattr(_extractors, "_run_pdftotext", lambda path: canned)

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\n%fake\n")
        result = _extract_pdf_text(f)
        assert result.status == "ok"
        assert result.sections == ("Shipping Details", "Customer Details")

    def test_failed_pdf_extraction_leaves_sections_empty(self, tmp_path, monkeypatch):
        """When pdftotext is missing, ExtractionResult.status is
        'tool_missing' and sections stays ()."""
        from fda.organize import _extractors
        from fda.organize._extractors import _extract_pdf_text

        monkeypatch.setattr(_extractors, "_which", lambda name: None)

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\n%fake\n")
        result = _extract_pdf_text(f)
        assert result.status == "tool_missing"
        assert result.sections == ()
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
python3 -m pytest tests/test_organize_extractors.py::TestSectionsWiredIntoExtractors -v
```

Expected: FAIL — `sections` is `()` for the populate-tests because the extractors don't call the helper yet.

- [ ] **Step 3: Wire the helper into `_read_text` and `_extract_pdf_text`.**

Edit `fda/organize/_extractors.py`. Add the import at the top (after the existing `from fda.organize.models import ExtractionResult` line):

```python
from fda.organize._sections import extract_sections_from_text
```

Update `_read_text` (currently around lines 38-43) to populate `sections`:

```python
def _read_text(path: Path) -> ExtractionResult:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ExtractionResult(text=None, status="failed", note=str(e))
    return ExtractionResult(
        text=text,
        status="ok",
        sections=extract_sections_from_text(text),
    )
```

Update `_extract_pdf_text` (currently around lines 82-97) to populate `sections` on the success path. The two failure paths (`tool_missing`, `failed`) leave `sections=()` by default — do not pass the kwarg there:

```python
def _extract_pdf_text(path: Path) -> ExtractionResult:
    if _which("pdftotext") is None:
        return ExtractionResult(
            text=None,
            status="tool_missing",
            note="pdftotext not on PATH (install poppler)",
        )
    raw = _run_pdftotext(path)
    text = raw[:_PDF_PIPE_CAP_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        return ExtractionResult(
            text=None,
            status="failed",
            note="pdftotext produced no text (image-only PDF?)",
        )
    return ExtractionResult(
        text=text,
        status="ok",
        sections=extract_sections_from_text(text),
    )
```

The top-level `extract()` function does not change — its exception path already returns `ExtractionResult(text=None, status="failed", note=str(e))` with default `sections=()`.

- [ ] **Step 4: Run the tests to verify they pass.**

```bash
python3 -m pytest tests/test_organize_extractors.py -v
```

Expected: all tests in the file pass, including the new class.

- [ ] **Step 5: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "$(cat <<'EOF'
organize(extractors): populate ExtractionResult.sections via _sections

_read_text and _extract_pdf_text both call extract_sections_from_text
on success and pass the result through. Failure paths (tool_missing,
failed, OSError) keep the default empty tuple. Pins v1 CSV behavior:
typical CSVs have no colon-headers or ALL-CAPS dividers, so sections
stays empty until a format-native extractor is added (deferred per spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

<!-- ===================== CHUNK 1 END ===================== -->

---

<!-- ===================== CHUNK 2 START — Reader (Task 5) ===================== -->

### Task 5: Reader copies `sections` into `CatalogEntry`, preserves through summarizer failure

**Files:**
- Modify: `fda/organize/reader.py` (`_summarize_one`, `_fail_entry`, the final rebuild loop in `read()`)
- Modify: `tests/test_organize_reader.py` (add a test class at end of file)

The Reader pattern matches the existing `verbatim_head` preservation (commit `3c008ba`). When the Haiku summarizer call fails (timeout, exception, unparseable JSON), the deterministic structural fingerprint computed by the extractor is still valid and must be preserved on the resulting `CatalogEntry`. `_fail_entry` gains a `sections` keyword argument; both call sites in `_summarize_one` pass `extraction.sections`. The final rebuild loop in `read()` also threads the field through the global sort + path_id assignment pass.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_organize_reader.py`:

```python
# ---------------------------------------------------------------------------
# sections: copied from ExtractionResult through Reader to CatalogEntry,
# preserved across summarizer failure
# ---------------------------------------------------------------------------


class TestSectionsPropagation:
    def test_populated_from_extracted_text(
        self, workspace, fake_backend, logger
    ):
        """Reader copies extraction.sections into CatalogEntry.sections."""
        from fda.organize import reader

        body = (
            "Order ID: 10488\n"
            "\n"
            "Shipping Details:\n"
            "ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "Hanna Moos\n"
        )
        (workspace / "a.txt").write_text(body)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.sections == ("Shipping Details", "Customer Details")

    def test_empty_when_extraction_returns_no_sections(
        self, workspace, fake_backend, logger
    ):
        """A CSV (or any text without colon-headers / ALL-CAPS dividers)
        yields sections=()."""
        from fda.organize import reader

        (workspace / "data.csv").write_text(
            "customer_id,order_date\n1,2024-01-01\n"
        )
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.sections == ()

    def test_preserved_through_summarizer_timeout(
        self, workspace, logger, monkeypatch
    ):
        """Mirrors the existing verbatim_head preservation contract:
        when the Haiku summarizer call times out, the deterministic
        sections list is still attached to the failed CatalogEntry."""
        from fda.organize import reader

        body = (
            "Shipping Details:\n"
            "ACME\n"
            "\n"
            "Customer Details:\n"
            "Hanna\n"
        )
        (workspace / "a.txt").write_text(body)

        class TimeoutBackend:
            def complete(self, **kwargs):
                raise TimeoutError("simulated backend timeout")

        catalog = reader.read(
            workspace, backend=TimeoutBackend(), logger=logger,
        )
        e = catalog.entries[0]
        assert e.summary_failed is True
        assert e.sections == ("Shipping Details", "Customer Details")

    def test_preserved_through_summarizer_exception(
        self, workspace, logger
    ):
        from fda.organize import reader

        body = "Shipping Details:\nACME\n"
        (workspace / "a.txt").write_text(body)

        class BoomBackend:
            def complete(self, **kwargs):
                raise RuntimeError("boom")

        catalog = reader.read(
            workspace, backend=BoomBackend(), logger=logger,
        )
        e = catalog.entries[0]
        assert e.summary_failed is True
        assert e.sections == ("Shipping Details",)

    def test_preserved_through_unparseable_summary(
        self, workspace, logger
    ):
        from fda.organize import reader

        body = "Shipping Details:\nACME\n"
        (workspace / "a.txt").write_text(body)

        class GarbageBackend:
            def complete(self, **kwargs):
                return "this is not JSON"

        catalog = reader.read(
            workspace, backend=GarbageBackend(), logger=logger,
        )
        e = catalog.entries[0]
        assert e.summary_failed is True
        assert e.sections == ("Shipping Details",)
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
python3 -m pytest tests/test_organize_reader.py::TestSectionsPropagation -v
```

Expected: FAIL — happy-path test fails because Reader doesn't copy `sections` into the entry yet; the three failure tests fail because `_fail_entry` doesn't carry the field.

- [ ] **Step 3: Update `_summarize_one` to copy `sections` into the success-path `CatalogEntry`.**

Edit `fda/organize/reader.py`. In `_summarize_one`, find the success-path return (currently around lines 155-170) and add `sections=extraction.sections`:

```python
    return (
        CatalogEntry(
            path_id="",  # filled in by caller after global sort
            path=str(path),
            ext=path.suffix.lower(),
            size_bytes=size,
            summary=summary,
            type_label=type_label,
            is_junk=False,
            summary_failed=False,
            extract_status=extraction.status,
            verbatim_head=head,
            sections=extraction.sections,
        ),
        "done",
        "",
    )
```

- [ ] **Step 4: Update `_fail_entry` signature + body to accept and preserve `sections`.**

Find `_fail_entry` (currently around lines 173-192). Add a `sections` keyword argument with default `()`, and pass it into the constructed `CatalogEntry`:

```python
def _fail_entry(
    path: Path,
    size: int,
    extract_status: str,
    _why: str,
    *,
    verbatim_head: str = "",
    sections: tuple[str, ...] = (),
) -> CatalogEntry:
    return CatalogEntry(
        path_id="",
        path=str(path),
        ext=path.suffix.lower(),
        size_bytes=size,
        summary="",
        type_label="",
        is_junk=False,
        summary_failed=True,
        extract_status=extract_status,
        verbatim_head=verbatim_head,
        sections=sections,
    )
```

- [ ] **Step 5: Update both `_fail_entry` call sites in `_summarize_one` to pass `extraction.sections` through.**

Find the timeout and exception branches in `_summarize_one` (currently around lines 128-139). Both already pass `verbatim_head=head`; add `sections=extraction.sections` alongside:

```python
    except TimeoutError as e:
        return (
            _fail_entry(
                path, size, extraction.status, str(e),
                verbatim_head=head,
                sections=extraction.sections,
            ),
            "timeout",
            str(e),
        )
    except Exception as e:  # noqa: BLE001 — never abort a run because one file fails
        return (
            _fail_entry(
                path, size, extraction.status, str(e),
                verbatim_head=head,
                sections=extraction.sections,
            ),
            "fail",
            str(e),
        )
```

And the unparseable-summary path (currently around lines 145-153) needs the same:

```python
    try:
        parsed = json.loads(raw)
        type_label = str(parsed.get("type_label", ""))[:32]
        summary = str(parsed.get("summary", ""))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return (
            _fail_entry(
                path, size, extraction.status, "unparseable summary",
                verbatim_head=head,
                sections=extraction.sections,
            ),
            "fail",
            "unparseable summary",
        )
```

- [ ] **Step 6: Update the deadline-failed path inside `_worker`.**

Find `_worker` in `read()` (currently around lines 235-256). The deadline branch returns a `_fail_entry` without extracting first — for that case, `extraction.sections` is unavailable, so leave `sections=()` (no change to that call). Confirm it still calls `_fail_entry(p, size, "failed", "deadline")` with no `sections=` kwarg.

There is also a TOCTOU exception handler inside the `as_completed` loop (currently around lines 264-273). It runs only when the worker raised before producing an entry, so `extraction` is unavailable — leave `sections=()` there too. No change.

- [ ] **Step 7: Update the final rebuild loop to thread `sections` through global sort + path_id assignment.**

Find the rebuild block in `read()` (currently around lines 295-311). The `CatalogEntry(...)` constructor inside the comprehension copies every field from `e`; add `sections=e.sections`:

```python
    finalized = tuple(
        CatalogEntry(
            path_id=f"f{idx:03d}",
            path=e.path,
            ext=e.ext,
            size_bytes=e.size_bytes,
            summary=e.summary,
            type_label=e.type_label,
            is_junk=e.is_junk,
            summary_failed=e.summary_failed,
            extract_status=e.extract_status,
            verbatim_head=e.verbatim_head,
            sections=e.sections,
        )
        for idx, e in enumerate(ordered)
    )
```

- [ ] **Step 8: Run the tests to verify they pass.**

```bash
python3 -m pytest tests/test_organize_reader.py::TestSectionsPropagation -v
```

Expected: PASS.

- [ ] **Step 9: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. The verbatim_head preservation tests added in commit `3c008ba` continue to pass — sections preservation rides on the same paths.

- [ ] **Step 10: Commit.**

```bash
git add fda/organize/reader.py tests/test_organize_reader.py
git commit -m "$(cat <<'EOF'
organize(reader): populate CatalogEntry.sections + preserve on failure

Threads ExtractionResult.sections through _summarize_one (success path)
and _fail_entry (timeout / exception / unparseable JSON paths) into the
CatalogEntry. The final-rebuild loop carries the field through the
deterministic global sort + path_id assignment.

Mirrors the existing verbatim_head preservation contract: deterministic
signals computed before the LLM call are not lost when the LLM call
fails. Deadline and TOCTOU branches leave sections=() because no
extraction was performed in those branches.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

<!-- ===================== CHUNK 2 END ===================== -->

---

<!-- ===================== CHUNK 3 START — Classifier internals + prompts (Tasks 6–8) ===================== -->

### Task 6: Classifier `_entry_dict` carries `sections`; sampling reserves slots per shape

**Files:**
- Modify: `fda/organize/classifier.py` (add `TAXONOMY_SAMPLE_SHAPE_BUDGET` constant, extend `_entry_dict`, insert a new step in `_sample_for_taxonomy`)
- Modify: `tests/test_organize_classifier.py` (add a test class for the wire format and the sampling-shape budget)

`_entry_dict` is shared by Stage A's `_build_proposer_prompt` and Stage B's `_build_assigner_prompt`, so adding the field once propagates to both stages. The sampling rule is purely additive — the existing 6-step algorithm keeps working, with a new "step 5.5" that reserves up to 10 slots for distinct sections-shape signatures before the evenly-spaced fill.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_organize_classifier.py`. Use the existing `_entry` fixture helper if one already exists; otherwise paste a minimal builder at the top of the new class:

```python
# ---------------------------------------------------------------------------
# sections: visible in both Stage A (proposer) and Stage B (assigner) wire
# payloads; sampling reserves slots per distinct sections-shape
# ---------------------------------------------------------------------------


class TestSectionsWireFormat:
    def _make_entry(
        self, *, path_id: str, path: str = "/x.txt",
        sections: tuple[str, ...] = (),
        verbatim_head: str = "",
        summary: str = "summary",
    ):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id=path_id,
            path=path,
            ext=".txt",
            size_bytes=10,
            summary=summary,
            type_label="text",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head=verbatim_head,
            sections=sections,
        )

    def test_entry_dict_includes_sections(self):
        from fda.organize.classifier import _entry_dict

        e = self._make_entry(
            path_id="f000",
            sections=("Shipping Details", "Customer Details"),
        )
        d = _entry_dict(e)
        assert d["sections"] == ("Shipping Details", "Customer Details")

    def test_entry_dict_sections_default_empty_tuple(self):
        from fda.organize.classifier import _entry_dict

        e = self._make_entry(path_id="f000")
        assert _entry_dict(e)["sections"] == ()

    def test_proposer_payload_carries_sections(self):
        import json
        from fda.organize.classifier import _build_proposer_prompt

        sample = [
            self._make_entry(
                path_id="f000",
                sections=("Shipping Details", "Customer Details"),
            ),
            self._make_entry(path_id="f001", sections=("Products",)),
        ]
        raw = _build_proposer_prompt(sample, "instructions")
        payload = json.loads(raw)
        assert payload["CATALOG"][0]["sections"] == [
            "Shipping Details", "Customer Details"
        ]
        assert payload["CATALOG"][1]["sections"] == ["Products"]

    def test_assigner_payload_carries_sections(self):
        import json
        from fda.organize.classifier import _build_assigner_prompt
        from fda.organize.models import Taxonomy, TaxonomyCategory

        taxonomy = Taxonomy(
            categories=(
                TaxonomyCategory(
                    category_name="Purchase-Orders",
                    subpath="POs",
                    description="Simple POs",
                    criteria="Documents with Products section.",
                ),
                TaxonomyCategory(
                    category_name="Shipping-Orders",
                    subpath="Shipping",
                    description="Detailed shipping docs",
                    criteria="Documents with Shipping Details, Customer Details, and Shipper sections.",
                ),
            ),
            fallback_category=TaxonomyCategory(
                category_name="Misc",
                subpath="Misc",
                description="Catch-all",
                criteria="When no other category fits.",
            ),
        )
        batch = [
            self._make_entry(
                path_id="f000",
                sections=("Shipping Details", "Customer Details"),
            ),
        ]
        raw = _build_assigner_prompt(batch, taxonomy, "instructions")
        payload = json.loads(raw)
        assert payload["BATCH"][0]["sections"] == [
            "Shipping Details", "Customer Details"
        ]


class TestSamplingShapeBudget:
    def test_constant_exists(self):
        from fda.organize.classifier import TAXONOMY_SAMPLE_SHAPE_BUDGET

        assert TAXONOMY_SAMPLE_SHAPE_BUDGET == 10

    def test_rare_shapes_reserved_in_sample(self):
        """Construct a 200-entry catalog with 6 distinct sections-shapes,
        most concentrated in the dominant shape. The new shape-budget
        rule should reserve at least one entry per distinct shape, up
        to TAXONOMY_SAMPLE_SHAPE_BUDGET.

        Critical for the test's validity: every entry lives in the SAME
        flat directory so the existing top-level-dir + leaf-dir sampling
        rules cannot accidentally substitute for the shape-budget rule.
        Also use a single shared extension. The only diversity signal is
        the `sections` tuple — any rule that doesn't read `sections`
        cannot satisfy this test.
        """
        from fda.organize.classifier import _sample_for_taxonomy
        from fda.organize.models import CatalogEntry

        def _e(idx, shape):
            return CatalogEntry(
                path_id=f"f{idx:03d}",
                # Flat directory; same extension. Top-level-dir sampling
                # sees one bucket; extension sampling sees one bucket.
                path=f"/tmp/flat/file_{idx:03d}.txt",
                ext=".txt",
                size_bytes=10,  # uniform size; "largest" rule sees no signal
                summary="similar summary",
                type_label="text",
                is_junk=False,
                summary_failed=False,
                extract_status="ok",
                sections=shape,
            )

        shapes = [
            ("Products",),
            ("Shipping Details", "Customer Details", "Products"),
            ("Quote", "Items", "Valid Until"),
            ("Patient Name", "Diagnosis", "Treatment"),
            ("From", "To", "Subject"),
            ("Sheet:Q3", "Date", "Revenue"),
        ]
        # 150 entries of shape[0], 10 each of the other 5 — total 200.
        entries = [_e(i, shapes[0]) for i in range(150)]
        for s_idx, shape in enumerate(shapes[1:], start=1):
            entries += [_e(150 + s_idx * 10 + i, shape) for i in range(10)]

        sample = _sample_for_taxonomy(entries, "/tmp")
        sample_shapes = {e.sections for e in sample}
        # All 6 distinct shapes must appear. With every entry in the same
        # directory and same extension, only a rule that reads `sections`
        # can produce this result — the shape-budget rule.
        assert len(sample_shapes) == 6
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
python3 -m pytest tests/test_organize_classifier.py::TestSectionsWireFormat tests/test_organize_classifier.py::TestSamplingShapeBudget -v
```

Expected: FAIL — `_entry_dict` doesn't include `sections`; `TAXONOMY_SAMPLE_SHAPE_BUDGET` doesn't exist; the rare-shapes test may or may not fail depending on luck (assert it for determinism).

- [ ] **Step 3: Add `TAXONOMY_SAMPLE_SHAPE_BUDGET` constant to `classifier.py`.**

Edit `fda/organize/classifier.py`. Find the existing constants block (around lines 43-72, just after the imports) and add:

```python
TAXONOMY_SAMPLE_SHAPE_BUDGET = 10
```

Place it next to `TAXONOMY_SAMPLE_FALLBACK_BUDGET = 100` for clustering-by-purpose.

- [ ] **Step 4: Extend `_entry_dict` to include `sections`.**

Find `_entry_dict` (around lines 90-101) and add `"sections": e.sections`:

```python
def _entry_dict(e: CatalogEntry) -> dict[str, Any]:
    return {
        "path_id": e.path_id,
        "path": e.path,
        "ext": e.ext,
        "size_bytes": e.size_bytes,
        "summary": _truncate_summary(e.summary) if e.summary
                   else "summary unavailable",
        "type_label": e.type_label,
        "extract_status": e.extract_status,
        "verbatim_head": e.verbatim_head,
        "sections": e.sections,
    }
```

- [ ] **Step 5: Insert the shape-budget step into `_sample_for_taxonomy`.**

Find `_sample_for_taxonomy` (around lines 113-194). The new step lands **between** step 2 (top-level dir, ~lines 141-156) and step 3 (leaf dir, ~lines 158-165). This placement matters: step 3 (leaf-dir) consumes up to 70 of the 100 target slots and could starve rare shapes if shape-budget ran later. Inserting before step 3 upholds the spec's "guarantees Stage A sees at least one exemplar of each structural shape" language.

```python
    # 2b) Up to TAXONOMY_SAMPLE_SHAPE_BUDGET entries chosen by distinct
    # sections-shape signature. Guarantees rare structural types reach
    # Stage A even when they're a small fraction of the catalog.
    #
    # De-dup intent: if the first iter-entry of shape S was already
    # chosen by step 1 or 2 (extension/top-level-dir), `_add(e)` returns
    # False but we still mark the shape "seen" — shape S is represented
    # in `chosen` regardless of which rule put it there, so subsequent
    # shape-S entries skip via `seen_shapes`.
    seen_shapes: set[tuple[str, ...]] = set()
    shape_added = 0
    for e in entries:
        if shape_added >= TAXONOMY_SAMPLE_SHAPE_BUDGET:
            break
        if len(chosen) >= TAXONOMY_SAMPLE_TARGET_SIZE:
            break
        sig = e.sections
        if sig in seen_shapes:
            continue
        seen_shapes.add(sig)
        if _add(e):
            shape_added += 1
```

This is purely additive: it reserves at most 10 of the 100 target slots for distinct shapes, and de-duplicates against everything already chosen via the existing `_add` helper. Existing tests that don't exercise shape diversity continue to pass because the rule never removes any entry — it only reserves slots that the leaf-dir / largest / fill rules would otherwise consume.

- [ ] **Step 6: Run the new tests to verify they pass.**

```bash
python3 -m pytest tests/test_organize_classifier.py::TestSectionsWireFormat tests/test_organize_classifier.py::TestSamplingShapeBudget -v
```

Expected: PASS for all six tests.

- [ ] **Step 7: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. Existing classifier tests are unaffected because the new sampling step only adds entries; it never removes any.

- [ ] **Step 8: Commit.**

```bash
git add fda/organize/classifier.py tests/test_organize_classifier.py
git commit -m "$(cat <<'EOF'
organize(classifier): include sections in entry payload + shape-budget

_entry_dict (shared by Stage A proposer and Stage B assigner payloads)
now carries CatalogEntry.sections. _sample_for_taxonomy gains a new
purely-additive step that reserves up to TAXONOMY_SAMPLE_SHAPE_BUDGET=10
slots for distinct sections-shape signatures, so rare structural types
reach the proposer even when they're a small fraction of a large
catalog. Existing sampling rules are unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Update `taxonomy-proposer/SKILL.md` (Stage A prompt)

**Files:**
- Modify: `fda/organize/skills/taxonomy-proposer/SKILL.md`
- Modify: `tests/test_organize_constraints.py` (the `TestSkillContents::test_proposer_does_not_reference_path_ids_for_assignments` test continues to pass; this task adds new content adjacent to it)

The proposer prompt today instructs the model to use `verbatim_head` and filename for taxonomy proposal. We add two pieces:
1. **Structural fingerprint as signal** — when sampled docs share topic but their `sections` lists differ in size or content, propose distinct categories.
2. **Encode discriminating sections in `criteria`** — so Stage B (which can't read prose semantically) has concrete labels to match.

- [ ] **Step 1: Read the current proposer prompt to know where to insert.**

```bash
cat fda/organize/skills/taxonomy-proposer/SKILL.md
```

Note where the existing `verbatim_head` / filename guidance lives so the new bullets land alongside.

- [ ] **Step 2: Update the input-schema block to list `sections`.**

Open `fda/organize/skills/taxonomy-proposer/SKILL.md`. Find the existing field list:

```markdown
- CATALOG (JSON): a list of entries. Each entry has fields
  `path_id`, `path`, `ext`, `size_bytes`, `summary`, `type_label`,
  `extract_status`, `verbatim_head`.
  - `summary` is Reader's prose description (truncated to 200 characters).
  - `verbatim_head` is the raw first ~300 chars of the file's extracted
    text, with leading whitespace stripped. It is NOT a summary — it's
    actual file content. Empty string if extraction failed.
  - The last component of `path` is the filename.
```

Replace with:

```markdown
- CATALOG (JSON): a list of entries. Each entry has fields
  `path_id`, `path`, `ext`, `size_bytes`, `summary`, `type_label`,
  `extract_status`, `verbatim_head`, `sections`.
  - `summary` is Reader's prose description (truncated to 200 characters).
  - `verbatim_head` is the raw first ~300 chars of the file's extracted
    text, with leading whitespace stripped. It is NOT a summary — it's
    actual file content. Empty string if extraction failed.
  - `sections` is a deterministic list of section/field labels extracted
    from the document by a pure-Python regex pass (not by an LLM). Empty
    list when extraction failed or the document carries no labeled
    sections.
  - The last component of `path` is the filename.
```

- [ ] **Step 3: Append the two new behavioral rules.**

In the same file, after the existing signal-priority bullets (the section that already mentions `verbatim_head` and basenames), add the following two bullets verbatim. Keep them near the top of the prompt — Stage A is short.

```markdown
- **Structural fingerprint as signal.** Each entry has `sections` — a
  list of section/field labels extracted directly from the document
  (not by an LLM). When sampled documents share similar topics or
  summaries but their `sections` lists are clearly different in size
  or content, propose them as **distinct categories**. Two "order
  documents" with `sections=["Products"]` and
  `sections=["Shipping Details", "Customer Details", "Employee",
  "Shipper", "Order Details", "Products"]` are not the same document
  type — the second has structural fields the first doesn't. Look for
  this kind of structural diversity in the sample and reflect it in
  the taxonomy.

- **When categories are structurally distinct, encode that in
  `criteria`.** Stage B has access to the same `sections` lists you
  do, but it can only compare them to your category criteria — and
  `criteria` is free prose. When two categories differ structurally,
  write the discriminating section names directly into `criteria`
  (e.g., `"criteria": "Documents with Shipping Details, Customer
  Details, Employee, and Shipper sections in addition to Products."`).
  Stage B will then have something concrete to match. Categories
  whose discriminator is purely topical (no structural distinction)
  need no section names in `criteria`.
```

The exact wording above is also reproduced in the spec at
`docs/superpowers/specs/2026-05-07-classifier-structural-sections-design.md` §5; copy it from there if any whitespace gets mangled.

- [ ] **Step 4: Run the constraints test to confirm the prompt still passes invariants.**

```bash
python3 -m pytest tests/test_organize_constraints.py -v
```

Expected: PASS. The proposer constraint (`Do NOT emit any path_id references` or `DO NOT emit`) is unchanged; the new bullets don't affect it.

- [ ] **Step 5: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. Existing classifier tests use stub backends that ignore the prompt body, so the prompt change can't break them.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/skills/taxonomy-proposer/SKILL.md
git commit -m "$(cat <<'EOF'
organize(prompts): teach proposer to use sections + structural criteria

Stage A now treats sections-list divergence as a category-splitting
signal (e.g., simple "Products"-only docs vs detailed shipping docs
with six labeled sections — different document types even when
summaries look alike). Also instructs the proposer to write
discriminating section names directly into category criteria so
Stage B has concrete labels to match against — criteria is prose,
not structured.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Update `taxonomy-assigner/SKILL.md` (Stage B prompt)

**Files:**
- Modify: `fda/organize/skills/taxonomy-assigner/SKILL.md`

The assigner today has a 4-step priority hierarchy: filename (informative) → ignore filename (uninformative) → verbatim_head + summary → empty verbatim_head. We:

1. Insert a new step #3 **between** filename and prose: structural fingerprint vs taxonomy criteria.
2. Renumber the old step #3 to #4 and old step #4 to #5/#6 with the `extract_status`-aware fallback.
3. Add an explicit filename-vs-sections conflict rule (filename still wins).

- [ ] **Step 1: Read the current assigner prompt to know where to edit.**

```bash
cat fda/organize/skills/taxonomy-assigner/SKILL.md
```

Existing steps are around lines 26-46.

- [ ] **Step 2: Update the input-schema block to list `sections`.**

In `fda/organize/skills/taxonomy-assigner/SKILL.md`, find the existing field list:

```markdown
- BATCH (JSON): a list of file entries. Each has `path_id`, `path`, `ext`,
  `size_bytes`, `summary`, `type_label`, `extract_status`, `verbatim_head`.
  - `path` is the full file path; the last component is the filename.
  - `summary` is Reader's prose, truncated to 200 chars; may be imprecise
    about document type.
  - `verbatim_head` is the raw first ~300 chars of extracted text, leading
    whitespace stripped. Empty string if extraction failed.
```

Replace with:

```markdown
- BATCH (JSON): a list of file entries. Each has `path_id`, `path`, `ext`,
  `size_bytes`, `summary`, `type_label`, `extract_status`, `verbatim_head`,
  `sections`.
  - `path` is the full file path; the last component is the filename.
  - `summary` is Reader's prose, truncated to 200 chars; may be imprecise
    about document type.
  - `verbatim_head` is the raw first ~300 chars of extracted text, leading
    whitespace stripped. Empty string if extraction failed.
  - `sections` is a deterministic list of section/field labels extracted
    from the document by a pure-Python regex pass (not by an LLM). Empty
    list when extraction failed or the document carries no labeled
    sections.
```

- [ ] **Step 3: Replace the priority-hierarchy block with the new 6-step version.**

In `fda/organize/skills/taxonomy-assigner/SKILL.md`, find the section starting `Signal priority for assignment (read this carefully):` and replace its numbered list (current steps 1-4) with this exact text:

```markdown
Signal priority for assignment (read this carefully):

1. **Filename, if informative.** If the basename (last component of `path`)
   contains words that hint at document type or business purpose — e.g.
   `Invoice_10488.pdf`, `Q3_Sales_Report.xlsx`, `PO-2024-0042.pdf`,
   `meeting-notes-2024-08.md` — use it as a strong signal. Confirm with
   `verbatim_head` when possible, but a clearly-named file usually settles
   the assignment.
2. **If the filename is uninformative — ignore it.** Long hex strings (16+
   contiguous hex characters such as `0fa84d61b3158eaba46dee96.pdf`),
   UUID-like patterns, and generic placeholders like `doc1.pdf`,
   `Untitled.pdf`, `IMG_4521.jpg`, `scan_001.pdf` carry no semantic signal.
   Make the decision from `verbatim_head`, `sections`, and `summary` alone.
   When in doubt about whether a filename is informative, default to
   ignoring it rather than over-weighting it.
3. **Structural fingerprint vs taxonomy criteria.** Each entry carries
   `sections` — a deterministic list of section/field labels extracted
   directly from the document (not by an LLM). Stage A has been instructed
   to write discriminating section names into category `criteria` when
   categories differ structurally. Compare the file's `sections` list to
   each category's `criteria`: when two categories are otherwise plausible
   for a file, the one whose criteria *names* sections that overlap with
   the file's `sections` is the right one — even if both summaries
   describe similar topics. Example: a file with
   `sections=["Shipping Details", "Customer Details", "Shipper"]` belongs
   in a category whose criteria mention those names, not in a generic
   "Purchase Orders" category — even if both summaries mention an "order."
   Use simple substring overlap, not semantic similarity. An empty
   `sections` list never *excludes* a category — step 3 only ever
   *prefers* a structurally-matching category over alternatives.
4. **Within `verbatim_head` + `summary`:** if they disagree about document
   type, trust `verbatim_head`. Look for literal type labels in the slice
   (e.g. `"Invoice"`, `"Purchase Orders"`, `"Statement"`, `"Receipt"`)
   before falling back to prose. The summary may have been generated
   without a type label visible in the document and may have guessed.
5. **Empty-or-uninformative `sections`.** Behavior depends on
   `extract_status`:
   - `extract_status == "ok"` and `sections == []`: extraction succeeded
     but the document has no labeled sections (e.g., a free-form email,
     a flat CSV). Skip step 3 entirely; rely on filename + verbatim_head
     + summary.
   - `extract_status != "ok"` (extraction failed, no extractor, tool
     missing): no structural signal is available — same fallback as
     above. The empty list does not mean "no structure"; it means "we
     couldn't tell."
6. **Empty `verbatim_head` AND empty `sections` AND uninformative
   filename:** use `summary` only.

**Filename vs sections conflict.** When an informative filename suggests
one category but `sections` overlap better with a different category's
criteria, **filename wins** (step 1 has highest priority). Map the file
to the filename-suggested category. Real-world example:
`Invoice_old_template.pdf` with sections matching a "Quote" category —
the user's filename intent overrides the structural drift.
```

- [ ] **Step 4: Verify the assigner prompt still mentions `path_id` and `EXACTLY ONCE` (constraint test).**

```bash
python3 -m pytest tests/test_organize_constraints.py::TestSkillContents -v
```

Expected: PASS. The new content doesn't remove any of the existing constraint markers.

- [ ] **Step 5: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. Existing classifier tests use stub backends; the prompt-text change can't break them.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/skills/taxonomy-assigner/SKILL.md
git commit -m "$(cat <<'EOF'
organize(prompts): add structural-fingerprint priority to assigner

Stage B's signal priority hierarchy now has 6 steps instead of 4:
filename → uninformative-filename ignore → NEW step 3 (structural
fingerprint vs criteria) → verbatim_head/summary → empty-sections
fallback by extract_status → final summary-only fallback. Adds an
explicit filename-vs-sections conflict rule (filename wins).

The extract_status-aware fallback distinguishes "extraction failed
so we don't know the structure" from "doc legitimately has no labeled
sections" — both yield sections=() but only the former should suppress
step 3's preference logic.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

<!-- ===================== CHUNK 3 END ===================== -->

---

<!-- ===================== CHUNK 4 START — Regression tests + constraints (Tasks 9–10) ===================== -->

### Task 9: Stage B regression tests with pinned fixture text

**Files:**
- Modify: `tests/test_organize_classifier.py` (add a new test class)

Three regression cases, each with a fake backend that follows the prompt's substring-overlap rule:

1. **Structural-conflict** — pinned fixture text proves regex outputs match the spec.
2. **Empty-sections by extract_status** — `ok+empty` and `failed+empty` both fall through to summary-only.
3. **Filename-vs-sections conflict** — filename wins.

These tests fail loudly if `sections` is dropped from the wire format or the priority hierarchy is misordered.

- [ ] **Step 1: Verify the regex output for the pinned fixture text.**

Before writing the assertion, run this verification using a quoted heredoc (single-quoted EOF prevents any shell interpolation, so `$1560.00` reaches Python verbatim):

```bash
python3 <<'PY'
from fda.organize._sections import extract_sections_from_text

simple_po = """Purchase Orders

Order ID: 10488
Order Date: 2024-03-15

Products:
  - Widget A x 5

Total: $1560.00
"""

detailed = """Order ID: 10488

Shipping Details:
  Frankenversand

Customer Details:
  Hanna Moos

Employee:
  Janet Leverling

Shipper:
  Speedy Express

Order Details:
  Date: 2024-03-15

Products:
  - Widget A x 5

Total: $1560.00
"""

print("simple_po =>", extract_sections_from_text(simple_po))
print("detailed  =>", extract_sections_from_text(detailed))
PY
```

Expected:
```
simple_po => ('Products',)
detailed  => ('Shipping Details', 'Customer Details', 'Employee', 'Shipper', 'Order Details', 'Products')
```

If the output differs, **stop and re-read the spec** before writing the test.

- [ ] **Step 2: Write the failing tests.**

Append to `tests/test_organize_classifier.py`. The fake backend pattern follows the existing `TestPriorityRegressions` style (see commit `c4caaa9` for verbatim/filename-priority regression tests):

```python
# ---------------------------------------------------------------------------
# Stage B regression: sections drives category choice when topic is shared
# ---------------------------------------------------------------------------


class TestStructuralSectionsRegressions:
    """Three pinned cases:
      1. Topic-similar files with different sections-shapes route to
         different categories.
      2. Empty sections falls through cleanly regardless of extract_status.
      3. Filename overrides sections when they conflict.
    """

    def _entry(
        self, *, path_id: str, path: str,
        sections: tuple[str, ...] = (),
        verbatim_head: str = "",
        summary: str = "summary",
        extract_status: str = "ok",
    ):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id=path_id,
            path=path,
            ext=".pdf",
            size_bytes=10,
            summary=summary,
            type_label="text",
            is_junk=False,
            summary_failed=False,
            extract_status=extract_status,
            verbatim_head=verbatim_head,
            sections=sections,
        )

    def _taxonomy(self):
        from fda.organize.models import Taxonomy, TaxonomyCategory
        return Taxonomy(
            categories=(
                TaxonomyCategory(
                    category_name="Purchase-Orders",
                    subpath="POs",
                    description="Simple purchase orders",
                    criteria="Documents with Products section.",
                ),
                TaxonomyCategory(
                    category_name="Shipping-Orders",
                    subpath="Shipping",
                    description="Detailed shipping documents",
                    criteria=(
                        "Documents with Shipping Details, Customer Details, "
                        "Employee, Shipper, and Order Details sections."
                    ),
                ),
                TaxonomyCategory(
                    category_name="Sales-Invoices",
                    subpath="Invoices",
                    description="Invoices",
                    criteria="Sales invoices.",
                ),
            ),
            fallback_category=TaxonomyCategory(
                category_name="Misc",
                subpath="Misc",
                description="Catch-all",
                criteria="When no other category fits.",
            ),
        )

    def _make_backend(self):
        """Fake backend that follows the assigner prompt's documented
        priority order. Reads BATCH from the prompt JSON and decides
        category based on filename → sections → verbatim_head → summary.

        The hash-detection regex anchors on the stem (with extension
        suffix) to avoid false-positives from filenames that contain
        embedded 16+ hex substrings by coincidence. This mirrors the
        intent of the prompt's "long hex string" wording.
        """
        import json
        import re

        # Anchored: full basename must be hex stem + extension.
        HASH_BASENAME_RE = re.compile(
            r"^[0-9a-f]{16,}\.[a-z0-9]+$", re.IGNORECASE,
        )

        class StubBackend:
            def complete(self, *, system, messages, **kwargs):
                # Use entry["sections"] (not .get) so the test fails
                # loudly if Task 6's wire-format change is missing.
                user = messages[0]["content"]
                payload = json.loads(user)
                taxonomy = payload["TAXONOMY"]
                cats = taxonomy["categories"]
                fallback = taxonomy["fallback_category"]["category_name"]
                assignments = []

                for entry in payload["BATCH"]:
                    path = entry["path"]
                    basename = path.rsplit("/", 1)[-1]
                    sections = entry["sections"]   # contract assertion
                    extract_status = entry["extract_status"]
                    verbatim = entry["verbatim_head"]
                    summary = entry["summary"]

                    chosen = None

                    # Step 1: informative filename.
                    lower = basename.lower()
                    if "invoice" in lower:
                        chosen = "Sales-Invoices"
                    elif "purchase" in lower or lower.startswith("po"):
                        chosen = "Purchase-Orders"
                    # Step 2: ignore filename if it's a pure hash stem
                    # (anchored regex). Don't override; just don't decide.
                    elif HASH_BASENAME_RE.match(basename):
                        pass

                    # Step 3: structural fingerprint vs criteria
                    # (only when extract_status == "ok" AND sections nonempty).
                    if (
                        chosen is None
                        and extract_status == "ok"
                        and sections
                    ):
                        best = None
                        best_score = 0
                        for c in cats:
                            score = sum(
                                1 for s in sections if s in c["criteria"]
                            )
                            if score > best_score:
                                best = c["category_name"]
                                best_score = score
                        if best:
                            chosen = best

                    # Step 4: verbatim_head literal type labels.
                    if chosen is None:
                        if "Purchase Orders" in verbatim:
                            chosen = "Purchase-Orders"
                        elif "Invoice" in verbatim:
                            chosen = "Sales-Invoices"

                    # Step 5/6: summary only.
                    if chosen is None:
                        if "invoice" in summary.lower():
                            chosen = "Sales-Invoices"
                        elif "shipping" in summary.lower():
                            chosen = "Shipping-Orders"

                    if chosen is None:
                        chosen = fallback

                    assignments.append({
                        "path_id": entry["path_id"],
                        "category_name": chosen,
                    })

                return json.dumps({"assignments": assignments})

        return StubBackend()

    def test_structural_conflict_routes_correctly(self, logger):
        """Both files have hash filenames and topic-similar summaries.
        Only the sections list distinguishes them. The fake backend must
        follow the prompt's substring-overlap rule and route them to
        DIFFERENT categories."""
        from fda.organize.classifier import _run_stage_b

        # Per spec §7: pinned fixture text. The literal regex output
        # was verified manually before writing this test (see Step 1).
        entry_a = self._entry(
            path_id="f000",
            path="/tmp/0fa84d61b3158eaba46dee96.pdf",
            sections=("Products",),  # from "Purchase Orders" doc text
            verbatim_head="Purchase Orders\n\nOrder ID: 10488",
            summary="order document",
        )
        entry_b = self._entry(
            path_id="f001",
            path="/tmp/7c2adef94015b1d65d2c8a3f.pdf",
            sections=(  # from "Order ID: ... + Shipping Details + ..."
                "Shipping Details", "Customer Details",
                "Employee", "Shipper", "Order Details", "Products",
            ),
            verbatim_head="Order ID: 10488\n\nShipping Details:",
            summary="order document",
        )
        assignments = _run_stage_b(
            [entry_a, entry_b],
            self._taxonomy(),
            "instructions",
            backend=self._make_backend(),
            logger=logger,
            skill=_DummySkill(),
        )
        assert assignments["f000"] == "Purchase-Orders"
        assert assignments["f001"] == "Shipping-Orders"

    def test_empty_sections_ok_status_falls_through(self, logger):
        """extract_status == 'ok' and sections == () means doc has no
        labeled sections — step 3 is skipped, summary alone decides."""
        from fda.organize.classifier import _run_stage_b

        entry = self._entry(
            path_id="f000",
            path="/tmp/0fa84d61b3158eaba46dee96.pdf",
            sections=(),
            verbatim_head="",
            summary="invoice for ACME",
            extract_status="ok",
        )
        assignments = _run_stage_b(
            [entry],
            self._taxonomy(),
            "instructions",
            backend=self._make_backend(),
            logger=logger,
            skill=_DummySkill(),
        )
        # Falls through to summary-only logic; "invoice" in summary
        # routes to Sales-Invoices via step 5/6 of the priority list.
        assert assignments["f000"] == "Sales-Invoices"

    def test_empty_sections_failed_status_falls_through(self, logger):
        """extract_status == 'failed' and sections == () should behave
        identically to the 'ok'+empty case — both fall through to
        summary-only."""
        from fda.organize.classifier import _run_stage_b

        entry = self._entry(
            path_id="f000",
            path="/tmp/0fa84d61b3158eaba46dee96.pdf",
            sections=(),
            verbatim_head="",
            summary="invoice for ACME",
            extract_status="failed",
        )
        assignments = _run_stage_b(
            [entry],
            self._taxonomy(),
            "instructions",
            backend=self._make_backend(),
            logger=logger,
            skill=_DummySkill(),
        )
        assert assignments["f000"] == "Sales-Invoices"

    def test_filename_overrides_sections_conflict(self, logger):
        """Informative filename ('Invoice_...') wins over a sections list
        that overlaps better with a different category's criteria."""
        from fda.organize.classifier import _run_stage_b

        entry = self._entry(
            path_id="f000",
            path="/tmp/Invoice_old_template.pdf",
            sections=(
                "Shipping Details", "Customer Details",
                "Employee", "Shipper", "Order Details",
            ),
            verbatim_head="Order ID: 10488",
            summary="order document",
        )
        assignments = _run_stage_b(
            [entry],
            self._taxonomy(),
            "instructions",
            backend=self._make_backend(),
            logger=logger,
            skill=_DummySkill(),
        )
        # Filename wins; structural overlap with Shipping-Orders is ignored.
        assert assignments["f000"] == "Sales-Invoices"
```

The `_DummySkill` helper is needed for `_run_stage_b`. It does **not** currently exist in `tests/test_organize_classifier.py` — the existing `TestPriorityRegressions` class uses different mocking. Add the stub once at module scope (top of the test file, near other helpers) before defining `TestStructuralSectionsRegressions`:

```python
class _DummySkill:
    """Minimal SkillConfig stub. _run_stage_b only reads `body` and
    `model` from this object."""
    body = "skill body"
    model = "claude-sonnet-4-6"
```

- [ ] **Step 3: Add a Stage A diversity test using a fake proposer backend.**

This closes the chunk-4 coverage gap (the table promises Stage A diversity coverage but the tests above only exercise Stage B). The test uses a fake backend that inspects the catalog payload's `sections` field and returns a taxonomy with one category per distinct shape.

Append to the same `TestStructuralSectionsRegressions` class:

```python
    def test_stage_a_proposes_distinct_categories_when_shapes_diverge(
        self, logger,
    ):
        """End-to-end Stage A: a sample with two distinct sections-shapes
        but topic-similar summaries flows through _propose_taxonomy with
        a fake backend that reads the `sections` field; the resulting
        taxonomy contains a category per shape, not a single merged one.
        """
        import json
        from fda.organize.classifier import _propose_taxonomy

        sample = [
            self._entry(
                path_id=f"f{i:03d}",
                path=f"/tmp/flat/file_{i:03d}.txt",
                sections=("Products",),
                summary="order document",
            )
            for i in range(3)
        ] + [
            self._entry(
                path_id=f"f{i:03d}",
                path=f"/tmp/flat/file_{i:03d}.txt",
                sections=(
                    "Shipping Details", "Customer Details",
                    "Employee", "Shipper", "Order Details", "Products",
                ),
                summary="order document",
            )
            for i in range(3, 6)
        ]

        class ProposerBackend:
            def complete(self, *, system, messages, **kwargs):
                user = messages[0]["content"]
                payload = json.loads(user)
                # Read `sections` directly — fails loudly if Task 6
                # didn't put it in the proposer payload.
                shapes = {
                    tuple(e["sections"]) for e in payload["CATALOG"]
                }
                cats = []
                for i, shape in enumerate(sorted(shapes)):
                    cats.append({
                        "category_name": f"Cat-{i}",
                        "subpath": f"Cat-{i}",
                        "description": "auto-generated",
                        "criteria": "Documents with " + ", ".join(shape) + " sections.",
                    })
                return json.dumps({
                    "categories": cats,
                    "fallback_category": {
                        "category_name": "Misc",
                        "subpath": "Misc",
                        "description": "catch-all",
                        "criteria": "When no other category fits.",
                    },
                })

        taxonomy = _propose_taxonomy(
            sample, "instructions",
            backend=ProposerBackend(),
            logger=logger,
            skill=_DummySkill(),
        )
        assert len(taxonomy.categories) >= 2, (
            f"expected ≥2 categories from 2 distinct shapes; got "
            f"{[c.category_name for c in taxonomy.categories]}"
        )
```

- [ ] **Step 4: Run the new tests.**

```bash
python3 -m pytest tests/test_organize_classifier.py::TestStructuralSectionsRegressions -v
```

These are regression-locking tests, not red-green TDD. Tasks 6-8 already wired the data and prompts; the assertions here verify that contract holds. Expected: PASS for all four tests on the first run.

If any test fails, the failure mode tells you what regressed:
- Structural-conflict `f000` routed to `Misc` → Task 6's `_entry_dict` isn't carrying `sections`.
- Both files routed to the same category → fake backend's substring-overlap rule didn't find a winning category. Check that the taxonomy `criteria` actually mention the discriminating section names.
- Stage A diversity test sees 1 category → `_propose_taxonomy` payload doesn't contain `sections` per entry, OR the fake backend assertion `e["sections"]` raised KeyError. Both failure modes point to incomplete Task 6 wiring.

- [ ] **Step 5: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit.**

```bash
git add tests/test_organize_classifier.py
git commit -m "$(cat <<'EOF'
organize(classifier): regression tests for structural-sections priority

Four pinned regression cases:
- Stage B structural-conflict: two hash-named, topic-similar files
  distinguished ONLY by sections-shape route to different categories.
- Stage B empty-sections fallback: extract_status="ok"+empty and
  "failed"+empty both fall through to summary-only without tripping
  step 3 logic.
- Stage B filename-vs-sections: informative filename overrides
  structural overlap (filename keeps highest priority).
- Stage A diversity: a sample with two distinct sections-shapes flows
  through _propose_taxonomy and produces ≥2 categories — fake backend
  reads the `sections` field from the payload, asserting the contract
  fails loudly if the wire format ever drops it.

The pinned Stage B fixture text matches single-pass
extract_sections_from_text output verified manually before writing the
assertion. The fake backend uses anchored hex-basename matching to
avoid false-positives on embedded hex substrings.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Extend `CONSTS` lint test for the new constants

**Files:**
- Modify: `tests/test_organize_constraints.py` (the `CONSTS` mapping in `TestEachConstantHasOneHome`)

Four new entries: three for `_sections.py`, one for `classifier.py`. Per Codex review #5, do NOT extend `DISTINCTIVE_LITERALS` — `15`, `40`, and `10` are too generic and would false-positive elsewhere.

- [ ] **Step 1: Open `tests/test_organize_constraints.py` and find the `CONSTS` dict** (around lines 51-72).

- [ ] **Step 2: Add the four new entries.**

Insert after the existing `READER_*` and `VERBATIM_HEAD_CHARS` entries (so all reader/extractor adjacent tunables cluster together):

```python
        "MAX_SECTIONS_PER_FILE": ("_sections.py", "15"),
        "SECTION_HEADER_MAX_CHARS": ("_sections.py", "40"),
        "SECTION_SCAN_BYTES": ("_sections.py", "16 * 1024"),
        "TAXONOMY_SAMPLE_SHAPE_BUDGET": ("classifier.py", "10"),
```

`SECTION_HEADER_MIN_CHARS = 3` is intentionally NOT in this dict — `3` is too generic for a literal-uniqueness check (Codex review #5 + same exception we made for `VERBATIM_HEAD_CHARS = 300`). The named-constant-defined check covers it; literal collisions don't matter.

- [ ] **Step 3: Run the constraints test to confirm it passes.**

```bash
python3 -m pytest tests/test_organize_constraints.py::TestEachConstantHasOneHome -v
```

Expected: PASS for both the named-constant and distinctive-literal sub-tests. The named-constant check verifies each new constant is defined exactly where the dict says; the distinctive-literal check is unchanged and still passes.

- [ ] **Step 4: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add tests/test_organize_constraints.py
git commit -m "$(cat <<'EOF'
organize(constraints): register new sections constants in CONSTS check

Extends the named-constant-defined check to cover the three
_sections.py constants (MAX_SECTIONS_PER_FILE, SECTION_HEADER_MAX_CHARS,
SECTION_SCAN_BYTES) and the new classifier sampling budget
(TAXONOMY_SAMPLE_SHAPE_BUDGET). SECTION_HEADER_MIN_CHARS=3 is
intentionally excluded — its literal value is too generic for a
uniqueness check, mirroring the exception already in place for
VERBATIM_HEAD_CHARS=300.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

<!-- ===================== CHUNK 4 END ===================== -->

---

### Task 11 (manual): Validate against the Northwind fixture

**Files:** none (manual verification + report).

This task is not test-driven; it measures real Sonnet behavior against the test fixture. Run after Chunk 4 lands.

> **Prerequisite:** at the time of writing this plan, `scripts/` is untracked in git (see `git status` — the directory holds `diag_organize.py` and the fixture-randomization script but neither is committed). Before running this task, **either**:
>
> 1. Confirm the user has committed those scripts, **or**
> 2. Verify the scripts exist on disk via `ls -la scripts/` and proceed using the local copies, **or**
> 3. Use the Obsidian runbook `00_Me/02_Side_Hustle/Lion_Chemtech/FDA/FDA Test Fixture Randomization Runbook.md` to generate a fixture by hand.
>
> If none of those work, stop and ask the user how to proceed. **Do not synthesize a fresh fixture script** as part of this task.

- [ ] **Step 1: Generate a fresh fixture copy.**

The existing fixture under `/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003` may have been mutated by previous organize runs. Generate a new one. The exact command depends on the prerequisite above:

- If `scripts/randomize_company_documents.py` (or similar) is present, invoke it with the source folder path documented in the Obsidian runbook (typically `/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/02_company_documents`) and an output path under `/tmp/fda-test-sets/`.
- If using the Obsidian runbook's manual recipe: follow the documented `cp -r` + rename + `manifest.csv` write-out steps.

Verify the output: `ls /tmp/fda-test-sets/<fresh-fixture-name>/ | wc -l` should show ~10 folders + 1 manifest, and `cat /tmp/fda-test-sets/<fresh-fixture-name>/manifest.csv | wc -l` should show ~101 rows.

- [ ] **Step 2: Run organize with the new fixture, applying the plan.**

```bash
python3 scripts/diag_organize.py /tmp/fda-test-sets/<fresh-fixture-name> --apply 2>&1 | tee /tmp/organize-run.log
```

The detailed log path will appear in the output (`📝 logging to ~/.fda/logs/organize/...`).

If `scripts/diag_organize.py` is missing, stop and ask the user — do not synthesize an equivalent. The script is a standing diagnostic harness; the project owner controls its shape.

- [ ] **Step 3: Cross-reference against `manifest.csv`.**

For each file in the fixture's `manifest.csv`, find its post-run location and compare against the manifest's "true category" column. Score per-class accuracy.

The acceptance threshold (per spec §8): the simple-PO vs detailed-shipping-order split that the verbatim-head spec couldn't resolve should now produce ≥95% per-class accuracy. The other classes (Invoices, Stock Reports) should remain at the post-Chunk-4 verbatim-head level (they were already correct).

- [ ] **Step 4: Inspect the `~/.fda/logs/organize/...` log.**

Search for:
- `TAXONOMY_PROPOSED categories=N fallback="..."` — confirm at least 4 categories were proposed (PO, Invoice, Shipping, Stock Report).
- `CLASSIFIER_GROUP category="Shipping-And-Fulfillment" files=N` — confirm shipping orders landed in their own group, not merged into POs.
- `ASSIGNER_BATCH_DONE batch=...` — confirm Stage B completed without coercing files to fallback at high rates (`ASSIGNER_COERCE_FALLBACK` events should be rare or absent).

- [ ] **Step 5: Report numbers to the user.**

Format:

```
Fixture: randomized-company-documents-<date>
Total files: NNN
Per-class accuracy:
  Purchase Orders:        XX/30 (YY%)
  Invoices:                XX/30 (YY%)
  Shipping Orders:         XX/34 (YY%)  ← the regression target
  Stock Reports:           X/6  (YY%)
  Manifest:                X/1  (YY%)
Overall:                   XXX/NNN (YY%)
```

If shipping-order accuracy is < 95%, **do not declare success** — re-read the log, identify which step in Stage B's priority hierarchy failed, and report back rather than ad-hoc tuning.

If shipping-order accuracy is ≥ 95%, the implementation is complete. Note any other categories that regressed compared to pre-Chunk-4 baseline.

---

## Self-review

After writing the plan above, re-checked against the spec at
`docs/superpowers/specs/2026-05-07-classifier-structural-sections-design.md`:

| Spec section | Plan task |
|---|---|
| §1 `ExtractionResult.sections` | Task 1 |
| §2 `CatalogEntry.sections` | Task 2 |
| §3 `_sections.py` module + helper + 4 constants + extractor wiring | Tasks 3, 4 |
| §4 Reader pass-through + `_fail_entry` preservation | Task 5 |
| §5 Stage A payload + sampling-shape budget + prompt | Tasks 6, 7 |
| §6 Stage B payload + 6-step priority hierarchy + filename-vs-sections rule | Tasks 6 (payload), 8 (prompt) |
| §7 Tests (models, sections, extractors, reader, classifier, constraints) | Tasks 1-2, 3, 4, 5, 6+9, 10 |
| §8 Manual validation | Task 11 |

No spec section is uncovered. No placeholders ("TBD", "TODO") in plan tasks. Type and method names are consistent across tasks (`extract_sections_from_text`, `MAX_SECTIONS_PER_FILE`, `TAXONOMY_SAMPLE_SHAPE_BUDGET`, `_entry_dict`, `_sample_for_taxonomy`, `_run_stage_b`, `_DummySkill`).
