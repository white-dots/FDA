# Organize: `.hwpx` + `.hwp` Extractors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two new extractors to `fda/organize/_extractors.py` so Korean office documents flow through the organize pipeline: `_extract_hwpx` (XML in zip; OWPML / TTAK.OT-10.0203; stdlib `zipfile` + `defusedxml`) and `_extract_hwp` (binary OLE; HWP 5.x; hard dep `pyhwp`). Both produce plaintext + `sections` populated by `extract_sections_from_text` so Korean A's regexes (bracket / colon / bullet) pick up Korean headings automatically.

**Architecture:** Faithful extension of the docx/xlsx/pptx/csv format-onboarding pattern — one extractor function per format in `_extractors.py`, two new entries in the `EXTRACTORS` dict, no new modules, no protocol abstraction, no rewrite of existing extractors. Reader keeps ownership of the 64 KiB `text` contract cap (`READER_TEXT_CAP_BYTES`); the new extractors do not cap `text` themselves. Five new memory-bound numeric constants (`_HWPX_SECTION_FILES_MAX`, `_HWPX_SECTION_BYTES_MAX`, `_HWPX_TOTAL_BYTES_MAX`, `_HWPX_COMPRESSION_RATIO_MAX`, `_HWP_BYTES_MAX`).

**Tech Stack:** Python 3.12+, two new runtime deps (`pyhwp` for `.hwp`; `defusedxml` for safe XML parsing). Plus stdlib `zipfile`, `io`, `re`. pytest. Spec at `docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md`.

**Pretest:** every task ends with running the full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short` (CLAUDE.md command). If that path is unavailable on the executing host, fall back to whichever Python 3.12 the test fixture conftest works under.

**License posture (recap from spec):** `pyhwp` is AGPLv3+. The spec resolved this — accepted for internal / Lion Chemtech scope (no PyPI publication, no public SaaS, no LICENSE file). If any of the re-evaluation triggers fire (PyPI publication, public SaaS, commercial SDK), revisit before this work ships. No code changes around licensing in this plan.

**Decision gate at Task 0:** if `pip install pyhwp` fails on Python 3.12 / 3.13, the HWP half of this plan (Tasks 9–12) **halts**. The HWPX half (Tasks 1–8 plus Task 13's HWPX-only pipeline integration) ships independently. The spec's Critical Clarification spells this out — there is no working subprocess fallback, since the `hwp5txt` console script is installed by the same package.

**Test Python path:** all `pytest` invocations below assume `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest`. Substitute the equivalent on your host if needed.

**stdlib + dep API quick reference:**
- `zipfile.ZipFile(path, "r")` — context manager. `BadZipFile` raised on non-zip input.
- `zf.namelist()` — list of member names. `zf.getinfo(name)` returns `ZipInfo`.
- `ZipInfo.file_size` — uncompressed size; `ZipInfo.compress_size` — compressed; `ZipInfo.flag_bits` — encryption flag is `0x1`.
- `zf.read(name)` — full uncompressed bytes (DO NOT call before checking `file_size`).
- `defusedxml.ElementTree.fromstring(bytes_or_str)` — drop-in for `xml.etree.ElementTree.fromstring`; raises `xml.etree.ElementTree.ParseError` on malformed XML and `defusedxml.common.DefusedXmlException` (subclasses: `DTDForbidden`, `EntitiesForbidden`, `ExternalReferenceForbidden`) on intentionally-malicious payloads.
- `Element.iter()` — depth-first iteration over self and descendants. `tag` is `"{namespace}localname"` for namespaced elements.
- `pyhwp` (verified surface, against upstream `pyhwp/hwp5/hwp5txt.py`):
  - `from hwp5.xmlmodel import Hwp5File` — text-transform-capable file wrapper. Wrap in `contextlib.closing` (pyhwp's own `main()` does this).
  - `from hwp5.hwp5txt import TextTransform` — class. **Note:** `transform_hwp5_to_text` is a `@property` on `TextTransform` (not a module-level function); the property returns a callable. Usage: `t = TextTransform(); t.transform_hwp5_to_text(hwp, buf)`.
  - `Hwp5File(str(path))` — accepts a string path; rejects `pathlib.Path` (coerce explicitly).
  - `hwp.fileheader.flags.password` — boolean (no exception on password-protected docs).
  - `hwp.fileheader.flags.distributable` — boolean (no exception on distributed docs; pyhwp routes them through ViewText).

---

## Task 0: Spike — verify pyhwp + defusedxml install on Python 3.12; harvest 2–3 public `.hwpx` (decision gate)

**No production code in this task.** Outputs: pinned package versions, confirmed API surface against installed code, harvested public fixtures committed under `tests/fixtures/hwp/` (or recorded gap), and a go/no-go decision for the HWP half of the plan.

**Files:**
- Create: `tests/fixtures/hwp/.gitkeep` (directory placeholder; harvested files land here if available)
- Append: `docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md` (record spike findings inline — pinned versions and any gap)

- [ ] **Step 1: Install `defusedxml` and verify**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pip install defusedxml
/Users/john/.pyenv/versions/3.12.8/bin/python -c "import defusedxml.ElementTree as DET; print(DET.__name__); from defusedxml.common import DefusedXmlException, DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden; print('exceptions ok')"
```
Expected: prints `defusedxml.ElementTree` and `exceptions ok`. If the import fails, stop and surface the error — `defusedxml` is small, well-maintained, and pure Python; failure here is unexpected and indicates a broken environment.

- [ ] **Step 2: Install `pyhwp` and verify on Python 3.12**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pip install pyhwp
```
Expected: install succeeds. **If install fails on Python 3.12 / 3.13,** record the exact pip error, abort the HWP half of this plan (Tasks 9–12), and continue with HWPX-only (the rest of the tasks). pyhwp's PyPI classifiers stop at Python 3.8, so this is a real risk — see spec "Dependency strategy".

- [ ] **Step 3: Verify pyhwp's API surface against the installed package**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -c "
from hwp5.xmlmodel import Hwp5File
from hwp5.hwp5txt import TextTransform
t = TextTransform()
fn = t.transform_hwp5_to_text   # @property returns a callable
print('imports ok:', Hwp5File, TextTransform, fn)
"
```
Expected: prints `imports ok: <class 'hwp5.xmlmodel.Hwp5File'> <class 'hwp5.hwp5txt.TextTransform'> <function ...>`.

If any import fails, the plan's pinned API is wrong for the installed pyhwp version. Record the actual symbol locations (search via `python -c "import hwp5; help(hwp5)"` and the package's source) and update the plan's Step 4 / Task 9 / Task 10 accordingly before continuing.

- [ ] **Step 4: Verify pyhwp opens one of the user's hand-made samples and produces Korean text**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -c "
import io
from contextlib import closing
from pathlib import Path
from hwp5.xmlmodel import Hwp5File
from hwp5.hwp5txt import TextTransform
p = Path('/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples').glob('*.hwp')
sample = next(iter(p))
with closing(Hwp5File(str(sample))) as hwp:
    print('header.flags.password =', hwp.fileheader.flags.password)
    print('header.flags.distributable =', hwp.fileheader.flags.distributable)
    buf = io.BytesIO()
    TextTransform().transform_hwp5_to_text(hwp, buf)
    text = buf.getvalue().decode('utf-8', errors='replace')
print('text length:', len(text))
print('first 200 chars:', repr(text[:200]))
"
```
Expected: prints `header.flags.password = False`, `header.flags.distributable = False`, a non-zero text length, and the `first 200 chars` should contain at least one Hangul syllable (U+AC00..U+D7A3) OR Hanyang Private-Use Area character (U+E000..U+F8FF). Either form passes — the spec accepts PUA as a v1 known limitation.

If the user's sample is password-protected or distributable, try the next sample. If ALL 10 samples land on `password=True` or `distributable=True`, record that — the implementation will still be correct, but the real-corpus validation in Task 12 will need different test material.

- [ ] **Step 5: Pin package versions in the local environment**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -m pip show pyhwp defusedxml | grep -E '^(Name|Version):'
```
Record the pinned versions. They go into `pyproject.toml` as minimum versions in Task 2.

