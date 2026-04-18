"""Tests for the FileIndexer module."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from fda.file_indexer import FileIndexer, ScannedFile, INDEXABLE_EXTENSIONS, SKIP_DIRS
from fda.state.project_state import ProjectState


@pytest.fixture
def state(tmp_state_db):
    """ProjectState with a temp db."""
    s = ProjectState(db_path=tmp_state_db)
    yield s
    s.close()


@pytest.fixture
def sample_tree(tmp_path):
    """Small filesystem tree for scanning tests."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume.pdf").write_text("pdf content")
    (tmp_path / "Documents" / "notes.md").write_text("markdown notes")
    (tmp_path / "Documents" / "subfolder").mkdir()
    (tmp_path / "Documents" / "subfolder" / "data.csv").write_text("a,b\n1,2\n")
    # Noise that should be skipped
    (tmp_path / "Documents" / "node_modules").mkdir()
    (tmp_path / "Documents" / "node_modules" / "skipme.js").write_text("ignored")
    (tmp_path / "Documents" / ".hidden.txt").write_text("hidden file")
    (tmp_path / "Documents" / "binary.xyz").write_text("unknown ext")  # not indexable
    (tmp_path / "Documents" / "empty.txt").write_text("")  # empty file
    return tmp_path


@pytest.fixture
def mock_embedder():
    """
    Mock fastembed TextEmbedding — returns deterministic random vectors
    so we can test without loading the real 100MB model.
    """
    class FakeEmbedder:
        def __init__(self):
            self._rng = np.random.default_rng(42)
        def embed(self, texts):
            for _ in texts:
                yield self._rng.standard_normal(384).astype(np.float32)

    fake = FakeEmbedder()
    with patch("fda.file_indexer._get_embedder", return_value=fake):
        # Also clear any cached embedders
        import fda.file_indexer as fi_mod
        fi_mod._EMBEDDER_CACHE.clear()
        yield fake


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def test_scan_finds_indexable_files_only(state, sample_tree):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    files = list(indexer._scan_files())
    names = {f.filename for f in files}
    assert "resume.pdf" in names
    assert "notes.md" in names
    assert "data.csv" in names
    assert "skipme.js" not in names  # inside node_modules
    assert ".hidden.txt" not in names
    assert "binary.xyz" not in names
    assert "empty.txt" not in names


def test_scan_extracts_metadata(state, sample_tree):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    files = list(indexer._scan_files())
    resume = next(f for f in files if f.filename == "resume.pdf")
    assert resume.extension == ".pdf"
    assert resume.size == len("pdf content")
    assert resume.mtime > 0
    assert resume.parent_dir.endswith("Documents")


def test_scan_respects_skip_dirs():
    assert "node_modules" in SKIP_DIRS
    assert ".git" in SKIP_DIRS
    assert "__pycache__" in SKIP_DIRS


def test_indexable_extensions_include_common_types():
    assert ".pdf" in INDEXABLE_EXTENSIONS
    assert ".html" in INDEXABLE_EXTENSIONS
    assert ".md" in INDEXABLE_EXTENSIONS
    assert ".py" in INDEXABLE_EXTENSIONS


# ---------------------------------------------------------------------------
# Embedding text construction
# ---------------------------------------------------------------------------

def test_build_embedding_text_includes_filename_and_path_words(state):
    indexer = FileIndexer(state)
    f = ScannedFile(
        path="/Users/jim/Documents/lion_chemtech/Proposals/ax_proposal.pdf",
        filename="ax_proposal.pdf",
        parent_dir="/Users/jim/Documents/lion_chemtech/Proposals",
        extension=".pdf",
        size=1024,
        mtime=1000.0,
    )
    text = indexer._build_embedding_text(f)
    assert "ax_proposal.pdf" in text
    assert "lion" in text
    assert "chemtech" in text
    assert "proposal" in text
    assert "type:pdf" in text


# ---------------------------------------------------------------------------
# Incremental indexing (mocked embedder)
# ---------------------------------------------------------------------------

def test_run_embeds_new_files_and_skips_unchanged(state, sample_tree, mock_embedder):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    stats = indexer.run()
    assert stats.scanned == 3
    assert stats.embedded == 3
    assert stats.skipped == 0

    stats2 = indexer.run()
    assert stats2.scanned == 3
    assert stats2.embedded == 0
    assert stats2.skipped == 3


def test_run_reindexes_when_file_mtime_changes(state, sample_tree, mock_embedder):
    import time
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    indexer.run()
    time.sleep(0.05)
    (sample_tree / "Documents" / "resume.pdf").write_text("updated content")

    stats = indexer.run()
    assert stats.embedded >= 1


def test_run_deletes_missing_files_from_index(state, sample_tree, mock_embedder):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    indexer.run()
    assert state.get_file_embeddings_stats()["total"] == 3

    (sample_tree / "Documents" / "notes.md").unlink()
    stats = indexer.run()
    assert stats.deleted == 1
    assert state.get_file_embeddings_stats()["total"] == 2


def test_run_force_reembeds_all(state, sample_tree, mock_embedder):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    indexer.run()

    stats = indexer.run(force=True)
    assert stats.embedded == 3
    assert stats.skipped == 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_returns_empty_when_index_empty(state, mock_embedder):
    indexer = FileIndexer(state)
    results = indexer.search("anything")
    assert results == []


def test_search_returns_ranked_results(state, sample_tree, mock_embedder):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    indexer.run()

    results = indexer.search("find the resume", k=5)
    assert len(results) > 0
    assert all("score" in r for r in results)
    assert all("path" in r for r in results)
    assert all(isinstance(r["score"], float) for r in results)
    # Results should be sorted by score (descending)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# State round-trip
# ---------------------------------------------------------------------------

def test_upsert_and_get_embedding_mtime(state):
    vec = np.random.rand(384).astype(np.float32)
    state.upsert_file_embedding(
        path="/tmp/foo.pdf",
        filename="foo.pdf",
        parent_dir="/tmp",
        extension=".pdf",
        size=100,
        mtime=123456.0,
        embedding=vec.tobytes(),
        embedding_text="foo.pdf | tmp foo | type:pdf",
    )
    assert state.get_file_embedding_mtime("/tmp/foo.pdf") == 123456.0
    assert state.get_file_embedding_mtime("/nonexistent") is None


def test_delete_file_embeddings(state):
    vec = np.random.rand(384).astype(np.float32)
    for p in ["/tmp/a.pdf", "/tmp/b.pdf", "/tmp/c.pdf"]:
        state.upsert_file_embedding(
            path=p, filename=p.split("/")[-1], parent_dir="/tmp", extension=".pdf",
            size=10, mtime=1.0, embedding=vec.tobytes(), embedding_text="x",
        )
    deleted = state.delete_file_embeddings(["/tmp/a.pdf", "/tmp/b.pdf"])
    assert deleted == 2
    assert state.get_file_embedding_mtime("/tmp/a.pdf") is None
    assert state.get_file_embedding_mtime("/tmp/c.pdf") == 1.0


def test_indexer_run_is_recorded(state, sample_tree, mock_embedder):
    indexer = FileIndexer(state, roots=[str(sample_tree / "Documents")])
    indexer.run()
    stats = state.get_file_embeddings_stats()
    assert stats["last_run"] is not None
    assert stats["last_run"]["files_embedded"] == 3
    assert stats["last_run"]["finished_at"] is not None
