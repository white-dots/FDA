# tests/test_organize_business_context.py
"""End-to-end check that organize() loads ~/.fda/business_context.md and
forwards its text to the classifier."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_file(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fake_target(tmp_path: Path) -> Path:
    target = tmp_path / "corpus"
    target.mkdir()
    _make_file(target / "a.txt", "alpha file")
    _make_file(target / "b.txt", "beta file")
    return target


def test_organize_passes_business_context_to_classifier(
    fake_target, tmp_path, monkeypatch,
):
    """If ~/.fda/business_context.md exists, organize() must forward its
    text into classifier.classify() as the `business_context` kwarg."""
    from fda.organize import classifier, organize

    fda_home = tmp_path / "fda_home"
    fda_home.mkdir()
    bc_text = "## Folder Granularity\nTreat orders as one bucket."
    (fda_home / "business_context.md").write_text(bc_text, encoding="utf-8")
    monkeypatch.setenv("FDA_HOME", str(fda_home))

    backend = MagicMock()

    with patch.object(classifier, "classify") as fake_classify:
        from fda.organize.models import Groupings
        fake_classify.return_value = Groupings(items=(), overall_reason="")
        organize(
            str(fake_target),
            instructions="",
            backend=backend,
            allowed_roots=[fake_target.parent],
            preview=True,
            route=False,
            metadata=False,
        )

    assert fake_classify.called
    kwargs = fake_classify.call_args.kwargs
    assert kwargs.get("business_context") == bc_text


def test_organize_missing_business_context_passes_empty_string(
    fake_target, tmp_path, monkeypatch,
):
    """If the file is absent, organize() forwards an empty string —
    classifier behaves exactly as today."""
    from fda.organize import classifier, organize

    fda_home = tmp_path / "fda_home"
    fda_home.mkdir()  # no business_context.md inside
    monkeypatch.setenv("FDA_HOME", str(fda_home))

    backend = MagicMock()

    with patch.object(classifier, "classify") as fake_classify:
        from fda.organize.models import Groupings
        fake_classify.return_value = Groupings(items=(), overall_reason="")
        organize(
            str(fake_target),
            instructions="",
            backend=backend,
            allowed_roots=[fake_target.parent],
            preview=True,
            route=False,
            metadata=False,
        )

    assert fake_classify.called
    kwargs = fake_classify.call_args.kwargs
    assert kwargs.get("business_context") == ""