- [ ] **Step 6: Create the fixtures directory placeholder**

```bash
mkdir -p /Users/hogyeongkim/Desktop/Projects/FDA/FDA/tests/fixtures/hwp
touch /Users/hogyeongkim/Desktop/Projects/FDA/FDA/tests/fixtures/hwp/.gitkeep
```

- [ ] **Step 7: Harvest 2–3 public `.hwpx` and `.hwp` documents (best-effort)**

Search the Korea Data Portal (`data.go.kr`) and other public-domain Korean government sources for small (< 50 KiB each) `.hwpx` AND `.hwp` files. Confirm public-domain licensing, then commit them under `tests/fixtures/hwp/`.

The spec called for committed `.hwp` fixtures so the HWP test surface does not silently disappear on hosts without the user's private corpus. If a tiny non-confidential `.hwp` cannot be harvested publicly, an alternative is to commit **one** of the user's hand-made samples (with the user's permission) — they are non-confidential by construction (per the spec, the user created them as test data).

If you cannot find suitably-licensed small samples within a reasonable time-box (~30 minutes) AND cannot get permission to commit a user sample, **document the gap** in the "Spike findings" section (Step 8) and move on — the spec is still shippable. The harvested-fixture spot-check in Task 8 becomes a `pytest.skip` when no harvested `.hwpx` is present, and the HWP tests in Tasks 9–11 already `pytest.skip` when `_HWP_SAMPLES_DIR` is missing.

- [ ] **Step 8: Record spike findings in the spec**

Edit `docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md`. Append a new section at the end (after "Out-of-scope follow-ups (post-ship)"):

```markdown
## Spike findings (2026-05-09)

- `pyhwp` installed cleanly on Python 3.12: <yes/no>. Pinned version: <X.Y.Z>.
- `defusedxml` installed cleanly: <yes/no>. Pinned version: <X.Y.Z>.
- API surface verified: `Hwp5File` from `hwp5.xmlmodel`; `TextTransform` from `hwp5.hwp5txt` (its `.transform_hwp5_to_text` is a `@property` returning a callable).
- One user sample (`<filename>`) round-trips: text length <N>, contains <Hangul / PUA / both>.
- Harvested `.hwpx` fixtures committed under `tests/fixtures/hwp/`: <list, OR "none — gap recorded; harvested-fixture spot-check in Task 8 skipped">.
- Harvested or user-permitted `.hwp` fixtures committed under `tests/fixtures/hwp/`: <list, OR "none — gap recorded; HWP tests skip without user's private corpus">.
- HWP half decision: <GO / NO-GO; if NO-GO, Tasks 9–12 are skipped>.
```

- [ ] **Step 9: Commit**

```bash
git add tests/fixtures/hwp/.gitkeep tests/fixtures/hwp/ docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md
git commit -m "organize(spike): pin pyhwp+defusedxml versions; harvest public hwpx (or record gap)"
```

(If no harvested files were committed, only the `.gitkeep` and spec changes go in.)

---

## Task 1: Reserve five `_HWPX_*` / `_HWP_*` constants

**Files:**
- Modify: `fda/organize/_extractors.py` (add five new constants after the csv block at line 78)
- Modify: `tests/test_organize_constraints.py` (add five entries to `CONSTS`)

- [ ] **Step 1: Add five entries to the `CONSTS` dict**

Edit `tests/test_organize_constraints.py`. Inside `class TestEachConstantHasOneHome:`, append to the `CONSTS` dict (just below the `_CSV_KOREAN_LABEL_MIN_CHARS` entry at line 92):

```python
        "_HWPX_SECTION_FILES_MAX": ("_extractors.py", "100"),
        "_HWPX_SECTION_BYTES_MAX": ("_extractors.py", "4 * 1024 * 1024"),
        "_HWPX_TOTAL_BYTES_MAX": ("_extractors.py", "8 * 1024 * 1024"),
        "_HWPX_COMPRESSION_RATIO_MAX": ("_extractors.py", "100"),
        "_HWP_BYTES_MAX": ("_extractors.py", "16 * 1024 * 1024"),
```

- [ ] **Step 2: Run the constants test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py::TestEachConstantHasOneHome::test_constant_defined_in_owning_module -v`
Expected: FAIL — `_HWPX_SECTION_FILES_MAX missing from _extractors.py`.

- [ ] **Step 3: Add the five constants to `_extractors.py`**

Edit `fda/organize/_extractors.py`. Just below the existing `_CSV_KOREAN_LABEL_MIN_CHARS = 2` line (~line 77), add:

```python

# .hwpx (XML in zip; OWPML / TTAK.OT-10.0203). Memory-bound only — Reader
# owns the 64 KiB contract cap via READER_TEXT_CAP_BYTES.
_HWPX_SECTION_FILES_MAX = 100               # archive-entry count cap
_HWPX_SECTION_BYTES_MAX = 4 * 1024 * 1024   # per-section-file uncompressed size cap (checked via ZipInfo.file_size BEFORE read)
_HWPX_TOTAL_BYTES_MAX = 8 * 1024 * 1024     # cumulative uncompressed cap across all sections (checked BEFORE next read)
_HWPX_COMPRESSION_RATIO_MAX = 100           # uncompressed/compressed ratio cap per entry (zip-bomb defense)

# .hwp (binary OLE / HWP 5.x via pyhwp).
_HWP_BYTES_MAX = 16 * 1024 * 1024    # input file size cap (checked via path.stat().st_size BEFORE open)
```

- [ ] **Step 4: Run the constants test to verify it passes**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_constraints.py -v`
Expected: PASS (all entries in `CONSTS` find their owning module).

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_constraints.py
git commit -m "organize(extractors): reserve hwpx + hwp constants"
```

---

## Task 2: Add `pyhwp` and `defusedxml` to `pyproject.toml`

**Files:**
- Modify: `pyproject.toml` (add two entries to `dependencies` under `[project]`)

- [ ] **Step 1: Add the two new dependencies**

Edit `pyproject.toml`. Inside the `[project]` block, append two entries to the `dependencies` list — placement immediately after `"python-pptx>=1.0.2",` (existing line):

```toml
dependencies = [
    "anthropic>=0.45.0",
    "pandas>=1.5.0",
    "openpyxl>=3.0.9",
    "python-docx>=1.0.0",
    "python-pptx>=1.0.2",
    "defusedxml>=0.7.1",
    "pyhwp>=0.1b15",
    "msal>=1.20.0",
    "requests>=2.28.0",
    "pyyaml>=6.0",
]
```

(Substitute the actual minimum versions from Task 0 Step 5 if they differ. `pyhwp` PyPI floor is `0.1b15` at time of writing.)

**If Task 0's HWP decision was NO-GO,** drop only the `pyhwp` line; keep the `defusedxml` line.

- [ ] **Step 2: Verify the file parses**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python -c "import tomllib; tomllib.loads(open('/Users/hogyeongkim/Desktop/Projects/FDA/FDA/pyproject.toml').read()); print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green (pyproject change does not affect runtime tests).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "organize(deps): add pyhwp and defusedxml as runtime dependencies"
```

---

## Task 3: HWPX synthetic-fixture helper + happy path + register

Lock in the simplest end-to-end case: a single-section `.hwpx` archive with one paragraph containing a Korean bracket header. Define a reusable `_build_hwpx` test helper, write `_extract_hwpx`, register it in `EXTRACTORS`.

**Files:**
- Modify: `fda/organize/_extractors.py` (add `_extract_hwpx`, register `.hwpx`)
- Modify: `tests/test_organize_extractors.py` (new `_build_hwpx` helper, new `TestHwpxText` class)

- [ ] **Step 1: Write the failing test (synthetic fixture helper + happy path)**

Append to `tests/test_organize_extractors.py`:

```python
# ---------------------------------------------------------------------------
# .hwpx — XML in zip (OWPML / TTAK.OT-10.0203)
# ---------------------------------------------------------------------------

# Namespace URIs observed across HWPX revisions. Synthetic fixtures cover
# all three families to exercise local-name matching.
_HWPX_NS_2011 = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_HWPX_NS_2016 = "http://www.hancom.co.kr/hwpml/2016/paragraph"
_HWPX_NS_2021 = "http://www.owpml.org/owpml/2021/paragraph"


def _build_hwpx(
    tmp_path,
    name: str = "doc.hwpx",
    sections: list[list[list[str]]] | None = None,
    namespace: str = _HWPX_NS_2011,
    mimetype: bytes = b"application/hwp+zip",
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """Build a minimal valid .hwpx archive on disk and return its path.

    `sections` is a list of section files; each section file is a list of
    paragraphs; each paragraph is a list of run texts (the <hp:t> contents).
    Default: one section, one paragraph, one run "[발주서]".

    Note: there is no `encrypted=True` parameter — Python's zipfile.writestr
    resets ZipInfo.flag_bits in _open_to_write, so setting flag_bits before
    writestr does not produce an encrypted entry. The encrypted-flag test
    monkeypatches ZipFile.infolist instead.
    """
    import zipfile
    from xml.etree import ElementTree as ET

    if sections is None:
        sections = [[["[발주서]"]]]

    p = tmp_path / name
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # mimetype is conventionally the first member, stored uncompressed.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, mimetype)

        for idx, section in enumerate(sections):
            ET.register_namespace("hp", namespace)
            root = ET.Element(f"{{{namespace}}}sec")
            for para in section:
                p_el = ET.SubElement(root, f"{{{namespace}}}p")
                for run_text in para:
                    run_el = ET.SubElement(p_el, f"{{{namespace}}}run")
                    t_el = ET.SubElement(run_el, f"{{{namespace}}}t")
                    t_el.text = run_text
            xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            zf.writestr(f"Contents/section{idx}.xml", xml_bytes)

        for member_name, member_data in (extra_members or {}).items():
            zf.writestr(member_name, member_data)
    return p


class TestHwpxText:
    def test_single_section_single_paragraph_yields_run_text(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[[["Hello world"]]])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "Hello world" in r.text

    def test_korean_bracket_header_picked_up_as_section(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[[["[발주서]"]]])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.sections == ("발주서",)

    def test_multiple_runs_in_one_paragraph_join_with_empty_string(self, tmp_path):
        """Runs join with "" — HWPX runs are token-level; spaces are explicit."""
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[[["회사 ", "정보:"]]])
        r = _extractors.extract(p)
        assert r.status == "ok"
        # Two adjacent runs concatenate into "회사 정보:" — colon-header regex picks it up.
        assert "회사 정보" in r.sections

    def test_empty_archive_zero_section_files_returns_ok(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, sections=[])
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == ""
        assert r.sections == ()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpxText -v`
Expected: FAIL — `extract()` returns `status="no_extractor"` because `.hwpx` is not registered.

- [ ] **Step 3: Add stdlib + defusedxml imports to `_extractors.py`**

Edit `fda/organize/_extractors.py`. The existing imports (lines 17-25) include `csv`, `io`, `itertools`, `re`, `shutil`, `subprocess`, `time`, `pathlib.Path`, `Callable`. Add `zipfile` to the alphabetic group:

```python
import csv
import io
import itertools
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Callable
```

- [ ] **Step 4: Implement `_extract_hwpx`**

Edit `fda/organize/_extractors.py`. Add the new function below `_extract_csv` (after the `_label_min_chars` helper, around line 545):

```python
def _hwpx_localname(tag: str) -> str:
    """Return the local-name of an XML tag.

    Tags from `xml.etree.ElementTree` are formatted as "{namespace}localname"
    when namespaced, or "localname" plain when not. The rsplit handles both.
    """
    return tag.rsplit("}", 1)[-1]


_HWPX_SECTION_FILE_RE = re.compile(r"^Contents/section(\d+)\.xml$")


def _extract_hwpx(path: Path) -> ExtractionResult:
    """Extract text + sections from a .hwpx file.

    Format: ZIP archive (OWPML / TTAK.OT-10.0203). Body text lives in
    Contents/section*.xml. Each is XML with paragraphs (<hp:p>) holding
    runs (<hp:run>) holding text (<hp:t>). Namespace URIs vary across
    revisions (2011/2016/2021); we match by local-name to handle all.

    Sections: produced by extract_sections_from_text() over the joined
    plaintext — Korean A's regexes (bracket / colon / bullet) pick up
    Korean headings automatically.

    Memory-bound caps:
    - _HWPX_SECTION_FILES_MAX: archive entry count.
    - _HWPX_SECTION_BYTES_MAX: per-entry uncompressed size (checked via
      ZipInfo.file_size BEFORE zf.read).
    - _HWPX_TOTAL_BYTES_MAX: cumulative uncompressed size across sections.
    - _HWPX_COMPRESSION_RATIO_MAX: per-entry zip-bomb defense.
    """
    # defusedxml is mandatory in v1; hard import (no try/except — pyproject
    # makes it a runtime dep).
    import defusedxml.ElementTree as DET
    from defusedxml.common import DefusedXmlException
    from xml.etree.ElementTree import ParseError as _XmlParseError

    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as e:
        return ExtractionResult(text=None, status="failed", note=str(e))

    try:
        # mimetype member required, exact match.
        try:
            mt = zf.read("mimetype").strip()
        except KeyError:
            return ExtractionResult(
                text=None, status="failed", note="not an OWPML hwpx"
            )
        if mt != b"application/hwp+zip":
            return ExtractionResult(
                text=None, status="failed", note="not an OWPML hwpx"
            )

        # Reject any encrypted entries.
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                return ExtractionResult(
                    text=None, status="failed", note="encrypted hwpx"
                )

        # Locate section files; sort numerically by suffix.
        sections_by_idx: list[tuple[int, zipfile.ZipInfo]] = []
        for info in zf.infolist():
            m = _HWPX_SECTION_FILE_RE.match(info.filename)
            if m:
                sections_by_idx.append((int(m.group(1)), info))
        sections_by_idx.sort(key=lambda pair: pair[0])

        if not sections_by_idx:
            text = ""
            return ExtractionResult(
                text=text,
                status="ok",
                sections=extract_sections_from_text(text),
            )

        # Truncate at file-count cap (do not abort).
        sections_by_idx = sections_by_idx[:_HWPX_SECTION_FILES_MAX]

        per_section_texts: list[str] = []
        cumulative = 0
        for _idx, info in sections_by_idx:
            # Compression-ratio guard FIRST: any one entry exceeding the
            # ratio is treated as malicious — abort the whole archive.
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > _HWPX_COMPRESSION_RATIO_MAX:
                return ExtractionResult(
                    text=None,
                    status="failed",
                    note="compressed hwpx zip bomb",
                )
            # Per-section uncompressed-size cap (checked BEFORE read).
            if info.file_size > _HWPX_SECTION_BYTES_MAX:
                per_section_texts.append("")
                continue
            # Cumulative cap (checked BEFORE next read).
            if cumulative + info.file_size > _HWPX_TOTAL_BYTES_MAX:
                break
            cumulative += info.file_size

            data = zf.read(info.filename)
            try:
                root = DET.fromstring(data)
            except DefusedXmlException as e:
                # Intentionally-malicious payload (DTD / billion laughs /
                # external reference) — abort the whole extraction.
                return ExtractionResult(
                    text=None, status="failed", note=str(e)
                )
            except _XmlParseError:
                # Benign mid-section corruption — skip this section, keep going.
                per_section_texts.append("")
                continue

            # Walk paragraphs, then runs within each paragraph. Insert "\n"
            # between paragraphs so line-oriented regexes in _sections.py
            # can pattern-match.
            paragraph_lines: list[str] = []
            for el in root.iter():
                if _hwpx_localname(el.tag) != "p":
                    continue
                run_parts: list[str] = []
                for sub in el.iter():
                    if _hwpx_localname(sub.tag) == "t" and sub.text:
                        run_parts.append(sub.text)
                paragraph_lines.append("".join(run_parts))
            per_section_texts.append("\n".join(paragraph_lines))
    finally:
        zf.close()

    text = "\n".join(per_section_texts)
    return ExtractionResult(
        text=text,
        status="ok",
        sections=extract_sections_from_text(text),
    )
```

- [ ] **Step 5: Register `.hwpx` in `EXTRACTORS`**

Edit `fda/organize/_extractors.py`. In the `EXTRACTORS` dict (lines 548-559), add `".hwpx": _extract_hwpx` between `".pptx"` and the closing brace:

```python
EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _extract_csv,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".hwpx": _extract_hwpx,
}
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpxText -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): _extract_hwpx happy path + register"
```

---

## Task 4: HWPX namespace coverage (2011/2016/2021) + paragraph separators

Lock in that the local-name-matching strategy works across all three observed namespace families, and that paragraph separators are inserted between `<hp:p>` elements (so multi-paragraph documents flow as multi-line text into `_sections.py`).

**Note on TDD discipline:** Task 3's implementation already does both — it walks by local-name and joins paragraphs with `"\n"`. This task locks the contract via regression tests. Tests are expected to PASS the first time. If any fails, Task 3 deviated from spec — fix the implementation, do not loosen the test.

**Files:**
- Modify: `tests/test_organize_extractors.py` (extend `TestHwpxText` with namespace + multi-paragraph tests)

- [ ] **Step 1: Append namespace + paragraph-separator tests**

Append inside `class TestHwpxText:` (just before the next class):

```python
    @pytest.mark.parametrize("namespace", [_HWPX_NS_2011, _HWPX_NS_2016, _HWPX_NS_2021])
    def test_namespace_variants_all_yield_text(self, tmp_path, namespace):
        from fda.organize import _extractors

        p = _build_hwpx(
            tmp_path,
            name=f"ns_{hash(namespace) % 1000}.hwpx",
            sections=[[["[제목]"]]],
            namespace=namespace,
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "제목" in r.sections, f"namespace {namespace} did not match local-name"

    def test_multi_paragraph_separated_by_newlines(self, tmp_path):
        """Each <hp:p> ends with a "\\n" so line-oriented section regexes match."""
        from fda.organize import _extractors

        p = _build_hwpx(
            tmp_path,
            sections=[[
                ["[발주서]"],
                ["회사 정보:"],
                ["■ 주의사항"],
            ]],
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # All three Korean banner styles parse — bracket, colon, bullet.
        assert r.sections == ("발주서", "회사 정보", "주의사항")

    def test_multi_section_files_separated_by_newlines(self, tmp_path):
        """Each section file's text ends with a "\\n" before the next section
        joins, so paragraph breaks across HWPX section boundaries do not get
        glued together."""
        from fda.organize import _extractors

        p = _build_hwpx(
            tmp_path,
            sections=[
                [["[발주서]"]],   # section0.xml
                [["회사 정보:"]],  # section1.xml
            ],
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert "발주서" in r.sections
        assert "회사 정보" in r.sections
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpxText -v`
Expected: PASS (3 namespace parametrized + 2 paragraph tests added; all 9 in the class pass).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): hwpx namespace coverage (2011/2016/2021) + paragraph separators"
```

---

## Task 5: HWPX failure modes — not-zip, missing/wrong mimetype, encrypted entries

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestHwpxFailure` class)

