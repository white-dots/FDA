"""
Journal entry retrieval with ranking.

Implements two-pass retrieval: filter by tags and keywords, then rank by
relevance and recency with decay.
"""

import math
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.config import (
    DEFAULT_RETRIEVAL_TOP_N,
    RELEVANCE_WEIGHT,
    RECENCY_WEIGHT,
    DECAY_RATES,
    JOURNAL_DIR,
    INDEX_PATH,
)
from fda.journal.index import JournalIndex


class JournalRetriever:
    """
    Retrieves relevant journal entries using two-pass ranking.

    First filters entries by tags and keywords, then ranks results
    using a combination of relevance and recency scores with decay rates.
    """

    def __init__(self, journal_dir: Path = JOURNAL_DIR):
        """
        Initialize the retriever.

        Args:
            journal_dir: Directory containing journal entries.
        """
        self.journal_dir = Path(journal_dir)
        self.index = JournalIndex(INDEX_PATH)

    def retrieve(
        self,
        query_tags: Optional[list[str]] = None,
        query_text: str = "",
        top_n: int = DEFAULT_RETRIEVAL_TOP_N,
    ) -> list[dict[str, Any]]:
        """
        Retrieve relevant journal entries.

        Two-pass process:
        1. Filter by tags and keywords
        2. Rank by (0.6 * relevancy + 0.4 * recency) with decay

        Args:
            query_tags: Tags to search for.
            query_text: Text query for keyword search.
            top_n: Number of results to return.

        Returns:
            Top N ranked entries with scores.
        """
        query_tags = query_tags or []

        # Pass 1: Filter entries matching tags or keywords
        if query_tags or query_text:
            candidates = self.index.search(tags=query_tags, keywords=query_text)
        else:
            # If no query, return all entries
            candidates = self.index.entries.copy()

        if not candidates:
            return []

        # Pass 2: Score and rank
        scored_entries = []
        for entry in candidates:
            relevance = self._calculate_relevance_score(entry, query_tags, query_text)
            recency = self._calculate_recency_score(entry)

            # Combined score using configured weights
            combined_score = (RELEVANCE_WEIGHT * relevance) + (RECENCY_WEIGHT * recency)

            scored_entry = {
                **entry,
                "relevance_score": round(relevance, 4),
                "recency_score": round(recency, 4),
                "combined_score": round(combined_score, 4),
            }
            scored_entries.append(scored_entry)

        # Sort by combined score descending
        scored_entries.sort(key=lambda e: e["combined_score"], reverse=True)

        return scored_entries[:top_n]

    def _calculate_relevance_score(
        self,
        entry: dict[str, Any],
        query_tags: list[str],
        query_text: str,
    ) -> float:
        """
        Calculate relevance score for an entry.

        Args:
            entry: Entry metadata.
            query_tags: Query tags.
            query_text: Query text.

        Returns:
            Relevance score between 0 and 1.
        """
        score = 0.0
        max_possible = 0.0

        entry_tags = set(entry.get("tags", []))
        summary = entry.get("summary", "").lower()

        # Tag matching (up to 0.5 points)
        if query_tags:
            max_possible += 0.5
            query_tag_set = set(query_tags)
            matching_tags = entry_tags.intersection(query_tag_set)
            if query_tag_set:
                tag_match_ratio = len(matching_tags) / len(query_tag_set)
                score += 0.5 * tag_match_ratio

        # Keyword matching (up to 0.5 points)
        if query_text:
            max_possible += 0.5
            query_words = query_text.lower().split()

            if query_words:
                # Check how many query words appear in summary
                matching_words = sum(1 for word in query_words if word in summary)
                word_match_ratio = matching_words / len(query_words)

                # Also check in tags
                tags_text = " ".join(entry_tags).lower()
                tag_word_matches = sum(1 for word in query_words if word in tags_text)
                tag_word_ratio = tag_word_matches / len(query_words)

                # Take the best match ratio
                best_ratio = max(word_match_ratio, tag_word_ratio)
                score += 0.5 * best_ratio

        # Normalize to 0-1 range
        if max_possible > 0:
            return score / max_possible

        # If no query provided, give a baseline score
        return 0.5

    def _calculate_recency_score(
        self,
        entry: dict[str, Any],
    ) -> float:
        """
        Calculate recency score with decay.

        Score decays based on age and the entry's relevance_decay setting.
        Recent entries score higher.

        Args:
            entry: Entry metadata with 'created_at' and 'relevance_decay'.

        Returns:
            Recency score between 0 and 1.
        """
        created_at_str = entry.get("created_at", "")
        if not created_at_str:
            return 0.5  # Default middle score if no timestamp

        try:
            created_at = datetime.fromisoformat(created_at_str)
        except (ValueError, TypeError):
            return 0.5

        now = datetime.now()
        age_days = (now - created_at).total_seconds() / (24 * 3600)

        # Get decay rate
        decay_setting = entry.get("relevance_decay", "medium")
        decay_rate = DECAY_RATES.get(decay_setting, DECAY_RATES["medium"])

        # Exponential decay: exp(-decay_rate * days_old)
        # This gives 1.0 for brand new entries and decays toward 0
        recency_score = math.exp(-decay_rate * age_days)

        return max(0.0, min(1.0, recency_score))

    def _read_entry_content(self, filename: str) -> str:
        """
        Read the full content of a journal entry.

        Args:
            filename: The entry filename.

        Returns:
            The entry content (without frontmatter).
        """
        filepath = self.journal_dir / filename
        if not filepath.exists():
            return ""

        content = filepath.read_text(encoding="utf-8")

        # Strip frontmatter if present
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()

        return content

    def retrieve_with_content(
        self,
        query_tags: Optional[list[str]] = None,
        query_text: str = "",
        top_n: int = DEFAULT_RETRIEVAL_TOP_N,
    ) -> list[dict[str, Any]]:
        """
        Retrieve entries with their full content included.

        Args:
            query_tags: Tags to search for.
            query_text: Text query for keyword search.
            top_n: Number of results to return.

        Returns:
            Top N ranked entries with scores and content.
        """
        results = self.retrieve(query_tags, query_text, top_n)

        for entry in results:
            filename = entry.get("filename", "")
            if filename:
                entry["content"] = self._read_entry_content(filename)

        return results

    def get_related_entries(
        self,
        entry_filename: str,
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Find entries related to a given entry based on shared tags.

        Args:
            entry_filename: Filename of the reference entry.
            top_n: Number of related entries to return.

        Returns:
            List of related entries, excluding the reference entry.
        """
        reference = self.index.get_entry(entry_filename)
        if not reference:
            return []

        reference_tags = reference.get("tags", [])
        if not reference_tags:
            return []

        # Search for entries with overlapping tags
        results = self.retrieve(query_tags=reference_tags, top_n=top_n + 1)

        # Remove the reference entry from results
        results = [e for e in results if e.get("filename") != entry_filename]

        return results[:top_n]
