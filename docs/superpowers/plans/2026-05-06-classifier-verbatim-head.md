# Classifier Verbatim-Head Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover ≥95% per-class classifier accuracy on the Northwind test fixture by (A) tightening the Reader prompt to forbid invented type labels, (B) adding a 300-char verbatim slice of each file's text to `CatalogEntry` and surfacing it to both classifier stages, and (C) making filename-as-signal an explicit, prioritized input to the classifier prompts.

**Architecture:** Reader gains a deterministic Python helper that stores `extracted_text.lstrip()[:300]` on each `CatalogEntry`. Classifier's existing per-entry payload builder (`_entry_dict` in `classifier.py`) — used by both Stage A (taxonomy proposer) and Stage B (assigner) — adds the new field. Three SKILL.md prompts are updated: `file-summarizer` (forbid invented type labels), `taxonomy-proposer` (use verbatim slice + filename when sampling), `taxonomy-assigner` (explicit priority hierarchy `filename → verbatim → summary`). PlanBuilder, Executor, Verifier, and orchestration code are untouched.

**Tech Stack:** Python 3.9+, stdlib `dataclasses`, existing `fda.claude_backend` (Haiku for Reader, Sonnet for Classifier), `pytest`. No new dependencies.

**Spec source:** `docs/superpowers/specs/2026-05-06-classifier-verbatim-head-design.md`. Read it if any task is ambiguous.

**Python interpreter:** `python3` (the project's `requires-python = ">=3.9"`). On this checkout `/Users/john/.pyenv/versions/3.12.8/bin/python` does not exist; use `python3`.

**Test command (run after every code-changing step):**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

A pre-commit hook runs the same suite — never use `--no-verify`.

---

## How to read this plan

The plan is **9 tasks**. Steps inside each task are bite-sized (2-5 min). Dependency order is strict: do not start task N+1 until task N is committed and the suite is green.

**Constraints reaffirmed for every task:**

- Never modify `fda/organize/executor.py` or `fda/organize/verifier.py`.
- Never modify `fda/organize/planner.py` or `fda/organize/prompts.py`.
- New constants are defined exactly once in their owning module and added to the constraints test (`CONSTS` mapping).
- No hardcoded model IDs in `.py` files — model IDs only appear in SKILL.md frontmatter.
- One task ≈ one commit. Commit message follows the project style: `organize: <short verb>...` or `organize(<scope>): <short verb>...`.

---

## File map

### Modified files

```
fda/organize/models.py                                  # Task 1
fda/organize/reader.py                                  # Task 2
fda/organize/classifier.py                              # Task 5
fda/organize/skills/file-summarizer/SKILL.md            # Task 4
fda/organize/skills/taxonomy-proposer/SKILL.md          # Task 6
fda/organize/skills/taxonomy-assigner/SKILL.md          # Task 7

tests/test_organize_models.py                           # Task 1
tests/test_organize_reader.py                           # Task 2
tests/test_organize_constraints.py                      # Task 3
tests/test_organize_classifier.py                       # Tasks 5, 8
```

### New files

None.

---

### Task 1: Add `verbatim_head` field to `CatalogEntry`

**Files:**
- Modify: `fda/organize/models.py:75-86`
- Modify: `tests/test_organize_models.py` (add a test class at end of file)

The field must default to `""` so existing constructors (real ones in `reader.py` and test fixtures in `tests/test_organize_classifier.py:_entry`) keep working without arg changes.

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_organize_models.py`:

```python
class TestCatalogEntryVerbatimHead:
    def test_default_is_empty_string(self):
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
        assert e.verbatim_head == ""

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
            verbatim_head="Order ID: 10488\n\nShipping Details:",
        )
        assert e.verbatim_head == "Order ID: 10488\n\nShipping Details:"
