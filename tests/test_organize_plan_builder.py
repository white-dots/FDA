"""Tests for fda.organize.plan_builder.build()."""

from __future__ import annotations

from pathlib import Path

import pytest


def _grouping(category, subpath, file_ids, reason="grp"):
    from fda.organize.models import Grouping
    return Grouping(category=category, subpath=subpath, file_ids=tuple(file_ids), reason=reason)


def _groupings(*items, overall_reason="all"):
    from fda.organize.models import Groupings
    return Groupings(items=tuple(items), overall_reason=overall_reason)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


class TestHappyPath:
    def test_groupings_become_create_dir_plus_move_ops(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("b")
        path_by_id = {
            "f000": str(workspace / "a.txt"),
            "f001": str(workspace / "b.txt"),
        }
        groupings = _groupings(
            _grouping("Texts", "Texts", ["f000", "f001"], reason="text-shaped"),
        )

        plan = plan_builder.build(
            target_dir=str(workspace),
            groupings=groupings,
            path_by_id=path_by_id,
            junk_paths=[],
        )

        kinds = [op.kind for op in plan.operations]
        # Order: create_dir then moves
        assert kinds[0] == OperationKind.CREATE_DIR
        assert kinds.count(OperationKind.MOVE) == 2
        # Reason inherits from grouping
        moves = [op for op in plan.operations if op.kind == OperationKind.MOVE]
        assert all(op.reason == "text-shaped" for op in moves)


class TestPathIdResolution:
    def test_unknown_path_id_raises_with_id(self, workspace):
        from fda.organize import plan_builder
        plan_builder_error = plan_builder.PlanBuilderError

        path_by_id = {"f000": str(workspace / "a.txt")}
        (workspace / "a.txt").write_text("a")
        groupings = _groupings(
            _grouping("X", "X", ["f000", "f999"]),
        )
        with pytest.raises(plan_builder_error) as exc:
            plan_builder.build(str(workspace), groupings, path_by_id, [])
        assert "f999" in str(exc.value)


class TestSubpathMerging:
    def test_two_groupings_same_subpath_share_one_create_dir(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("b")
        path_by_id = {
            "f000": str(workspace / "a.txt"),
            "f001": str(workspace / "b.txt"),
        }
        groupings = _groupings(
            _grouping("A", "Same", ["f000"]),
            _grouping("B", "Same", ["f001"]),
        )
        plan = plan_builder.build(str(workspace), groupings, path_by_id, [])
        create_dirs = [op for op in plan.operations if op.kind == OperationKind.CREATE_DIR]
        assert len(create_dirs) == 1


class TestDuplicatePathIdAcrossGroupings:
    def test_raises_with_id_and_path(self, workspace):
        from fda.organize import plan_builder

        (workspace / "a.txt").write_text("a")
        path_by_id = {"f000": str(workspace / "a.txt")}
        groupings = _groupings(
            _grouping("A", "A", ["f000"]),
            _grouping("B", "B", ["f000"]),
        )
        with pytest.raises(plan_builder.PlanBuilderError) as exc:
            plan_builder.build(str(workspace), groupings, path_by_id, [])
        msg = str(exc.value)
        assert "f000" in msg and str(workspace / "a.txt") in msg


class TestIntraGroupDuplicate:
    def test_dedupes_and_logs(self, workspace, caplog):
        from fda.organize import plan_builder

        (workspace / "a.txt").write_text("a")
        path_by_id = {"f000": str(workspace / "a.txt")}
        groupings = _groupings(
            _grouping("A", "A", ["f000", "f000"]),
        )
        plan = plan_builder.build(str(workspace), groupings, path_by_id, [])
        from fda.organize.models import OperationKind
        moves = [op for op in plan.operations if op.kind == OperationKind.MOVE]
        assert len(moves) == 1


class TestSourceMissing:
    def test_missing_source_dropped_with_log(self, workspace, caplog):
        from fda.organize import plan_builder

        path_by_id = {"f000": str(workspace / "ghost.txt")}  # no file on disk
        groupings = _groupings(_grouping("A", "A", ["f000"]))
        with pytest.raises(plan_builder.PlanBuilderError) as exc:
            plan_builder.build(str(workspace), groupings, path_by_id, [])
        assert "all operations dropped" in str(exc.value)


class TestJunkOnly:
    def test_delete_only_plan(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        junk = workspace / ".DS_Store"
        junk.write_bytes(b"\x00")
        plan = plan_builder.build(
            str(workspace),
            _groupings(),
            path_by_id={},
            junk_paths=[str(junk)],
        )
        kinds = [op.kind for op in plan.operations]
        assert kinds == [OperationKind.DELETE]


class TestEmptyEverything:
    def test_no_groupings_no_junk_returns_empty_plan(self, workspace):
        from fda.organize import plan_builder

        plan = plan_builder.build(str(workspace), _groupings(), {}, [])
        assert plan.operations == ()


class TestAllDropped:
    def test_raises_with_summary(self, workspace):
        from fda.organize import plan_builder

        # Source missing on disk -> dropped; non-empty input still ends in zero ops.
        path_by_id = {"f000": str(workspace / "ghost.txt")}
        groupings = _groupings(_grouping("A", "A", ["f000"]))
        with pytest.raises(plan_builder.PlanBuilderError):
            plan_builder.build(str(workspace), groupings, path_by_id, [])


class TestSubpathSanitization:
    def test_control_characters_stripped(self, workspace):
        from fda.organize import plan_builder

        (workspace / "a.txt").write_text("a")
        path_by_id = {"f000": str(workspace / "a.txt")}
        groupings = _groupings(_grouping("A", "Bad\x00Name", ["f000"]))
        plan = plan_builder.build(str(workspace), groupings, path_by_id, [])
        for op in plan.operations:
            if op.destination:
                assert "\x00" not in op.destination

    def test_trailing_dots_stripped(self, workspace):
        from fda.organize import plan_builder

        (workspace / "a.txt").write_text("a")
        path_by_id = {"f000": str(workspace / "a.txt")}
        groupings = _groupings(_grouping("A", "Stuff.../", ["f000"]))
        plan = plan_builder.build(str(workspace), groupings, path_by_id, [])
        for op in plan.operations:
            if op.destination and op.destination != str(workspace):
                # Each component has been stripped of trailing dots
                for part in Path(op.destination).relative_to(workspace).parts:
                    assert not part.endswith(".")
                    assert not part.startswith(".") or part == ".git"


class TestSubpathTraversal:
    def test_double_dot_raises(self, workspace):
        from fda.organize import plan_builder

        (workspace / "a.txt").write_text("a")
        path_by_id = {"f000": str(workspace / "a.txt")}
        groupings = _groupings(_grouping("A", "../escape", ["f000"]))
        with pytest.raises(plan_builder.PlanBuilderError):
            plan_builder.build(str(workspace), groupings, path_by_id, [])


class TestNoOpMove:
    def test_already_in_destination_dropped(self, workspace):
        from fda.organize import plan_builder

        sub = workspace / "Texts"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        path_by_id = {"f000": str(sub / "a.txt")}
        groupings = _groupings(_grouping("Texts", "Texts", ["f000"]))
        with pytest.raises(plan_builder.PlanBuilderError) as exc:
            plan_builder.build(str(workspace), groupings, path_by_id, [])
        assert "all operations dropped" in str(exc.value)


class TestPlannedCollision:
    def test_two_files_same_basename_renamed(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        (workspace / "x").mkdir()
        (workspace / "y").mkdir()
        (workspace / "x" / "report.pdf").write_text("a")
        (workspace / "y" / "report.pdf").write_text("b")
        path_by_id = {
            "f000": str(workspace / "x" / "report.pdf"),
            "f001": str(workspace / "y" / "report.pdf"),
        }
        groupings = _groupings(_grouping("R", "R", ["f000", "f001"]))
        plan = plan_builder.build(str(workspace), groupings, path_by_id, [])
        moves = [op for op in plan.operations if op.kind == OperationKind.MOVE]
        dests = sorted(Path(op.destination).name for op in moves)
        assert dests == ["report (2).pdf", "report.pdf"]


class TestExistingFileCollision:
    def test_existing_disk_file_pushes_rename(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        target = workspace / "R"
        target.mkdir()
        (target / "report.pdf").write_text("squatter")  # not a planned source
        (workspace / "report.pdf").write_text("a")
        path_by_id = {"f000": str(workspace / "report.pdf")}
        groupings = _groupings(_grouping("R", "R", ["f000"]))
        plan = plan_builder.build(str(workspace), groupings, path_by_id, [])
        move = next(op for op in plan.operations if op.kind == OperationKind.MOVE)
        assert Path(move.destination).name == "report (2).pdf"


class TestEmptyGrouping:
    def test_empty_file_ids_raises(self, workspace):
        from fda.organize import plan_builder

        groupings = _groupings(_grouping("A", "A", []))
        with pytest.raises(plan_builder.PlanBuilderError):
            plan_builder.build(str(workspace), groupings, {}, [])


class TestValidationDrop:
    def test_validation_failure_drops_op(self, workspace):
        # When validate_operation rejects a move (e.g., destination inside a
        # git repo), it should be dropped. We set up: source outside any repo,
        # destination inside a child .git repo.
        from fda.organize import plan_builder

        repo = workspace / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        src = workspace / "a.txt"
        src.write_text("a")
        path_by_id = {"f000": str(src)}
        groupings = _groupings(_grouping("Inside", "repo/Sub", ["f000"]))
        with pytest.raises(plan_builder.PlanBuilderError) as exc:
            plan_builder.build(str(workspace), groupings, path_by_id, [])
        assert "all operations dropped" in str(exc.value)


class TestPlanOrdering:
    def test_create_dir_then_move_then_delete(self, workspace):
        from fda.organize import plan_builder
        from fda.organize.models import OperationKind

        (workspace / "a.txt").write_text("a")
        junk = workspace / ".DS_Store"
        junk.write_bytes(b"\x00")
        path_by_id = {"f000": str(workspace / "a.txt")}
        groupings = _groupings(_grouping("A", "A", ["f000"]))
        plan = plan_builder.build(
            str(workspace), groupings, path_by_id, [str(junk)],
        )
        kinds = [op.kind for op in plan.operations]
        first_move = kinds.index(OperationKind.MOVE)
        first_delete = kinds.index(OperationKind.DELETE)
        last_create_dir = max(
            i for i, k in enumerate(kinds) if k == OperationKind.CREATE_DIR
        )
        assert last_create_dir < first_move < first_delete