- [ ] **Step 1: Append failure-mode tests**

```python
class TestHwpxFailure:
    def test_not_a_zip_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "fake.hwpx"
        p.write_bytes(b"this is not a zip file at all")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_zip_without_mimetype_member_returns_failed(self, tmp_path):
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "no_mt.hwpx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("Contents/section0.xml", b"<root/>")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "OWPML" in r.note or "hwpx" in r.note

    def test_wrong_mimetype_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, mimetype=b"application/zip")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "OWPML" in r.note or "hwpx" in r.note

    def test_mimetype_with_extra_suffix_rejected_exact_match(self, tmp_path):
        """Regression: prefix-only matches like `application/hwp+zip-bad` must
        be rejected. The implementation compares exactly after .strip()."""
        from fda.organize import _extractors

        p = _build_hwpx(tmp_path, mimetype=b"application/hwp+zip-bad")
        r = _extractors.extract(p)
        assert r.status == "failed"

    def test_encrypted_entry_returns_failed(self, tmp_path, monkeypatch):
        """Python's zipfile.ZipFile.writestr resets ZipInfo.flag_bits during
        write, so a real encrypted-flag entry cannot be produced via the
        helper. We instead monkeypatch ZipFile.infolist so it returns a
        ZipInfo with flag_bits 0x1 set, simulating the runtime check."""
        from fda.organize import _extractors
        import zipfile

        p = _build_hwpx(tmp_path)

        original_infolist = zipfile.ZipFile.infolist

        def faked_infolist(self):
            infos = original_infolist(self)
            for info in infos:
                if info.filename.startswith("Contents/section"):
                    info.flag_bits |= 0x1
                    break
            return infos

        monkeypatch.setattr(zipfile.ZipFile, "infolist", faked_infolist)
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "encrypted" in r.note
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpxFailure -v`
Expected: PASS (5 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): hwpx failure modes (not-zip, mimetype, encryption)"
```

---

## Task 6: HWPX zip-bomb caps — file-count, per-section, cumulative, compression-ratio

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestHwpxCaps` class)

