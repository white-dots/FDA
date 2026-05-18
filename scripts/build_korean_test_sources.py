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


def _locked_pdf_bytes() -> bytes:
    """A minimal encrypted (password-protected) PDF: pdftotext cannot
    extract text from it. `.pdf` is NOT a storage-blob type, so this is a
    #5(b) honesty counter-example — it must stay quarantined, never S3."""
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setEncrypt = None  # noqa: explicit: encryption set below
    from reportlab.lib import pdfencrypt

    enc = pdfencrypt.StandardEncryption("userpw", ownerPassword="ownerpw")
    c = canvas.Canvas(buf, encrypt=enc)
    c.drawString(72, 720, "locked")
    c.save()
    return buf.getvalue()


def build_junk(out: Path, *, rng: random.Random) -> list[Path]:
    """Write a realistic junk pile; return the created paths.

    S3 drivers (#5a): a few files of each storage-blob family so all three
    folders (미디어_Media / 압축파일_Archives / 백업_Backups) appear and
    route to s3 deterministically. Honesty counter-examples (#5b): the
    password-locked PDFs, zero-byte `.bin` files (a no-extractor type — an
    empty `.txt` would extract ok and NOT quarantine), and an
    unknown-extension `.xyz` binary are genuinely unreadable but NOT
    storage-blob types — they must stay quarantined with no cloud
    destination.
    """
    out.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []

    # --- S3 drivers: storage-blob families (#5a) -----------------------
    for i in range(3):  # media -> StorageBlobMedia
        p = out / f"clip_{i:02d}.mp4"
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + rng.randbytes(64))
        made.append(p)
    for i in range(3):  # archive -> StorageBlobArchive
        p = out / f"backup_{i:02d}.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("inner.txt", f"x{i}")
        made.append(p)
    tgz = out / "release.tar.gz"  # archive (Path.suffix == ".gz")
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("inner.txt")
        data = b"x"
        info.size = len(data)
        import io as _io

        tf.addfile(info, _io.BytesIO(data))
    made.append(tgz)
    for i in range(2):  # backup -> StorageBlobBackup
        p = out / f"db_dump_{i:02d}.sql"
        p.write_text(
            f"-- dump {i}\nINSERT INTO t VALUES ({i});\n", encoding="utf-8"
        )
        made.append(p)
    for i in range(2):  # backup -> StorageBlobBackup
        p = out / f"old_{i:02d}.bak"
        p.write_bytes(rng.randbytes(256))
        made.append(p)

    # --- Honesty counter-examples: unreadable but NOT blobs (#5b) -------
    for i in range(6):  # password-locked PDFs
        p = out / f"locked_{i:02d}.pdf"
        p.write_bytes(_locked_pdf_bytes())
        made.append(p)
    unknown = out / "mystery.xyz"  # unknown extension, no extractor
    unknown.write_bytes(rng.randbytes(128))
    made.append(unknown)
    for i in range(2):  # zero-byte, NO-EXTRACTOR type. NOT .txt: an empty
        p = out / f"empty_{i}.bin"  # .txt extracts as ok and would NOT
        p.write_bytes(b"")          # quarantine -> false honesty signal.
        made.append(p)

    # --- Plain noise (readable / OS junk) ------------------------------
    for i in range(3):
        p = out / f"app_{i}.log"
        p.write_text(f"[INFO] line {i}\n" * 50, encoding="utf-8")
        made.append(p)
    ds = out / ".DS_Store"
    ds.write_bytes(b"\x00\x00\x00\x01Bud1")
    made.append(ds)

    return made


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


if __name__ == "__main__":
    sys.exit(main())
