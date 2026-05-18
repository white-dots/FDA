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