- [ ] **Step 1: Append cap-enforcement tests**

**Test strategy:** size-based caps require fixtures that are large enough to trip them but compress poorly enough not to trip the ratio guard first. The cleanest path is `monkeypatch.setattr(_extractors, "_HWPX_*_MAX", smaller_value)` per test, which lets us build tiny fixtures.

```python
class TestHwpxCaps:
    def test_section_file_count_truncated_at_max(self, tmp_path, monkeypatch):
        """When more than _HWPX_SECTION_FILES_MAX section files exist, the
        extractor truncates at the cap rather than aborting."""
        from fda.organize import _extractors

        # Lower the file-count cap so we can build a small fixture.
        monkeypatch.setattr(_extractors, "_HWPX_SECTION_FILES_MAX", 5)
        # Build cap + 3 section files; only the first 5 should be walked.
        sections = [[[f"section_{i}_marker"]] for i in range(8)]
        p = _build_hwpx(tmp_path, sections=sections)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # First 5 sections present; sections past the cap absent.
        assert "section_0_marker" in r.text
        assert "section_4_marker" in r.text
        assert "section_5_marker" not in r.text
        assert "section_7_marker" not in r.text

    def test_oversized_section_skipped_not_aborted(self, tmp_path, monkeypatch):
        """A section whose ZipInfo.file_size exceeds _HWPX_SECTION_BYTES_MAX
        is skipped (text=""); other sections still produce text."""
        from fda.organize import _extractors

        # Lower the per-section cap so a 4 KiB body trips it; raise the
        # ratio cap so the highly-compressible body doesn't trip the bomb
        # guard first.
        monkeypatch.setattr(_extractors, "_HWPX_SECTION_BYTES_MAX", 1024)
        monkeypatch.setattr(_extractors, "_HWPX_COMPRESSION_RATIO_MAX", 100_000)

        big_body = "x" * 8 * 1024     # ~8 KiB run text > 1 KiB section cap
        p = _build_hwpx(
            tmp_path,
            sections=[
                [[big_body]],         # section0: oversized → skipped
                [["[발주서]"]],       # section1: normal → walked
            ],
        )
        r = _extractors.extract(p)
        assert r.status == "ok"
        # section0 skipped — its big_body not present.
        assert "x" * 100 not in r.text
        # section1 walked.
        assert r.sections == ("발주서",)

    def test_cumulative_cap_stops_walk(self, tmp_path, monkeypatch):
        """When cumulative file_size exceeds _HWPX_TOTAL_BYTES_MAX before the
        next read, the walk stops with what we have."""
        from fda.organize import _extractors

        # Lower the cumulative cap so a fixture of a few KiB sections trips it.
        # Raise the ratio cap so highly-compressible bodies don't trip the bomb.
        monkeypatch.setattr(_extractors, "_HWPX_TOTAL_BYTES_MAX", 12 * 1024)
        monkeypatch.setattr(_extractors, "_HWPX_SECTION_BYTES_MAX", 8 * 1024)
        monkeypatch.setattr(_extractors, "_HWPX_COMPRESSION_RATIO_MAX", 100_000)

        body = "x" * 5 * 1024  # ~5 KiB body per section
        # Markers are arbitrary ASCII strings checked in r.text — the Korean
        # section regexes are NOT exercised here, this test asserts walk
        # truncation only.
        sections = [
            [["s0_marker_text", body]],
            [["s1_marker_text", body]],
            [["s2_marker_text", body]],   # cumulative > 12 KiB before this read
            [["final_marker_text"]],
        ]
        p = _build_hwpx(tmp_path, sections=sections)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # First two sections walked (text present); third and final stopped.
        assert "s0_marker_text" in r.text
        assert "s1_marker_text" in r.text
        assert "s2_marker_text" not in r.text
        assert "final_marker_text" not in r.text

    def test_compression_ratio_bomb_aborts_archive(self, tmp_path):
        """A single entry whose uncompressed/compressed ratio exceeds
        _HWPX_COMPRESSION_RATIO_MAX is treated as malicious — abort whole."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "bomb.hwpx"
        # Write a "section0.xml" that's highly compressible (1 MiB of "A")
        # so DEFLATE produces a tiny compressed size — ratio in the thousands.
        big_xml = b"<?xml version='1.0'?><root>" + b"A" * (1024 * 1024) + b"</root>"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr("Contents/section0.xml", big_xml)
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "bomb" in r.note or "compress" in r.note
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpxCaps -v`
Expected: PASS (4 tests). If any fails, the Task 3 implementation deviated from a cap rule — fix the implementation.

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): hwpx zip-bomb caps (file-count, per-section, cumulative, ratio)"
```

---

## Task 7: HWPX XML safety — ParseError partial recovery vs DefusedXmlException abort

Lock in the two-class XML-error split: benign mid-section corruption skips that section and continues; intentionally-malicious DTD/entity payloads abort the whole extraction.

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestHwpxXmlSafety` class)

