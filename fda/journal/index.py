"""
Journal index management.

Maintains an index of journal entries for fast lookup and search.
"""

import json
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.config import INDEX_PATH


class JournalIndex:
    """
    Manages the journal entry index.

    Stores metadata about all journal entries and supports searching
    by tags and keywords.
    """

    def __init__(self, index_path: Path = INDEX_PATH):
        """
        Initialize the journal index.

        Args:
            index_path: Path to the index.json file.
        """
        self.index_path = Path(index_path)
        self.entries: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        """
        Load the index from disk.

        If index doesn't exist, initializes an empty index.
        """
        if self.index_path.exists():
            try:
                with open(self.index_path, "r") as f:
                    data = json.load(f)
                    self.entries = data.get("entries", [])
            except (json.JSONDecodeError, IOError):
                self.entries = []
        else:
            self.entries = []
            # Ensure parent directory exists
            self.index_path.parent.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        """
        Save the index to disk.
        """
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "entries": self.entries,
            "updated_at": datetime.now().isoformat(),
            "count": len(self.entries),
        }
        with open(self.index_path, "w") as f:
            json.dump(data, f, indent=2)

    def add_entry(self, metadata: dict[str, Any]) -> None:
        """
        Add an entry to the index.

        Args:
            metadata: Entry metadata (filename, author, tags, summary,
                     created_at, relevance_decay).
        """
        required_fields = ["filename", "author", "tags", "summary", "created_at"]
        for field in required_fields:
            if field not in metadata:
                raise ValueError(f"Missing required field: {field}")

        # Check for duplicate filenames
        existing = [e for e in self.entries if e["filename"] == metadata["filename"]]
        if existing:
            # Update existing entry
            idx = self.entries.index(existing[0])
            self.entries[idx] = metadata
        else:
            self.entries.append(metadata)

        self.save()

    def remove_entry(self, filename: str) -> bool:
        """
        Remove an entry from the index by filename.

        Args:
            filename: The filename of the entry to remove.

        Returns:
            True if entry was found and removed, False otherwise.
        """
        original_len = len(self.entries)
        self.entries = [e for e in self.entries if e["filename"] != filename]
        if len(self.entries) < original_len:
            self.save()
            return True
        return False

    def get_entry(self, filename: str) -> Optional[dict[str, Any]]:
        """
        Get a specific entry by filename.

        Args:
            filename: The filename to look up.

        Returns:
            Entry metadata or None if not found.
        """
        for entry in self.entries:
            if entry["filename"] == filename:
                return entry
        return None

    def search(
        self,
        tags: Optional[list[str]] = None,
        keywords: str = "",
    ) -> list[dict[str, Any]]:
        """
        Search the index by tags and keywords.

        Args:
            tags: List of tags to filter by (matches any tag).
            keywords: Keywords to search for in summary and content.

        Returns:
            List of matching entry metadata dictionaries.
        """
        results = []

        for entry in self.entries:
            matches = True

            # Filter by tags (match any)
            if tags:
                entry_tags = set(entry.get("tags", []))
                if not entry_tags.intersection(set(tags)):
                    matches = False

            # Filter by keywords (search in summary)
            if keywords and matches:
                keywords_lower = keywords.lower()
                summary_lower = entry.get("summary", "").lower()
                # Also search in tags
                tags_text = " ".join(entry.get("tags", [])).lower()

                if keywords_lower not in summary_lower and keywords_lower not in tags_text:
                    # Try word-by-word matching
                    keyword_words = keywords_lower.split()
                    text_to_search = f"{summary_lower} {tags_text}"
                    if not any(word in text_to_search for word in keyword_words):
                        matches = False

            if matches:
                results.append(entry)

        return results

    def get_by_date_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """
        Get entries created within a date range.

        Args:
            start: Start date.
            end: End date.

        Returns:
            List of matching entries.
        """
        results = []
        start_str = start.isoformat()
        end_str = end.isoformat()

        for entry in self.entries:
            created_at = entry.get("created_at", "")
            if start_str <= created_at <= end_str:
                results.append(entry)

        # Sort by date descending
        results.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return results

    def get_by_author(self, author: str) -> list[dict[str, Any]]:
        """
        Get all entries by a specific author.

        Args:
            author: Author name to filter by.

        Returns:
            List of entries by the author.
        """
        return [e for e in self.entries if e.get("author") == author]

    def get_all_tags(self) -> list[str]:
        """
        Get all unique tags across all entries.

        Returns:
            Sorted list of unique tags.
        """
        tags = set()
        for entry in self.entries:
            tags.update(entry.get("tags", []))
        return sorted(tags)

    def get_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get the most recent entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of recent entries, sorted by date descending.
        """
        sorted_entries = sorted(
            self.entries,
            key=lambda e: e.get("created_at", ""),
            reverse=True,
        )
        return sorted_entries[:limit]
