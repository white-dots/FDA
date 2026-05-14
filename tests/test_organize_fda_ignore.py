# tests/test_organize_fda_ignore.py
"""Tests for fda.organize._fda_ignore."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_builtin_defaults_contains_expected_names():
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS
    assert ".fda-ignore" in BUILTIN_DEFAULTS
    assert "manifest.csv" in BUILTIN_DEFAULTS
    assert "README.md" in BUILTIN_DEFAULTS
    assert "README.*" in BUILTIN_DEFAULTS
    assert "LICENSE" in BUILTIN_DEFAULTS
    assert "LICENSE.*" in BUILTIN_DEFAULTS


def test_builtin_defaults_is_frozenset():
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS
    assert isinstance(BUILTIN_DEFAULTS, frozenset)


def test_load_patterns_returns_defaults_when_no_file(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    result = load_patterns(tmp_path)
    # Defaults first (sorted), then user (empty).
    assert result == tuple(sorted(BUILTIN_DEFAULTS))


def test_load_patterns_merges_user_patterns_with_defaults(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").write_text("inventory.csv\nNOTES.md\n")
    result = load_patterns(tmp_path)
    # Defaults appear first, then user patterns in file order.
    assert result == tuple(sorted(BUILTIN_DEFAULTS)) + ("inventory.csv", "NOTES.md")


def test_load_patterns_strips_comments_and_blank_lines(tmp_path):
    from fda.organize._fda_ignore import load_patterns
    (tmp_path / ".fda-ignore").write_text(
        "# a comment\n"
        "\n"
        "manifest.csv\n"
        "report-*.txt  # trailing comment\n"
        "   # full-line comment with leading spaces\n"
        "\n"
    )
    result = load_patterns(tmp_path)
    user = result[len(result) - 2:]
    assert user == ("manifest.csv", "report-*.txt")


def test_load_patterns_empty_or_comments_only_returns_defaults(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").write_text(
        "# just comments\n"
        "\n"
        "   # another\n"
    )
    result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))


def test_load_patterns_falls_back_to_defaults_on_decode_error(tmp_path, caplog):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    # Bytes that are not valid UTF-8 (0xff start byte).
    (tmp_path / ".fda-ignore").write_bytes(b"\xff\xfe\xfd")
    with caplog.at_level("WARNING"):
        result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))
    assert any(".fda-ignore unreadable" in r.message for r in caplog.records)


def test_load_patterns_falls_back_to_defaults_on_unreadable_file(
    tmp_path, caplog, monkeypatch,
):
    from pathlib import Path
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").write_text("manifest.csv\n")
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == ".fda-ignore":
            raise PermissionError("forced for test")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with caplog.at_level("WARNING"):
        result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))
    assert any(".fda-ignore unreadable" in r.message for r in caplog.records)


def test_load_patterns_when_fda_ignore_is_directory(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    (tmp_path / ".fda-ignore").mkdir()
    result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS))


def test_load_patterns_follows_symlink_to_regular_file(tmp_path):
    from fda.organize._fda_ignore import BUILTIN_DEFAULTS, load_patterns
    real = tmp_path / "real.txt"
    real.write_text("inventory.csv\n")
    (tmp_path / ".fda-ignore").symlink_to(real)
    result = load_patterns(tmp_path)
    assert result == tuple(sorted(BUILTIN_DEFAULTS)) + ("inventory.csv",)
