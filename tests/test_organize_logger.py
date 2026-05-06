# tests/test_organize_logger.py
"""Tests for fda.organize._logger.OrganizeLogger."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def logs_root(tmp_path):
    return tmp_path / "logs"


def _read_lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


def _parse_event_line(line: str) -> tuple[str, str, dict[str, object]]:
    """Returns (timestamp, event, fields_dict)."""
    match = re.match(r"^\[(\d{2}:\d{2}:\d{2}\.\d{3})\] (\w+) ?(.*)$", line)
    assert match, f"line did not match event format: {line!r}"
    ts, event, rest = match.groups()
    fields: dict[str, object] = {}
    for token in re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)', rest):
        key, raw = token
        if raw.startswith('"'):
            fields[key] = json.loads(raw)
        elif raw in ("true", "false"):
            fields[key] = raw == "true"
        else:
            try:
                fields[key] = int(raw)
            except ValueError:
                try:
                    fields[key] = float(raw)
                except ValueError:
                    fields[key] = raw
    return ts, event, fields


class TestDefaultPath:
    def test_uses_default_root_when_none(self, logs_root, monkeypatch):
        from fda.organize import _logger

        monkeypatch.setattr(_logger, "DEFAULT_LOG_ROOT", logs_root)
        target_basename = "my-target"

        log = _logger.OrganizeLogger(log_path=None, target_basename=target_basename)
        try:
            log.log("RUN_START", target=target_basename)
        finally:
            log.close()

        files = list(logs_root.glob("*.log"))
        assert len(files) == 1
        # Filename: <YYYYMMDD-HHMMSS-mmm>-<basename>.log
        assert re.match(r"^\d{8}-\d{6}-\d{3}-my-target\.log$", files[0].name)
        assert log.path == files[0]


class TestCustomPath:
    def test_uses_provided_path(self, logs_root, tmp_path):
        from fda.organize import _logger

        custom = tmp_path / "custom.log"
        log = _logger.OrganizeLogger(log_path=custom, target_basename="x")
        try:
            log.log("RUN_START", target="x")
        finally:
            log.close()
        assert log.path == custom
        assert custom.exists()


class TestDisableLogging:
    def test_log_path_false_disables_file_logging(self, tmp_path, capsys):
        from fda.organize import _logger

        seen: list[str] = []
        log = _logger.OrganizeLogger(
            log_path=False,
            target_basename="x",
            progress_callback=seen.append,
        )
        try:
            log.log("RUN_START", target="x")
        finally:
            log.close()
        assert log.path is None
        assert seen, "progress_callback should still receive events"


class TestEventFormat:
    def test_strings_are_json_encoded(self, tmp_path):
        from fda.organize import _logger

        path = tmp_path / "a.log"
        log = _logger.OrganizeLogger(log_path=path, target_basename="x")
        try:
            log.log("READER_FILE_DONE", path="/tmp/a b/c.txt", elapsed_ms=123, ok=True)
        finally:
            log.close()

        line = _read_lines(path)[0]
        ts, event, fields = _parse_event_line(line)
        assert event == "READER_FILE_DONE"
        assert fields["path"] == "/tmp/a b/c.txt"
        assert fields["elapsed_ms"] == 123
        assert fields["ok"] is True


class TestSpecialCharacters:
    def test_filename_with_newline_and_quote(self, tmp_path):
        from fda.organize import _logger

        path = tmp_path / "a.log"
        log = _logger.OrganizeLogger(log_path=path, target_basename="x")
        try:
            log.log("READER_FILE_DONE", path='/tmp/a "quoted"\nname.txt', elapsed_ms=1)
        finally:
            log.close()

        lines = _read_lines(path)
        assert len(lines) == 1, "embedded newline must not split the line"
        ts, event, fields = _parse_event_line(lines[0])
        assert fields["path"] == '/tmp/a "quoted"\nname.txt'

    def test_filename_with_unicode_preserved(self, tmp_path):
        from fda.organize import _logger

        path = tmp_path / "a.log"
        log = _logger.OrganizeLogger(log_path=path, target_basename="x")
        try:
            log.log("READER_FILE_DONE", path="/tmp/한글.txt", elapsed_ms=1)
        finally:
            log.close()
        ts, event, fields = _parse_event_line(_read_lines(path)[0])
        assert fields["path"] == "/tmp/한글.txt"


class TestMkdirFailure:
    def test_run_continues_when_logfile_cannot_open(self, tmp_path, caplog):
        from fda.organize import _logger

        # Point the logger at a path whose parent open will fail
        bad = tmp_path / "no-such-dir" / "deep.log"

        seen: list[str] = []
        with patch("fda.organize._logger.Path.mkdir", side_effect=PermissionError("nope")):
            with caplog.at_level(logging.WARNING, logger="fda.organize._logger"):
                log = _logger.OrganizeLogger(
                    log_path=bad,
                    target_basename="x",
                    progress_callback=seen.append,
                )
                try:
                    log.log("RUN_START", target="x")
                finally:
                    log.close()

        assert log.path is None
        assert seen, "progress_callback should still fire"
        assert any("could not open organize log" in r.message for r in caplog.records)


class TestProgressForwarding:
    def test_progress_callback_receives_each_event(self, tmp_path):
        from fda.organize import _logger

        path = tmp_path / "a.log"
        seen: list[str] = []
        log = _logger.OrganizeLogger(
            log_path=path,
            target_basename="x",
            progress_callback=seen.append,
        )
        try:
            log.log("RUN_START", target="x")
            log.log("RUN_END", status="success")
        finally:
            log.close()
        assert any("RUN_START" in s for s in seen)
        assert any("RUN_END" in s for s in seen)


class TestLongValueTruncation:
    def test_long_summary_truncated_before_encoding(self, tmp_path):
        from fda.organize import _logger

        path = tmp_path / "a.log"
        log = _logger.OrganizeLogger(log_path=path, target_basename="x")
        try:
            big = "a" * 5000
            log.log("READER_FILE_DONE", path="/tmp/x.txt", summary=big)
        finally:
            log.close()
        ts, event, fields = _parse_event_line(_read_lines(path)[0])
        # Default truncate length is 200 chars; full path is preserved.
        assert fields["path"] == "/tmp/x.txt"
        assert isinstance(fields["summary"], str)
        assert len(fields["summary"]) <= _logger.LONG_VALUE_TRUNCATE_CHARS + 1  # +"…" marker


class TestPathCollision:
    def test_collision_appends_pid_suffix(self, tmp_path, monkeypatch):
        from fda.organize import _logger

        # Override the auto-pick root and freeze the timestamp so we know
        # exactly which path the logger will try to open.
        monkeypatch.setattr(_logger, "DEFAULT_LOG_ROOT", tmp_path)
        monkeypatch.setattr(_logger, "_now_stamp", lambda: "20260506-001530-742")
        squat = tmp_path / "20260506-001530-742-x.log"
        squat.write_text("squatter\n")

        log = _logger.OrganizeLogger(log_path=None, target_basename="x")
        try:
            log.log("RUN_START", target="x")
        finally:
            log.close()
        assert log.path is not None
        assert log.path != squat
        assert "-pid" in log.path.name
        # Squatter content untouched.
        assert squat.read_text(encoding="utf-8") == "squatter\n"