- [ ] **Step 1: Append XML-safety tests**

```python
class TestHwpxXmlSafety:
    def test_one_malformed_section_other_section_still_walked(self, tmp_path):
        """Mid-section ParseError → skip that section's text=""; other
        sections still produce text. Status stays "ok"."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "partial.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            # section0: malformed XML.
            zf.writestr("Contents/section0.xml", b"<root><unclosed>")
            # section1: well-formed, contains a Korean header.
            ns = _HWPX_NS_2011
            xml = (
                f'<?xml version="1.0"?>'
                f'<hp:sec xmlns:hp="{ns}">'
                f'<hp:p><hp:run><hp:t>[발주서]</hp:t></hp:run></hp:p>'
                f'</hp:sec>'
            ).encode("utf-8")
            zf.writestr("Contents/section1.xml", xml)
        r = _extractors.extract(p)
        assert r.status == "ok"
        # section0 skipped, section1 walked — its bracket header lands.
        assert r.sections == ("발주서",)

    def test_all_sections_malformed_returns_ok_empty(self, tmp_path):
        """When all section files are unparseable, text="" is acceptable;
        status stays "ok"."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "all_bad.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr("Contents/section0.xml", b"<<not xml>>")
            zf.writestr("Contents/section1.xml", b"</also bad/>")
        r = _extractors.extract(p)
        assert r.status == "ok"
        assert r.text == ""
        assert r.sections == ()

    def test_dtd_payload_aborts_whole_extraction(self, tmp_path):
        """A section file containing a DOCTYPE (DTD) is rejected by defusedxml's
        DTDForbidden — abort the whole extraction with status="failed"."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "dtd.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr(
                "Contents/section0.xml",
                b'<?xml version="1.0"?>'
                b'<!DOCTYPE foo [<!ENTITY x "hello">]>'
                b"<root>&x;</root>",
            )
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None

    def test_billion_laughs_payload_aborts_whole_extraction(self, tmp_path):
        """A section file with nested entity expansion (billion laughs) is
        rejected by defusedxml's EntitiesForbidden — abort."""
        from fda.organize import _extractors
        import zipfile

        p = tmp_path / "lol.hwpx"
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            zf.writestr(
                "Contents/section0.xml",
                b'<?xml version="1.0"?>'
                b'<!DOCTYPE lolz ['
                b'<!ENTITY lol "lol">'
                b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">'
                b']>'
                b"<lolz>&lol2;</lolz>",
            )
        r = _extractors.extract(p)
        assert r.status == "failed"
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpxXmlSafety -v`
Expected: PASS (4 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): hwpx XML safety (ParseError skip vs DefusedXmlException abort)"
```

---

## Task 8: HWPX reader integration + harvested-fixture spot check

`CatalogEntry.sections` flows end-to-end through Reader for `.hwpx`, in both happy-path and failure cases. Plus a harvested-fixture spot check (skipped if Task 0 recorded the harvest gap).

**Files:**
- Modify: `tests/test_organize_reader.py` (extend `TestSectionsPropagationDocxXlsx` with two `.hwpx` cases + one harvested spot-check)

The reader API matches the existing docx/xlsx/pptx/csv tests:
- `reader.read(workspace, backend=fake_backend, logger=logger)` returns a catalog
- `catalog.entries[i].path`, `.sections`, `.extract_status`, `.verbatim_head` all populated
- `e.path.endswith("foo.hwpx")` matcher
- `workspace`, `fake_backend`, `logger` fixtures already defined

- [ ] **Step 1: Add three `.hwpx` tests inside `class TestSectionsPropagationDocxXlsx`**

Open `tests/test_organize_reader.py`, find `class TestSectionsPropagationDocxXlsx`, and append these three methods immediately after the existing `test_csv_failed_extraction_yields_empty_sections_in_catalog` method (around line 832):

```python
    def test_hwpx_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        from fda.organize import reader
        import zipfile

        ns = "http://www.hancom.co.kr/hwpml/2011/paragraph"
        f = workspace / "doc.hwpx"
        with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"application/hwp+zip")
            xml = (
                f'<?xml version="1.0"?>'
                f'<hp:sec xmlns:hp="{ns}">'
                f'<hp:p><hp:run><hp:t>[발주서]</hp:t></hp:run></hp:p>'
                f'<hp:p><hp:run><hp:t>회사 정보:</hp:t></hp:run></hp:p>'
                f'</hp:sec>'
            ).encode("utf-8")
            zf.writestr("Contents/section0.xml", xml)

        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("doc.hwpx"))
        assert e.extract_status == "ok"
        assert e.sections == ("발주서", "회사 정보")

    def test_hwpx_failed_extraction_yields_empty_sections_in_catalog(
        self, workspace, fake_backend, logger
    ):
        """Parallel to docx/xlsx/pptx/csv: when the hwpx extractor fails (not
        a zip), the catalog entry's sections is preserved as ()."""
        from fda.organize import reader

        f = workspace / "broken.hwpx"
        f.write_bytes(b"not a zip file")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("broken.hwpx"))
        assert e.extract_status == "failed"
        assert e.sections == ()

    def test_hwpx_harvested_public_fixture_spot_check(
        self, workspace, fake_backend, logger, tmp_path
    ):
        """Spot-check against a real-world public-domain `.hwpx` (harvested in
        Task 0). Skips if the harvest gap was recorded — see spec spike findings."""
        from pathlib import Path
        import shutil
        from fda.organize import reader

        fixture_dir = Path(__file__).resolve().parent / "fixtures" / "hwp"
        harvested = sorted(fixture_dir.glob("*.hwpx"))
        if not harvested:
            pytest.skip("no harvested .hwpx fixtures (Task 0 gap)")

        # Copy the first harvested fixture into the workspace so reader sees it.
        sample = harvested[0]
        dest = workspace / sample.name
        shutil.copy2(sample, dest)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith(sample.name))
        # Real-world docs may or may not have headers _sections.py picks up;
        # the assertion is just that extraction succeeded.
        assert e.extract_status == "ok"
        # Sections is a tuple — empty tuple is fine, but the type should hold.
        assert isinstance(e.sections, tuple)
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx -k hwpx -v`
Expected: PASS (3 tests; spot-check may skip if no harvest happened).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_reader.py
git commit -m "organize(reader): hwpx sections preserved through reader (passthrough + failure + spot-check)"
```