```

- [ ] **Step 2: Run the test to verify it fails.**

```bash
python3 -m pytest tests/test_organize_models.py::TestCatalogEntryVerbatimHead -v
```

Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'verbatim_head'` (and the no-arg case can't assert on a non-existent attribute).

- [ ] **Step 3: Add the field to `CatalogEntry`.**

Edit `fda/organize/models.py`. Find the existing `CatalogEntry` dataclass (around line 75) and add `verbatim_head: str = ""` as the last field. Result:

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
```

- [ ] **Step 4: Run the test to verify it passes.**

```bash
python3 -m pytest tests/test_organize_models.py::TestCatalogEntryVerbatimHead -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite to catch regressions.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. The default `""` keeps every existing `CatalogEntry(...)` call working.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/models.py tests/test_organize_models.py
git commit -m "$(cat <<'EOF'
organize(models): add CatalogEntry.verbatim_head with default ""

Backing field for the upcoming verbatim-head signal that Reader will
populate and the Classifier will consume. Defaults to empty so existing
constructors remain backward-compatible.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Reader populates `verbatim_head` from extracted text

**Files:**
- Modify: `fda/organize/reader.py` (add constant + helper, populate field in `_summarize_one`)
- Modify: `tests/test_organize_reader.py` (add a test class at end of file)

The slice is computed in Python after `_extractors.extract(path)` returns, **before** the Haiku call. Rule: `text.lstrip()[:VERBATIM_HEAD_CHARS]` where `VERBATIM_HEAD_CHARS = 300`. Empty string when extraction did not return `status == "ok"` or returned no text.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_organize_reader.py`:

```python
# ---------------------------------------------------------------------------
# verbatim_head: deterministic Python slice of extracted text
# ---------------------------------------------------------------------------


class TestVerbatimHead:
    def test_populated_with_lstripped_first_chars(
        self, workspace, fake_backend, logger
    ):
        """Reader stores extracted_text.lstrip()[:VERBATIM_HEAD_CHARS] on
        the catalog entry — leading whitespace removed, newlines preserved
        inside the slice."""
        from fda.organize import reader

        body = (
            "\n\n  \n"
            "Order ID: 10488\n"
            "\n"
            "Shipping Details:\n"
            "Ship Name: Frankenversand\n"
        )
        (workspace / "a.txt").write_text(body)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.verbatim_head.startswith("Order ID: 10488")
        assert "\n" in e.verbatim_head  # internal newlines preserved
        assert not e.verbatim_head.startswith("\n")
        assert not e.verbatim_head.startswith(" ")

    def test_capped_at_constant(self, workspace, fake_backend, logger):
        """The slice never exceeds VERBATIM_HEAD_CHARS characters."""
        from fda.organize import reader

        big = "x" * (reader.VERBATIM_HEAD_CHARS * 4)
        (workspace / "big.txt").write_text(big)
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert len(e.verbatim_head) == reader.VERBATIM_HEAD_CHARS

    def test_empty_on_extraction_failure(self, workspace, logger):
        """When the backend errors out (summary_failed=True), the entry
        still carries an empty verbatim_head — never None."""
        from fda.organize import reader

        backend = MagicMock()
        backend.complete.side_effect = RuntimeError("boom")
        (workspace / "a.txt").write_text("Order ID: 10488\nShipping Details:\n")
        catalog = reader.read(workspace, backend=backend, logger=logger)
        e = catalog.entries[0]
        assert e.summary_failed is True
        assert e.verbatim_head == ""

    def test_empty_for_unsupported_extension(self, workspace, fake_backend, logger):
        """No extractor → extract_status='no_extractor' → verbatim_head is ''."""
        from fda.organize import reader

        (workspace / "a.zzz").write_bytes(b"\x00\x01\x02\x03")
        catalog = reader.read(workspace, backend=fake_backend, logger=logger)
        e = catalog.entries[0]
        assert e.extract_status == "no_extractor"
        assert e.verbatim_head == ""
```

- [ ] **Step 2: Run the tests to verify they fail.**

```bash
python3 -m pytest tests/test_organize_reader.py::TestVerbatimHead -v
```

Expected: FAIL — `AttributeError: module 'fda.organize.reader' has no attribute 'VERBATIM_HEAD_CHARS'` and entries' `verbatim_head` is always `""` (so the lstrip test fails too).

- [ ] **Step 3: Add the constant and helper to `reader.py`.**

Edit `fda/organize/reader.py`. Add the constant alongside the other module-level constants (around line 32, after `READER_WORKER_COUNT = 8`):

```python
READER_WORKER_COUNT = 8
VERBATIM_HEAD_CHARS = 300
```

Add the helper near the top, just below `_TRUNCATE_MARKER`:

```python
def _verbatim_head(extraction: ExtractionResult) -> str:
    """Deterministic head slice for the Classifier's grounding signal.

    Returns extracted_text.lstrip()[:VERBATIM_HEAD_CHARS] when extraction
    succeeded; "" otherwise. No LLM call.
    """
    if extraction.status != "ok" or not extraction.text:
        return ""
    return extraction.text.lstrip()[:VERBATIM_HEAD_CHARS]
```

- [ ] **Step 4: Populate the field in `_summarize_one`.**

In `fda/organize/reader.py`, find `_summarize_one` (around line 91). After `extraction = _extractors.extract(path)` (line 104), compute the slice once and pass it through to the success-case `CatalogEntry` constructor. Modify the success-path return (around lines 131-145) to include the new field:

```python
def _summarize_one(
    path: Path,
    *,
    backend,
    skill: _skills.SkillConfig,
    timeout_seconds: float,
) -> tuple[CatalogEntry, str, str]:
    """Returns (entry, log_event_kind, detail).

    log_event_kind is 'done', 'timeout', or 'fail'.
    detail carries a human-readable description for failure logs.
    """
    size = path.stat().st_size
    extraction = _extractors.extract(path)
    head = _verbatim_head(extraction)
    user = _build_user_message(path, extraction, size)
    try:
        raw = backend.complete(
            system=skill.body,
            messages=[{"role": "user", "content": user}],
            model=skill.model,
            max_tokens=512,
            temperature=0.0,
            timeout=timeout_seconds,
        )
    except TimeoutError as e:
        return _fail_entry(path, size, extraction.status, str(e)), "timeout", str(e)
    except Exception as e:  # noqa: BLE001 — never abort a run because one file fails
        return _fail_entry(path, size, extraction.status, str(e)), "fail", str(e)

    try:
        parsed = json.loads(raw)
        type_label = str(parsed.get("type_label", ""))[:32]
        summary = str(parsed.get("summary", ""))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return (
            _fail_entry(path, size, extraction.status, "unparseable summary"),
            "fail",
            "unparseable summary",
        )

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
        ),
        "done",
        "",
    )
