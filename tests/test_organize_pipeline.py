# tests/test_organize_pipeline.py
"""End-to-end tests for fda.organize.organize() — Reader -> Classifier -> PlanBuilder -> Executor -> Verifier."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fda.organize import organize, apply_plan
from fda.organize.models import Plan, PlanResult


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    (root / "b.txt").write_text("bbb")
    (root / ".DS_Store").write_bytes(b"\x00")

    # Minimal .docx (one Title paragraph) so the integration corpus exercises
    # the new format end-to-end.
    from docx import Document
    docx_path = root / "report.docx"
    d = Document()
    p = d.add_paragraph("Report Title")
    p.style = d.styles["Title"]
    d.save(str(docx_path))

    # Minimal .xlsx (one sheet, one header row).
    import openpyxl
    xlsx_path = root / "data.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Order ID", "Customer"])
    wb.save(str(xlsx_path))
    wb.close()

    # Minimal .pptx (one titled slide).
    from pptx import Presentation
    pptx_path = root / "deck.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Pipeline Demo"
    prs.save(str(pptx_path))

    # Minimal .csv (one header row, one data row).
    csv_path = root / "data.csv"
    csv_path.write_text("customer_id,order_date\n1,2024-01-01\n")

    return root


def _scripted_backend(workspace):
    """Parse the actual prompt body so the fake responds with real path_ids
    (path_ids are assigned in catalog order across ALL entries including
    junk, so '.DS_Store' shifts a.txt to f001 and b.txt to f002 — hardcoding
    f000/f001 would be wrong)."""
    backend = MagicMock()

    summary = json.dumps({"type_label": "text", "summary": "plain text"})
    taxonomy = json.dumps({
        "categories": [{
            "category_name": "Texts", "subpath": "Texts",
            "description": "plain text files", "criteria": "text-shaped",
        }],
        "fallback_category": {
            "category_name": "Misc", "subpath": "Misc",
            "description": "fallback", "criteria": "could not categorize",
        },
    })

    def fake_complete(*, messages, **_):
        body = messages[0]["content"]
        if "PATH:" in body:
            return summary
        # Stage A or Stage B is JSON
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return ""
        if "BATCH" in parsed:
            return json.dumps({
                "assignments": [
                    {"path_id": e["path_id"], "category_name": "Texts"}
                    for e in parsed["BATCH"]
                ],
            })
        if "CATALOG" in parsed:
            return taxonomy
        return ""

    backend.complete.side_effect = fake_complete
    return backend


class TestOrganize:
    def test_full_pipeline(self, workspace):
        backend = _scripted_backend(workspace)
        result = organize(
            str(workspace),
            "sort",
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert isinstance(result, PlanResult)
        # Both files moved into Texts/
        assert (workspace / "Texts" / "a.txt").exists()
        assert (workspace / "Texts" / "b.txt").exists()
        # Junk deleted
        assert not (workspace / ".DS_Store").exists()
        # New format files were also categorized (the scripted backend
        # assigns every entry to "Texts", so they all land in the same dir).
        assert (workspace / "Texts" / "report.docx").exists()
        assert (workspace / "Texts" / "data.xlsx").exists()
        assert (workspace / "Texts" / "deck.pptx").exists()
        assert (workspace / "Texts" / "data.csv").exists()
        assert result.log_path is not None
        assert "Texts" in result.summary or "text-shaped" in result.summary

    def test_preview_returns_plan_without_executing(self, workspace):
        backend = _scripted_backend(workspace)
        plan = organize(
            str(workspace),
            "",
            preview=True,
            backend=backend,
            allowed_roots=[workspace.parent],
        )
        assert isinstance(plan, Plan)
        assert (workspace / "a.txt").exists()
        assert (workspace / ".DS_Store").exists()
        assert (workspace / "report.docx").exists()
        assert (workspace / "data.xlsx").exists()
        assert (workspace / "deck.pptx").exists()
        assert (workspace / "data.csv").exists()
        assert plan.log_path is not None

    def test_invalid_target_raises(self, workspace):
        backend = MagicMock()
        with pytest.raises(ValueError):
            organize(
                str(workspace.parent),  # outside allowed_roots
                "",
                backend=backend,
                allowed_roots=[workspace],
            )