---

## Task 9: HWP happy path against real user sample + register

**Skip this task and Tasks 10–12 if Task 0's HWP decision was NO-GO.**

Lock in the simplest end-to-end case for `.hwp`: a real user sample round-trips through `_extract_hwp` and produces non-empty Korean text. Register `.hwp` in `EXTRACTORS`.

**Files:**
- Modify: `fda/organize/_extractors.py` (add `_extract_hwp`, register `.hwp`)
- Modify: `tests/test_organize_extractors.py` (new `TestHwpText` class)

The user has 10 hand-made `.hwp` samples at `/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples/`. We do not commit them to the repo (private test data); tests reference them via that absolute path and `pytest.skip` if they're missing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_organize_extractors.py`:

```python
# ---------------------------------------------------------------------------
# .hwp — binary OLE compound document (HWP 5.x), via pyhwp
# ---------------------------------------------------------------------------

_HWP_SAMPLES_DIR = Path("/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples")


def _first_hwp_sample() -> Path | None:
    """Return the first .hwp sample that round-trips through the extractor
    successfully (status == "ok" with non-empty text). None if dir/samples
    missing or all samples fail (e.g., all password-protected).

    This guards against the first sorted sample being password-protected or
    distributable — those cases would fail _extract_hwp's flag checks and
    cause downstream tests to fail for the wrong reason.
    """
    if not _HWP_SAMPLES_DIR.is_dir():
        return None
    from fda.organize import _extractors  # local import to avoid cycle at module load
    for sample in sorted(_HWP_SAMPLES_DIR.glob("*.hwp")):
        r = _extractors.extract(sample)
        if r.status == "ok" and r.text:
            return sample
    return None


class TestHwpText:
    def test_real_user_sample_round_trips(self, tmp_path):
        """The user has 10 hand-made .hwp samples; verify pyhwp opens one and
        produces Korean text in EITHER form (precomposed Hangul or Hanyang PUA)."""
        from fda.organize import _extractors

        sample = _first_hwp_sample()
        if sample is None:
            pytest.skip(f"no .hwp samples at {_HWP_SAMPLES_DIR}")

        # Copy to tmp_path so the test does not mutate the user's corpus.
        import shutil
        dest = tmp_path / sample.name
        shutil.copy2(sample, dest)

        r = _extractors.extract(dest)
        assert r.status == "ok"
        assert r.text is not None
        # Accept either precomposed Hangul OR Hanyang PUA — pyhwp emission
        # for legacy Korean docs varies. PUA is documented v1 limitation.
        has_hangul = any("가" <= c <= "힣" for c in r.text)
        has_pua = any("" <= c <= "" for c in r.text)
        assert has_hangul or has_pua, (
            f"sample {sample.name} produced neither Hangul nor PUA characters"
        )


class TestHwpSections:
    """Deterministic regression test for sections-wiring in _extract_hwp.

    Patches TextTransform so its transform_hwp5_to_text writes known Korean
    plaintext containing bracket / colon / bullet headers. Verifies that
    _extract_hwp threads the buffer's bytes through extract_sections_from_text
    and surfaces the labels in ExtractionResult.sections.

    This is a committed regression guard — Task 12's per-sample real-corpus
    pass records empirical behavior but does not assert on specific labels
    (we don't know in advance which user samples have which headers).
    """

    def test_known_korean_text_flows_into_sections(self, tmp_path):
        from fda.organize import _extractors
        from unittest.mock import patch, MagicMock

        sample = _first_hwp_sample()
        if sample is None:
            pytest.skip(f"no .hwp samples at {_HWP_SAMPLES_DIR}")
        import shutil
        dest = tmp_path / sample.name
        shutil.copy2(sample, dest)

        known_korean = (
            "[발주서]\n"
            "회사 정보:\n"
            "■ 주의사항\n"
            "본문 내용...\n"
        )

        def fake_transform(hwp, buf):
            buf.write(known_korean.encode("utf-8"))

        # _extract_hwp does `TextTransform().transform_hwp5_to_text(hwp, buf)`.
        # Patch the class so any new instance's transform_hwp5_to_text is fake.
        fake_class = MagicMock()
        fake_class.return_value.transform_hwp5_to_text = fake_transform
        with patch("hwp5.hwp5txt.TextTransform", fake_class):
            r = _extractors.extract(dest)

        assert r.status == "ok"
        assert r.text == known_korean
        assert r.sections == ("발주서", "회사 정보", "주의사항")
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpText tests/test_organize_extractors.py::TestHwpSections -v`
Expected: FAIL — `extract()` returns `status="no_extractor"` because `.hwp` is not registered.

- [ ] **Step 3: Implement `_extract_hwp`**

Edit `fda/organize/_extractors.py`. Add the new function below `_extract_hwpx` (at the end of the function definitions, before the `EXTRACTORS` dict at line 548):