```

Note: `_fail_entry` and `_junk_entry` already construct entries without `verbatim_head` — the dataclass default `""` covers them. **Do not** modify those helpers.

The final-sort loop near the end of `read()` (around lines 264-277) re-builds entries to assign `path_id`. It must carry `verbatim_head` through. Edit that comprehension:

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
        )
        for idx, e in enumerate(ordered)
    )
```

- [ ] **Step 5: Run the new tests to verify they pass.**

```bash
python3 -m pytest tests/test_organize_reader.py::TestVerbatimHead -v
```

Expected: PASS for all four cases.

- [ ] **Step 6: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 7: Commit.**

```bash
git add fda/organize/reader.py tests/test_organize_reader.py
git commit -m "$(cat <<'EOF'
organize(reader): populate CatalogEntry.verbatim_head from extracted text

Adds VERBATIM_HEAD_CHARS=300 and a deterministic Python helper that
stores extracted_text.lstrip()[:300] on each catalog entry. No LLM call.
Empty when extraction did not return status='ok'. Threaded through the
final path_id-assignment rebuild so the field survives the sort.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Register `VERBATIM_HEAD_CHARS` in the constraints test

**Files:**
- Modify: `tests/test_organize_constraints.py:51-71`

The named-constant check (`TestEachConstantHasOneHome.test_constant_defined_in_owning_module`) iterates over the `CONSTS` dict and asserts each entry is defined in its owning module. Adding `VERBATIM_HEAD_CHARS` keeps the surface area honest. We do **not** add `300` to `DISTINCTIVE_LITERALS` because `READER_TOTAL_TIMEOUT_SECONDS = 300` already lives in `reader.py` (semantically a seconds timeout, not a chars cap), so a literal-uniqueness check would be ambiguous — see the spec's section 6.

- [ ] **Step 1: Add the entry.**

Edit `tests/test_organize_constraints.py`. In the `CONSTS` dict (lines 51-71), add a new entry. The dict is alphabetical-ish but mixed; place the new entry next to the other reader entries:

```python
    CONSTS = {
        # constant -> (owning module relative path, literal source as in code)
        "READER_TEXT_CAP_BYTES": ("reader.py", "64 * 1024"),
        "READER_PER_FILE_TIMEOUT_SECONDS": ("reader.py", "30"),
        "READER_TOTAL_TIMEOUT_SECONDS": ("reader.py", "300"),
        "READER_WORKER_COUNT": ("reader.py", "8"),
        "VERBATIM_HEAD_CHARS": ("reader.py", "300"),
        "TAXONOMY_SAMPLE_FULL_THRESHOLD": ("classifier.py", "150"),
        # ... (rest unchanged)
```

- [ ] **Step 2: Run the constraints suite.**

```bash
python3 -m pytest tests/test_organize_constraints.py -v
```

Expected: PASS (the constant was defined in `reader.py` during Task 2; this test merely registers it).

- [ ] **Step 3: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 4: Commit.**

```bash
git add tests/test_organize_constraints.py
git commit -m "$(cat <<'EOF'
organize(constraints): register VERBATIM_HEAD_CHARS in CONSTS check

Adds the new reader.py tunable to the named-constant-defined check.
Not added to DISTINCTIVE_LITERALS because READER_TOTAL_TIMEOUT_SECONDS
already uses literal 300 in the same module (seconds vs chars — distinct
units), so literal-uniqueness would be ambiguous.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Tighten `file-summarizer/SKILL.md` to forbid invented type labels (A)

**Files:**
- Modify: `fda/organize/skills/file-summarizer/SKILL.md`

The current Rules block invites Haiku to assert a `type_label` and a confident prose `summary` even when the document text carries no explicit label. We add explicit forbidance and replacement guidance.

- [ ] **Step 1: Edit the SKILL body.**

Open `fda/organize/skills/file-summarizer/SKILL.md`. Replace the existing `Rules:` block (lines 24-29) with this expanded block. Keep everything else (frontmatter, input description, output schema, examples) unchanged.

Original block to replace:

```
Rules:
- Output ONLY the JSON object. No prose, no markdown fences.
- `type_label` is a short tag like `invoice`, `meeting-notes`, `python-source`, `image`, `archive`. Lowercase-with-dashes.
- `summary` is one sentence. Do not exceed 200 characters. Do not include the file path.
- You DO NOT decide whether the file is junk. Junk handling happens elsewhere.
- If the file is impossible to classify (corrupt, empty, opaque), set `type_label` to `unknown` and write a one-sentence summary saying so.
```

New block:

```
Rules:
- Output ONLY the JSON object. No prose, no markdown fences.
- `type_label` is a short tag like `invoice`, `meeting-notes`, `python-source`, `image`, `archive`. Lowercase-with-dashes.
- `summary` is one sentence. Do not exceed 200 characters. Do not include the file path.
- You DO NOT decide whether the file is junk. Junk handling happens elsewhere.
- If the file is impossible to classify (corrupt, empty, opaque), set `type_label` to `unknown` and write a one-sentence summary saying so.

Type-label grounding rules — important:
- DO NOT name a document type unless that exact phrase appears verbatim in the
  extracted text. If the text says "Purchase Orders" or "Invoice" at the top,
  use it. If the text only says "Order ID: …" with shipping/customer/shipper
  sections and no explicit type word, do not call it a "purchase order" or a
  "shipping order" — describe its structure instead.
- When no type label is present, prefer a neutral structural `summary` such as
  "Order document with shipping, customer, employee, shipper, products, and
  shipped-date sections, dated 2017-03-27, total 1560.0." Use a generic
  `type_label` such as `order-document` or `unknown`, NOT `purchase-order` or
  `shipping-order`.
- A downstream classifier sees a separate verbatim slice of the file's text
  for grounding. Your job is neutral, accurate description — not confident
  guessing. Hallucinated type labels harm classification accuracy more than
  a vague but correct summary does.
```

- [ ] **Step 2: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass. (No automated test asserts the file-summarizer prompt body; this is a review-only change.)

- [ ] **Step 3: Commit.**

```bash
git add fda/organize/skills/file-summarizer/SKILL.md
git commit -m "$(cat <<'EOF'
organize(prompts): forbid invented document-type labels in file-summarizer

Reader's Haiku call previously confabulated 'purchase order' / 'shipping
order' summaries for documents whose text carried no explicit type label
(e.g., true shipping orders that only contain 'Order ID: …' plus
structural sections). Adds explicit grounding rules: name a type only
when the phrase appears verbatim in the text; otherwise emit a neutral
structural summary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Classifier `_entry_dict` includes `verbatim_head` (wire format change for both stages)

**Files:**
- Modify: `fda/organize/classifier.py:90-100`
- Modify: `tests/test_organize_classifier.py` (add a test class)

`_entry_dict` is the single per-entry payload builder used by both Stage A (`_build_proposer_prompt`) and Stage B (`_build_assigner_prompt`). Updating it once threads `verbatim_head` into both stages' JSON payloads.

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_organize_classifier.py`:

```python
# ---------------------------------------------------------------------------
# Wire format: verbatim_head present in classifier payloads
# ---------------------------------------------------------------------------


class TestVerbatimHeadInPayload:
    def test_assigner_batch_payload_carries_verbatim_head(self, logger):
        """The Stage B per-batch JSON payload must include each entry's
        verbatim_head alongside path, summary, etc."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        captured: dict[str, str] = {}

        def capture(*, system, messages, **kwargs):
            payload = messages[0]["content"]
            captured["payload"] = payload
            # Distinguish Stage A (CATALOG) from Stage B (BATCH).
            if '"BATCH"' in payload:
                return _assignment_payload([("f000", "Texts")])
            return _taxonomy_payload(["Texts"])

        backend = MagicMock()
        backend.complete.side_effect = capture

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/0fa84d61b3158eaba46dee96.pdf",
            ext=".pdf",
            size_bytes=2686,
            summary="Order document with shipping sections.",
            type_label="order-document",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head="Order ID: 10488\n\nShipping Details:\nShip Name: Frankenversand",
        )
        cat = _catalog([e])
        classifier.classify(cat, "sort", backend=backend, logger=logger)

        # The captured payload from the *last* call (Stage B) should mention
        # the verbatim_head field name AND the slice content.
        assert "verbatim_head" in captured["payload"]
        assert "Order ID: 10488" in captured["payload"]
        assert "Shipping Details" in captured["payload"]

    def test_proposer_sample_payload_carries_verbatim_head(self, logger):
        """The Stage A sample JSON payload must include verbatim_head per
        sampled entry."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        captured: dict[str, str] = {}

        def capture(*, system, messages, **kwargs):
            payload = messages[0]["content"]
            if '"CATALOG"' in payload and '"BATCH"' not in payload:
                captured["stage_a"] = payload
                return _taxonomy_payload(["Texts"])
            return _assignment_payload([("f000", "Texts")])

        backend = MagicMock()
        backend.complete.side_effect = capture

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/Invoice_10488.pdf",
            ext=".pdf",
            size_bytes=1024,
            summary="Order document for ACME Corp.",
            type_label="order-document",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head="Order ID: 10488\nCustomer: ACME",
        )
        cat = _catalog([e])
        classifier.classify(cat, "sort", backend=backend, logger=logger)

        assert "verbatim_head" in captured["stage_a"]
        assert "Order ID: 10488" in captured["stage_a"]
```

- [ ] **Step 2: Run the new tests to verify they fail.**

```bash
python3 -m pytest tests/test_organize_classifier.py::TestVerbatimHeadInPayload -v
```

Expected: FAIL — assertions on `"verbatim_head" in captured["payload"]` fail because `_entry_dict` doesn't emit the field yet.

- [ ] **Step 3: Add the field to `_entry_dict`.**

Edit `fda/organize/classifier.py`. Replace the existing `_entry_dict` (lines 90-100):

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
    }
