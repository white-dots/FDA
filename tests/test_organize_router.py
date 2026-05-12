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