```python
def _extract_hwp(path: Path) -> ExtractionResult:
    """Extract text + sections from a .hwp file (HWP 5.x via pyhwp).

    pyhwp is a hard runtime dep; the ImportError branch is defense-in-depth
    against corrupt venv / partial install — it should be unreachable in
    practice.

    pyhwp does NOT raise on password-protected or distributable docs:
    passwords pass through with a warning (decryption unsupported);
    distributable docs route through ViewText. We inspect the FileHeader
    flags directly and fail those cases as v1 does not support them.

    Mojibake / Hanyang PUA is a known v1 limitation: pyhwp may emit PUA
    characters (U+E000..U+F8FF) for some legacy Korean text rather than
    precomposed Hangul. PUA passes through `text` cleanly but does not
    match _sections.py's contains_hangul() check, so PUA-heavy files
    produce sections=() even when visually they have headers.
    """
    from contextlib import closing

    try:
        from hwp5.xmlmodel import Hwp5File
        from hwp5.hwp5txt import TextTransform
    except ImportError as e:
        return ExtractionResult(text=None, status="tool_missing", note=str(e))

    if path.stat().st_size > _HWP_BYTES_MAX:
        return ExtractionResult(
            text=None, status="failed", note="oversized hwp"
        )

    # pyhwp's Hwp5File rejects pathlib.Path; coerce explicitly to str.
    # Wrap in contextlib.closing — pyhwp's own main() does the same to
    # release the underlying OLE/file handle on exit.
    try:
        hwp_file = Hwp5File(str(path))
    except Exception as e:  # noqa: BLE001 — malformed OLE, v3, etc.
        return ExtractionResult(text=None, status="failed", note=str(e))

    with closing(hwp_file) as hwp:
        header = hwp.fileheader
        if header.flags.password:
            return ExtractionResult(
                text=None, status="failed", note="password-protected hwp"
            )
        if header.flags.distributable:
            return ExtractionResult(
                text=None, status="failed", note="distributed hwp"
            )

        buf = io.BytesIO()
        try:
            # transform_hwp5_to_text is a @property on TextTransform that
            # returns a callable; invoke it with (hwp5file, dest_buffer).
            TextTransform().transform_hwp5_to_text(hwp, buf)
        except Exception as e:  # noqa: BLE001 — pyhwp may raise on truncated streams
            return ExtractionResult(text=None, status="failed", note=str(e))

    text = buf.getvalue().decode("utf-8", errors="replace")
    return ExtractionResult(
        text=text,
        status="ok",
        sections=extract_sections_from_text(text),
    )
```

- [ ] **Step 4: Register `.hwp` in `EXTRACTORS`**

Edit `fda/organize/_extractors.py`. In the `EXTRACTORS` dict (lines 548-560), add `".hwp": _extract_hwp` between `.hwpx` and the closing brace:

```python
EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _extract_csv,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".hwpx": _extract_hwpx,
    ".hwp": _extract_hwp,
}
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpText tests/test_organize_extractors.py::TestHwpSections -v`
Expected: PASS (2 tests; the sections test patches `hwp5.hwp5txt.TextTransform` so it does not depend on real Korean content in the user's samples).

- [ ] **Step 6: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add fda/organize/_extractors.py tests/test_organize_extractors.py
git commit -m "organize(extractors): _extract_hwp happy path + register"
```

---

## Task 10: HWP failure modes — oversized, non-OLE, password-flag, distributable-flag

**Files:**
- Modify: `tests/test_organize_extractors.py` (new `TestHwpFailure` class)

The password / distributable cases use `unittest.mock.patch` to flip the flag — fabricating a real password-protected or distributed `.hwp` from scratch is not feasible.

- [ ] **Step 1: Append failure-mode tests**

```python
class TestHwpFailure:
    def test_non_ole_input_returns_failed(self, tmp_path):
        from fda.organize import _extractors

        p = tmp_path / "fake.hwp"
        p.write_bytes(b"this is not an OLE compound document")
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert r.text is None
        assert r.sections == ()

    def test_oversized_file_rejected_before_open(self, tmp_path):
        """A file larger than _HWP_BYTES_MAX is rejected via path.stat()
        before pyhwp is invoked — no parse work done."""
        from fda.organize import _extractors
        from fda.organize._extractors import _HWP_BYTES_MAX

        p = tmp_path / "huge.hwp"
        # Spec says check is path.stat().st_size > cap. Make file size > cap.
        p.write_bytes(b"\x00" * (_HWP_BYTES_MAX + 1))
        r = _extractors.extract(p)
        assert r.status == "failed"
        assert "oversized" in r.note

    def test_password_protected_returns_failed(self, tmp_path):
        """When header.flags.password is True, return failed — pyhwp does
        NOT raise on password-protected docs (decryption unsupported)."""
        from fda.organize import _extractors
        from unittest.mock import patch, MagicMock

        sample = _first_hwp_sample()
        if sample is None:
            pytest.skip(f"no .hwp samples at {_HWP_SAMPLES_DIR}")
        import shutil
        dest = tmp_path / sample.name
        shutil.copy2(sample, dest)

        # Patch Hwp5File so its instance reports password=True.
        fake_header = MagicMock()
        fake_header.flags.password = True
        fake_header.flags.distributable = False
        fake_hwp = MagicMock()
        fake_hwp.fileheader = fake_header

        with patch("hwp5.xmlmodel.Hwp5File", return_value=fake_hwp):
            r = _extractors.extract(dest)
        assert r.status == "failed"
        assert "password" in r.note

    def test_distributable_returns_failed(self, tmp_path):
        """When header.flags.distributable is True, return failed — v1 does
        not extract from ViewText-wrapped distributable docs."""
        from fda.organize import _extractors
        from unittest.mock import patch, MagicMock

        sample = _first_hwp_sample()
        if sample is None:
            pytest.skip(f"no .hwp samples at {_HWP_SAMPLES_DIR}")
        import shutil
        dest = tmp_path / sample.name
        shutil.copy2(sample, dest)

        fake_header = MagicMock()
        fake_header.flags.password = False
        fake_header.flags.distributable = True
        fake_hwp = MagicMock()
        fake_hwp.fileheader = fake_header

        with patch("hwp5.xmlmodel.Hwp5File", return_value=fake_hwp):
            r = _extractors.extract(dest)
        assert r.status == "failed"
        assert "distributed" in r.note
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_extractors.py::TestHwpFailure -v`
Expected: PASS (4 tests).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_extractors.py
git commit -m "organize(extractors): hwp failure modes (non-OLE, oversize, password flag, distributable flag)"
```

---

## Task 11: HWP reader integration

**Files:**
- Modify: `tests/test_organize_reader.py` (extend `TestSectionsPropagationDocxXlsx` with two `.hwp` cases)

- [ ] **Step 1: Add two `.hwp` tests inside `class TestSectionsPropagationDocxXlsx`**

Open `tests/test_organize_reader.py`, find `class TestSectionsPropagationDocxXlsx`, and append these two methods after the `test_hwpx_harvested_public_fixture_spot_check` method from Task 8:

```python
    def test_hwp_sections_flow_through_reader(
        self, workspace, fake_backend, logger
    ):
        """Real user sample round-trips through Reader; sections may be ()
        when the sample is PUA-heavy (documented v1 limitation)."""
        from pathlib import Path
        import shutil
        from fda.organize import reader

        samples_dir = Path(
            "/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples"
        )
        if not samples_dir.is_dir():
            pytest.skip(f"no .hwp samples at {samples_dir}")
        samples = sorted(samples_dir.glob("*.hwp"))
        if not samples:
            pytest.skip(f"no .hwp samples at {samples_dir}")

        sample = samples[0]
        dest = workspace / sample.name
        shutil.copy2(sample, dest)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith(sample.name))
        assert e.extract_status == "ok"
        assert isinstance(e.sections, tuple)

    def test_hwp_failed_extraction_yields_empty_sections_in_catalog(
        self, workspace, fake_backend, logger
    ):
        """Non-OLE input → status="failed", sections=()."""
        from fda.organize import reader

        f = workspace / "broken.hwp"
        f.write_bytes(b"not an OLE compound document")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = next(c for c in catalog.entries if c.path.endswith("broken.hwp"))
        assert e.extract_status == "failed"
        assert e.sections == ()
```

- [ ] **Step 2: Run the new tests**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_reader.py::TestSectionsPropagationDocxXlsx -k hwp -v`
Expected: PASS (the hwpx tests from Task 8 + the two new hwp tests; the hwp passthrough may skip if user samples aren't present on the host).

- [ ] **Step 3: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_organize_reader.py
git commit -m "organize(reader): hwp sections preserved through reader (passthrough + failure)"
```

---

## Task 12: HWP real-corpus validation pass — record results in spec

Run `_extract_hwp` against ALL 10 of the user's hand-made `.hwp` samples and document the results in the spec. This mirrors the Korean A spec's real-corpus validation pattern.

**Files:**
- Append: `docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md` (new "Real-corpus validation results" section)

- [ ] **Step 1: Write a one-shot validation script**

Create a temporary script (do not commit) at `/tmp/validate_hwp.py`:

```python
#!/usr/bin/env python3
"""One-shot validation pass over the user's 10 .hwp samples.

Usage:
    /Users/john/.pyenv/versions/3.12.8/bin/python /tmp/validate_hwp.py

Prints one line per sample with:
    status, text length, has Hangul, has PUA, sections tuple.
"""
from pathlib import Path
import sys

# Make the repo importable without installation.
sys.path.insert(0, "/Users/hogyeongkim/Desktop/Projects/FDA/FDA")

from fda.organize import _extractors

samples_dir = Path(
    "/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples"
)
samples = sorted(samples_dir.glob("*.hwp"))
print(f"Found {len(samples)} samples in {samples_dir}\n")

for sample in samples:
    r = _extractors.extract(sample)
    text = r.text or ""
    has_hangul = any("가" <= c <= "힣" for c in text)
    has_pua = any("" <= c <= "" for c in text)
    print(
        f"{sample.name}: status={r.status} "
        f"len={len(text)} hangul={has_hangul} pua={has_pua} "
        f"sections={r.sections}"
    )
```

- [ ] **Step 2: Run the validation script**

```bash
/Users/john/.pyenv/versions/3.12.8/bin/python /tmp/validate_hwp.py
```
Expected: 10 lines printed. Capture stdout — these results go into the spec.

- [ ] **Step 3: Append "Real-corpus validation results" to the spec**

Edit `docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md`. After the "Spike findings" section (created in Task 0), append:

```markdown
## Real-corpus validation results (2026-05-09)

10 hand-made `.hwp` samples at `/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples`. Each was passed through `_extract_hwp` and inspected for:

- `status == "ok"`
- text length > 0
- Hangul (U+AC00..U+D7A3) presence
- Hanyang PUA (U+E000..U+F8FF) presence
- non-empty `sections` (only required when Hangul is present, per spec)

Per-sample results:

| Sample | status | len | hangul | pua | sections |
|---|---|---|---|---|---|
| 새 문서.hwp | <ok / failed> | <N> | <yes/no> | <yes/no> | <tuple> |
| 새 문서 (1).hwp | ... | ... | ... | ... | ... |
| ...8 more rows... | | | | | |

Aggregate findings:
- Samples passing `status="ok"`: <N> / 10.
- Samples with Hangul: <N>.
- Samples with PUA: <N>.
- Samples with non-empty sections: <N>.
- Samples with PUA-only output (sections=() expected, v1 limitation): <N>.

Document any false negatives (visible structure A's regexes miss) and any PUA-heavy samples observed — both feed v2 brainstorm inputs (numbered-header pattern; PUA transliteration). Neither blocks ship.
```

Fill the table from the script output in Step 2.

- [ ] **Step 4: Run the full suite one more time**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-05-09-organize-hwpx-hwp-design.md
git commit -m "organize(spec): record hwp real-corpus validation results"
```

(Delete `/tmp/validate_hwp.py` afterwards — not part of the repo.)

---

## Task 13: Pipeline integration — add `.hwpx` to the integration corpus

Wire `.hwpx` into the end-to-end pipeline test. `.hwp` is intentionally **not** added — it depends on the user's private corpus (which may not exist on every host) and is already exercised by the unit + reader integration tests.

**Files:**
- Modify: `tests/test_organize_pipeline.py` (extend `workspace` fixture, extend `test_full_pipeline` and `test_preview_returns_plan_without_executing` assertions)

- [ ] **Step 1: Extend the `workspace` fixture with `.hwpx`**

Open `tests/test_organize_pipeline.py`. In the `workspace` fixture, immediately **after** the existing `csv_path.write_text(...)` line (line 53), and **before** `return root`, add:

```python

    # Minimal .hwpx (one section, one paragraph with a Korean bracket header).
    import zipfile as _zipfile
    hwpx_path = root / "korean.hwpx"
    ns = "http://www.hancom.co.kr/hwpml/2011/paragraph"
    with _zipfile.ZipFile(hwpx_path, "w", compression=_zipfile.ZIP_DEFLATED) as zf:
        info = _zipfile.ZipInfo("mimetype")
        info.compress_type = _zipfile.ZIP_STORED
        zf.writestr(info, b"application/hwp+zip")
        xml = (
            f'<?xml version="1.0"?>'
            f'<hp:sec xmlns:hp="{ns}">'
            f'<hp:p><hp:run><hp:t>[발주서]</hp:t></hp:run></hp:p>'
            f'</hp:sec>'
        ).encode("utf-8")
        zf.writestr("Contents/section0.xml", xml)
```

- [ ] **Step 2: Extend `test_full_pipeline` to assert the hwpx is moved**

In `class TestOrganize`, method `test_full_pipeline`, after the existing `assert (workspace / "Texts" / "data.csv").exists()` line (line 121), add:

```python
        assert (workspace / "Texts" / "korean.hwpx").exists()
```

- [ ] **Step 3: Extend `test_preview_returns_plan_without_executing` to assert hwpx survives preview**

In the same class, method `test_preview_returns_plan_without_executing`, after the existing `assert (workspace / "data.csv").exists()` line (line 140), add:

```python
        assert (workspace / "korean.hwpx").exists()
```

- [ ] **Step 4: Run the pipeline test**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/test_organize_pipeline.py -v`
Expected: PASS — `test_full_pipeline` and `test_preview_returns_plan_without_executing` both pass; the hwpx flows through the pipeline alongside docx/xlsx/pptx/csv.

- [ ] **Step 5: Run the full suite**

Run: `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_organize_pipeline.py
git commit -m "organize(integration): hwpx flows through reader and pipeline"
```

---

## Done condition

- `fda/organize/_extractors.py` defines five new constants (`_HWPX_SECTION_FILES_MAX`, `_HWPX_SECTION_BYTES_MAX`, `_HWPX_TOTAL_BYTES_MAX`, `_HWPX_COMPRESSION_RATIO_MAX`, `_HWP_BYTES_MAX`), the `_extract_hwpx` function, the `_extract_hwp` function, the `_hwpx_localname` helper, and the `_HWPX_SECTION_FILE_RE` regex. `EXTRACTORS` registers `.hwpx → _extract_hwpx` and `.hwp → _extract_hwp` (the latter only if Task 0's HWP decision was GO).
- `pyproject.toml` lists `defusedxml` and (if HWP GO) `pyhwp` in `dependencies`.
- All test classes added in Tasks 3–11 are green: `TestHwpxText`, `TestHwpxFailure`, `TestHwpxCaps`, `TestHwpxXmlSafety`, `TestHwpText`, `TestHwpSections`, `TestHwpFailure`. Reader passthrough + spot-check tests in `TestSectionsPropagationDocxXlsx` are green.
- Real-corpus validation results recorded in spec (Task 12).
- Pipeline integration test green with `.hwpx` in the corpus (Task 13). `.hwp` is intentionally not in the pipeline corpus — its passthrough is covered by Tasks 9 + 11 because the `.hwp` source corpus is private to this host.
- Full suite green via `/Users/john/.pyenv/versions/3.12.8/bin/python -m pytest tests/ -x -q --tb=short`.
- 14 commits on `dev_branch`, each tied to one task (12 if HWP NO-GO at Task 0).

After completion, the Obsidian "Future Plan - Structural Sections Across Formats" note should be updated by the user to reflect `.hwpx` and `.hwp` shipped (parallel to the csv update on 2026-05-08 and the Korean A update on 2026-05-08). That update is **not** an implementation step — it's the user's record-keeping after merge.
