# Organize: `.hwpx` + `.hwp` Extractors — Design

**Date:** 2026-05-09
**Branch:** dev_branch
**Predecessors:**
- `.csv` extractor — shipped 2026-05-08 (commits `56ee370` … `bc01195`)
- Korean structural-fingerprint coverage in `_sections.py` (sub-project A) — shipped 2026-05-08 (commits `923bfe9` … `93a9913`)

**Tracker:** `Future Plan - Structural Sections Across Formats.md` (Obsidian, Lion_Chemtech/FDA)

## Goal

Add two dedicated extractors for the Korean office formats:

- `_extract_hwpx` for `.hwpx` (XML in zip; OWPML / TTAK.OT-10.0203). Stdlib `zipfile` + `defusedxml`.
- `_extract_hwp` for `.hwp` (binary OLE compound document; HWP 5.x). Hard dep on `pyhwp`.

Both produce plaintext text that flows through `_sections.py`, where Korean A's regexes (bracket / colon / bullet) pick up Korean headings automatically. End state: Korean office documents in either format produce non-empty `text` and (when their content matches A's regexes) non-empty `sections`. Today both extensions return `status="no_extractor"` and contribute nothing to the classifier.

## Non-goals

- **Korean structural-fingerprint regex changes** — Korean A shipped them; this work consumes them, not extends them.
- **v2 numbered-header pattern** (`1. 데이터 관리 현황`) — separate brainstorm; not invoked here.
- **HWP v3** (legacy pre-1997 binary format) — `pyhwp` documents HWP 5.x only; v3 files return `status="failed"`.
- **`.hml` legacy XML format** — separate extension if ever needed.
- **Password-protected / DRM / "distributed" (multi-user-locked) `.hwp`** — return `status="failed"` with note. We do not crack protection.
- **ZIP-protected (encrypted) `.hwpx`** — same: `status="failed"` with note.
- **Embedded-object cracking** — extract only the plaintext that the parser surfaces. Charts, drawings, embedded OLE blobs, BinData streams: skipped silently. Their plaintext surrogate (caption / alt-text) is included if and only if the parser yields it as a text run.
- **Formatting fidelity** — drop styles, fonts, colors, layout, headers/footers' positioning. Preserve only paragraph-level text order.
- **Tables-as-structure** — flatten table cells row-by-row, top-to-bottom, left-to-right into the plaintext stream. Do NOT synthesize per-table section labels (no analog to xlsx sheets here; the file-level regex picks up structural headings inside the flattened text).
- **`.hwt` template format** — out.
- **Refactoring existing extractors or splitting `_extractors.py`** — explicitly forbidden by the chosen Approach A. CLAUDE.md surgical-changes rule applies.
- **csv-helpers cleanup PR** — separate, independent task. The new `.hwpx` and `.hwp` extractors import the public helpers directly from `_sections.py`; csv's private copies remain unchanged by this work.

## Architecture

Faithful extension of the docx/xlsx/pptx/csv format-onboarding pattern: one extractor function per format, registered in the `EXTRACTORS` dict. No new modules, no protocol abstraction, no rewrite of existing extractors.

