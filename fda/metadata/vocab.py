# fda/metadata/vocab.py
"""Canonical English vocabularies for the metadata layer + Korean display
labels for UI rendering.

The DB stores stable English codes (these maps are the source of truth).
Adding a code here is a code change. Mapping a customer's local Korean name
(e.g. `영업기획부`) to a canonical code (`sales`) is handled by their
`~/.fda/business_context.md`, not by editing this file.
"""
from __future__ import annotations

DEPARTMENTS: tuple[str, ...] = (
    "sales", "finance", "hr", "production", "rd",
    "legal", "operations", "marketing", "executive", "unknown",
)

DOCUMENT_TYPES: tuple[str, ...] = (
    "invoice", "contract", "report", "proposal", "memo",
    "policy", "presentation", "spreadsheet", "image", "data",
    "archive", "correspondence", "unknown",
)

CONFIDENTIALITY: tuple[str, ...] = (
    "public", "internal", "confidential", "restricted",
)

_DEPARTMENT_KO: dict[str, str] = {
    "sales": "영업",
    "finance": "재무",
    "hr": "인사",
    "production": "생산",
    "rd": "연구개발",
    "legal": "법무",
    "operations": "운영",
    "marketing": "마케팅",
    "executive": "경영진",
    "unknown": "미분류",
}

_DOCUMENT_TYPE_KO: dict[str, str] = {
    "invoice": "청구서",
    "contract": "계약서",
    "report": "보고서",
    "proposal": "제안서",
    "memo": "메모",
    "policy": "정책문서",
    "presentation": "발표자료",
    "spreadsheet": "스프레드시트",
    "image": "이미지",
    "data": "데이터",
    "archive": "압축파일",
    "correspondence": "서신",
    "unknown": "미분류",
}

_CONFIDENTIALITY_KO: dict[str, str] = {
    "public": "공개",
    "internal": "내부",
    "confidential": "기밀",
    "restricted": "제한",
}


def ko_label_for_department(code: str) -> str:
    """Return Korean label for a department code; fall back to the code itself."""
    return _DEPARTMENT_KO.get(code, code)


def ko_label_for_document_type(code: str) -> str:
    return _DOCUMENT_TYPE_KO.get(code, code)


def ko_label_for_confidentiality(code: str) -> str:
    return _CONFIDENTIALITY_KO.get(code, code)
