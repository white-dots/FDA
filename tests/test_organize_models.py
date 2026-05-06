"""Tests for fda.organize.models — dataclass invariants."""

import pytest
from dataclasses import FrozenInstanceError

from fda.organize.models import (
    OperationKind,
    Operation,
    Plan,
    OperationOutcome,
    PlanResult,
)


def _make_op(kind=OperationKind.MOVE, source="/t/a.txt", destination="/t/b/a.txt"):
    return Operation(kind=kind, source=source, destination=destination, reason="test")


class TestOperationKind:
    def test_values_are_lowercase_strings(self):
        assert OperationKind.CREATE_DIR.value == "create_dir"
        assert OperationKind.MOVE.value == "move"
        assert OperationKind.DELETE.value == "delete"


class TestOperation:
    def test_construct(self):
        op = _make_op()
        assert op.kind == OperationKind.MOVE
        assert op.source == "/t/a.txt"
        assert op.destination == "/t/b/a.txt"
        assert op.reason == "test"

    def test_frozen(self):
        op = _make_op()
        with pytest.raises(FrozenInstanceError):
            op.reason = "changed"  # type: ignore[misc]


class TestPlan:
    def test_construct_with_tuple_operations(self):
        plan = Plan(
            target="/t",
            instructions="sort",
            operations=(_make_op(),),
            grouping_summary="one move",
        )
        assert isinstance(plan.operations, tuple)
        assert len(plan.operations) == 1

    def test_frozen(self):
        plan = Plan(target="/t", instructions="", operations=(), grouping_summary="")
        with pytest.raises(FrozenInstanceError):
            plan.target = "/x"  # type: ignore[misc]


class TestOperationOutcome:
    def test_defaults(self):
        op = _make_op()
        outcome = OperationOutcome(operation_index=0, operation=op, status="applied")
        assert outcome.error is None
        assert outcome.rescue_note is None

    def test_status_values(self):
        op = _make_op()
        for status in ("applied", "rescued", "skipped", "failed"):
            OperationOutcome(operation_index=0, operation=op, status=status)


class TestPlanResult:
    def test_construct(self):
        plan = Plan(target="/t", instructions="", operations=(), grouping_summary="")
        result = PlanResult(
            plan=plan,
            outcomes=(),
            leftover_empty_dirs=(),
            discrepancies=(),
            repos_skipped=(),
            summary="nothing to do",
        )
        assert result.summary == "nothing to do"
        assert isinstance(result.outcomes, tuple)
        assert isinstance(result.leftover_empty_dirs, tuple)


class TestCatalogEntry:
    def test_construct_and_immutable(self):
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/a.txt",
            ext=".txt",
            size_bytes=10,
            summary="text",
            type_label="text",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
        )
        assert e.path_id == "f000"
        with pytest.raises(Exception):  # FrozenInstanceError on dataclass
            e.path = "/tmp/b.txt"  # type: ignore[misc]


class TestCatalog:
    def test_construct_with_entries_and_skipped_repos(self):
        from fda.organize.models import Catalog, CatalogEntry

        e = CatalogEntry(
            path_id="f000", path="/tmp/a.txt", ext=".txt", size_bytes=1,
            summary="x", type_label="text", is_junk=False,
            summary_failed=False, extract_status="ok",
        )
        c = Catalog(target="/tmp", entries=(e,), git_repos_skipped=("/tmp/repo",))
        assert c.entries[0] is e
        assert c.git_repos_skipped == ("/tmp/repo",)


class TestTaxonomyCategory:
    def test_construct(self):
        from fda.organize.models import TaxonomyCategory

        cat = TaxonomyCategory(
            category_name="Invoices",
            subpath="Finance/Invoices",
            description="Invoices and receipts.",
            criteria="Files that look like invoices.",
        )
        assert cat.category_name == "Invoices"


class TestTaxonomy:
    def test_category_name_set(self):
        from fda.organize.models import Taxonomy, TaxonomyCategory

        a = TaxonomyCategory("A", "A/", "", "")
        b = TaxonomyCategory("B", "B/", "", "")
        fb = TaxonomyCategory("Misc", "Misc/", "", "")
        t = Taxonomy(categories=(a, b), fallback_category=fb)
        assert t.category_name_set() == {"A", "B", "Misc"}


class TestGrouping:
    def test_carries_path_ids(self):
        from fda.organize.models import Grouping

        g = Grouping(
            category="Invoices",
            subpath="Finance/Invoices",
            file_ids=("f000", "f001"),
            reason="invoice-shaped",
        )
        assert g.file_ids == ("f000", "f001")


class TestGroupings:
    def test_construct(self):
        from fda.organize.models import Grouping, Groupings

        g = Grouping("A", "A/", ("f000",), "")
        gs = Groupings(items=(g,), overall_reason="grouped")
        assert gs.items[0] is g
        assert gs.overall_reason == "grouped"


class TestExtractionResult:
    def test_default_note_is_empty(self):
        from fda.organize.models import ExtractionResult

        r = ExtractionResult(text="hi", status="ok")
        assert r.note == ""

    @pytest.mark.parametrize("status", ["ok", "no_extractor", "tool_missing", "failed"])
    def test_each_status_value_accepted(self, status):
        from fda.organize.models import ExtractionResult

        r = ExtractionResult(text=None, status=status, note="")
        assert r.status == status


class TestPlanCarriesLogPath:
    def test_plan_default_log_path_is_none(self):
        from fda.organize.models import Plan

        p = Plan(target="/tmp", instructions="", operations=(), grouping_summary="")
        assert p.log_path is None

    def test_plan_accepts_log_path(self):
        from fda.organize.models import Plan

        p = Plan(
            target="/tmp", instructions="", operations=(), grouping_summary="",
            log_path="/tmp/x.log",
        )
        assert p.log_path == "/tmp/x.log"


class TestPlanResultCarriesLogPath:
    def test_default_log_path_is_none(self):
        from fda.organize.models import Plan, PlanResult

        p = Plan(target="/tmp", instructions="", operations=(), grouping_summary="")
        r = PlanResult(
            plan=p, outcomes=(), leftover_empty_dirs=(),
            discrepancies=(), repos_skipped=(), summary="",
        )
        assert r.log_path is None
