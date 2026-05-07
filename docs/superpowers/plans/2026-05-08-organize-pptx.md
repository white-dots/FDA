# Organize: `.pptx` Extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `.pptx` text extractor to `fda/organize/_extractors.py` that produces an `ExtractionResult` with `text` (per-slide banner + shape text + speaker notes; drives `verbatim_head` + Haiku summary) and `sections` (slide titles in slide order; drives the classifier's structural-fingerprint signal).

**Architecture:** Faithful extension of the docx/xlsx format-onboarding pattern — one extractor function in `_extractors.py`, one entry in the `EXTRACTORS` dict, no new modules, no protocol abstraction. Reader keeps ownership of the 64 KiB `text` contract cap (`READER_TEXT_CAP_BYTES`), so the extractor does not cap `text` itself; three new constants (`_PPTX_SLIDES_MAX`, `_PPTX_SHAPES_PER_SLIDE_MAX`, `_PPTX_NOTES_CHARS_PER_SLIDE_MAX`) bound in-process memory while serializing.

**Tech Stack:** Python 3.12+, `python-pptx>=1.0.2` (new runtime dep), pytest. Spec at `docs/superpowers/specs/2026-05-07-organize-pptx-design.md`.

**Pretest:** every task ends with running the full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` (CLAUDE.md command). If that path is unavailable on the executing host, fall back to whichever Python 3.12 the test fixture conftest works under (the docx/xlsx plan verified `/opt/anaconda3/bin/python3 -m pytest ...` works as well).

**python-pptx API quick reference (1.0.x):**
- Load: `prs = pptx.Presentation(str(path))`. Not a context manager; no `close()`. Let GC release the zip handle.
- Slides: `prs.slides` is iterable in slide order; `prs.slide_layouts[N]` for fixture construction (`[0]`=Title Slide, `[1]`=Title+Content, `[5]`=Title Only, `[6]`=Blank).
- Title: `slide.shapes.title` returns the title-placeholder shape or `None` (does not raise) when the layout has no title placeholder.
- Title text: `title.text` (defined as `self.text_frame.text` in the public API — these are identical).
- Shapes: `slide.shapes` is iterable; `shape.has_text_frame` is `True` for text-bearing shapes; `shape.text_frame.text` joins runs/paragraphs with `\n`.
- Notes: **Always** gate access on `slide.has_notes_slide` first — `slide.notes_slide` *creates* a notes slide as a side effect when one does not exist. Even when `has_notes_slide` is `True`, `slide.notes_slide.notes_text_frame` can be `None` (notes placeholder removed from the notes-slide layout); always None-check before reading `.text`.

---

## Task 1: Wire `python-pptx` dependency and reserve three new constants

**Files:**
- Modify: `pyproject.toml:11-19` (dependencies list)
- Modify: `fda/organize/_extractors.py` (add three new constants after the xlsx block ending at line 46)
- Modify: `tests/test_organize_constraints.py:51-81` (add three entries to `CONSTS`)

- [ ] **Step 1: Add three constants to the `CONSTS` dict**

Edit `tests/test_organize_constraints.py`. Inside `class TestEachConstantHasOneHome:`, append to the `CONSTS` dict (just below the `_XLSX_*` entries at line 80):

```python
        "_PPTX_SLIDES_MAX": ("_extractors.py", "100"),
        "_PPTX_SHAPES_PER_SLIDE_MAX": ("_extractors.py", "50"),
        "_PPTX_NOTES_CHARS_PER_SLIDE_MAX": ("_extractors.py", "2000"),
```

- [ ] **Step 2: Run the constants test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py::TestEachConstantHasOneHome::test_constant_defined_in_owning_module -v`
Expected: FAIL — `_PPTX_SLIDES_MAX missing from _extractors.py`.

- [ ] **Step 3: Add the three constants to `_extractors.py`**

Edit `fda/organize/_extractors.py`. Just below the existing `_XLSX_MERGED_CELLS_MIN = 3` line (~line 46), add:

```python

# pptx serialization caps (memory-bound the in-process slide/shape/notes walk;
# do NOT cap `text` — Reader owns the 64 KiB contract cap via
# READER_TEXT_CAP_BYTES).
_PPTX_SLIDES_MAX = 100
_PPTX_SHAPES_PER_SLIDE_MAX = 50
_PPTX_NOTES_CHARS_PER_SLIDE_MAX = 2000
```