**Files touched:**
- `fda/organize/_extractors.py` — add `_extract_hwpx` and `_extract_hwp`; add `".hwpx": _extract_hwpx` and `".hwp": _extract_hwp` to `EXTRACTORS`. Add per-format memory-bound constants. The existing import of `extract_sections_from_text` at the top of the file (line 32) is reused — both new extractors call it on their produced plaintext and pass the result through `ExtractionResult.sections`, mirroring how `_read_text` and `_extract_pdf_text` already populate sections (lines 94, 152). No additional `_sections.py` symbols are needed for these extractors.
- `pyproject.toml` — add `pyhwp` and `defusedxml` to `dependencies`. No optional-extras split (per the user's "hard dep" choice). Pin minimum versions once the implementation plan's spike confirms 3.12 compatibility.
- `tests/test_organize_extractors.py` — new test classes per format (`TestHwpxText`, `TestHwpxSections`, `TestHwpxFailure`, `TestHwpxCaps`, `TestHwpText`, `TestHwpSections`, `TestHwpFailure`, `TestHwpCaps`). Synthetic .hwpx fixtures built in-test via stdlib zipfile + ElementTree; .hwp fixtures harvested from the user's `~/Desktop/Projects/doc_agent_test_data/hwp_samples/` corpus + a couple of pyhwp-compatible minimal fixtures committed under `tests/fixtures/hwp/` (very small, public-format, non-Korean-confidential content) for repository self-containedness.
- `tests/test_organize_reader.py` — Reader passes `sections` through for `.hwpx` and `.hwp` (parallel to existing PDF/text/docx/xlsx/pptx/csv coverage).
- `tests/test_organize_constraints.py` — register new constants + any new fallback labels in the `CONSTS` check.
- `tests/test_organize_pipeline.py` — add `.hwpx` to the integration corpus. (`.hwp` is intentionally NOT in the pipeline corpus — its passthrough depends on the user's private corpus and is already exercised by extractor + reader integration tests.)

**Files not touched:** `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, all skill prompts. Reader already dispatches by extension and copies `sections` through.

**Two new runtime dependencies:** `pyhwp` (AGPLv3+) and `defusedxml`. `.hwpx` uses stdlib `zipfile` + `defusedxml.ElementTree`. `defusedxml` is promoted from follow-up to v1 dep after security review — Python's stdlib `xml.etree` Expat backend can still be vulnerable to internal-entity expansion / large-token DoS depending on Expat version, and the cost of `defusedxml` is one import substitution.

## License posture

**pyhwp is AGPLv3+.** AGPL is viral copyleft with a network/SaaS clause. The decision (recorded 2026-05-09) is to **accept AGPL exposure given fda-system's current distribution scope**: internal / Lion Chemtech use only, no PyPI publication, no public SaaS deployment, no LICENSE file declared in the repo today (effectively all-rights-reserved). At this scope, AGPL's copyleft trigger conditions do not apply.

**Re-evaluation triggers** (any of these change the calculus and require revisiting before the change ships):

- Decision to publish `fda-system` to PyPI under any permissive license (MIT/Apache/BSD).
- Decision to deploy `fda-system` as a public SaaS where third parties access the service over a network.
- Decision to redistribute `fda-system` to commercial customers as an SDK.

If any of these become real, the path forward is one of: switch to optional-extras (`pip install fda-system[hwp]`), evaluate a different `.hwp` parser, or vendor a stripped-down HWP 5 text-only reader. None of those work is in scope for this spec.

**Text contract:** Neither new extractor applies its own 64 KiB cap on `text`. Reader owns that contract via `READER_TEXT_CAP_BYTES` (`reader.py:29`) and applies byte-boundary-safe truncation plus the `[TRUNCATED at 64KB]` marker. The new constants exist only to bound in-process memory while reading, parsing, walking, and serializing — never as the contract cap.

**Status semantics:** these extractors return `"ok"`, `"failed"`, or (for `.hwp` only, in the unlikely case the runtime dep is somehow uninstalled at import time) `"tool_missing"`. Reader already handles all three (`reader.py:77-91`). No new statuses introduced.

## `_extract_hwpx`

```python
def _extract_hwpx(path: Path) -> ExtractionResult: ...
```

### Format primer

`.hwpx` is a ZIP archive (OWPML / TTAK.OT-10.0203). The contents follow a layout similar to `.docx`:

```
mimetype                          # uncompressed, "application/hwp+zip"
META-INF/
  container.xml                   # points to the rootfile (Contents/content.hpf)
Contents/
  content.hpf                     # OPF manifest, lists section files
  header.xml                      # styles, character maps, etc. — IGNORED
  section0.xml                    # body text, chapter 1
  section1.xml                    # body text, chapter 2
  ...
Preview/                          # IGNORED
BinData/                          # IGNORED (embedded images, OLE, etc.)
```

Body text lives in `Contents/section*.xml` files. Each is XML with paragraphs (`<hp:p>` → `<hp:run>` → `<hp:t>` text runs). The exact namespace URI varies across HWPX revisions; matching by local-name avoids hard-coding any specific namespace.

> **Namespace coverage (verified):** observed namespace families include `http://www.hancom.co.kr/hwpml/2011/paragraph`, `http://www.hancom.co.kr/hwpml/2016/paragraph`, and the newer `http://www.owpml.org/owpml/2021/...` family used by current Hancom Office output. Rather than match any of them explicitly, walk all elements and select those whose tag's local-name (after the `}`) is `t`. Synthetic test fixtures should include at least one document per namespace family (2011, 2016, 2021).

### Step 1 — open as zip, validate

- Open via `zipfile.ZipFile(path, "r")`. Wrap in `try/except zipfile.BadZipFile` → `status="failed"`.
- Read `mimetype` member. If absent → `status="failed", note="not an OWPML hwpx"`. Otherwise compare via `zf.read("mimetype").strip() == b"application/hwp+zip"` (exact match after stripping whitespace; reject prefix-only matches like `application/hwp+zip-bad`).
- Reject encrypted entries: iterate `zf.infolist()`; if any `info.flag_bits & 0x1` is set → `status="failed", note="encrypted hwpx"`. (ZIP encryption flag.)

### Step 2 — locate section files

Two strategies; we use the simpler one:

- **Chosen:** glob the namelist for `Contents/section\d+\.xml`. Sort numerically by the integer suffix. Walk in that order.
- **Rejected:** parse `META-INF/container.xml` → `Contents/content.hpf` → resolve manifest items in spine order. More format-correct (Hancom's parsing guide treats `content.hpf` spine order as canonical) but adds two parses for negligible benefit; the section-file naming convention is stable in real-world Hangul Office output. **Accepted limitation:** if a future document uses non-standard section-file names listed only in the spine, we will miss them. The spec accepts this risk — flip to spine parsing in a follow-up if such a document appears in the corpus.

If no section files match → `text="", sections=()`, `status="ok"`. (Empty doc: legitimate.)

Cap the count of section files walked at `_HWPX_SECTION_FILES_MAX` (default 100) — protects against a malicious archive with thousands of entries.

### Step 3 — parse text per section file

For each section file (in order), up to the cap:

- **Pre-decompression cap check (zip-bomb defense):** look up `ZipInfo.file_size` (uncompressed) for the entry. Skip if `file_size > _HWPX_SECTION_BYTES_MAX` (default 4 MiB) — set the run's text to `""`, do not call `zf.read`, do not abort the whole extraction. Track cumulative `file_size` across sections; if cumulative exceeds `_HWPX_TOTAL_BYTES_MAX` (default 8 MiB) BEFORE reading the next entry, stop walking.
- **Compression-ratio guard:** also reject any entry where `file_size / max(compress_size, 1) > _HWPX_COMPRESSION_RATIO_MAX` (default 100) — defends against highly-compressed zip-bomb members that pass the absolute-size check.
- Only after both checks pass: read via `zf.read(name)`.
- Parse with `defusedxml.ElementTree.fromstring(data)` (the `defusedxml` import is mandatory in v1; see Architecture). Two distinct error classes:
  - `ET.ParseError` (mid-section corruption / not-well-formed XML) → **skip this section file** (its text is `""`); do not abort the whole extraction. Consistent with partial-recovery behavior.
  - `defusedxml.common.DefusedXmlException` (and subclasses `DTDForbidden`, `EntitiesForbidden`, `ExternalReferenceForbidden`) → **abort the whole extraction** with `status="failed", note=str(e)`. These indicate intentionally-malicious payloads, not benign corruption; failing the whole file is the safer signal to the rest of the pipeline.
- Walk all elements. Collect `.text` (and `.tail` if non-trivial; details below) of every element whose local-name is `t`. Local-name match: `tag.rsplit("}", 1)[-1] == "t"`.
- Join collected runs with `""` (empty string — runs in HWPX are already token-level; spaces inside runs are explicit). Append the section's joined text to a per-section list.
- Between sections, append a single `"\n"` separator so paragraph breaks across HWPX section boundaries do not get glued together.

Open question pinned in the implementation plan: do we also want to insert `"\n"` between paragraphs (`<hp:p>`) inside a single section file? Without that, all paragraphs in one section join into a single line, which defeats the line-oriented `_sections.py` regex. **Decision for v1:** YES — also insert `"\n"` after each paragraph element. Implementation walks paragraphs, then within each paragraph walks runs. This is more code than a flat `.iter("...t")` loop but produces line-shaped output that `_sections.py` can pattern-match.

### Step 4 — return

```python
text = "\n".join(per_section_texts)
return ExtractionResult(
    text=text,
    status="ok",
    sections=extract_sections_from_text(text),
)
```

The extractor itself calls `extract_sections_from_text(text)` and passes the result through `ExtractionResult.sections`. This mirrors `_read_text` (line 94) and `_extract_pdf_text` (line 152) — Reader does NOT run `_sections` over plaintext on its own; each extractor is responsible for populating its own `sections`. Reader copies `sections` through unchanged.

For `text == ""` the call is harmless: `extract_sections_from_text("")` early-returns `()` (`_sections.py:132-133`).

### Edge cases (HWPX)

| Case | Behavior |
|---|---|
| Not a zip | `status="failed", note="not a zip"` |
| Zip but no `mimetype` member or wrong mimetype | `status="failed", note="not an OWPML hwpx"` |
| Any zip entry has encryption bit set | `status="failed", note="encrypted hwpx"` |
| Zero section files | `status="ok", text="", sections=()` |
| One or more section files unparseable XML | Skip the unparseable ones; continue. If ALL unparseable → `text=""` is acceptable; status stays `"ok"`. |
| Section file `ZipInfo.file_size` > `_HWPX_SECTION_BYTES_MAX` | Skip that section (do NOT call `zf.read`); do not abort. |
| Cumulative `file_size` > `_HWPX_TOTAL_BYTES_MAX` | Stop walking BEFORE next `zf.read`; return what we have. |
| Compression ratio `file_size / max(compress_size, 1)` > `_HWPX_COMPRESSION_RATIO_MAX` | `status="failed", note="compressed hwpx zip bomb"` (any one entry exceeding the ratio fails the whole archive — these are not benign). |
| Section count > `_HWPX_SECTION_FILES_MAX` | Truncate at the cap; do not abort. |
| `defusedxml.common.DefusedXmlException` on any section | `status="failed", note=str(e)` (abort the whole extraction). |
| Whole-archive read raises `OSError` | `status="failed", note=str(e)` |

## `_extract_hwp`

```python
def _extract_hwp(path: Path) -> ExtractionResult: ...
```

### Format primer

`.hwp` is a Microsoft Compound Document (OLE2) wrapping HWP 5.x streams. Hancom publishes the format spec (HWP Binary Specification 5.x, Korean). The Python parser of record is `pyhwp` (provides a `hwp5txt` CLI plus an importable API).

### Dependency strategy

`pyhwp` is added to `pyproject.toml` `dependencies` (license posture resolved — see above). The implementation plan's task 0 spike must verify three things before the rest of the plan executes:

1. `pyhwp` installs cleanly on Python 3.12 / 3.13. **Risk:** pyhwp's PyPI classifiers stop at Python 3.8 with no active `python_requires`; install on 3.12+ is unverified and may fail.
2. The Python API is stable enough to import directly. The confirmed top-level package is `hwp5`; the plaintext-extraction surface is `hwp5.hwp5txt.TextTransform` (importable). The console script is `hwp5txt`.
3. The library's plaintext output preserves Korean characters. Verify against one of the user's 10 hand-made samples.

**Critical clarification (codex review finding):** the prior "fall back to subprocess-CLI if import-broken" strategy does NOT solve install failure — the `hwp5txt` console script is installed by the same package. If `pip install pyhwp` fails on 3.12, neither the library nor the CLI is available. Real fallback options if the spike fails: (a) vendor and patch pyhwp (AGPL-bound; out of scope here), (b) find an alternative `.hwp` parser (none known with comparable coverage), (c) drop sub-project C from this spec and brainstorm a replacement strategy. The spike's outcome is therefore a hard go/no-go gate for C — the plan must not proceed past task 0 for the HWP path if install fails.

### Step 1 — guard the dep

```python
try:
    import hwp5                                      # pyhwp top-level package (verified)
    from hwp5.xmlmodel import Hwp5File               # text-transform-capable file wrapper
    from hwp5.hwp5txt import transform_hwp5_to_text  # plaintext extraction (verified per pyhwp source)
except ImportError as e:
    return ExtractionResult(text=None, status="tool_missing", note=str(e))
```

This branch should be unreachable in practice because the dep is hard. It exists for defense-in-depth (corrupt venv, partial install) so a missing import never aborts a run. Note: `Hwp5File` is imported from `hwp5.xmlmodel`, NOT `hwp5.filestructure` — the xmlmodel wrapper is what `transform_hwp5_to_text` operates on.

### Step 2 — pre-open size check

```python
if path.stat().st_size > _HWP_BYTES_MAX:
    return ExtractionResult(text=None, status="failed", note="oversized hwp")
```

Reader owns the output cap. This cap memory-bounds pyhwp's in-process parse.

### Step 3 — open and inspect FileHeader flags

Open via `Hwp5File(str(path))` — pyhwp's `Hwp5File` constructor checks for `basestring`-shaped input and rejects `pathlib.Path`, so we coerce to `str` explicitly:

```python
try:
    hwp = Hwp5File(str(path))
except Exception as e:                       # malformed OLE, v3, etc.
    return ExtractionResult(text=None, status="failed", note=str(e))

header = hwp.fileheader                      # FileHeader; field name pinned in plan task 0
if header.flags.password:
    return ExtractionResult(text=None, status="failed", note="password-protected hwp")
if header.flags.distributable:
    # pyhwp passes distributable docs through a ViewText wrapper; v1 fails them,
    # v2 may opt in to ViewText extraction once we understand the format better.
    return ExtractionResult(text=None, status="failed", note="distributed hwp")
```

**Codex finding (pass 1):** pyhwp does NOT raise on password or distributable — passwords pass through with a warning (decryption is unsupported in pyhwp), and distributable docs route through ViewText. Direct flag inspection is the correct surface.

### Step 4 — extract text

pyhwp's plaintext API takes a destination buffer (BytesIO) — it streams text out, not a return value:

```python
import io
buf = io.BytesIO()
try:
    transform_hwp5_to_text(hwp, buf)         # exact call signature pinned in plan task 0
except Exception as e:
    return ExtractionResult(text=None, status="failed", note=str(e))
text = buf.getvalue().decode("utf-8", errors="replace")
```

The plan task 0 spike confirms the precise function name and signature against the installed package — Codex verified `transform_hwp5_to_text` from the pyhwp source, but pyhwp's internal API has shifted across versions, so the plan pins it once installed.

**Mojibake / Hanyang PUA known limitation:** pyhwp may emit Hanyang Private-Use Area characters (codepoints in U+E000..U+F8FF range) for some legacy Korean text rather than precomposed Hangul (U+AC00..U+D7A3). These pass through `text` cleanly but do NOT match `_sections.py`'s `contains_hangul()` Hangul-Syllables check, so PUA-heavy `.hwp` files will produce `sections=()` even when visually they have headers. **v1 accepts this limitation** rather than widening the regex (PUA is not unambiguously Korean). Document in real-corpus validation if observed; surface as a v2 brainstorm input. The original "all-`?` mojibake sentinel" is dropped from the spec — too narrow to catch the real failure mode (PUA), and risks false-positives on legitimately-empty docs.

### Step 5 — return

```python
return ExtractionResult(
    text=text,
    status="ok",
    sections=extract_sections_from_text(text),
)
```

Same pattern as `.hwpx`: the extractor itself calls `extract_sections_from_text(text)` and populates `sections`. Reader copies it through.

### Edge cases (HWP)

| Case | Detection | Behavior |
|---|---|---|
| Not an OLE compound doc | `Hwp5File()` raises | `status="failed", note=str(e)` |
| HWP v3 (pre-5) | `Hwp5File()` raises (pyhwp doesn't parse v3) | `status="failed", note="hwp v3 unsupported"` |
| Distributed | `header.flags.distributable` is True | `status="failed", note="distributed hwp"` |
| Password-protected | `header.flags.password` is True | `status="failed", note="password-protected hwp"` |
| Truncated / malformed streams | `transform_hwp5_to_text(hwp, buf)` raises | `status="failed", note=str(e)` |
| File > `_HWP_BYTES_MAX` | `path.stat().st_size` check before open | `status="failed", note="oversized hwp"` |
| Empty body | `transform_hwp5_to_text` writes 0 bytes to `buf` | `status="ok", text="", sections=()` |
| Hanyang PUA characters | Pass through `text` cleanly | `status="ok"`, `sections` may be `()` (PUA evades `contains_hangul`) — known v1 limitation, see Step 3 |

## Constants

Added to `_extractors.py`:

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

Numerical caps mirror the docx/xlsx/pptx/csv pattern: in-process memory-bound, never the contract cap. Caps are tunable; defaults chosen to comfortably exceed real-world Korean office docs (the user's 10 samples are all ~85 KiB) while still rejecting pathological inputs.

No new symbols are imported from `_sections.py` for these extractors. The existing `extract_sections_from_text` import (already present at `_extractors.py:32`) is reused. `KOREAN_LABEL_MIN_CHARS`, `contains_hangul`, `SECTION_HEADER_MAX_CHARS`, and `MAX_SECTIONS_PER_FILE` are all consumed inside `extract_sections_from_text` itself; the new extractors do not need per-cell length-guard logic the way csv does.

## Validation strategy

### `.hwp` (sub-project C) — real-corpus pass

After implementation passes unit tests, run the extractor against all 10 of the user's hand-made samples in `~/Desktop/Projects/doc_agent_test_data/hwp_samples/`. For each sample:

1. Confirm `status == "ok"`.
2. Confirm `text` contains Korean characters in EITHER form: precomposed Hangul (U+AC00..U+D7A3) OR Hanyang Private-Use Area (U+E000..U+F8FF). Pass on either; PUA-only output is a known pyhwp emission for some legacy docs and is not a failure.
3. Confirm running `_sections.extract_sections_from_text(text)` over the result produces a non-empty tuple WHEN the source document contains bracket / colon / bullet headers AND the output contains precomposed Hangul. **Skip the sections-regex assertion when the output is PUA-only** — Korean A's regexes match U+AC00..U+D7A3 only; this is a documented v1 limitation, not a failure of this work.

Record findings in this spec under a "Real-corpus validation results" section appended at the end (mirrors the Korean A spec's pattern). Document any false negatives (visible structure that A's regexes miss) AND any PUA-heavy samples observed — those become inputs to the v2 numbered-header brainstorm and a future PUA-transliteration brainstorm. Neither blocks this ship.

### `.hwpx` (sub-project B) — synthetic + harvested public docs

No user-provided `.hwpx` samples exist (user cannot create them). Two-pronged validation:

1. **Synthetic fixtures (in-test):** Build minimal valid `.hwpx` archives in test code via stdlib `zipfile` + `xml.etree.ElementTree`. Cover: empty body, single-paragraph, multi-paragraph, multi-section, namespace variants (2011, 2016, AND 2021 OWPML namespace families — at least one fixture per family), one-section-file-malformed, encrypted-flag-set rejection, missing-mimetype rejection, compression-ratio-bomb rejection.
2. **Harvested public corpus (under `tests/fixtures/hwp/`):** download 2–3 public-domain `.hwpx` documents from a Korean government open-data source (e.g., Korea Data Portal `data.go.kr` publishes `.hwpx`-formatted public records). Commit them to the repo under `tests/fixtures/hwp/` only if they are genuinely public-domain and small (< 50 KiB each). Their purpose is spot-check: real-world `.hwpx` produced by Hangul Office may have quirks the synthetic fixtures don't capture (unusual run nesting, comment annotations, etc.). If we can't find suitably-licensed small samples, skip this step and document the gap — the spec is still shippable, the gap goes in the post-ship follow-up checklist.

The implementation plan's first task is the harvest step (so subsequent tasks can write fixtures-aware tests). If harvest fails, plan task 0 records that and moves on; we do not block on it.

## Test plan

Mirroring the csv (~50 tests) and pptx (23 tests) patterns. Estimated counts (final numbers settled in plan tasks):

**HWPX tests** (`tests/test_organize_extractors.py`):
- `TestHwpxText` — happy path single-section, multi-section, empty body, namespace variants (2011, 2016, 2021), paragraph separators inserted between `<hp:p>`.
- `TestHwpxSections` — synthetic fixtures embedding bracket / colon / bullet headers in their text runs; assert the extractor populates `ExtractionResult.sections` and that Reader copies it through to `CatalogEntry.sections`.
- `TestHwpxFailure` — not-a-zip, missing-mimetype, wrong-mimetype (including `application/hwp+zip-bad` exact-match regression), encrypted-flag-set, oversized-section (rejected via `ZipInfo.file_size` before `read`), oversized-cumulative, compression-ratio-bomb (small compressed/large uncompressed entry), malformed XML in one section but not all (partial recovery), HWPX with zero section files.
- `TestHwpxCaps` — `_HWPX_SECTION_FILES_MAX`, `_HWPX_SECTION_BYTES_MAX`, `_HWPX_TOTAL_BYTES_MAX`, `_HWPX_COMPRESSION_RATIO_MAX` boundary tests.

**HWP tests** (`tests/test_organize_extractors.py`):
- `TestHwpText` — happy path against ≥1 real sample (committed), Korean text round-trips intact, multi-paragraph order preserved.
- `TestHwpSections` — at least one sample with a known `[제목]` or `제목:` header in body text; assert `_sections.extract_sections_from_text` picks it up.
- `TestHwpFailure` — non-OLE input (e.g., truncated bytes, plain text mislabeled as `.hwp`), oversized file (rejected via `path.stat().st_size` before open), `header.flags.password` triggers password-protected failure, `header.flags.distributable` triggers distributed failure.
- `TestHwpCaps` — `_HWP_BYTES_MAX` boundary.

**Reader integration** (`tests/test_organize_reader.py`): one test per format that confirms `CatalogEntry.sections` is populated end-to-end for a sample with known headers, and preserved as `()` on extractor failure.

**Constants registry** (`tests/test_organize_constraints.py`): register the five new memory-bound constants (`_HWPX_SECTION_FILES_MAX`, `_HWPX_SECTION_BYTES_MAX`, `_HWPX_TOTAL_BYTES_MAX`, `_HWPX_COMPRESSION_RATIO_MAX`, `_HWP_BYTES_MAX`) in the existing `CONSTS` check.

**Pipeline integration** (`tests/test_organize_pipeline.py`): add `.hwpx` fixture to the integration corpus. `.hwp` is intentionally not in the pipeline corpus — its passthrough is covered by the extractor + reader integration tests, since the `.hwp` source corpus is private to one host.

Test target: ~50 net new tests (≈30 hwpx + ≈20 hwp). Suite runs after every commit per CLAUDE.md.

## Implementation order

The plan ships in this sequence on `dev_branch`:

1. **Task 0 — spike** (no production code): verify pyhwp installs on 3.12, pin its API, verify HWPX namespace handling against harvested public samples (or note the gap). Outputs: pinned function call signatures, pinned namespace-handling decision, harvested-fixture commit (or recorded gap).
2. **HWPX first** (lower risk, std-lib only):
   - Reserve constants commit.
   - Happy-path `_extract_hwpx` + register.
   - Failure modes (not-a-zip, missing/wrong mimetype, encryption flag).
   - Section-file walk + paragraph separator.
   - Cap enforcement.
   - Reader integration test.
3. **HWP second** (depends on spike):
   - Add `pyhwp` to `pyproject.toml`.
   - Happy-path `_extract_hwp` + register.
   - Failure modes (non-OLE, v3, distributed via FileHeader.flags, password via FileHeader.flags, oversized).
   - Reader integration test.
4. **Pipeline integration** + constants-registry update + final corpus pass.

Each step is a separate commit. Pre-commit hook enforces the full 113+net-new test suite.

## Out-of-scope follow-ups (post-ship)

- **csv-helpers cleanup PR** (already queued before this work; independent).
- **v2 numbered-header pattern in `_sections.py`** (deferred from Korean A; needs its own brainstorm).
- **`.hml` extractor** if real `.hml` files appear in the corpus.
- **Embedded-image OCR** for `.hwp(x)` — out for v1; would need an OCR runtime dep. Open Lion Chemtech corpus signal first.
- **content.hpf spine parsing** for `.hwpx` — flip from glob-based section discovery if a real document with non-standard section-file naming appears.
- **Per-table section labeling** if Lion Chemtech `.hwp(x)` corpus shows tables-as-structure dominate (parallel to xlsx's `Sheet:<name>` pattern, but at the table level).
- **Hanyang PUA → Hangul transliteration** for `.hwp` if PUA-heavy documents prove common — would feed Korean A's regex engine on previously-invisible structure.
- **Distributed-doc ViewText extraction** for `.hwp` — pyhwp wraps distributable docs and exposes `Hwp5File.text` as `ViewText`; v1 fails them, v2 may opt in.
- **Real-corpus validation results section** appended to this spec post-ship.

## Spike findings (2026-05-09)

- `pyhwp` installed cleanly on Python 3.12 (.venv/bin/python is 3.12.11): yes. Pinned version: 0.1b15.
- `defusedxml` installed cleanly: yes. Pinned version: 0.7.1.
- API surface verified: `Hwp5File` from `hwp5.xmlmodel`; `TextTransform` from `hwp5.hwp5txt` (its `.transform_hwp5_to_text` is a `@property` returning a callable — confirmed it returns `BaseTransform.make_transform_hwp5.<locals>.transform_hwp5`).
- Sample round-trip: 3 of the user's hand-made samples checked (`새 문서 (1).hwp`, `새 문서 (2).hwp`, `새 문서 (3).hwp`); all open with `password=0`, `distributable=0`; produce 500–650 chars of clean precomposed Hangul text (no PUA observed in the spot-check). Note: `header.flags.password` and `.distributable` are integers (0/1) not bools — the falsy check `if header.flags.password:` works correctly.
- Harvested `.hwpx` fixtures committed under `tests/fixtures/hwp/`: none — gap recorded; harvested-fixture spot-check in Task 8 will skip. Auto-mode time-box did not allow Korea Data Portal harvest.
- Harvested or user-permitted `.hwp` fixtures committed under `tests/fixtures/hwp/`: none — gap recorded; HWP tests skip without user's private corpus.
- Test Python path: `/Users/hogyeongkim/Desktop/Projects/FDA/FDA/.venv/bin/python` (project venv, Python 3.12.11). The plan's documented path (`/Users/john/.pyenv/versions/3.12.8/bin/python`) does not exist on this host; using the project venv per the plan's "fall back to whichever Python 3.12 the test fixture conftest works under" clause.
- HWP half decision: **GO**.

## Real-corpus validation results (2026-05-09)

10 hand-made `.hwp` samples at `/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/hwp_samples`. Each was passed through `_extract_hwp` and inspected for:

- `status == "ok"`
- text length > 0
- Hangul (U+AC00..U+D7A3) presence
- Hanyang PUA (U+E000..U+F8FF) presence
- non-empty `sections` (expected only when the content contains Korean A's bracket/colon/bullet patterns; numbered Arabic-digit headers and PUA-only output are documented v1 limitations)

Per-sample results:

| Sample | status | len | hangul | pua | sections |
|---|---|---|---|---|---|
| 새 문서 (1).hwp | ok | 636 | True | False | () |
| 새 문서 (2).hwp | ok | 535 | True | False | () |
| 새 문서 (3).hwp | ok | 536 | True | False | () |
| 새 문서 (4).hwp | ok | 547 | True | False | () |
| 새 문서 (5).hwp | ok | 556 | True | False | () |
| 새 문서 (6).hwp | ok | 599 | True | False | () |
| 새 문서 (7).hwp | ok | 587 | True | False | () |
| 새 문서 (8).hwp | ok | 573 | True | False | () |
| 새 문서 (9).hwp | ok | 621 | True | False | () |
| 새 문서.hwp | ok | 645 | True | False | () |

Aggregate findings:
- Samples passing `status="ok"`: 10 / 10.
- Samples with Hangul: 10.
- Samples with PUA: 0.
- Samples with non-empty sections: 0.
- Samples with PUA-only output (sections=() expected, v1 limitation): 0.

**False negatives (visible structure A's regexes miss):** All 10 samples use the numbered-header pattern (`1. 채용 요청 배경`, `2. 생산 실적`, `3. 주요 작업 내용`, etc.). Korean A's v1 regexes cover bracket `[제목]`, colon `제목:`, and bullet `■ 제목` patterns only — numbered Arabic-digit headers are the deferred v2 numbered-header pattern. The `sections=()` result for all 10 samples is therefore an expected v1 limitation, not an extractor defect. Text extraction and Hangul round-trip are confirmed correct on all 10 samples.

**PUA observations:** No Hanyang PUA characters (U+E000..U+F8FF) observed in any sample. All samples produce clean precomposed Hangul (U+AC00..U+D7A3). The PUA-handling path remains untested on real corpus; its behavior is exercised only via the synthetic unit tests in `TestHwpFailure`.

**v2 brainstorm inputs:**
- **Numbered-header pattern** (`\d+\.\s+<Hangul label>`): present in all 10 real samples; unambiguously the dominant heading style in this Lion Chemtech corpus. High priority for Korean B / v2 `_sections.py` work.
- **PUA transliteration**: no real-corpus signal yet. Remains a theoretical concern for older `.hwp` files; not observed here.
