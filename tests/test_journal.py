"""
Tests for the journal system: writing, reading, indexing, and retrieval.
"""

import pytest
from datetime import datetime, timedelta
from pathlib import Path


class TestJournalWriter:
    """Tests for JournalWriter — entry creation and file format."""

    def test_write_entry_creates_file(self, journal_writer, tmp_journal_dir):
        path = journal_writer.write_entry(
            author="test-agent",
            tags=["test", "unit"],
            summary="Test entry",
            content="This is test content.",
            relevance_decay="medium",
        )
        assert path.exists()
        assert path.suffix == ".md"
        assert path.parent == tmp_journal_dir

    def test_write_entry_contains_frontmatter(self, journal_writer):
        path = journal_writer.write_entry(
            author="fda",
            tags=["deploy", "local"],
            summary="Deployed fix",
            content="Fixed the bug.",
        )
        content = path.read_text()
        assert content.startswith("---")
        assert "author: fda" in content
        assert "deploy" in content
        assert "local" in content
        assert 'title: "Deployed fix"' in content

    def test_write_entry_updates_index(self, journal_writer):
        journal_writer.write_entry(
            author="worker",
            tags=["task"],
            summary="Index check",
            content="body",
        )
        entries = journal_writer.index.entries
        assert len(entries) == 1
        assert entries[0]["author"] == "worker"
        assert entries[0]["summary"] == "Index check"

    def test_read_entry_roundtrip(self, journal_writer):
        path = journal_writer.write_entry(
            author="test",
            tags=["roundtrip"],
            summary="Roundtrip test",
            content="The body content here.",
            relevance_decay="slow",
        )
        result = journal_writer.read_entry(path.name)
        assert result["metadata"]["author"] == "test"
        assert result["metadata"]["relevance_decay"] == "slow"
        assert "The body content here." in result["content"]

    def test_append_to_entry(self, journal_writer):
        path = journal_writer.write_entry(
            author="test",
            tags=["append"],
            summary="Append test",
            content="Original.",
        )
        journal_writer.append_to_entry(path, "Appended text.")
        content = path.read_text()
        assert "Original." in content
        assert "Appended text." in content

    def test_slugify(self, journal_writer):
        assert journal_writer._slugify("Hello World!") == "hello-world"
        assert journal_writer._slugify("Fix bug #123") == "fix-bug-123"
        assert journal_writer._slugify("") == "untitled"
        assert journal_writer._slugify("---") == "untitled"

    def test_filename_format(self, journal_writer):
        ts = datetime(2025, 3, 15, 10, 30, 45)
        name = journal_writer._generate_filename("Deploy hotfix", ts)
        assert name.startswith("2025-03-15_10-30-45_")
        assert name.endswith(".md")
        assert "deploy-hotfix" in name

    def test_multiple_entries_indexed(self, journal_writer):
        for i in range(5):
            journal_writer.write_entry(
                author="test",
                tags=[f"tag-{i}"],
                summary=f"Entry {i}",
                content=f"Content {i}",
            )
        assert len(journal_writer.index.entries) == 5


