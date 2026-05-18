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
