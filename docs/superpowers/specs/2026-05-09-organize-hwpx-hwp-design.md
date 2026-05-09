# Organize: `.hwpx` + `.hwp` Extractors — Design

**Date:** 2026-05-09
**Branch:** dev_branch
**Predecessors:**
- `.csv` extractor — shipped 2026-05-08 (commits `56ee370` … `bc01195`)
- Korean structural-fingerprint coverage in `_sections.py` (sub-project A) — shipped 2026-05-08 (commits `923bfe9` … `93a9913`)

**Tracker:** `Future Plan - Structural Sections Across Formats.md` (Obsidian, Lion_Chemtech/FDA)

## Goal

Add two dedicated extractors for the Korean office formats:

- `_extract_hwpx` for `.hwpx` (XML in zip; OWPML / TTAK.OT-10.0203). Stdlib only.
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
- `pyproject.toml` — add `pyhwp` to `dependencies`. No optional-extras split (per the user's "hard dep" choice). Pin a minimum version once the implementation plan's spike confirms 3.12 compatibility.
- `tests/test_organize_extractors.py` — new test classes per format (`TestHwpxText`, `TestHwpxSections`, `TestHwpxFailure`, `TestHwpxCaps`, `TestHwpText`, `TestHwpSections`, `TestHwpFailure`, `TestHwpCaps`). Synthetic .hwpx fixtures built in-test via stdlib zipfile + ElementTree; .hwp fixtures harvested from the user's `~/Desktop/Projects/doc_agent_test_data/hwp_samples/` corpus + a couple of pyhwp-compatible minimal fixtures committed under `tests/fixtures/hwp/` (very small, public-format, non-Korean-confidential content) for repository self-containedness.
- `tests/test_organize_reader.py` — Reader passes `sections` through for `.hwpx` and `.hwp` (parallel to existing PDF/text/docx/xlsx/pptx/csv coverage).
- `tests/test_organize_constraints.py` — register new constants + any new fallback labels in the `CONSTS` check.
- `tests/test_organize_pipeline.py` — add `.hwpx` and `.hwp` files to the integration corpus.

**Files not touched:** `_sections.py`, `models.py`, `reader.py`, `classifier.py`, `plan_builder.py`, `executor.py`, `verifier.py`, all skill prompts. Reader already dispatches by extension and copies `sections` through.

**One new runtime dependency:** `pyhwp`. `.hwpx` uses stdlib only.

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

Body text lives in `Contents/section*.xml` files. Each is XML with paragraphs (`<hp:p>` → `<hp:run>` → `<hp:t>` text runs). The exact namespace URI varies across HWPX revisions (2010 / 2014 / 2017); `.iter()` over local-name (`endswith("}t")`) avoids hard-coding the namespace.

> **Implementation note (to be pinned in the plan):** the namespace URIs the spec is most likely to encounter are `http://www.hancom.co.kr/hwpml/2011/paragraph` (older) and `http://www.hancom.co.kr/hwpml/2016/paragraph` (newer). Rather than match either explicitly, walk all elements and select those whose tag's local-name (after the `}`) is `t`. Implementation-plan task 0 must verify this against the harvested public samples.

### Step 1 — open as zip, validate

- Open via `zipfile.ZipFile(path, "r")`. Wrap in `try/except zipfile.BadZipFile` → `status="failed"`.
- Read `mimetype` member. If absent OR contents do not start with `b"application/hwp+zip"` → `status="failed", note="not an OWPML hwpx"`. (Lenient on trailing whitespace; strict on the prefix because the format spec mandates uncompressed `mimetype` as the first member with that exact body.)
- Reject encrypted entries: iterate `zf.infolist()`; if any `info.flag_bits & 0x1` is set → `status="failed", note="encrypted hwpx"`. (ZIP encryption flag.)

### Step 2 — locate section files

Two strategies; we use the simpler one:

- **Chosen:** glob the namelist for `Contents/section\d+\.xml`. Sort numerically by the integer suffix. Walk in that order.
- **Rejected:** parse `META-INF/container.xml` → `Contents/content.hpf` → resolve manifest items. More format-correct but adds two parses for negligible benefit; the section-file naming convention is stable across HWPX revisions and easier to verify against fixtures.

If no section files match → `text="", sections=()`, `status="ok"`. (Empty doc: legitimate.)

Cap the count of section files walked at `_HWPX_SECTION_FILES_MAX` (default 100) — protects against a malicious archive with thousands of entries.

### Step 3 — parse text per section file

For each section file (in order), up to the cap:

- Read its bytes from the zip via `zf.read(name)`. Skip if size > `_HWPX_SECTION_BYTES_MAX` (default 4 MiB) — set the run's text to `""`, do not abort the whole extraction. Cumulative bytes parsed across all sections is also capped at `_HWPX_TOTAL_BYTES_MAX` (default 8 MiB). If exceeded, stop reading further sections.
- Parse with `xml.etree.ElementTree.fromstring(data)`. On `ET.ParseError` → skip this section file (its text is `""`); do not abort. Note: `xml.etree` resolves no entities by default in modern Python, but for explicit defense-in-depth the implementation plan may switch to `defusedxml.ElementTree` if a benchmark shows acceptable cost — listed as an open follow-up below; not required for v1.
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
| Section file > `_HWPX_SECTION_BYTES_MAX` | Skip that section; do not abort. |
| Cumulative bytes > `_HWPX_TOTAL_BYTES_MAX` | Stop walking; return what we have. |
| Section count > `_HWPX_SECTION_FILES_MAX` | Truncate at the cap; do not abort. |
| Whole-archive read raises `OSError` | `status="failed", note=str(e)` |

## `_extract_hwp`

```python
def _extract_hwp(path: Path) -> ExtractionResult: ...
```

### Format primer

`.hwp` is a Microsoft Compound Document (OLE2) wrapping HWP 5.x streams. Hancom publishes the format spec (HWP Binary Specification 5.x, Korean). The Python parser of record is `pyhwp` (provides a `hwp5txt` CLI plus an importable API).

### Dependency strategy

`pyhwp` is added to `pyproject.toml` `dependencies` (hard dep, per the user's chosen option). The implementation plan's task 0 spike must verify three things before the rest of the plan executes:

1. `pyhwp` installs cleanly on Python 3.12 / 3.13 (the `requires-python = ">=3.9"` line).
2. The Python API is stable enough to import directly (vs shelling out to the `hwp5txt` CLI). If it's import-broken, fall back to a `subprocess.run([sys.executable, "-m", "hwp5txt", ...])` invocation in-process. Both are acceptable; the API path is preferred for speed and error visibility.
3. The library's plaintext output preserves Korean characters (i.e., does not lossy-decode to question marks). Verify against one of the user's 10 hand-made samples.

If the spike reveals `pyhwp` is broken on 3.12, the design pivots to a subprocess-to-`hwp5txt`-CLI strategy (still hard dep — the install would carry the CLI). The user already accepted "hard dep" as a category; CLI vs library is an implementation detail. Spec stays valid; plan adapts.

### Step 1 — guard the dep

```python
try:
    import hwp5  # pyhwp's top-level package; exact name pinned in plan task 0
except ImportError as e:
    return ExtractionResult(text=None, status="tool_missing", note=str(e))
```

This branch should be unreachable in practice because the dep is hard. It exists for defense-in-depth (corrupt venv, partial install) so a missing import never aborts a run.

### Step 2 — open and extract

The exact pyhwp call is **pinned in implementation-plan task 0** after the spike. The spec assumes a contract roughly:

```python
hwp5.dataio.UnpackErrors  # or whatever the lib raises on malformed
text = hwp5.tools.hwp5txt.extract_text(path)  # placeholder; real name TBD in plan
```

Behavior the implementation plan must wrap regardless of API shape:

- Wrap in `try/except` to catch:
  - File-format errors (malformed OLE, wrong version) → `status="failed", note=...`
  - Distributed (locked) doc indicator if pyhwp surfaces it → `status="failed", note="distributed hwp"`
  - HWP v3 detected → `status="failed", note="hwp v3 unsupported"`
- Memory-bound the in-process work via `_HWP_BYTES_MAX` (default 16 MiB) read cap on the input file (not on output text — Reader owns the output cap). If the file is larger, the implementation plan decides between (a) reading the cap and passing a `BytesIO` to pyhwp (only works if pyhwp accepts a stream — verify in spike) or (b) returning `status="failed", note="oversized hwp"`. **Default decision for v1:** (b) — fail fast on oversized; revisit only if real Lion Chemtech docs trigger this.

### Step 3 — return

```python
return ExtractionResult(
    text=text,
    status="ok",
    sections=extract_sections_from_text(text),
)
```

Same pattern as `.hwpx`: the extractor itself calls `extract_sections_from_text(text)` and populates `sections`. Reader copies it through.

### Edge cases (HWP)

| Case | Behavior |
|---|---|
| Not an OLE compound doc | `status="failed", note="not an OLE compound doc"` |
| HWP v3 (pre-5) | `status="failed", note="hwp v3 unsupported"` |
| Distributed (DRM-locked) | `status="failed", note="distributed hwp"` |
| Password-protected | `status="failed", note="password-protected hwp"` |
| Truncated / malformed streams | `status="failed", note=<lib msg>` |
| File > `_HWP_BYTES_MAX` | `status="failed", note="oversized hwp"` |
| Empty body | `status="ok", text="", sections=()` |
| Encoding mojibake (lib returns `?` for Hangul) | `status="failed", note="hwp decode error"` — detect via a sentinel check: if input file is non-trivial size but output text is all `?` and contains zero Hangul, treat as decode failure rather than a legitimately-empty doc. Implementation-plan task verifies the sentinel against samples. |

## Constants

Added to `_extractors.py`:

```python
# .hwpx (XML in zip; OWPML / TTAK.OT-10.0203). Memory-bound only — Reader
# owns the 64 KiB contract cap via READER_TEXT_CAP_BYTES.
_HWPX_SECTION_FILES_MAX = 100        # archive-entry count cap
_HWPX_SECTION_BYTES_MAX = 4 * 1024 * 1024   # per-section-file decompressed size cap
_HWPX_TOTAL_BYTES_MAX = 8 * 1024 * 1024     # cumulative cap across all sections

# .hwp (binary OLE / HWP 5.x via pyhwp).
_HWP_BYTES_MAX = 16 * 1024 * 1024    # input file size cap
```

Numerical caps mirror the docx/xlsx/pptx/csv pattern: in-process memory-bound, never the contract cap. Caps are tunable; defaults chosen to comfortably exceed real-world Korean office docs (the user's 10 samples are all ~85 KiB) while still rejecting pathological inputs.

No new symbols are imported from `_sections.py` for these extractors. The existing `extract_sections_from_text` import (already present at `_extractors.py:32`) is reused. `KOREAN_LABEL_MIN_CHARS`, `contains_hangul`, `SECTION_HEADER_MAX_CHARS`, and `MAX_SECTIONS_PER_FILE` are all consumed inside `extract_sections_from_text` itself; the new extractors do not need per-cell length-guard logic the way csv does.

## Validation strategy

### `.hwp` (sub-project C) — real-corpus pass

After implementation passes unit tests, run the extractor against all 10 of the user's hand-made samples in `~/Desktop/Projects/doc_agent_test_data/hwp_samples/`. For each sample:

1. Confirm `status == "ok"`.
2. Confirm `text` contains Korean (at least one Hangul code point).
3. Confirm running `_sections.extract_sections_from_text(text)` over the result produces a non-empty tuple WHEN the source document contains bracket / colon / bullet headers — i.e., visually inspect each sample to know which patterns it contains, then assert the regex catches them.

Record findings in this spec under a "Real-corpus validation results" section appended at the end (mirrors the Korean A spec's pattern). Document any false negatives (visible structure that A's regexes miss) — those become inputs to the v2 numbered-header brainstorm or future refinements; they do NOT block this ship.

### `.hwpx` (sub-project B) — synthetic + harvested public docs

No user-provided `.hwpx` samples exist (user cannot create them). Two-pronged validation:

1. **Synthetic fixtures (in-test):** Build minimal valid `.hwpx` archives in test code via stdlib `zipfile` + `xml.etree.ElementTree`. Cover: empty body, single-paragraph, multi-paragraph, multi-section, namespace variants (2011 vs 2016 paragraph namespace), one-section-file-malformed, encrypted-flag-set rejection, missing-mimetype rejection.
2. **Harvested public corpus (under `tests/fixtures/hwp/`):** download 2–3 public-domain `.hwpx` documents from a Korean government open-data source (e.g., Korea Data Portal `data.go.kr` publishes `.hwpx`-formatted public records). Commit them to the repo under `tests/fixtures/hwp/` only if they are genuinely public-domain and small (< 50 KiB each). Their purpose is spot-check: real-world `.hwpx` produced by Hangul Office may have quirks the synthetic fixtures don't capture (unusual run nesting, comment annotations, etc.). If we can't find suitably-licensed small samples, skip this step and document the gap — the spec is still shippable, the gap goes in the post-ship follow-up checklist.

The implementation plan's first task is the harvest step (so subsequent tasks can write fixtures-aware tests). If harvest fails, plan task 0 records that and moves on; we do not block on it.

## Test plan

Mirroring the csv (~50 tests) and pptx (23 tests) patterns. Estimated counts (final numbers settled in plan tasks):

**HWPX tests** (`tests/test_organize_extractors.py`):
- `TestHwpxText` — happy path single-section, multi-section, empty body, namespace variants (2011 vs 2016), paragraph separators inserted between `<hp:p>`.
- `TestHwpxSections` — synthetic fixtures embedding bracket / colon / bullet headers in their text runs; assert reader-side `_sections` extraction picks them up.
- `TestHwpxFailure` — not-a-zip, missing-mimetype, wrong-mimetype, encrypted-flag-set, oversized-section, oversized-cumulative, malformed XML in one section but not all (partial recovery), HWPX with zero section files.
- `TestHwpxCaps` — `_HWPX_SECTION_FILES_MAX`, `_HWPX_SECTION_BYTES_MAX`, `_HWPX_TOTAL_BYTES_MAX` boundary tests.

**HWP tests** (`tests/test_organize_extractors.py`):
- `TestHwpText` — happy path against ≥1 real sample (committed), Korean text round-trips intact, multi-paragraph order preserved.
- `TestHwpSections` — at least one sample with a known `[제목]` or `제목:` header in body text; assert `_sections.extract_sections_from_text` picks it up.
- `TestHwpFailure` — non-OLE input (e.g., truncated bytes, plain text mislabeled as `.hwp`), oversized file, mojibake-sentinel detection.
- `TestHwpCaps` — `_HWP_BYTES_MAX` boundary.

**Reader integration** (`tests/test_organize_reader.py`): one test per format that confirms `CatalogEntry.sections` is populated end-to-end for a sample with known headers, and preserved as `()` on extractor failure.

**Constants registry** (`tests/test_organize_constraints.py`): register the four new memory-bound constants in the existing `CONSTS` check.

**Pipeline integration** (`tests/test_organize_pipeline.py`): add `.hwpx` and `.hwp` fixtures to the integration corpus.

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
   - Failure modes (non-OLE, v3, distributed, password, oversized, mojibake sentinel).
   - Reader integration test.
4. **Pipeline integration** + constants-registry update + final corpus pass.

Each step is a separate commit. Pre-commit hook enforces the full 113+net-new test suite.

## Out-of-scope follow-ups (post-ship)

- **csv-helpers cleanup PR** (already queued before this work; independent).
- **v2 numbered-header pattern in `_sections.py`** (deferred from Korean A; needs its own brainstorm).
- **`.hml` extractor** if real `.hml` files appear in the corpus.
- **Embedded-image OCR** for `.hwp(x)` — out for v1; would need an OCR runtime dep. Open Lion Chemtech corpus signal first.
- **defusedxml swap** for `.hwpx` parsing if benchmarks show stdlib `xml.etree` is acceptable in throughput. Defense-in-depth, not correctness.
- **Per-table section labeling** if Lion Chemtech `.hwp(x)` corpus shows tables-as-structure dominate (parallel to xlsx's `Sheet:<name>` pattern, but at the table level).
- **Real-corpus validation results section** appended to this spec post-ship.