class TestJournalIndex:
    """Tests for JournalIndex — search and lookup."""

    def test_add_and_get_entry(self, journal_index):
        journal_index.add_entry({
            "filename": "2025-01-01_00-00-00_test.md",
            "author": "test",
            "tags": ["a", "b"],
            "summary": "Test entry",
            "created_at": datetime.now().isoformat(),
        })
        entry = journal_index.get_entry("2025-01-01_00-00-00_test.md")
        assert entry is not None
        assert entry["author"] == "test"

    def test_search_by_tags(self, journal_index):
        now = datetime.now().isoformat()
        journal_index.add_entry({
            "filename": "a.md", "author": "x", "tags": ["deploy", "local"],
            "summary": "Deploy entry", "created_at": now,
        })
        journal_index.add_entry({
            "filename": "b.md", "author": "x", "tags": ["investigation"],
            "summary": "Investigation entry", "created_at": now,
        })
        results = journal_index.search(tags=["deploy"])
        assert len(results) == 1
        assert results[0]["filename"] == "a.md"

    def test_search_by_keywords(self, journal_index):
        now = datetime.now().isoformat()
        journal_index.add_entry({
            "filename": "a.md", "author": "x", "tags": ["worker"],
            "summary": "Fixed authentication bug", "created_at": now,
        })
        journal_index.add_entry({
            "filename": "b.md", "author": "x", "tags": ["worker"],
            "summary": "Added new feature", "created_at": now,
        })
        results = journal_index.search(keywords="authentication")
        assert len(results) == 1
        assert results[0]["filename"] == "a.md"

    def test_remove_entry(self, journal_index):
        journal_index.add_entry({
            "filename": "del.md", "author": "x", "tags": ["test"],
            "summary": "To delete", "created_at": datetime.now().isoformat(),
        })
        assert journal_index.remove_entry("del.md") is True
        assert journal_index.get_entry("del.md") is None

    def test_get_by_author(self, journal_index):
        now = datetime.now().isoformat()
        journal_index.add_entry({
            "filename": "a.md", "author": "alice", "tags": ["x"],
            "summary": "Alice's entry", "created_at": now,
        })
        journal_index.add_entry({
            "filename": "b.md", "author": "bob", "tags": ["x"],
            "summary": "Bob's entry", "created_at": now,
        })
        results = journal_index.get_by_author("alice")
        assert len(results) == 1

    def test_get_recent(self, journal_index):
        for i in range(10):
            ts = datetime(2025, 1, 1 + i).isoformat()
            journal_index.add_entry({
                "filename": f"{i}.md", "author": "x", "tags": ["x"],
                "summary": f"Entry {i}", "created_at": ts,
            })
        recent = journal_index.get_recent(3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["filename"] == "9.md"

    def test_get_all_tags(self, journal_index):
        now = datetime.now().isoformat()
        journal_index.add_entry({
            "filename": "a.md", "author": "x", "tags": ["alpha", "beta"],
            "summary": "A", "created_at": now,
        })
        journal_index.add_entry({
            "filename": "b.md", "author": "x", "tags": ["beta", "gamma"],
            "summary": "B", "created_at": now,
        })
        tags = journal_index.get_all_tags()
        assert tags == ["alpha", "beta", "gamma"]

    def test_duplicate_filename_updates(self, journal_index):
        now = datetime.now().isoformat()
        journal_index.add_entry({
            "filename": "dup.md", "author": "x", "tags": ["v1"],
            "summary": "Original", "created_at": now,
        })
        journal_index.add_entry({
            "filename": "dup.md", "author": "x", "tags": ["v2"],
            "summary": "Updated", "created_at": now,
        })
        assert len(journal_index.entries) == 1
        assert journal_index.entries[0]["summary"] == "Updated"

    def test_persistence_across_loads(self, journal_index, tmp_index_path):
        journal_index.add_entry({
            "filename": "persist.md", "author": "x", "tags": ["p"],
            "summary": "Persistent", "created_at": datetime.now().isoformat(),
        })
        # Create a new index from the same file
        from fda.journal.index import JournalIndex
        reloaded = JournalIndex(index_path=tmp_index_path)
        assert len(reloaded.entries) == 1
        assert reloaded.entries[0]["filename"] == "persist.md"


class TestJournalRetriever:
    """Tests for JournalRetriever — ranked search with decay."""

    def _seed_entries(self, journal_writer, count=5):
        """Create test entries with varying ages and tags."""
        paths = []
        for i in range(count):
            p = journal_writer.write_entry(
                author="test",
                tags=[f"tag-{i}", "common"],
                summary=f"Entry number {i} about topic-{i}",
                content=f"Content for entry {i}",
                relevance_decay="medium",
            )
            paths.append(p)
        return paths

    def test_retrieve_returns_ranked_results(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer)
        results = journal_retriever.retrieve(query_tags=["common"])
        assert len(results) == 5
        # Results should have scores
        assert "combined_score" in results[0]
        assert "relevance_score" in results[0]
        assert "recency_score" in results[0]

    def test_retrieve_filters_by_tag(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer)
        results = journal_retriever.retrieve(query_tags=["tag-2"])
        assert len(results) == 1
        assert "tag-2" in results[0]["tags"]

    def test_retrieve_with_text_query(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer)
        results = journal_retriever.retrieve(query_text="topic-3")
        assert len(results) >= 1
        assert any("topic-3" in r["summary"] for r in results)

    def test_retrieve_respects_top_n(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer, count=10)
        results = journal_retriever.retrieve(query_tags=["common"], top_n=3)
        assert len(results) == 3

    def test_retrieve_with_content(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer, count=2)
        results = journal_retriever.retrieve_with_content(query_tags=["common"])
        assert len(results) == 2
        assert "content" in results[0]
        assert results[0]["content"]  # non-empty

    def test_get_related_entries(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer, count=5)
        # All entries share "common" tag, so they should be related
        first_entry = journal_writer.index.entries[0]["filename"]
        related = journal_retriever.get_related_entries(first_entry, top_n=3)
        assert len(related) <= 3
        # Should not include the reference entry itself
        assert all(r["filename"] != first_entry for r in related)

    def test_empty_query_returns_all(self, journal_writer, journal_retriever):
        self._seed_entries(journal_writer, count=3)
        results = journal_retriever.retrieve()
        assert len(results) == 3