- [ ] **Step 4: Update `pyproject.toml` dependencies**

Edit `pyproject.toml`. Add `python-pptx>=1.0.2` to the runtime dependencies list. The full block becomes:

```toml
dependencies = [
    "anthropic>=0.45.0",
    "pandas>=1.5.0",
    "openpyxl>=3.0.9",
    "python-docx>=1.0.0",
    "python-pptx>=1.0.2",
    "msal>=1.20.0",
    "requests>=2.28.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 5: Install the editable package so the new dep is importable**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pip install -e .`
Expected: pip resolves and installs `python-pptx` and its transitive deps (`lxml`, `XlsxWriter`, `Pillow`).

- [ ] **Step 6: Run the constants test to verify it passes**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py -v`
Expected: PASS (all entries in `CONSTS` find their owning module + literal).

- [ ] **Step 7: Run the full suite to confirm no regression**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml fda/organize/_extractors.py tests/test_organize_constraints.py
git commit -m "organize(extractors): declare python-pptx dep and reserve pptx constants"
```

---

## Task 2: `_extract_pptx` happy path — slide titles become sections

**Files:**
- Modify: `fda/organize/_extractors.py` (add `_extract_pptx`, register `.pptx` in `EXTRACTORS` after the `.xlsx` entry)
- Modify: `tests/test_organize_extractors.py` (new `TestPptxSections` class with fixture helper + happy-path tests)

- [ ] **Step 1: Write the failing test (single slide with title + multi-slide order/dedupe)**

Append a new class and helper to `tests/test_organize_extractors.py`:

```python
# ---------------------------------------------------------------------------
# .pptx — slide-title placeholders drive sections; banner+shapes+notes drive text
# ---------------------------------------------------------------------------


def _build_pptx(path, *, slides):
    """Build a minimal .pptx at `path`.

    `slides` is a list of dicts:
      {"layout": int (slide_layouts index, default 0=Title Slide),
       "title": str|None,
       "body_shapes": list[str]|None,
       "notes": str|None}

    `notes` is set ONLY when a string is provided — touching `slide.notes_slide`
    has a creation side effect, so leaving it None must NOT touch it.
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    for spec in slides:
        layout_idx = spec.get("layout", 0)
        slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])
        if spec.get("title") is not None and slide.shapes.title is not None:
            slide.shapes.title.text = spec["title"]
        for body_text in spec.get("body_shapes") or []:
            tx = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
            tx.text_frame.text = body_text
        notes = spec.get("notes")
        if notes is not None:  # only touch notes_slide if explicitly requested
            slide.notes_slide.notes_text_frame.text = notes
    prs.save(str(path))


class TestPptxSections:
    def test_single_slide_with_title_yields_one_section(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "a.pptx"
        _build_pptx(p, slides=[{"title": "Quarterly Review"}])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("Quarterly Review",)

    def test_multi_slide_titles_preserved_in_order(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "deck.pptx"
        _build_pptx(p, slides=[
            {"title": "Intro"},
            {"title": "Results"},
            {"title": "Next Steps"},
        ])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("Intro", "Results", "Next Steps")

    def test_duplicate_titles_deduped(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "dups.pptx"
        _build_pptx(p, slides=[
            {"title": "Agenda"},
            {"title": "Agenda"},
            {"title": "Wrap"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Agenda", "Wrap")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxSections -v`
Expected: FAIL — `r.status == "no_extractor"` (no `.pptx` in the dispatch table yet).

- [ ] **Step 3: Implement `_extract_pptx` skeleton with sections only**

Edit `fda/organize/_extractors.py`. Add the new function below `_extract_xlsx` (after the `wb1.close()` block ends, around line 295):