```

- [ ] **Step 4: Run the new tests to verify they pass.**

```bash
python3 -m pytest tests/test_organize_classifier.py::TestVerbatimHeadInPayload -v
```

Expected: PASS for both cases.

- [ ] **Step 5: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit.**

```bash
git add fda/organize/classifier.py tests/test_organize_classifier.py
git commit -m "$(cat <<'EOF'
organize(classifier): include verbatim_head in per-entry payload

_entry_dict now carries the new field through to both Stage A's CATALOG
and Stage B's BATCH JSON payloads. Two wire-format tests pin the
behavior. Cost: ~7K extra tokens to one Stage A call; ~200K extra tokens
spread across all of Stage B for a 10K-file run — negligible.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Update `taxonomy-proposer/SKILL.md` (Stage A: verbatim slice + filename signal)

**Files:**
- Modify: `fda/organize/skills/taxonomy-proposer/SKILL.md`

The Stage A prompt must (i) document the new `verbatim_head` field in its input schema, (ii) advise using filename signal when informative, and (iii) advise using the verbatim slice to perceive structural diversity. The existing constraint test (`test_proposer_does_not_reference_path_ids_for_assignments`) requires the `Do NOT emit any 'path_id' references` line to remain — keep it.

- [ ] **Step 1: Edit the input description.**

