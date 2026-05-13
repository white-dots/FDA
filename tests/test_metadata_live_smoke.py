# tests/test_metadata_live_smoke.py
"""Live smoke test — runs the metadata stage against the synthetic corpus
using a real Sonnet backend. Gated on FDA_LIVE_SMOKE=1.

Asserts STRUCTURAL correctness only:
- Every classifiable file gets a row with a valid vocab triple.
- Pydantic validation passed (no records would have been stored otherwise).
- Fail-closed override fires on at least one of the deliberately
  low-confidence fixtures (empty.txt / garbled.bin).

Does NOT assert specific department/document_type labels — those are
Claude's judgment.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _live() -> bool:
    return os.environ.get("FDA_LIVE_SMOKE") == "1"


@pytest.mark.skipif(not _live(),
                    reason="set FDA_LIVE_SMOKE=1 to run against real Sonnet")
def test_metadata_live_smoke_on_fixture_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("FDA_HOME", str(tmp_path / ".fda"))
    from fda.claude_backend import get_claude_backend
    from fda.metadata import run as metadata_run
    from fda.metadata.vocab import (
        CONFIDENTIALITY, DEPARTMENTS, DOCUMENT_TYPES,
    )
    from fda.organize import reader
    from fda.organize._logger import OrganizeLogger

    # __file__-anchored so pytest CWD doesn't matter.
    fixture_dir = Path(__file__).parent / "fixtures" / "metadata_corpus"
    expected_files = sorted(p.name for p in fixture_dir.iterdir() if p.is_file())
    assert len(expected_files) == 10, (
        f"expected exactly 10 fixtures, got {len(expected_files)}: {expected_files}"
    )

    backend = get_claude_backend()
    olog = OrganizeLogger(log_path=None, target_basename="smoke",
                          progress_callback=lambda m: print(m))
    catalog = reader.read(fixture_dir, backend=backend, logger=olog)
    report = metadata_run(
        target_path=fixture_dir, catalog=catalog,
        backend=backend, logger=olog,
        progress_callback=lambda m: print(m),
    )
    # All 10 fixtures must be seen — no silent reader-side skip.
    assert report.files_seen == 10, (
        f"expected 10 files seen, got {report.files_seen}"
    )

    import sqlite3
    conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
    rows = conn.execute(
        "SELECT department, document_type, confidentiality, "
        "fail_closed_override FROM documents"
    ).fetchall()
    # 10 distinct sha256 → 10 rows (no two fixtures share bytes).
    assert len(rows) == 10, f"expected 10 documents rows, got {len(rows)}"
    for dep, dt, conf, fc in rows:
        assert dep in DEPARTMENTS, f"unknown department: {dep!r}"
        assert dt in DOCUMENT_TYPES, f"unknown document_type: {dt!r}"
        assert conf in CONFIDENTIALITY, f"unknown confidentiality: {conf!r}"
    # Both empty.txt and garbled.bin must produce fail-closed rows.
    # We verify by joining documents to document_paths (the path is the
    # signal of which fixture a row came from).
    fail_closed_paths = conn.execute(
        "SELECT p.path FROM documents d "
        "JOIN document_paths p ON p.sha256 = d.sha256 "
        "WHERE d.fail_closed_override = 1"
    ).fetchall()
    fail_closed_names = {Path(p[0]).name for p in fail_closed_paths}
    # At minimum the two deliberately-broken fixtures must be fail-closed.
    # Other fixtures may also be fail-closed if Sonnet reports low
    # confidence — that's acceptable.
    assert "empty.txt" in fail_closed_names, (
        f"empty.txt was not fail-closed; got {fail_closed_names}"
    )
    assert "garbled.bin" in fail_closed_names, (
        f"garbled.bin was not fail-closed; got {fail_closed_names}"
    )
    conn.close()