```python
def _extract_pptx(path: Path) -> ExtractionResult:
    """Extract text + slide-title sections from a .pptx file.

    Sections: slide titles read from the title placeholder, in slide order,
    deduped, length-guarded, capped at MAX_SECTIONS_PER_FILE. Slides whose
    layout has no title placeholder contribute nothing to sections.

    Text: per slide, "Slide N: <title>\\n" banner + shape text from every
    text-bearing shape + speaker notes (when present, with all guards).
    No extractor-side byte cap — Reader owns the 64 KiB contract cap.
    """
    from pptx import Presentation

    prs = Presentation(str(path))
    sections: list[str] = []
    seen: dict[str, None] = {}
    text_parts: list[str] = []

    for slide_idx, slide in enumerate(prs.slides, start=1):
        if slide_idx > _PPTX_SLIDES_MAX:
            break

        title_shape = slide.shapes.title
        title_text = ""
        if title_shape is not None:
            raw = title_shape.text or ""
            title_text = " ".join(raw.split())
            if (
                SECTION_HEADER_MIN_CHARS <= len(title_text) <= SECTION_HEADER_MAX_CHARS
                and title_text not in seen
                and len(sections) < MAX_SECTIONS_PER_FILE
            ):
                seen[title_text] = None
                sections.append(title_text)

        if title_text:
            text_parts.append(f"Slide {slide_idx}: {title_text}\n")
        else:
            text_parts.append(f"Slide {slide_idx}:\n")

    return ExtractionResult(
        text="".join(text_parts),
        status="ok",
        sections=tuple(sections),
    )
```

- [ ] **Step 4: Register `.pptx` in `EXTRACTORS`**

Edit `fda/organize/_extractors.py`. In the `EXTRACTORS` dict (around line 297-307), add `".pptx": _extract_pptx` immediately **after** the `.xlsx` entry:

```python
EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _read_text,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
}
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxSections -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): _extract_pptx slide-title sections"
```

---

## Task 3: `_extract_pptx` sections edge cases — length guards, no-title slides, MAX cap

**Files:**
- Modify: `tests/test_organize_extractors.py` (extend `TestPptxSections` with edge cases)

- [ ] **Step 1: Append edge-case tests to `TestPptxSections`**

Add these methods inside the existing `TestPptxSections` class:

```python
    def test_slide_without_title_placeholder_contributes_no_section(self, tmp_path):
        """layout 6 (Blank) has no title placeholder; sections should skip it."""
        from fda.organize import _extractors

        p = tmp_path / "blank.pptx"
        _build_pptx(p, slides=[
            {"layout": 0, "title": "Has Title"},
            {"layout": 6, "title": None},
            {"layout": 0, "title": "After Blank"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Has Title", "After Blank")

    def test_two_char_title_filtered_by_min_chars(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "short.pptx"
        _build_pptx(p, slides=[{"title": "OK"}, {"title": "Real Title"}])
        r = _extractors.extract(p)
        assert r.sections == ("Real Title",)

    def test_long_title_filtered_by_max_chars(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "long.pptx"
        # 41 chars > SECTION_HEADER_MAX_CHARS (40)
        _build_pptx(p, slides=[
            {"title": "x" * 41},
            {"title": "Kept"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Kept",)

    def test_whitespace_only_title_filtered(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "ws.pptx"
        _build_pptx(p, slides=[
            {"title": "   "},
            {"title": "Real"},
        ])
        r = _extractors.extract(p)
        assert r.sections == ("Real",)

    def test_more_than_max_sections_capped(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._sections import MAX_SECTIONS_PER_FILE

        p = tmp_path / "many.pptx"
        _build_pptx(p, slides=[
            {"title": f"Slide{i:02d}"} for i in range(MAX_SECTIONS_PER_FILE + 5)
        ])
        r = _extractors.extract(p)
        assert len(r.sections) == MAX_SECTIONS_PER_FILE
        assert r.sections[0] == "Slide00"
        assert r.sections[-1] == f"Slide{MAX_SECTIONS_PER_FILE - 1:02d}"

    def test_empty_deck_yields_empty_sections(self, tmp_path):
        from fda.organize import _extractors
        from pptx import Presentation

        p = tmp_path / "empty.pptx"
        Presentation().save(str(p))
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ()
        assert r.text == ""
```

- [ ] **Step 2: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxSections -v`
Expected: PASS (9 tests total — 3 from Task 2 + 6 new).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): pptx sections edge cases (length guards, MAX cap, no-title)"
```

---

## Task 4: `_extract_pptx` text — banner + shape text serialization

**Files:**
- Modify: `fda/organize/_extractors.py` (extend the slide loop with shape iteration)
- Modify: `tests/test_organize_extractors.py` (new `TestPptxText` class)

