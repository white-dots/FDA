"""Tests for fda.organize._fs — validation primitives + apply functions."""

import pytest
from pathlib import Path

from fda.organize import _fs
from fda.organize.models import Operation, OperationKind


@pytest.fixture
def workspace(tmp_path):
    """Build a workspace with a regular file, a git repo, and a junk file."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / ".DS_Store").write_bytes(b"\x00")

    repo = root / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "tracked.py").write_text("# tracked")

    (root / "subdir").mkdir()
    return root


class TestValidateTarget:
    def test_inside_allowed_root(self, workspace):
        result = _fs.validate_target(str(workspace), [workspace.parent])
        assert result == workspace.resolve()

    def test_outside_allowed_root_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not in allowed"):
            _fs.validate_target("/etc", [tmp_path])

    def test_not_a_directory_raises(self, workspace):
        f = workspace / "a.txt"
        with pytest.raises(ValueError, match="not a directory"):
            _fs.validate_target(str(f), [workspace.parent])


class TestIsInsideGitRepo:
    def test_file_in_repo(self, workspace):
        assert _fs.is_inside_git_repo(workspace / "myrepo" / "tracked.py") is True

    def test_file_outside_repo(self, workspace):
        assert _fs.is_inside_git_repo(workspace / "a.txt") is False

    def test_nonexistent_path_uses_parent(self, workspace):
        # parent of a not-yet-created file inside a repo
        path = workspace / "myrepo" / "would_be_here.txt"
        assert _fs.is_inside_git_repo(path) is True


class TestIsJunkFile:
    def test_known_junk(self, workspace):
        assert _fs.is_junk_file(workspace / ".DS_Store") is True

    def test_regular_file(self, workspace):
        assert _fs.is_junk_file(workspace / "a.txt") is False


class TestIsEmptyFile:
    def test_empty(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert _fs.is_empty_file(f) is True

    def test_nonempty(self, tmp_path):
        f = tmp_path / "full.txt"
        f.write_text("hi")
        assert _fs.is_empty_file(f) is False


class TestValidateOperation:
    def test_create_dir_inside_target_ok(self, workspace):
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(workspace / "new"),
            reason="r",
        )
        _fs.validate_operation(op, workspace)  # no raise

    def test_path_must_be_absolute(self, workspace):
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination="new",
            reason="r",
        )
        with pytest.raises(ValueError, match="absolute"):
            _fs.validate_operation(op, workspace)

    def test_destination_outside_target_rejected(self, workspace, tmp_path):
        outside = tmp_path / "elsewhere"
        op = Operation(
            kind=OperationKind.CREATE_DIR,
            source=None,
            destination=str(outside),
            reason="r",
        )
        with pytest.raises(ValueError, match="outside target"):
            _fs.validate_operation(op, workspace)

    def test_move_source_in_git_repo_rejected(self, workspace):
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "myrepo" / "tracked.py"),
            destination=str(workspace / "moved.py"),
            reason="r",
        )
        with pytest.raises(ValueError, match="git repository"):
            _fs.validate_operation(op, workspace)

    def test_move_destination_in_git_repo_rejected(self, workspace):
        # NEW behavior: today's code only blocks the source side.
        op = Operation(
            kind=OperationKind.MOVE,
            source=str(workspace / "a.txt"),
            destination=str(workspace / "myrepo" / "a.txt"),
            reason="r",
        )
        with pytest.raises(ValueError, match="git repository"):
            _fs.validate_operation(op, workspace)

    def test_delete_non_junk_rejected(self, workspace):
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(workspace / "a.txt"),
            destination=None,
            reason="r",
        )
        with pytest.raises(ValueError, match="junk|empty"):
            _fs.validate_operation(op, workspace)

    def test_delete_empty_file_ok(self, workspace):
        empty = workspace / "empty.txt"
        empty.write_text("")
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(empty),
            destination=None,
            reason="r",
        )
        _fs.validate_operation(op, workspace)  # no raise

    def test_delete_junk_file_ok(self, workspace):
        op = Operation(
            kind=OperationKind.DELETE,
            source=str(workspace / ".DS_Store"),
            destination=None,
            reason="r",
        )
        _fs.validate_operation(op, workspace)  # no raise