Replace lines 9-13 of `fda/organize/skills/taxonomy-proposer/SKILL.md`:

Original:

```
You will receive (in the user message):
- USER_INSTRUCTIONS: free-form guidance from the operator (may be empty)
- CATALOG (JSON): a list of entries. Each entry has fields
  `path_id`, `path`, `ext`, `size_bytes`, `summary`, `type_label`, `extract_status`
  Summaries have already been truncated to 200 characters.
```

New:

```
You will receive (in the user message):
- USER_INSTRUCTIONS: free-form guidance from the operator (may be empty)
- CATALOG (JSON): a list of entries. Each entry has fields
  `path_id`, `path`, `ext`, `size_bytes`, `summary`, `type_label`,
  `extract_status`, `verbatim_head`.
  - `summary` is Reader's prose description (truncated to 200 characters).
  - `verbatim_head` is the raw first ~300 chars of the file's extracted
    text, with leading whitespace stripped. It is NOT a summary — it's
    actual file content. Empty string if extraction failed.
  - The last component of `path` is the filename.
```

- [ ] **Step 2: Add a "How to use these signals" section before the existing `Rules:` block.**

Find the line `Your job is to produce a TAXONOMY:` (line 15) and add a new section between the input description and that paragraph. Insert this block after the input description (right before line 15):

```
How to use these signals when proposing categories:

- **Filename as signal.** When sampling, examine basenames. If many files
  share an informative naming convention (e.g. `Invoice_*.pdf`, `PO_*.pdf`,
  `Q3_Sales_Report_*.xlsx`), let that inform what categories to propose.
  Ignore basenames that are hashes, random IDs, or generic placeholders
  (e.g. `0fa84d61b3158eaba46dee96.pdf`, `doc1.pdf`, `Untitled.pdf`); rely on
  `verbatim_head` and `summary` for those.
- **Verbatim slice as signal.** Use `verbatim_head` to perceive structural
  diversity in the corpus. If many summaries describe similar "order
  documents" but the slices show distinct templates — some begin with
  `"Purchase Orders"`, others with `"Invoice"`, others with `"Order ID:"`
  followed by a Shipping Details section — propose distinct categories
  accordingly.
- **Trust verbatim over prose.** When `summary` and `verbatim_head` disagree
  about what kind of document this is, the slice is the source of truth.

```

- [ ] **Step 3: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass — including `test_proposer_does_not_reference_path_ids_for_assignments` which checks the `Do NOT emit any 'path_id' references` line still exists.

- [ ] **Step 4: Commit.**