- [ ] **Step 1: Write the failing test (banner + shape text)**

Append to `tests/test_organize_extractors.py`:

```python
class TestPptxText:
    def test_banner_includes_slide_index_and_title(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "banner.pptx"
        _build_pptx(p, slides=[
            {"title": "Intro"},
            {"title": "Results"},
        ])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Slide 1: Intro" in r.text
        assert "Slide 2: Results" in r.text

    def test_no_title_banner_has_no_title_suffix(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "blank.pptx"
        _build_pptx(p, slides=[{"layout": 6, "title": None}])
        r = _extractors.extract(p)
        # Banner is exactly "Slide 1:\n" with no title suffix.
        assert "Slide 1:\n" in r.text
        assert "Slide 1: " not in r.text  # no trailing-space title form

    def test_shape_text_included_in_text(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "shapes.pptx"
        _build_pptx(p, slides=[
            {"title": "Header", "body_shapes": ["Bullet point one", "Bullet point two"]},
        ])
        r = _extractors.extract(p)
        assert "Bullet point one" in r.text
        assert "Bullet point two" in r.text

    def test_title_shape_appears_only_in_banner_or_shapes_not_omitted(self, tmp_path):
        """The title appears in the banner, and may also appear when shape
        iteration visits the title placeholder. Both are acceptable per spec
        (the banner makes slide order legible; the shape pass keeps iteration
        uniform). Assert presence, not exact count."""
        from fda.organize import _extractors

        p = tmp_path / "title_only.pptx"
        _build_pptx(p, slides=[{"title": "OnlyTitle"}])
        r = _extractors.extract(p)
        assert "OnlyTitle" in r.text
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxText -v`
Expected: FAIL — `test_shape_text_included_in_text` fails (no shape iteration yet); `test_no_title_banner_has_no_title_suffix` may already pass.

- [ ] **Step 3: Extend `_extract_pptx` with shape iteration**

Edit `fda/organize/_extractors.py`. Inside the existing `_extract_pptx` slide loop, **after** the banner append, add a shape walk. The full slide loop body now becomes:

```python
    for slide_idx, slide in enumerate(prs.slides, start=1):
        if slide_idx > _PPTX_SLIDES_MAX:
            break

        title_shape = slide.shapes.title
        title_text = ""
        if title_shape is not None:
            raw = title_shape.text or ""
            title_text = " ".join(raw.split())
            if (
                SECTION_HEADER_MIN_CHARS <= len(title_text) <= SECTION_HEADER_MAX_CHARS
                and title_text not in seen
                and len(sections) < MAX_SECTIONS_PER_FILE
            ):
                seen[title_text] = None
                sections.append(title_text)

        if title_text:
            text_parts.append(f"Slide {slide_idx}: {title_text}\n")
        else:
            text_parts.append(f"Slide {slide_idx}:\n")

        for shape_idx, shape in enumerate(slide.shapes, start=1):
            if shape_idx > _PPTX_SHAPES_PER_SLIDE_MAX:
                break
            if not shape.has_text_frame:
                continue
            body = shape.text_frame.text
            if body:
                text_parts.append(body + "\n")

        text_parts.append("\n")  # blank line between slides
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxText -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): pptx text — banner + shape-text serialization"
```

---

## Task 5: `_extract_pptx` — speaker notes with all guards

**Files:**
- Modify: `fda/organize/_extractors.py` (notes branch with `has_notes_slide`, `notes_text_frame is not None`, whitespace strip, char cap)
- Modify: `tests/test_organize_extractors.py` (new `TestPptxNotes` class)

- [ ] **Step 1: Write the failing tests (notes happy path + four guards)**

Append to `tests/test_organize_extractors.py`:

