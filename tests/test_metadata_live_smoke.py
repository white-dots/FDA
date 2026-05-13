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
    monkeypatch.setenv("HOME", str(tmp_path))
    from fda.claude_backend import get_claude_backend
    from fda.metadata import run as metadata_run
    from fda.metadata.vocab import (
        CONFIDENTIALITY, DEPARTMENTS, DOCUMENT_TYPES,
    )
    from fda.organize import reader
    from fda.organize._logger import OrganizeLogger

    fixture_dir = Path("tests/fixtures/metadata_corpus").resolve()
    backend = get_claude_backend()
    olog = OrganizeLogger(log_path=None, target_basename="smoke",
                          progress_callback=lambda m: print(m))
    catalog = reader.read(fixture_dir, backend=backend, logger=olog)
    report = metadata_run(
        target_path=fixture_dir, catalog=catalog,
        backend=backend, logger=olog,
        progress_callback=lambda m: print(m),
    )
    assert report.files_seen >= 9

    import sqlite3
    conn = sqlite3.connect(tmp_path / ".fda" / "metadata.db")
    rows = conn.execute(
        "SELECT department, document_type, confidentiality, "
        "fail_closed_override FROM documents"
    ).fetchall()
    assert len(rows) == report.files_seen
    for dep, dt, conf, fc in rows:
        assert dep in DEPARTMENTS
        assert dt in DOCUMENT_TYPES
        assert conf in CONFIDENTIALITY
    # At least one row must be the fail-closed empty/garbled file.
    assert any(fc == 1 for _, _, _, fc in rows), \
        "expected at least one fail_closed_override row"
    conn.close()