```bash
git add fda/organize/skills/taxonomy-proposer/SKILL.md
git commit -m "$(cat <<'EOF'
organize(prompts): teach taxonomy-proposer to use verbatim_head + filename

Documents the new verbatim_head input field and adds explicit guidance on
when to use filename signal (informative basenames) vs ignore it (hashes
/ random IDs). Tells the proposer to trust verbatim over prose summary
when they disagree about document type.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Update `taxonomy-assigner/SKILL.md` (Stage B: priority hierarchy `filename → verbatim → summary`)

**Files:**
- Modify: `fda/organize/skills/taxonomy-assigner/SKILL.md`

Stage B is the per-file decision point. The prompt gets an explicit numbered priority hierarchy. The existing constraint test (`test_assigner_emits_path_ids`) requires `path_id` and `EXACTLY ONCE` (case-insensitive) to remain — keep them.

- [ ] **Step 1: Edit the input description.**

Replace lines 14-16 of `fda/organize/skills/taxonomy-assigner/SKILL.md`:

Original:

```
- BATCH (JSON): a list of file entries. Each has `path_id`, `path`, `ext`,
  `size_bytes`, `summary`, `type_label`, `extract_status`. Summaries have
  been truncated to 200 chars.
```

New:

```
- BATCH (JSON): a list of file entries. Each has `path_id`, `path`, `ext`,
  `size_bytes`, `summary`, `type_label`, `extract_status`, `verbatim_head`.
  - `path` is the full file path; the last component is the filename.
  - `summary` is Reader's prose, truncated to 200 chars; may be imprecise
    about document type.
  - `verbatim_head` is the raw first ~300 chars of extracted text, leading
    whitespace stripped. Empty string if extraction failed.
```

- [ ] **Step 2: Add the signal-priority block before `OUTPUT FORMAT`.**

Find the line `OUTPUT FORMAT (single JSON object, no prose, no markdown fences):` (line 22) and insert this block immediately above it:

```
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
   Make the decision from `verbatim_head` and `summary` alone. When in
   doubt about whether a filename is informative, default to ignoring it
   rather than over-weighting it.
3. **Within `verbatim_head` + `summary`:** if they disagree about document
   type, trust `verbatim_head`. Look for literal type labels in the slice
   (e.g. `"Invoice"`, `"Purchase Orders"`, `"Statement"`, `"Receipt"`)
   before falling back to prose. The summary may have been generated
   without a type label visible in the document and may have guessed.
4. **Empty `verbatim_head` (extraction failed):** use `summary` only.

```

- [ ] **Step 3: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass — including `test_assigner_emits_path_ids` which checks `path_id` and `EXACTLY ONCE` are present.

- [ ] **Step 4: Commit.**

```bash
git add fda/organize/skills/taxonomy-assigner/SKILL.md
git commit -m "$(cat <<'EOF'
organize(prompts): add filename → verbatim → summary priority to assigner

Explicit numbered priority hierarchy for Stage B: trust an informative
filename, ignore a hash/random/generic filename, then prefer
verbatim_head over summary on type-label conflicts, finally fall back
to summary when extraction failed. Documents the new verbatim_head
field in the BATCH input schema.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Classifier regression tests (conflict, informative-filename, hash-filename)

**Files:**
- Modify: `tests/test_organize_classifier.py` (add a test class at end of file)

These are end-to-end tests on the classifier with a stubbed backend. They verify that **with our wire format**, a backend that follows the new prompt's priority returns the expected category. They do not (cannot) test live Sonnet judgment — that's covered by Task 9's manual validation.

The fake backend uses `side_effect` with a function that inspects the captured Stage B payload and returns the expected assignment based on what the prompt says to do.

- [ ] **Step 1: Write the three failing tests.**

Append to `tests/test_organize_classifier.py`:

```python
# ---------------------------------------------------------------------------
# Behavioral regressions: conflict, informative-filename, hash-filename
#
# These verify the wire format AND the end-to-end routing produced by a
# backend that follows the assigner prompt's priority hierarchy.
# ---------------------------------------------------------------------------


class TestPriorityRegressions:
    def _make_backend(self, taxonomy_categories, decide):
        """Return a MagicMock backend that:
          - returns the given Stage A taxonomy on the first call (whose
            payload contains 'CATALOG' but not 'BATCH'),
          - delegates Stage B per-file assignment to `decide(payload) -> {pid:
            category}` for each call whose payload contains 'BATCH'.
        """
        backend = MagicMock()

        def respond(*, system, messages, **kwargs):
            payload = messages[0]["content"]
            if '"BATCH"' in payload:
                mapping = decide(payload)
                return _assignment_payload(list(mapping.items()))
            return _taxonomy_payload(taxonomy_categories)

        backend.complete.side_effect = respond
        return backend

    def test_conflict_summary_says_PO_but_verbatim_says_shipping(self, logger):
        """Reader summary calls it a purchase order, but verbatim_head
        clearly shows shipping-order content. A backend following the
        prompt picks Shipping-And-Fulfillment."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/0fa84d61b3158eaba46dee96.pdf",
            ext=".pdf",
            size_bytes=2686,
            summary="Purchase order #10488 for Frankenversand in Munich.",
            type_label="purchase-order",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head=(
                "Order ID: 10488\n\nShipping Details:\n"
                "Ship Name: Frankenversand\nShipper Name: United Package\n"
                "Shipped Date: 2017-04-02"
            ),
        )

        def decide(payload: str) -> dict[str, str]:
            # The prompt says: ignore hash basenames; prefer verbatim_head over
            # summary on conflict; verbatim_head shows shipping content → ship.
            return {"f000": "Shipping-And-Fulfillment"}

        backend = self._make_backend(
            ["Purchase-Orders", "Shipping-And-Fulfillment"], decide,
        )
        result = classifier.classify(
            _catalog([e]), "sort", backend=backend, logger=logger,
        )
        cats = {g.category for g in result.items}
        assert "Shipping-And-Fulfillment" in cats
        assert "Purchase-Orders" not in cats

    def test_informative_filename_drives_routing(self, logger):
        """Filename `Invoice_10488.pdf` is the strongest signal even though
        verbatim_head doesn't contain the literal word 'Invoice'."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/Invoice_10488.pdf",
            ext=".pdf",
            size_bytes=1024,
            summary="Order document for ACME Corp with line items and total.",
            type_label="order-document",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head="Order ID: 10488\nCustomer: ACME\nTotal: 1234.5",
        )

        def decide(payload: str) -> dict[str, str]:
            # The prompt says: informative filename ('Invoice_10488') is a
            # strong signal even when verbatim_head lacks the literal word.
            return {"f000": "Sales-Invoices"}

        backend = self._make_backend(
            ["Sales-Invoices", "Purchase-Orders"], decide,
        )
        result = classifier.classify(
            _catalog([e]), "sort", backend=backend, logger=logger,
        )
        assert any(g.category == "Sales-Invoices" for g in result.items)

    def test_hash_filename_ignored_routing_from_content(self, logger):
        """Hash filename carries no signal — routing must come from the
        verbatim slice ('Monthly Stock Report') and summary."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/0fa84d61b3158eaba46dee96.pdf",
            ext=".pdf",
            size_bytes=1500,
            summary="Stock report for beverages with units sold and in stock.",
            type_label="stock-report",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head=(
                "Monthly Stock Report\nCategory: Beverages\n"
                "Units Sold: 240\nUnits In Stock: 1200"
            ),
        )

        def decide(payload: str) -> dict[str, str]:
            # The prompt says: hash basename → ignore; rely on verbatim_head
            # ('Monthly Stock Report') + summary → Stock-Reports.
            return {"f000": "Stock-Reports"}

        backend = self._make_backend(
            ["Stock-Reports", "Sales-Invoices"], decide,
        )
        result = classifier.classify(
            _catalog([e]), "sort", backend=backend, logger=logger,
        )
        assert any(g.category == "Stock-Reports" for g in result.items)
```

- [ ] **Step 2: Run the new tests to verify they pass.**

```bash
python3 -m pytest tests/test_organize_classifier.py::TestPriorityRegressions -v
```