```python
class TestPptxNotes:
    def test_notes_present_appended_to_text(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "notes.pptx"
        _build_pptx(p, slides=[
            {"title": "T", "notes": "Speaker context here."},
        ])
        r = _extractors.extract(p)
        assert "Notes: Speaker context here." in r.text

    def test_notes_truncated_at_cap(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _PPTX_NOTES_CHARS_PER_SLIDE_MAX

        long_notes = "x" * (_PPTX_NOTES_CHARS_PER_SLIDE_MAX + 200)
        p = tmp_path / "long_notes.pptx"
        _build_pptx(p, slides=[{"title": "T", "notes": long_notes}])
        r = _extractors.extract(p)
        # The full long_notes string should not appear; the truncated prefix should.
        truncated = "Notes: " + ("x" * _PPTX_NOTES_CHARS_PER_SLIDE_MAX)
        assert truncated in r.text
        assert long_notes not in r.text

    def test_no_notes_no_side_effect(self, tmp_path):
        """Critical: extracting from a deck with no notes must NOT cause
        slide.notes_slide to be created on disk."""
        from fda.organize import _extractors
        from pptx import Presentation

        p = tmp_path / "no_notes.pptx"
        _build_pptx(p, slides=[{"title": "T"}])  # no `notes` key
        # Sanity check: the deck on disk has no notes slide before extraction.
        prs_before = Presentation(str(p))
        assert prs_before.slides[0].has_notes_slide is False

        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Notes:" not in r.text

        # Re-open after extraction; assert notes slide was NOT created.
        # (Extraction does not write back to disk, but this also catches any
        # accidental in-process mutation that could leak to a future writer.)
        prs_after = Presentation(str(p))
        assert prs_after.slides[0].has_notes_slide is False

    def test_whitespace_only_notes_skipped(self, tmp_path):
        """PowerPoint commonly creates notes slides containing only a stray
        newline; those must NOT produce a spurious 'Notes:' line."""
        from fda.organize import _extractors

        p = tmp_path / "ws_notes.pptx"
        _build_pptx(p, slides=[{"title": "T", "notes": "\n"}])
        r = _extractors.extract(p)
        assert "Notes:" not in r.text

    def test_notes_text_frame_none_skipped(self, tmp_path, monkeypatch):
        """Even when has_notes_slide is True, notes_text_frame can be None
        (notes placeholder removed from the notes-slide layout). The extractor
        must None-check before reading .text."""
        from fda.organize import _extractors
        from pptx import Presentation

        p = tmp_path / "tf_none.pptx"
        _build_pptx(p, slides=[{"title": "T", "notes": "real notes"}])

        # Patch NotesSlide.notes_text_frame to None at the class level so the
        # extractor sees the None case without us having to manipulate XML.
        from pptx.slide import NotesSlide
        monkeypatch.setattr(
            NotesSlide, "notes_text_frame", property(lambda self: None)
        )

        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Notes:" not in r.text
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxNotes -v`
Expected: FAIL on every test that needs the notes branch (no notes branch exists yet).

- [ ] **Step 3: Add the notes branch to `_extract_pptx`**

Edit `fda/organize/_extractors.py`. Inside `_extract_pptx`, **inside** the slide loop, immediately **before** the trailing `text_parts.append("\n")` blank-line append, add:

```python
        if slide.has_notes_slide:
            notes_tf = slide.notes_slide.notes_text_frame
            if notes_tf is not None:
                notes_text = notes_tf.text or ""
                if notes_text.strip():
                    text_parts.append(
                        "Notes: "
                        + notes_text[:_PPTX_NOTES_CHARS_PER_SLIDE_MAX]
                        + "\n"
                    )
```

The full slide-loop body, in its final form, is now:

```python
    for slide_idx, slide in enumerate(prs.slides, start=1):
        if slide_idx > _PPTX_SLIDES_MAX:
            break

        title_shape = slide.shapes.title
        title_text = ""
        if title_shape is not None:
            raw = title_shape.text or ""
            title_text = " ".join(raw.split())
            if (
                SECTION_HEADER_MIN_CHARS <= len(title_text) <= SECTION_HEADER_MAX_CHARS
                and title_text not in seen
                and len(sections) < MAX_SECTIONS_PER_FILE
            ):
                seen[title_text] = None
                sections.append(title_text)

        if title_text:
            text_parts.append(f"Slide {slide_idx}: {title_text}\n")
        else:
            text_parts.append(f"Slide {slide_idx}:\n")

        for shape_idx, shape in enumerate(slide.shapes, start=1):
            if shape_idx > _PPTX_SHAPES_PER_SLIDE_MAX:
                break
            if not shape.has_text_frame:
                continue
            body = shape.text_frame.text
            if body:
                text_parts.append(body + "\n")

        if slide.has_notes_slide:
            notes_tf = slide.notes_slide.notes_text_frame
            if notes_tf is not None:
                notes_text = notes_tf.text or ""
                if notes_text.strip():
                    text_parts.append(
                        "Notes: "
                        + notes_text[:_PPTX_NOTES_CHARS_PER_SLIDE_MAX]
                        + "\n"
                    )

        text_parts.append("\n")  # blank line between slides
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxNotes -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): pptx speaker notes with has_notes_slide guard, None and whitespace handling, char cap"
```

