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
