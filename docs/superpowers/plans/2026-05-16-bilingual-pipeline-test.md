# Bilingual Pipeline Test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tooling for one sandboxed FDA organize run on a ~300-file bilingual corpus that produces evidence for the 4 test goals and resolves backlog #4 (over-fragmentation) and #5 (is the `s3` router branch dead).

**Architecture:** A new generator script writes two persistent, reusable source folders under `doc_agent_test_data/` (Korean business docs + realistic junk) with a ground-truth CSV; the existing `randomize_fda_fixture.py` pools all sources into one hash-renamed fixture; the existing `diag_organize.py` runs the pipeline under a sandboxed `FDA_HOME`; an extended `evaluate_fda_fixture.py` prints a plain scorecard graded by eye. This plan builds the tooling and artifacts only — it does NOT execute the expensive ~300-file pipeline run (operator-driven, real Claude backend).

**Tech Stack:** Python 3.12 (`.venv/bin/python`), `python-docx`, `reportlab` (`.venv` only, not `pyproject.toml`), `pytest`, FDA's own `fda.organize._extractors.extract`, SQLite FTS5.

**Spec:** `docs/superpowers/specs/2026-05-16-bilingual-pipeline-test-design.md`

---

## File Structure

| Path | Responsibility |
|---|---|
| `scripts/build_korean_test_sources.py` (create) | Deterministic generator: Korean docs (`.docx/.pdf/.txt`), `ground_truth.csv`, realistic junk; PDF font resolution + faithful self-verify. |
| `tests/test_build_korean_test_sources.py` (create) | Unit + smoke tests for the generator's pure logic and self-verify hard-fail. |
| `scripts/evaluate_fda_fixture.py` (modify) | Add `--fda-home`, bucket-size histogram, "S3 fired?" line + router precondition, `metadata.db` Korean-search check, Hangul-label scan. |
| `tests/test_evaluate_fda_fixture.py` (modify) | Tests for each new evaluator helper. |
| `docs/superpowers/specs/2026-05-16-business-context.draft.ko.md` (create) | Korean `business_context.md` draft (folder-granularity + doc-type rules); user edits before the run. |
| `docs/superpowers/plans/2026-05-16-bilingual-pipeline-test-eyeball-notes.md` (create) | Post-run verdict skeleton for #4 and #5. |

**Convention:** scripts are standalone single files (matching `randomize_fda_fixture.py`, `diag_organize.py`). Tests import them via `importlib.util.spec_from_file_location` exactly as `tests/test_evaluate_fda_fixture.py:9-24` already does. Run the suite with `.venv/bin/python -m pytest tests/ -x -q --tb=short`.

---

## Task 0: Install reportlab into .venv

**Files:** none (environment only)

- [ ] **Step 1: Confirm reportlab is absent**

Run: `.venv/bin/python -c "import reportlab" 2>&1`
Expected: `ModuleNotFoundError: No module named 'reportlab'`

- [ ] **Step 2: Install into .venv only**

Run: `.venv/bin/pip install reportlab`
Expected: ends with `Successfully installed reportlab-<version>`

- [ ] **Step 3: Verify import and that pyproject is untouched**

Run: `.venv/bin/python -c "import reportlab; print(reportlab.Version)" && git diff --quiet pyproject.toml && echo PYPROJECT_CLEAN`
Expected: a version string then `PYPROJECT_CLEAN` (reportlab is test-fixture tooling, deliberately NOT a project dependency).

No commit (no tracked files changed).

---

## Task 1: Korean content templates + `.txt`/`.docx` writers

**Files:**
- Create: `scripts/build_korean_test_sources.py`
- Test: `tests/test_build_korean_test_sources.py`

The six document types and their Korean markers:

