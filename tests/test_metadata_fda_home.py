"""Tests for fda.metadata.fda_home() env-var override."""
from __future__ import annotations

from pathlib import Path


class TestFdaHome:
    def test_default_is_home_dot_fda(self, monkeypatch):
        from fda.metadata import fda_home
        monkeypatch.delenv("FDA_HOME", raising=False)
        assert fda_home() == Path.home() / ".fda"

    def test_env_override_used_when_set(self, monkeypatch, tmp_path):
        from fda.metadata import fda_home
        monkeypatch.setenv("FDA_HOME", str(tmp_path / "fake"))
        assert fda_home() == tmp_path / "fake"

    def test_empty_env_value_falls_back_to_default(self, monkeypatch):
        """FDA_HOME='' should NOT override (empty string is falsy)."""
        from fda.metadata import fda_home
        monkeypatch.setenv("FDA_HOME", "")
        assert fda_home() == Path.home() / ".fda"
