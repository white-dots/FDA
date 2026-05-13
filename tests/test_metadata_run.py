# tests/test_metadata_run.py
"""Tests for fda.metadata.run() orchestration."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _catalog_with(entries):
    from fda.organize.models import Catalog
    return Catalog(target="/t", entries=tuple(entries),
                   git_repos_skipped=())


def _entry(idx, *, path, ext=".pdf", failed=False, summary="hello world"):
    from fda.organize.models import CatalogEntry
    return CatalogEntry(
        path_id=f"f{idx:03d}", path=path, ext=ext, size_bytes=100,
        summary=summary, type_label="doc", is_junk=False,
        summary_failed=failed,
        extract_status="failed" if failed else "ok",
        verbatim_head=summary, sections=(),
    )


def _good_response(n):
    return json.dumps([{
        "department": "finance", "document_type": "invoice",
        "confidentiality": "confidential", "summary": "x",
        "keywords": {"ko": [], "en": ["invoice"]}, "confidence": 0.9,
    } for _ in range(n)])


class TestRun:
    def test_happy_path_one_batch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata import run
        # Create two real files so sha256/mime/mtime work.
        f0 = tmp_path / "a.pdf"; f0.write_text("hello")
        f1 = tmp_path / "b.pdf"; f1.write_text("world")
        catalog = _catalog_with([
            _entry(0, path=str(f0)), _entry(1, path=str(f1)),
        ])
        backend = MagicMock()
        backend.complete.return_value = _good_response(2)
        logger = MagicMock()
        report = run(target_path=tmp_path, catalog=catalog,
                     backend=backend, logger=logger,
                     progress_callback=None)
        assert report.files_seen == 2
        assert report.files_classified == 2
        assert report.files_failed == 0
        assert report.batches_total == 1
        assert report.batches_retried == 0
        # DB row created.
        import sqlite3
        conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
        n = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert n == 2
        conn.close()

    def test_failed_extract_creates_fail_closed_row(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata import run
        f0 = tmp_path / "garbled.pdf"; f0.write_bytes(b"\x00\x01")
        catalog = _catalog_with([
            _entry(0, path=str(f0), failed=True, summary=""),
        ])
        backend = MagicMock()
        backend.complete.return_value = _good_response(0)
        logger = MagicMock()
        report = run(target_path=tmp_path, catalog=catalog,
                     backend=backend, logger=logger)
        assert report.files_seen == 1
        # Extraction failed → row stored with fail-closed defaults,
        # NOT counted as "classified" by the LLM.
        assert report.files_failed == 1
        import sqlite3
        conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
        row = conn.execute(
            "SELECT confidentiality, fail_closed_override, extract_status "
            "FROM documents"
        ).fetchone()
        assert row == ("restricted", 1, "failed")
        conn.close()

    def test_audit_sidecar_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from fda.metadata import run
        f0 = tmp_path / "a.pdf"; f0.write_text("hi")
        catalog = _catalog_with([_entry(0, path=str(f0))])
        backend = MagicMock()
        backend.complete.return_value = _good_response(1)
        logger = MagicMock()
        report = run(target_path=tmp_path, catalog=catalog,
                     backend=backend, logger=logger)
        sidecar = tmp_path / ".fda" / "runs" / f"{report.run_id}.md"
        assert sidecar.exists()
        text = sidecar.read_text(encoding="utf-8")
        assert "Files seen: 1" in text
        assert report.run_id in text