| key | Korean name | required marker substring |
|---|---|---|
| `contract` | 계약서 | `계약서` |
| `quote` | 견적서 | `견적서` |
| `hr` | 인사문서 | `인사` |
| `quarterly` | 분기보고서 | `분기보고서` |
| `visit` | 거래처방문보고서 | `거래처` |
| `minutes` | 회의록 | `회의록` |

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_korean_test_sources.py
"""Tests for scripts/build_korean_test_sources.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "build_korean_test_sources.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_korean_test_sources", SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _load_module()


DOC_TYPES = {
    "contract": "계약서",
    "quote": "견적서",
    "hr": "인사",
    "quarterly": "분기보고서",
    "visit": "거래처",
    "minutes": "회의록",
}


def test_korean_body_contains_marker_for_each_type(gen):
    for key, marker in DOC_TYPES.items():
        body = gen.korean_body(key, index=1)
        assert marker in body, f"{key} body missing {marker!r}"
        assert len(body) > 80, f"{key} body too short to be realistic"


def test_korean_body_is_deterministic(gen):
    assert gen.korean_body("contract", 3) == gen.korean_body("contract", 3)
    assert gen.korean_body("contract", 3) != gen.korean_body("contract", 4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q`
Expected: FAIL — `build_korean_test_sources.py` does not exist / `korean_body` undefined.

- [ ] **Step 3: Create the script with content templates**

```python
#!/usr/bin/env python3
"""Generate persistent FDA test source folders.

Builds two reusable source folders under doc_agent_test_data/:
  --korean-out : ~120 Korean business docs (.docx/.pdf/.txt) + ground_truth.csv
  --junk-out   : ~40 realistic junk / S3-bait files

Korean PDFs embed a real single-face Korean TTF and are self-verified
against FDA's own extractor (fda.organize._extractors.extract); a PDF whose
Korean text does not round-trip is a hard error.

Deterministic: a fixed --seed reproduces the same file set byte-for-byte
(except intentionally random binary junk, whose names are still seeded).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import random
import sys
import tarfile
import zipfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# Six business doc types -> (Korean type name, body template).
_DOC_TYPES: dict[str, str] = {
    "contract": "계약서",
    "quote": "견적서",
    "hr": "인사문서",
    "quarterly": "분기보고서",
    "visit": "거래처방문보고서",
    "minutes": "회의록",
}

_PARTIES = ["라이온켐텍", "한빛소재", "대정화학", "서경물산", "동방엔지니어링"]
_PEOPLE = ["김민준", "이서연", "박지호", "최유나", "정해성"]


def korean_body(doc_type: str, index: int) -> str:
    """Return realistic Korean body text for one document.

    Deterministic in (doc_type, index): used both for content and so a
    fixed seed reproduces identical files.
    """
    if doc_type not in _DOC_TYPES:
        raise KeyError(f"unknown doc_type: {doc_type!r}")
    name = _DOC_TYPES[doc_type]
    party = _PARTIES[index % len(_PARTIES)]
    person = _PEOPLE[index % len(_PEOPLE)]
    yyyy = 2024 + (index % 2)
    q = 1 + (index % 4)
    common = f"문서번호 {doc_type.upper()}-{yyyy}-{index:04d}\n작성자 {person}\n"
    bodies = {
        "contract": (
            f"{name}\n\n본 {name}는 주식회사 라이온켐텍(이하 '갑')과 "
            f"{party}(이하 '을') 간에 체결한다. 제1조 목적: 화학소재 "
            f"공급에 관한 사항을 정한다. 제2조 계약기간: {yyyy}년 1월 "
            f"1일부터 12월 31일까지로 한다. 제3조 대금: 갑은 을에게 "
            f"분기별로 지급한다."
        ),
        "quote": (
            f"{name}\n\n수신 {party} 귀중. 아래와 같이 견적을 제출합니다. "
            f"품목: 특수 폴리머 수지. 수량 1,000kg. 단가 12,500원. "
            f"공급가액 12,500,000원. 견적 유효기간 30일."
        ),
        "hr": (
            f"인사발령 통지서\n\n성명 {person}. 발령내용: {party} 협력 "
            f"전담 인사 배치. 직급 책임. 발령일 {yyyy}년 {q}월 1일. "
            f"본 인사문서는 인사규정 제12조에 따른다."
        ),
        "quarterly": (
            f"{yyyy}년 {q}분기보고서\n\n사업부 화학소재. 매출 "
            f"{38 + index}억원, 전분기 대비 {q*2}% 성장. 주요 거래처 "
            f"{party}. 다음 분기 전망: 수요 확대 예상."
        ),
        "visit": (
            f"거래처방문보고서\n\n방문처 {party}. 방문자 {person}. "
            f"방문일 {yyyy}년 {q}월 15일. 목적: 신규 단가 협의 및 "
            f"품질 클레임 대응. 결과: 차기 계약 갱신 긍정적."
        ),
        "minutes": (
            f"{q}분기 영업 회의록\n\n일시 {yyyy}년 {q}월 3일 14시. "
            f"참석 {person} 외 4명. 안건: {party} 납품 일정. 결정사항: "
            f"분기별 공급량 확정, 회의록 사내 공유."
        ),
    }
    return common + bodies[doc_type]


