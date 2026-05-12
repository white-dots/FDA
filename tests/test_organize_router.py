# tests/test_organize_router.py
"""Tests for fda.organize.router."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _entry(idx, *, ext=".pdf", failed=False, sections=(), size=1000,
           summary="x", path=None, subpath="Finance/Invoices"):
    """Build a CatalogEntry under /tmp/target/<subpath>/."""
    from fda.organize.models import CatalogEntry
    p = path or f"/tmp/target/{subpath}/{idx:03d}{ext}"
    return CatalogEntry(
        path_id=f"f{idx:03d}",
        path=p,
        ext=ext,
        size_bytes=size,
        summary="" if failed else summary,
        type_label="" if failed else "doc",
        is_junk=False,
        summary_failed=failed,
        extract_status="failed" if failed else "ok",
        sections=tuple(sections),
    )


class TestAggregateSignals:
    def test_basic_counts_and_sizes(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".pdf", size=1000),
            _entry(1, ext=".pdf", size=2000),
            _entry(2, ext=".docx", size=500),
        ]
        sig = _aggregate_signals(entries)
        assert sig.file_count == 3
        assert sig.total_size_bytes == 3500
        assert dict(sig.extension_distribution) == {".pdf": 2, ".docx": 1}
        assert sig.tabular_schema_consistent is False
        assert sig.all_extraction_failed is False

    def test_tabular_schema_consistent_when_all_csv_with_same_sections(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".csv", sections=("a", "b", "c")),
            _entry(1, ext=".csv", sections=("a", "b", "c")),
        ]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is True

    def test_tabular_schema_inconsistent_when_csv_headers_differ(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".csv", sections=("a", "b")),
            _entry(1, ext=".csv", sections=("a", "b", "c")),
        ]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is False

    def test_tabular_schema_inconsistent_when_mixed_extensions(self):
        from fda.organize.router import _aggregate_signals
        entries = [
            _entry(0, ext=".csv", sections=("a", "b")),
            _entry(1, ext=".pdf", sections=("a", "b")),
        ]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is False

    def test_tabular_schema_false_when_sections_empty(self):
        from fda.organize.router import _aggregate_signals
        entries = [_entry(0, ext=".csv", sections=())]
        sig = _aggregate_signals(entries)
        assert sig.tabular_schema_consistent is False

    def test_all_extraction_failed_when_every_summary_failed(self):
        from fda.organize.router import _aggregate_signals
        entries = [_entry(0, failed=True), _entry(1, failed=True)]
        sig = _aggregate_signals(entries)
        assert sig.all_extraction_failed is True

    def test_all_extraction_failed_false_when_one_succeeds(self):
        from fda.organize.router import _aggregate_signals
        entries = [_entry(0, failed=True), _entry(1, failed=False)]
        sig = _aggregate_signals(entries)
        assert sig.all_extraction_failed is False


class TestShortCircuit:
    def test_misc_category_short_circuits_to_s3(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0)])
        assert _short_circuit("Misc", sig) == "s3"

    def test_all_extraction_failed_short_circuits_to_s3(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0, failed=True), _entry(1, failed=True)])
        assert _short_circuit("Finance/Invoices", sig) == "s3"

    def test_normal_category_no_short_circuit(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0), _entry(1)])
        assert _short_circuit("Finance/Invoices", sig) is None

    def test_single_file_category_does_not_short_circuit(self):
        from fda.organize.router import _aggregate_signals, _short_circuit
        sig = _aggregate_signals([_entry(0)])
        assert _short_circuit("Reports/Sales", sig) is None


class _Logger:
    """Drop-in replacement for OrganizeLogger that records events."""
    def __init__(self):
        self.events = []
    def log(self, event, **fields):
        self.events.append((event, fields))


def _skill_response(destination="sharepoint", reason="ok", misfits=None):
    return json.dumps({
        "destination": destination,
        "reason": reason,
        "misfits": misfits or [],
    })


class TestRouteOneCategory:
    def test_happy_path_returns_destination_reason_and_empty_misfits(self):
        from fda.organize.router import _route_one_category, _aggregate_signals
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="People retrieve these.",
        )
        entries = [_entry(0), _entry(1)]
        sig = _aggregate_signals(entries)
        skill = MagicMock()
        skill.body = "skill body"
        skill.model = "claude-sonnet-4-6"
        dest, reason, misfits = _route_one_category(
            category_name="Finance/Invoices",
            description="d", criteria="c", subpath="Finance/Invoices",
            signals=sig, entries=entries,
            backend=backend, skill=skill, logger=_Logger(),
        )
        assert dest == "sharepoint"
        assert reason == "People retrieve these."
        assert misfits == []

    def test_propagates_misfit_records_verbatim(self):
        from fda.organize.router import _route_one_category, _aggregate_signals
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="r",
            misfits=[{
                "path_id": "f000",
                "suggested_destination": "rdbms",
                "reason": "Clean tabular.",
            }],
        )
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        _, _, misfits = _route_one_category(
            category_name="Finance/Invoices", description="d", criteria="c",
            subpath="Finance/Invoices", signals=sig, entries=entries,
            backend=backend, skill=skill, logger=_Logger(),
        )
        assert misfits == [{
            "path_id": "f000",
            "suggested_destination": "rdbms",
            "reason": "Clean tabular.",
        }]

    def test_unknown_destination_raises_router_error(self):
        from fda.organize.router import (
            _route_one_category, _aggregate_signals, RouterError,
        )
        backend = MagicMock()
        backend.complete.return_value = _skill_response(destination="gdrive")
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        with pytest.raises(RouterError, match="destination"):
            _route_one_category(
                category_name="x", description="d", criteria="c", subpath="x",
                signals=sig, entries=entries,
                backend=backend, skill=skill, logger=_Logger(),
            )

    def test_invalid_json_raises_router_error(self):
        from fda.organize.router import (
            _route_one_category, _aggregate_signals, RouterError,
        )
        backend = MagicMock()
        backend.complete.return_value = "not json at all"
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        with pytest.raises(RouterError, match="JSON"):
            _route_one_category(
                category_name="x", description="d", criteria="c", subpath="x",
                signals=sig, entries=entries,
                backend=backend, skill=skill, logger=_Logger(),
            )

    def test_misfit_with_unknown_path_id_raises_router_error(self):
        from fda.organize.router import (
            _route_one_category, _aggregate_signals, RouterError,
        )
        backend = MagicMock()
        backend.complete.return_value = _skill_response(misfits=[{
            "path_id": "fZZZ",
            "suggested_destination": "rdbms",
            "reason": "r",
        }])
        entries = [_entry(0)]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        with pytest.raises(RouterError, match="path_id"):
            _route_one_category(
                category_name="x", description="d", criteria="c", subpath="x",
                signals=sig, entries=entries,
                backend=backend, skill=skill, logger=_Logger(),
            )

    def test_prompt_payload_contains_signals_and_sample(self):
        from fda.organize.router import _route_one_category, _aggregate_signals
        backend = MagicMock()
        backend.complete.return_value = _skill_response()
        entries = [_entry(0, summary="invoice"), _entry(1, summary="po")]
        sig = _aggregate_signals(entries)
        skill = MagicMock(); skill.body = "x"; skill.model = "claude-sonnet-4-6"
        _route_one_category(
            category_name="Finance/Invoices", description="d", criteria="c",
            subpath="Finance/Invoices", signals=sig, entries=entries,
            backend=backend, skill=skill, logger=_Logger(),
        )
        body = backend.complete.call_args.kwargs["messages"][0]["content"]
        parsed = json.loads(body)
        assert parsed["category"]["category_name"] == "Finance/Invoices"
        assert parsed["signals"]["file_count"] == 2
        assert {e["path_id"] for e in parsed["sample"]} == {"f000", "f001"}


def _grouping(category, file_ids, subpath=None):
    from fda.organize.models import Grouping
    return Grouping(
        category=category, subpath=subpath or category,
        file_ids=tuple(file_ids), reason="",
    )


def _catalog(entries, target="/tmp/target"):
    from fda.organize.models import Catalog
    return Catalog(target=target, entries=tuple(entries), git_repos_skipped=())


def _groupings(items):
    from fda.organize.models import Groupings
    return Groupings(items=tuple(items), overall_reason="")


class TestRoutePublic:
    def test_routes_each_grouping(self, tmp_path):
        from fda.organize.router import route
        entries = [
            _entry(0, subpath="Finance/Invoices"),
            _entry(1, subpath="Finance/Invoices"),
            _entry(2, subpath="Reports/Sales", ext=".pptx"),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([
            _grouping("Finance/Invoices", ["f000", "f001"]),
            _grouping("Reports/Sales", ["f002"], subpath="Reports/Sales"),
        ])
        backend = MagicMock()
        backend.complete.side_effect = [
            _skill_response(destination="sharepoint", reason="r1"),
            _skill_response(destination="sharepoint", reason="r2"),
        ]
        # Rewrite the test entries onto tmp_path so misfit relative_path
        # resolution succeeds (route() relative-paths against target_path).
        entries = [
            _entry(0, path=str(tmp_path / "Finance/Invoices/000.pdf")),
            _entry(1, path=str(tmp_path / "Finance/Invoices/001.pdf")),
            _entry(2, path=str(tmp_path / "Reports/Sales/002.pptx"), ext=".pptx"),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        assert report.version == "1.0"
        assert report.target_root == str(tmp_path)
        names = [c.name for c in report.categories]
        assert names == ["Finance/Invoices", "Reports/Sales"]
        assert all(c.destination == "sharepoint" for c in report.categories)
        assert all(c.low_confidence is False for c in report.categories)

    def test_misc_category_short_circuits_without_calling_backend(self, tmp_path):
        from fda.organize.router import route
        entries = [_entry(0, path=str(tmp_path / "Misc/a.pdf"), subpath="Misc")]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Misc", ["f000"], subpath="Misc")])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        backend.complete.assert_not_called()
        assert report.categories[0].destination == "s3"
        assert report.categories[0].low_confidence is True

    def test_all_extraction_failed_short_circuits_without_calling_backend(self, tmp_path):
        from fda.organize.router import route
        entries = [
            _entry(0, failed=True, path=str(tmp_path / "Finance/a.pdf")),
            _entry(1, failed=True, path=str(tmp_path / "Finance/b.pdf")),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Finance", ["f000", "f001"])])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        backend.complete.assert_not_called()
        assert report.categories[0].destination == "s3"
        assert report.categories[0].low_confidence is True

    def test_empty_grouping_is_skipped(self, tmp_path):
        from fda.organize.router import route
        catalog = _catalog([], target=str(tmp_path))
        groupings = _groupings([_grouping("Empty", [])])
        backend = MagicMock()
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        assert report.categories == ()

    def test_misfit_relative_path_resolved_from_catalog(self, tmp_path):
        from fda.organize.router import route
        entries = [
            _entry(0, path=str(tmp_path / "Finance/Invoices/inv.pdf")),
            _entry(1, ext=".csv", path=str(tmp_path / "Finance/Invoices/sales.csv")),
        ]
        catalog = _catalog(entries, target=str(tmp_path))
        groupings = _groupings([_grouping("Finance/Invoices", ["f000", "f001"])])
        backend = MagicMock()
        backend.complete.return_value = _skill_response(
            destination="sharepoint", reason="r",
            misfits=[{
                "path_id": "f001",
                "suggested_destination": "rdbms",
                "reason": "Tabular.",
            }],
        )
        report = route(
            catalog=catalog, groupings=groupings, target_path=tmp_path,
            backend=backend, logger=_Logger(),
        )
        misfit = report.categories[0].misfits[0]
        assert misfit.path_id == "f001"
        assert misfit.relative_path == "Finance/Invoices/sales.csv"
        assert misfit.suggested_destination == "rdbms"