---

## Task 6: `_extract_pptx` — slide and shape caps + corrupt-file failure path

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestPptxCaps` and `TestPptxFailure` classes — caps already enforced by code from Tasks 2–5; this task locks the contracts in tests)

- [ ] **Step 1: Write the failing tests (caps + corrupt file)**

Append to `tests/test_organize_extractors.py`:

```python
class TestPptxCaps:
    def test_slides_beyond_cap_not_in_text(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _PPTX_SLIDES_MAX

        p = tmp_path / "many_slides.pptx"
        # Build _PPTX_SLIDES_MAX + 3 slides, each with a unique body shape.
        slide_specs = [
            {"title": None, "layout": 6, "body_shapes": [f"BODY_{i}"]}
            for i in range(_PPTX_SLIDES_MAX + 3)
        ]
        _build_pptx(p, slides=slide_specs)

        r = _extractors.extract(p)
        assert r.status == "ok"
        # Slides 1.._PPTX_SLIDES_MAX serialized; slides beyond are not.
        assert f"BODY_{_PPTX_SLIDES_MAX - 1}" in r.text
        assert f"BODY_{_PPTX_SLIDES_MAX}" not in r.text
        assert f"BODY_{_PPTX_SLIDES_MAX + 2}" not in r.text

    def test_shapes_beyond_cap_not_in_text(self, tmp_path):
        from fda.organize import _extractors
        from fda.organize._extractors import _PPTX_SHAPES_PER_SLIDE_MAX

        p = tmp_path / "many_shapes.pptx"
        # One slide with > _PPTX_SHAPES_PER_SLIDE_MAX text shapes.
        bodies = [f"SHAPE_{i}" for i in range(_PPTX_SHAPES_PER_SLIDE_MAX + 5)]
        _build_pptx(p, slides=[{"layout": 6, "title": None, "body_shapes": bodies}])

        r = _extractors.extract(p)
        assert r.status == "ok"
        # The shape iteration index is bounded by _PPTX_SHAPES_PER_SLIDE_MAX,
        # but the shape iterator may include the title placeholder etc., so
        # we cannot assert exactly which late shapes are dropped — just that
        # *some* late shapes are dropped and *some* early shapes are kept.
        kept = sum(1 for i in range(_PPTX_SHAPES_PER_SLIDE_MAX + 5) if f"SHAPE_{i}" in r.text)
        assert kept < _PPTX_SHAPES_PER_SLIDE_MAX + 5
        assert "SHAPE_0" in r.text


class TestPptxFailure:
    def test_corrupt_pptx_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "broken.pptx"
        p.write_bytes(b"not a real pptx")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestPptxCaps tests/test_organize_extractors.py::TestPptxFailure -v`
Expected: PASS (3 tests). The caps were implemented in Tasks 2 and 4; this task adds explicit regression coverage. The corrupt path is caught by the outer `extract()` try/except.

If any test fails, the cap implementation needs to be re-checked against the spec — do **not** loosen the test; fix the code.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): pptx slide/shape cap regression + corrupt-file failure"
```

---

## Task 7: Reader integration — `CatalogEntry.sections` populated for `.pptx`

**Files:**
- Modify: `tests/test_organize_reader.py:694` (extend the existing `TestSectionsPropagationDocxXlsx` class with parallel pptx cases)

The exact reader API from the existing docx/xlsx tests (`tests/test_organize_reader.py:710`) is:
- `reader.read(workspace, backend=fake_backend, logger=logger)` returns a catalog
- `catalog.entries` is a list; each entry has `.path` (string), `.sections` (tuple), `.extract_status`, `.verbatim_head`
- `e.path.endswith("foo.docx")` is the matcher convention
- `workspace`, `fake_backend`, `logger` are pytest fixtures already defined in `tests/test_organize_reader.py`

- [ ] **Step 1: Add two pptx tests inside `class TestSectionsPropagationDocxXlsx`**

Open `tests/test_organize_reader.py`, find `class TestSectionsPropagationDocxXlsx` (around line 694), and append these two methods inside it (immediately after the existing `test_xlsx_failed_extraction_yields_empty_sections_in_catalog` method):

```python
    def test_pptx_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        from fda.organize import reader
        from pptx import Presentation

        f = workspace / "deck.pptx"
        prs = Presentation()
        for title in ("Quarter Plan", "Risks"):
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = title
        prs.save(str(f))

        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("deck.pptx"))
        assert e.extract_status == "ok"
        assert e.sections == ("Quarter Plan", "Risks")
        assert e.verbatim_head.startswith("Slide 1: Quarter Plan")

    def test_pptx_failed_extraction_yields_empty_sections_in_catalog(
        self, workspace, fake_backend, logger
    ):
        """Parallel to docx/xlsx: when the extractor fails, the catalog
        entry's sections is preserved as ()."""
        from fda.organize import reader

        f = workspace / "broken.pptx"
        f.write_bytes(b"not a real pptx")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("broken.pptx"))
        assert e.extract_status == "failed"
        assert e.sections == ()
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx::test_pptx_sections_flow_through_reader tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx::test_pptx_failed_extraction_yields_empty_sections_in_catalog -v`
Expected: PASS (2 tests).

- [ ] **Step 4: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_organize_reader.py
git commit -m "organize(reader): pptx sections preserved through reader (passthrough + failure)"
```

---

## Task 8: Pipeline integration — add `.pptx` to the integration corpus

**Files:**
- Modify: `tests/test_organize_pipeline.py:17-43` (extend the `workspace` fixture with one pptx)
- Modify: `tests/test_organize_pipeline.py:90-109` (assert pptx lands in `Texts/`)
- Modify: `tests/test_organize_pipeline.py:111-125` (assert pptx exists in preview)

The existing pipeline `workspace` fixture seeds docx/xlsx inline using their format-native libraries (no separate builder helper). Mirror that pattern.

- [ ] **Step 1: Extend the `workspace` fixture with a pptx**

Open `tests/test_organize_pipeline.py`. In the `workspace` fixture (around lines 17-43), immediately **after** the xlsx block ending at `wb.close()` (~line 41), add:

```python

    # Minimal .pptx (one titled slide).
    from pptx import Presentation
    pptx_path = root / "deck.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Pipeline Demo"
    prs.save(str(pptx_path))
```

(Keep the `return root` line as the fixture's last statement.)

- [ ] **Step 2: Extend `test_full_pipeline` to assert the pptx is moved**

In `class TestOrganize`, method `test_full_pipeline` (around line 90), after the existing `assert (workspace / "Texts" / "data.xlsx").exists()` line (~107), add:

```python
        assert (workspace / "Texts" / "deck.pptx").exists()
```

- [ ] **Step 3: Extend `test_preview_returns_plan_without_executing` to assert pptx survives preview**

In the same class, method `test_preview_returns_plan_without_executing` (around line 111), after the existing `assert (workspace / "data.xlsx").exists()` line (~124), add:

```python
        assert (workspace / "deck.pptx").exists()
```

- [ ] **Step 4: Run the pipeline test**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -v`
Expected: PASS — `test_full_pipeline` and `test_preview_returns_plan_without_executing` both pass; the pptx flows through the pipeline alongside docx/xlsx.

- [ ] **Step 4: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_organize_pipeline.py
git commit -m "organize(integration): pptx flows through reader and pipeline"
```

---

## Done condition

- `pyproject.toml` declares `python-pptx>=1.0.2` in required deps.
- `fda/organize/_extractors.py` defines three new constants and `_extract_pptx`; `EXTRACTORS` registers `.pptx`.
- All test classes added in Tasks 2–8 are green.
- Full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`.
- Eight commits on `dev_branch`, each tied to one task.

After completion, the Obsidian "Future Plan - Structural Sections Across Formats" note should be updated by the user to reflect `.pptx` shipped (parallel to the docx/xlsx update on 2026-05-07). That update is **not** an implementation step — it's the user's record-keeping after merge.