def write_txt(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_docx(path: Path, text: str) -> None:
    from docx import Document

    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(str(path))


def main() -> int:  # assembled in Task 4
    raise SystemExit("CLI assembled in Task 4")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/build_korean_test_sources.py tests/test_build_korean_test_sources.py
git commit -m "feat(test-gen): Korean content templates + txt/docx writers"
```

---

## Task 2: Korean PDF — font resolution + faithful self-verify

**Files:**
- Modify: `scripts/build_korean_test_sources.py`
- Test: `tests/test_build_korean_test_sources.py`

Font resolution order: explicit `font_arg` → repo `tests/assets/NanumGothic.ttf` (optional, if present) → macOS fallback `/System/Library/Fonts/Supplemental/AppleGothic.ttf`. `.ttc` is rejected (reportlab `TTFont` needs a single face). The self-verify calls FDA's own extractor so it tests exactly what the pipeline will see.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_build_korean_test_sources.py

APPLE_GOTHIC = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")


def test_resolve_font_rejects_ttc(gen, tmp_path):
    ttc = tmp_path / "fake.ttc"
    ttc.write_bytes(b"\x00")
    with pytest.raises(ValueError, match="single-face .ttf"):
        gen.resolve_korean_font(str(ttc))


def test_resolve_font_falls_back_to_macos(gen):
    if not APPLE_GOTHIC.exists():
        pytest.skip("macOS Korean fallback font not present")
    assert gen.resolve_korean_font(None) == APPLE_GOTHIC


def test_pdf_roundtrips_korean_through_fda_extractor(gen, tmp_path):
    if not APPLE_GOTHIC.exists():
        pytest.skip("no Korean font available")
    p = tmp_path / "k.pdf"
    text = "계약서 라이온켐텍 분기보고서 회의록"
    gen.write_pdf(p, text, font_path=gen.resolve_korean_font(None))
    gen.assert_pdf_korean_ok(p, must_contain="계약서")  # raises on failure


def test_self_verify_hard_fails_on_non_korean_pdf(gen, tmp_path):
    if not APPLE_GOTHIC.exists():
        pytest.skip("no Korean font available")
    p = tmp_path / "latin.pdf"
    gen.write_pdf(p, "no hangul here", font_path=gen.resolve_korean_font(None))
    with pytest.raises(RuntimeError, match="self-verify"):
        gen.assert_pdf_korean_ok(p, must_contain="계약서")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q -k "font or pdf or self_verify"`
Expected: FAIL — `resolve_korean_font` / `write_pdf` / `assert_pdf_korean_ok` undefined.

- [ ] **Step 3: Implement font resolution, PDF writer, self-verify**

Insert into `scripts/build_korean_test_sources.py` above `def main`:

```python
_MACOS_FALLBACK_FONT = Path(
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf"
)
_VENDORED_FONT = _REPO / "tests" / "assets" / "NanumGothic.ttf"


def resolve_korean_font(font_arg: str | None) -> Path:
    """Resolve a usable single-face Korean .ttf.

    Order: explicit arg -> vendored tests/assets/NanumGothic.ttf ->
    macOS AppleGothic. .ttc collections are rejected. Hard error if none.
    """
    candidates: list[Path] = []
    if font_arg:
        candidates.append(Path(font_arg).expanduser())
    candidates.append(_VENDORED_FONT)
    candidates.append(_MACOS_FALLBACK_FONT)
    for c in candidates:
        if not c.exists():
            continue
        if c.suffix.lower() == ".ttc":
            raise ValueError(
                f"{c} is a .ttc collection; need a single-face .ttf "
                f"(reportlab TTFont cannot embed a .ttc face)"
            )
        if c.suffix.lower() != ".ttf":
            continue
        return c
    raise RuntimeError(
        "No usable Korean .ttf found. Tried: "
        + ", ".join(str(c) for c in candidates)
        + ". Pass --korean-font PATH or vendor tests/assets/NanumGothic.ttf."
    )


def write_pdf(path: Path, text: str, *, font_path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_name = "KoreanBody"
    pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont(font_name, 12)
    y = 800
    for line in text.split("\n"):
        c.drawString(50, y, line)
        y -= 18
        if y < 50:
            c.showPage()
            c.setFont(font_name, 12)
            y = 800
    c.save()


def assert_pdf_korean_ok(path: Path, *, must_contain: str) -> None:
    """Self-verify a generated PDF using FDA's OWN extractor.

    Faithful: the pipeline reads PDFs via fda.organize._extractors.extract
    (pdftotext under the hood), so we verify against the same path. Raises
    RuntimeError if extraction failed or the Korean marker is absent.
    """
    from fda.organize._extractors import extract

    result = extract(path)
    text = getattr(result, "text", "") or ""
    status = getattr(result, "status", "")
    if status != "ok" or must_contain not in text:
        raise RuntimeError(
            f"PDF self-verify failed for {path.name}: status={status!r} "
            f"marker={must_contain!r} present={must_contain in text}. "
            f"pdftotext (poppler) must be on PATH and the font must embed."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q -k "font or pdf or self_verify"`
Expected: PASS (4 tests; skips only if AppleGothic is absent).

- [ ] **Step 5: Commit**

```bash
git add scripts/build_korean_test_sources.py tests/test_build_korean_test_sources.py
git commit -m "feat(test-gen): Korean PDF writer with font resolution + FDA-faithful self-verify"
```

---

## Task 3: Realistic junk / S3-bait generator

**Files:**
- Modify: `scripts/build_korean_test_sources.py`
- Test: `tests/test_build_korean_test_sources.py`

Must guarantee at least one cluster of genuinely-unextractable files **of the same extension** so the quarantine layer forms an `all_extraction_failed` bucket (drives `s3` per spec). Password-locked PDFs are the reliable choice.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_build_korean_test_sources.py


def test_junk_includes_unextractable_pdf_cluster(gen, tmp_path):
    out = tmp_path / "junk"
    made = gen.build_junk(out, rng=__import__("random").Random("seed"))
    locked = sorted(out.glob("*.pdf"))
    assert len(locked) >= 5, "need a same-ext unextractable cluster for s3"
    from fda.organize._extractors import extract

    r = extract(locked[0])
    assert getattr(r, "status", "") != "ok", "locked PDF must be unextractable"
    names = {p.name for p in made}
    assert ".DS_Store" in names
    assert any(p.stat().st_size == 0 for p in made), "need a zero-byte file"
    assert any(p.suffix == ".log" for p in made)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q -k junk`
Expected: FAIL — `build_junk` undefined.

- [ ] **Step 3: Implement `build_junk`**

Insert into `scripts/build_korean_test_sources.py` above `def main`:

```python
def _locked_pdf_bytes() -> bytes:
    """A minimal encrypted (password-protected) PDF: pdftotext cannot
    extract text from it, so it lands in the quarantine all-failed bucket."""
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setEncrypt = None  # noqa: explicit: encryption set below
    from reportlab.lib import pdfencrypt

    enc = pdfencrypt.StandardEncryption("ownerpw", userPassword="userpw")
    c = canvas.Canvas(buf, encrypt=enc)
    c.drawString(72, 720, "locked")
    c.save()
    return buf.getvalue()


def build_junk(out: Path, *, rng: random.Random) -> list[Path]:
    """Write a realistic junk pile; return the created paths.

    Guarantees >=6 same-extension password-locked PDFs (the deterministic
    s3 driver) plus multi-extension quarantine coverage.
    """
    out.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []

    for i in range(6):  # same-ext unextractable cluster -> s3
        p = out / f"locked_{i:02d}.pdf"
        p.write_bytes(_locked_pdf_bytes())
        made.append(p)

    for i in range(3):
        p = out / f"app_{i}.log"
        p.write_text(f"[INFO] line {i}\n" * 50, encoding="utf-8")
        made.append(p)

    mp4 = out / "clip.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    made.append(mp4)

    sql = out / "db_dump.sql"
    sql.write_text("-- dump\nINSERT INTO t VALUES (1);\n", encoding="utf-8")
    made.append(sql)

    bak = out / "old.bak"
    bak.write_bytes(rng.randbytes(256))
    made.append(bak)

    z = out / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inner.txt", "x")
    made.append(z)

    tgz = out / "release.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("inner.txt")
        data = b"x"
        info.size = len(data)
        import io as _io

        tf.addfile(info, _io.BytesIO(data))
    made.append(tgz)

    for i in range(2):
        p = out / f"empty_{i}.txt"
        p.write_bytes(b"")
        made.append(p)

    ds = out / ".DS_Store"
    ds.write_bytes(b"\x00\x00\x00\x01Bud1")
    made.append(ds)

    return made
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q -k junk`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_korean_test_sources.py tests/test_build_korean_test_sources.py
git commit -m "feat(test-gen): realistic junk / s3-bait with guaranteed unextractable cluster"
```

---

## Task 4: CLI assembly + ground_truth.csv + determinism

**Files:**
- Modify: `scripts/build_korean_test_sources.py`
- Test: `tests/test_build_korean_test_sources.py`

~120 Korean docs spread over 6 types and 3 formats; `ground_truth.csv` columns: `abs_path,doc_type,doc_type_ko,fmt,business_purpose`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_build_korean_test_sources.py
import csv as _csv


def test_main_is_deterministic_and_writes_ground_truth(gen, tmp_path):
    if not APPLE_GOTHIC.exists():
        pytest.skip("no Korean font available")
    k1, j1 = tmp_path / "k1", tmp_path / "j1"
    rc = gen.run(korean_out=k1, junk_out=j1, seed="s", korean_font=None,
                 korean_count=24)
    assert rc == 0
    gt = k1 / "ground_truth.csv"
    assert gt.exists()
    rows = list(_csv.DictReader(gt.open(encoding="utf-8")))
    assert len(rows) == 24
    assert set(rows[0]) == {
        "abs_path", "doc_type", "doc_type_ko", "fmt", "business_purpose"
    }
    types = {r["doc_type"] for r in rows}
    assert types == set(DOC_TYPES)  # all six covered
    names1 = sorted(p.name for p in k1.rglob("*") if p.is_file())

    k2, j2 = tmp_path / "k2", tmp_path / "j2"
    gen.run(korean_out=k2, junk_out=j2, seed="s", korean_font=None,
            korean_count=24)
    names2 = sorted(p.name for p in k2.rglob("*") if p.is_file())
    assert names1 == names2  # deterministic in seed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q -k deterministic`
Expected: FAIL — `gen.run` undefined.

- [ ] **Step 3: Implement `run()` and `main()`**

Replace the placeholder `def main` in `scripts/build_korean_test_sources.py` with:

```python
_FORMATS = ("docx", "pdf", "txt")


def run(*, korean_out: Path, junk_out: Path, seed: str,
        korean_font: str | None, korean_count: int = 120) -> int:
    korean_out = Path(korean_out)
    junk_out = Path(junk_out)
    korean_out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    font_path = None
    keys = list(_DOC_TYPES)
    gt_rows: list[dict[str, str]] = []
    for i in range(korean_count):
        dt = keys[i % len(keys)]
        fmt = _FORMATS[i % len(_FORMATS)]
        body = korean_body(dt, i)
        stem = f"{_DOC_TYPES[dt]}_{i:04d}"
        path = korean_out / f"{stem}.{fmt}"
        if fmt == "txt":
            write_txt(path, body)
        elif fmt == "docx":
            write_docx(path, body)
        else:
            if font_path is None:
                font_path = resolve_korean_font(korean_font)
            write_pdf(path, body, font_path=font_path)
            assert_pdf_korean_ok(path, must_contain=_DOC_TYPES[dt][:2])
        gt_rows.append({
            "abs_path": str(path.resolve()),
            "doc_type": dt,
            "doc_type_ko": _DOC_TYPES[dt],
            "fmt": fmt,
            "business_purpose": _DOC_TYPES[dt],
        })

    with (korean_out / "ground_truth.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        w = csv.DictWriter(f, fieldnames=[
            "abs_path", "doc_type", "doc_type_ko", "fmt", "business_purpose"
        ])
        w.writeheader()
        w.writerows(gt_rows)

    build_junk(junk_out, rng=rng)
    print(f"korean: {korean_count} -> {korean_out}")
    print(f"junk:           -> {junk_out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--korean-out", required=True)
    ap.add_argument("--junk-out", required=True)
    ap.add_argument("--seed", default="bilingual-2026-05-16")
    ap.add_argument("--korean-font", default=None)
    ap.add_argument("--korean-count", type=int, default=120)
    a = ap.parse_args()
    return run(
        korean_out=Path(a.korean_out), junk_out=Path(a.junk_out),
        seed=a.seed, korean_font=a.korean_font,
        korean_count=a.korean_count,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_build_korean_test_sources.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/build_korean_test_sources.py tests/test_build_korean_test_sources.py
git commit -m "feat(test-gen): CLI + ground_truth.csv + deterministic seed"
```

---

## Task 5: evaluator — bucket-size histogram

**Files:**
- Modify: `scripts/evaluate_fda_fixture.py`
- Test: `tests/test_evaluate_fda_fixture.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_evaluate_fda_fixture.py


def test_bucket_histogram_sorts_by_size(evaluator):
    by_bucket = {"a": [1, 2], "b": [1], "c": [1, 2, 3]}
    hist = evaluator.bucket_histogram(by_bucket)
    assert hist == [("c", 3), ("a", 2), ("b", 1)]
    assert evaluator.bucket_histogram({}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k histogram`
Expected: FAIL — `bucket_histogram` undefined.

- [ ] **Step 3: Implement the helper**

Add to `scripts/evaluate_fda_fixture.py` (above `def main`):

```python
def bucket_histogram(by_bucket: dict) -> list[tuple[str, int]]:
    """(bucket, file_count) sorted by count desc, then name. The #4
    over-fragmentation signal: a human reads this list."""
    return sorted(
        ((b, len(v)) for b, v in by_bucket.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k histogram`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluate_fda_fixture.py tests/test_evaluate_fda_fixture.py
git commit -m "feat(eval): bucket-size histogram helper (#4 signal)"
```

---

## Task 6: evaluator — "S3 fired?" + router precondition

**Files:**
- Modify: `scripts/evaluate_fda_fixture.py`
- Test: `tests/test_evaluate_fda_fixture.py`

Per spec: "S3 did not fire" is only a #5 verdict if routing actually ran (`routing-report.json` present & readable).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_evaluate_fda_fixture.py


def test_s3_verdict(evaluator):
    fired = {"categories": [{"destination": "s3", "signals": {}}]}
    none = {"categories": [{"destination": "sharepoint", "signals": {}}]}
    assert evaluator.s3_verdict(fired) == "yes"
    assert evaluator.s3_verdict(none) == "no"
    assert evaluator.s3_verdict(None) == "INCONCLUSIVE (no routing-report.json)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k s3_verdict`
Expected: FAIL — `s3_verdict` undefined.

- [ ] **Step 3: Implement the helper**

Add to `scripts/evaluate_fda_fixture.py` (above `def main`):

```python
def s3_verdict(routing_data: dict | None) -> str:
    """yes/no/INCONCLUSIVE. None => routing-report.json missing or
    unreadable => the run cannot answer #5 (router failures are swallowed
    by organize(); see spec precondition)."""
    if routing_data is None:
        return "INCONCLUSIVE (no routing-report.json)"
    for c in routing_data.get("categories", []) or []:
        if c.get("destination") == "s3":
            return "yes"
    return "no"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k s3_verdict`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluate_fda_fixture.py tests/test_evaluate_fda_fixture.py
git commit -m "feat(eval): explicit S3-fired verdict with routing precondition (#5)"
```

---

## Task 7: evaluator — metadata.db Korean-search check

**Files:**
- Modify: `scripts/evaluate_fda_fixture.py`
- Test: `tests/test_evaluate_fda_fixture.py`

`metadata.db` schema: `documents` table; FTS5 `documents_fts(summary, keywords)` with `trigram` tokenizer (`fda/metadata/schema.py:161`). Trigram supports CJK substring `MATCH`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_evaluate_fda_fixture.py
import sqlite3


def test_metadata_check_counts_and_korean_hits(evaluator, tmp_path):
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE VIRTUAL TABLE documents_fts USING fts5("
        "summary, keywords, tokenize='trigram')"
    )
    conn.execute("CREATE TABLE documents (sha256 TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO documents VALUES ('a')")
    conn.execute("INSERT INTO documents VALUES ('b')")
    conn.execute(
        "INSERT INTO documents_fts(summary, keywords) "
        "VALUES ('계약서 라이온켐텍 분기보고서', '계약')"
    )
    conn.commit()
    conn.close()

    res = evaluator.metadata_check(tmp_path, query="계약")
    assert res["rows"] == 2
    assert res["korean_hits"] >= 1

    missing = evaluator.metadata_check(tmp_path / "nope", query="계약")
    assert missing["rows"] == 0
    assert missing["korean_hits"] == 0
    assert missing["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k metadata_check`
Expected: FAIL — `metadata_check` undefined.

- [ ] **Step 3: Implement the helper**

Add to `scripts/evaluate_fda_fixture.py` (above `def main`; add `import sqlite3` to the import block):

```python
def metadata_check(fda_home: Path, *, query: str = "계약") -> dict:
    """Open <fda_home>/metadata.db; return classified-row count and
    Korean FTS hit count. Never raises — a missing/locked DB is a
    reported finding, not a crash."""
    out = {"rows": 0, "korean_hits": 0, "error": ""}
    db = Path(fda_home) / "metadata.db"
    if not db.exists():
        out["error"] = f"metadata.db not found at {db}"
        return out
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            out["rows"] = conn.execute(
                "SELECT count(*) FROM documents"
            ).fetchone()[0]
            out["korean_hits"] = conn.execute(
                "SELECT count(*) FROM documents_fts "
                "WHERE documents_fts MATCH ?",
                (query,),
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k metadata_check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluate_fda_fixture.py tests/test_evaluate_fda_fixture.py
git commit -m "feat(eval): metadata.db row count + Korean FTS check"
```

---

## Task 8: evaluator — Hangul scan + wire new sections into report/CLI

**Files:**
- Modify: `scripts/evaluate_fda_fixture.py`
- Test: `tests/test_evaluate_fda_fixture.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_evaluate_fda_fixture.py


def test_has_hangul(evaluator):
    assert evaluator.has_hangul("계약서") is True
    assert evaluator.has_hangul("invoices/2024") is False
    assert evaluator.has_hangul("") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_evaluate_fda_fixture.py -q -k has_hangul`
Expected: FAIL — `has_hangul` undefined.

- [ ] **Step 3: Implement helper + wire all new sections into `main()`**

Add the helper to `scripts/evaluate_fda_fixture.py`:

```python
def has_hangul(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in s or "")
```

Add `--fda-home` to the argparse block in `main()` (next to `--no-write`):

```python
    parser.add_argument(
        "--fda-home",
        default=None,
        help="Sandbox FDA_HOME dir holding metadata.db (Korean-search check).",
    )
```

Then, in `main()`, immediately before the `output = "\n".join(report) + "\n"` line, append the new scorecard sections:

```python
    write_lines(report, "## Bucket-size histogram (#4 signal)", "")
    hist = bucket_histogram(by_bucket)
    write_lines(report, f"- buckets: {len(hist)}")
    write_lines(
        report,
        "- sizes: " + ", ".join(str(n) for _, n in hist) if hist else "- (none)",
        "",
    )

    rdata = None
    if routing_json.exists():
        try:
            rdata = json.loads(routing_json.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rdata = None
    write_lines(
        report, "## #5 — S3 fired?", "",
        f"- {s3_verdict(rdata)}", "",
    )

    if args.fda_home:
        mc = metadata_check(Path(args.fda_home))
        write_lines(
            report, "## Metadata layer", "",
            f"- classified rows: {mc['rows']}",
            f"- Korean search '계약' hits: {mc['korean_hits']}",
            (f"- error: {mc['error']}" if mc["error"] else ""),
            "",
        )

    ko_buckets = sum(1 for b in by_bucket if has_hangul(b))
    md_path = root / "routing-report.md"
    md_ko = md_path.exists() and has_hangul(
        md_path.read_text(encoding="utf-8", errors="ignore")
    )
    write_lines(
        report, "## Korean-label presence (#goal 4)", "",
        f"- buckets with Hangul names: {ko_buckets}/{len(by_bucket)}",
        f"- routing-report.md contains Hangul: {'yes' if md_ko else 'no'}",
        "",
    )
```

- [ ] **Step 4: Run the FULL suite (spec gate)**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS, no regressions (the spec requires the full suite green after the evaluator change).

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluate_fda_fixture.py tests/test_evaluate_fda_fixture.py
git commit -m "feat(eval): Hangul scan + wire histogram/S3/metadata/Korean into scorecard"
```

---

## Task 9: Korean business_context.md draft

**Files:**
- Create: `docs/superpowers/specs/2026-05-16-business-context.draft.ko.md`

- [ ] **Step 1: Write the draft**

```markdown
# 비즈니스 컨텍스트 (테스트 초안 — 사용자가 검토/수정)

> 이 파일은 테스트 실행 전 사용자가 검토·수정한 뒤
> `$FDA_HOME/business_context.md` 로 복사된다. 실제 `~/.fda` 는 건드리지 않는다.

## 폴더 세분화 규칙 (organize 전용)

- 같은 문서 종류는 하나의 폴더로 묶는다. 2~3개짜리 잘게 쪼갠 폴더를
  여러 개 만들지 말 것. 한 종류가 소수면 상위 종류 폴더로 합친다.
- 폴더 이름은 한국어 업무 용어로 짓는다 (예: `계약서`, `견적서`,
  `분기보고서`, `회의록`, `인사문서`, `거래처방문보고서`).
- 읽을 수 없는 파일(잠김/빈/손상)은 내용 기반 폴더로 추정하지 말고
  격리 폴더에 둔다.

## 문서 종류 가이드

- 계약서: 갑/을, 계약기간, 대금 조항이 있으면 계약서.
- 견적서: 품목·수량·단가·공급가액·유효기간이 있으면 견적서.
- 분기보고서: 분기 매출/성장률/전망이 있으면 보고서.
- 회의록: 일시·참석·안건·결정사항이 있으면 회의록.
- 인사문서: 발령·직급·인사규정 언급이 있으면 인사문서.
- 거래처방문보고서: 방문처·방문일·목적·결과가 있으면 방문보고서.

## 우선순위

- 이 컨텍스트는 영구 조직 선호이며, 1회성 USER_INSTRUCTIONS 가
  직접 충돌하면 USER_INSTRUCTIONS 가 우선한다.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-05-16-business-context.draft.ko.md
git commit -m "docs(test): Korean business_context.md draft for the pipeline test"
```

---

## Task 10: Eyeball-notes skeleton + final verification

**Files:**
- Create: `docs/superpowers/plans/2026-05-16-bilingual-pipeline-test-eyeball-notes.md`

This plan builds tooling only. The ~300-file pipeline run is operator-driven (real Claude backend, token cost) using the pinned commands in the spec; it is NOT executed here.

- [ ] **Step 1: Write the verdict skeleton**

```markdown
# Bilingual pipeline test — eyeball notes (fill after the run)

Spec: docs/superpowers/specs/2026-05-16-bilingual-pipeline-test-design.md
Fixture: /private/tmp/fda-test-sets/randomized-bilingual-2026-05-16-001
Seed: bilingual-2026-05-16   FDA_HOME: <sandbox dir>

## Preconditions (must hold before recording any verdict)
- [ ] routing-report.json present and readable
- [ ] organize log has NO "router stage failed" line
- [ ] step-2b real-source counts verified; no --quota exceeded its source

## Goal 1 — organization quality (#4)
- buckets: ___  sizes: ___
- verifier discrepancies: ___ (must be 0)
- VERDICT #4 (over-fragmentation): ___

## Goal 2 — router (#5)
- sharepoint ___ | s3 ___ | rdbms ___
- S3 fired? ___
- VERDICT #5 (s3 branch dead?): ___

## Goal 3 — metadata layer
- classified rows: ___  | Korean '계약' hits: ___

## Goal 4 — Korean handling
- Hangul bucket names: ___/___  | routing-report.md Hangul: ___
- business_context.md rules honored? ___
```

- [ ] **Step 2: Final full-suite gate**

Run: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
Expected: PASS (no regressions across the whole suite).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-05-16-bilingual-pipeline-test-eyeball-notes.md
git commit -m "docs(test): eyeball-notes verdict skeleton for #4/#5"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** generator script (Tasks 1-4) ✓; embedded-TTF Korean PDF + font resolution + reject .ttc + loud failure (Task 2) ✓; FDA-faithful self-verify (Task 2) ✓; no `.hwpx` (out of scope, honored) ✓; realistic junk + guaranteed same-ext unextractable cluster for s3 (Task 3) ✓; ground_truth.csv human-reference only, no automated cohesion scorer (Task 4, not consumed by evaluator) ✓; reportlab into `.venv` only (Task 0) ✓; evaluator `--fda-home` + histogram + S3-fired + routing precondition + metadata Korean search + Hangul scan (Tasks 5-8) ✓; Korean business_context draft (Task 9) ✓; run command + eyeball skeleton, run NOT auto-executed (Task 10) ✓; full suite gate after evaluator change (Tasks 8 & 10) ✓; generator self-verify hard-fail smoke test (Task 2 Step 1) ✓.

**Placeholder scan:** no TBD/TODO; every code step shows complete code.

**Type consistency:** `korean_body`, `write_txt`, `write_docx`, `resolve_korean_font`, `write_pdf`, `assert_pdf_korean_ok`, `build_junk`, `run`, `main` consistent across Tasks 1-4; `bucket_histogram`, `s3_verdict`, `metadata_check`, `has_hangul` consistent across Tasks 5-8 and referenced with the same signatures when wired in Task 8.
