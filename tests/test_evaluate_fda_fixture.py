# tests/test_evaluate_fda_fixture.py
"""Tests for scripts/evaluate_fda_fixture.py helpers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "evaluate_fda_fixture.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_fda_fixture", SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def evaluator():
    return _load_module()


def _cat(name: str, subpath: str, **extra) -> dict:
    base = {
        "name": name,
        "subpath": subpath,
        "destination": "sharepoint",
        "signals": {"file_count": 1},
        "reason": "",
    }
    base.update(extra)
    return base


class TestIndexRouterCategories:
    """Regression: today the evaluator keys its router index by `name`,
    which silently drops categories whose `name` and `subpath` differ —
    especially Korean subpaths with no Latin/dash equivalent of the
    English name. The fix is to key the index by `subpath`.

    These tests exercise a module-level helper `_index_router_categories`
    extracted in Step 3 so the index-build is independently testable."""

    def test_keys_categories_by_subpath(self, evaluator):
        data = {"categories": [
            _cat("Sales-Orders-Detailed", "Sales/Orders-Detailed"),
            _cat("Client-Visit-Reports", "영업/거래처방문보고서"),
        ]}
        index = evaluator._index_router_categories(data)
        assert set(index.keys()) == {
            "Sales/Orders-Detailed",
            "영업/거래처방문보고서",
        }

    def test_skips_categories_missing_subpath(self, evaluator):
        data = {"categories": [
            _cat("Foo", "Foo"),
            {"name": "BrokenNoSubpath", "destination": "s3"},
        ]}
        index = evaluator._index_router_categories(data)
        assert set(index.keys()) == {"Foo"}

    def test_empty_when_no_categories(self, evaluator):
        assert evaluator._index_router_categories({"categories": []}) == {}
        assert evaluator._index_router_categories({}) == {}


class TestMatchRouterCategoryBySubpath:
    """After the fix, `_match_router_category` is a one-line dict lookup
    over a subpath-keyed index. The dash-normalization / casefold /
    parent-segment fallbacks are gone — they only existed to paper over
    the old name-keyed index."""

    def test_subpath_with_slash_matches_nested_bucket(self, evaluator):
        cat = _cat("Sales-Orders-Detailed", "Sales/Orders-Detailed")
        index = {cat["subpath"]: cat}
        assert evaluator._match_router_category(
            "Sales/Orders-Detailed", index,
        ) is cat

    def test_korean_subpath_matches(self, evaluator):
        cat = _cat("Client-Visit-Reports", "영업/거래처방문보고서")
        index = {cat["subpath"]: cat}
        assert evaluator._match_router_category(
            "영업/거래처방문보고서", index,
        ) is cat

    def test_unknown_bucket_returns_none(self, evaluator):
        cat = _cat("Invoices", "Finance/Invoices")
        index = {cat["subpath"]: cat}
        assert evaluator._match_router_category(
            "HR/Onboarding", index,
        ) is None

    def test_empty_index_returns_none(self, evaluator):
        assert evaluator._match_router_category("Anything", {}) is None

    def test_no_dash_normalization_fallback(self, evaluator):
        """Regression: today's helper has a `bucket.replace('/', '-')`
        fallback. After the fix, a bucket "Sales-Orders-Detailed" must
        NOT match a subpath-key "Sales/Orders-Detailed". This test
        passes today (because the current code also returns None for
        this pair), but it pins the post-fix contract: no fuzz."""
        cat = _cat("Sales-Orders-Detailed", "Sales/Orders-Detailed")
        index = {cat["subpath"]: cat}
        assert evaluator._match_router_category(
            "Sales-Orders-Detailed", index,
        ) is None
