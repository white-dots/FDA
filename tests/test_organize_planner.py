"""Tests for fda.organize.planner — happy path, validation rejection, missed submit."""

import pytest
from pathlib import Path

from fda.organize import planner
from fda.organize.models import OperationKind


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / "b.txt").write_text("world")
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return root


def _valid_submit(workspace):
    return ("submit_plan", {
        "operations": [
            {
                "kind": "create_dir",
                "destination": str(workspace / "Texts"),
                "reason": "group text files",
            },
            {
                "kind": "move",
                "source": str(workspace / "a.txt"),
                "destination": str(workspace / "Texts" / "a.txt"),
                "reason": "text file",
            },
        ],
        "grouping_summary": "grouped texts",
    })


class TestBuildPlanHappyPath:
    def test_returns_plan_when_submit_called(self, workspace, stub_claude_backend):
        backend = stub_claude_backend([_valid_submit(workspace)])
        plan = planner.build_plan(workspace, "sort", backend=backend)
        assert plan.target == str(workspace)
        assert plan.instructions == "sort"
        assert plan.grouping_summary == "grouped texts"
        assert len(plan.operations) == 2
        assert plan.operations[0].kind == OperationKind.CREATE_DIR
        assert plan.operations[1].kind == OperationKind.MOVE
        assert plan.operations[1].reason == "text file"


class TestBuildPlanValidationRejection:
    def test_invalid_then_valid_submission_succeeds(self, workspace, stub_claude_backend):
        bad = ("submit_plan", {
            "operations": [
                {
                    "kind": "delete",
                    "source": str(workspace / "a.txt"),  # not junk, not empty
                    "reason": "want to delete",
                },
            ],
            "grouping_summary": "bad",
        })
        good = _valid_submit(workspace)
        backend = stub_claude_backend([bad], [good])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert len(plan.operations) == 2  # the second submission was accepted

    def test_all_or_nothing_on_partial_invalid(self, workspace, stub_claude_backend):
        # One valid op + one invalid op in the same submission -> entire
        # submission rejected; planner can resubmit.
        mixed = ("submit_plan", {
            "operations": [
                {
                    "kind": "create_dir",
                    "destination": str(workspace / "Texts"),
                    "reason": "ok",
                },
                {
                    "kind": "move",
                    "source": str(workspace / "a.txt"),
                    "destination": str(workspace / "repo" / "a.txt"),  # into git repo
                    "reason": "bad",
                },
            ],
            "grouping_summary": "mixed",
        })
        good = _valid_submit(workspace)
        backend = stub_claude_backend([mixed], [good])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert plan.grouping_summary == "grouped texts"


class TestBuildPlanMissedSubmit:
    def test_raises_when_submit_never_called(self, workspace, stub_claude_backend):
        # Stub does nothing; the planner's _submitted flag stays False.
        backend = stub_claude_backend([])  # zero iterations
        with pytest.raises(planner.PlannerDidNotSubmitError):
            planner.build_plan(workspace, "", backend=backend)


class TestSubmitPlanIdempotenceAndEmpty:
    def test_first_accepted_plan_is_kept_when_resubmitted_empty(
        self, workspace, stub_claude_backend
    ):
        # Models sometimes follow up an accepted plan with an empty resubmit.
        # The first plan must NOT be overwritten.
        good = _valid_submit(workspace)
        empty = ("submit_plan", {"operations": [], "grouping_summary": "oops"})
        backend = stub_claude_backend([good], [empty])
        plan = planner.build_plan(workspace, "", backend=backend)
        assert len(plan.operations) == 2  # not overwritten by the empty resubmit
        assert plan.grouping_summary == "grouped texts"

    def test_empty_plan_without_prior_submit_is_rejected(
        self, workspace, stub_claude_backend
    ):
        # An empty operations list with no prior submission must be rejected,
        # not silently accepted as a no-op plan.
        empty = ("submit_plan", {"operations": [], "grouping_summary": "nothing"})
        backend = stub_claude_backend([empty])  # only the empty submit, no follow-up
        with pytest.raises(planner.PlannerDidNotSubmitError):
            planner.build_plan(workspace, "", backend=backend)


class TestExecRead:
    def test_text_file_returns_contents(self, workspace):
        out = planner._exec_read(workspace, {"path": "a.txt"})
        assert out == "hello"

    def test_binary_extension_returns_stub_not_garbage(self, workspace):
        # An image file written with bytes — _exec_read must not return mojibake.
        img = workspace / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        out = planner._exec_read(workspace, {"path": "photo.png"})
        assert out.startswith("(binary file: .png")
        assert "PNG" not in out  # stub, not the raw header bytes

    def test_pdf_with_pdftotext_extracts_text(self, workspace, monkeypatch):
        pdf = workspace / "doc.pdf"
        pdf.write_bytes(b"%PDF-fake")
        import subprocess as _sp
        class _Result:
            stdout = "Invoice 12345\nCustomer: ACME\n"
            returncode = 0
        monkeypatch.setattr("shutil.which",
                            lambda name: "/fake/pdftotext" if name == "pdftotext" else None)
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: _Result())
        out = planner._extract_pdf_text(pdf)
        assert "Invoice 12345" in out

    def test_pdf_without_pdftotext_returns_stub(self, workspace, monkeypatch):
        pdf = workspace / "doc.pdf"
        pdf.write_bytes(b"%PDF-fake-content")
        monkeypatch.setattr("shutil.which", lambda name: None)
        out = planner._extract_pdf_text(pdf)
        assert "PDF" in out and "pdftotext" in out