Expected: PASS for all three. (After Task 5 the wire format already includes `verbatim_head`, and the fake backend simply enacts the prompt's priority — no extra production code is needed.)

- [ ] **Step 3: Run the full suite.**

```bash
python3 -m pytest tests/ -x -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 4: Commit.**

```bash
git add tests/test_organize_classifier.py
git commit -m "$(cat <<'EOF'
organize(classifier): regression tests for verbatim/filename priority

Three end-to-end stub-backend tests pinning the assigner contract:
1. verbatim_head wins over a misleading summary (the canonical Northwind
   shipping-order misclassification scenario);
2. an informative filename ('Invoice_10488.pdf') drives routing even
   when verbatim_head lacks the literal type word;
3. a hash basename is ignored; routing comes from verbatim + summary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Manual validation against the Northwind fixture

**Files:** none — this is a measurement task, not a code task.

The Northwind fixture at `/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003/` contains 100 documents plus `manifest.csv`. Ground truth from manifest: 30 invoices, 30 PurchaseOrders, 34 Shipping orders, 6 monthly-Category (stock reports). Acceptance threshold: ≥95% per-class accuracy and ≤5 misclassifications across all 100 documents.

**Important:** the fixture was already mutated by the prior `--apply` run on 2026-05-06. Files were moved into `Finance/`, `Procurement/`, `Operations/`, `Reference/`, `Misc/` subdirs. **Restore or regenerate the fixture before running.** If the user has a regenerator script, ask them to run it. Otherwise reset the fixture using the manifest:

```bash
python3 -c "
import csv, shutil
from pathlib import Path
mp = '/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003/Reference/Data-Manifests/manifest.csv'
if not Path(mp).exists():
    mp = '/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003/manifest.csv'
base = Path('/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003')
with open(mp) as f:
    for row in csv.DictReader(f):
        target = Path(row['randomized_path'])
        if target.exists():
            continue
        # Find the file under base by its basename (the apply phase preserved names)
        matches = list(base.rglob(target.name))
        if matches:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(matches[0]), str(target))
print('done')
"
```

- [ ] **Step 1: Confirm a fresh fixture is in place.**

```bash
find /tmp/fda-test-sets/randomized-company-documents-2026-05-05-003 -name '*.pdf' | wc -l
```

Expected: `100`.

```bash
find /tmp/fda-test-sets/randomized-company-documents-2026-05-05-003 -mindepth 1 -maxdepth 1 -type d | sort
```

Expected: 10 directories named `folder_1` through `folder_10`. If you see `Finance`, `Procurement`, `Operations`, etc., the fixture is still mutated from the prior run — restore it before continuing. If unsure, ask the user.

- [ ] **Step 2: Run the pipeline in apply mode.**

```bash
python3 scripts/diag_organize.py /tmp/fda-test-sets/randomized-company-documents-2026-05-05-003 --apply 2>&1 | tail -30
```

Expected: a final line `RESULT plan_ops=N outcomes={'applied': N} ...` and `Log: /tmp/fda-organize-diag-<timestamp>.log`.

Capture both log paths:
- Diag wrapper log: `/tmp/fda-organize-diag-<timestamp>.log`
- Structured pipeline log: `~/.fda/logs/organize/<timestamp>-randomized-company-documents-2026-05-05-003.log`

- [ ] **Step 3: Score the run against the manifest.**

```bash
python3 -c "
import csv
from pathlib import Path

base = Path('/tmp/fda-test-sets/randomized-company-documents-2026-05-05-003')
mp = base / 'Reference' / 'Data-Manifests' / 'manifest.csv'
if not mp.exists():
    mp = base / 'manifest.csv'

# Map class label (true folder name) -> list of (basename, current_subpath)
true_class = {}
with open(mp) as f:
    for row in csv.DictReader(f):
        rand = Path(row['randomized_path']).name
        orig = Path(row['original_path']).parent.name
        true_class[rand] = orig

# Reverse-look up where each file ended up under base
post = {}
for p in base.rglob('*.pdf'):
    rel = p.relative_to(base)
    post[p.name] = '/'.join(rel.parts[:-1])  # subdir(s) under base
# Score
buckets = {}
for name, true in true_class.items():
    cur = post.get(name, '<missing>')
    buckets.setdefault(true, {}).setdefault(cur, 0)
    buckets[true][cur] += 1
total = correct = 0
print(f'{\"true class\":<22}  {\"current subpath\":<55}  count')
print('-' * 90)
for true in sorted(buckets):
    for cur, n in sorted(buckets[true].items(), key=lambda x: -x[1]):
        total += n
        # Heuristic: 'correct' = current subpath contains the head of the true class name
        head = true.replace(' ', '').lower()[:6]
        if head in cur.lower().replace('-','').replace('_',''):
            correct += n
        print(f'{true:<22}  {cur:<55}  {n}')
acc = (correct / total) if total else 0.0
print(f'\nApprox accuracy (heuristic): {correct}/{total} = {acc:.1%}')
"
```

The heuristic accuracy printer matches a 6-character head of the true class name against the current subpath (e.g. `shippi` → matches `Operations/Shipping-And-Fulfillment`). It's a sanity guide, not a strict scorer — eyeball the printed table and verify each true class has its files concentrated in one expected subpath.

- [ ] **Step 4: Compare to acceptance threshold.**

Acceptance: ≥95% per-class accuracy AND ≤5 total misclassifications across the 100 PDFs (excluding `manifest.csv` itself).

If the run **passes**: report success to the user with both log paths. Done.

If the run **falls short**: capture the structured log path and the misclassified files, then escalate. Do not retry blindly — re-running the same code will produce the same drift (Sonnet is deterministic at temperature=0). Likely follow-ups (out of scope for this plan):

- Increase `VERBATIM_HEAD_CHARS` from 300 to 500 if the discriminator is later in the document.
- Add a tail-of-document slice (covered as a non-goal in the spec; revisit if needed).
- Move to taxonomy-aware markers (Stage A outputs literal markers per category).

- [ ] **Step 5: Document the result.**

This task does not produce a commit unless you choose to amend the spec with measured numbers. If you do, update the spec's "Manual validation (post-merge)" section with the actual accuracy and commit:

```bash
git add docs/superpowers/specs/2026-05-06-classifier-verbatim-head-design.md
git commit -m "$(cat <<'EOF'
organize(spec): record verbatim-head measured accuracy on Northwind fixture

[fill in: accuracy summary, misclassification list]

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If the result is reportable but not commit-worthy (e.g., expected and unsurprising), simply share the numbers with the user and exit.
