"""
Journal entry writer.

Writes structured journal entries as markdown files with YAML frontmatter.
"""

import re
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.config import JOURNAL_DIR, INDEX_PATH
from fda.journal.index import JournalIndex


class JournalWriter:
    """
    Writes journal entries with metadata to the project journal.

    Creates markdown files with YAML frontmatter and updates the index.
    """

    def __init__(self, journal_dir: Path = JOURNAL_DIR):
        """
        Initialize the journal writer.

        Args:
            journal_dir: Directory to write journal entries to.
        """
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.index = JournalIndex(INDEX_PATH)

    def write_entry(
        self,
        author: str,
        tags: list[str],
        summary: str,
        content: str,
        relevance_decay: str = "medium",
    ) -> Path:
        """
        Write a new entry to the journal.

        Creates a markdown file with YAML frontmatter containing metadata,
        and updates the journal index.

        Args:
            author: Author of the entry (agent or person name).
            tags: List of tags for categorization.
            summary: Brief one-line summary of the entry.
            content: Full markdown content of the entry.
            relevance_decay: How quickly this entry becomes outdated:
                           "fast" (0.1), "medium" (0.05), "slow" (0.01).

        Returns:
            Path to the written journal file.
        """
        now = datetime.now()
        filename = self._generate_filename(summary, now)
        filepath = self.journal_dir / filename

        # Create frontmatter
        frontmatter = self._create_frontmatter(
            author=author,
            tags=tags,
            summary=summary,
            relevance_decay=relevance_decay,
            created_at=now,
        )

        # Combine frontmatter and content
        full_content = f"{frontmatter}\n{content}"

        # Write the file
        filepath.write_text(full_content, encoding="utf-8")

        # Update the index
        self.index.add_entry({
            "filename": filename,
            "author": author,
            "tags": tags,
            "summary": summary,
            "created_at": now.isoformat(),
            "relevance_decay": relevance_decay,
        })

        return filepath

    def _generate_filename(self, summary: str, timestamp: Optional[datetime] = None) -> str:
        """
        Generate a filename for a journal entry.

        Args:
            summary: Entry summary for slug generation.
            timestamp: Optional timestamp (defaults to now).

        Returns:
            Filename in format: YYYY-MM-DD_HH-MM-SS_slug.md
        """
        ts = timestamp or datetime.now()
        date_part = ts.strftime("%Y-%m-%d_%H-%M-%S")

        # Create slug from summary
        slug = self._slugify(summary)

        # Limit slug length
        if len(slug) > 50:
            slug = slug[:50].rstrip("-")

        return f"{date_part}_{slug}.md"

    def _slugify(self, text: str) -> str:
        """
        Convert text to a URL-friendly slug.

        Args:
            text: Text to convert.

        Returns:
            Lowercase slug with hyphens.
        """
        # Convert to lowercase
        slug = text.lower()
        # Replace spaces and underscores with hyphens
        slug = re.sub(r"[\s_]+", "-", slug)
        # Remove non-alphanumeric characters (except hyphens)
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        # Remove multiple consecutive hyphens
        slug = re.sub(r"-+", "-", slug)
        # Remove leading/trailing hyphens
        slug = slug.strip("-")

        return slug or "untitled"

    def _create_frontmatter(
        self,
        author: str,
        tags: list[str],
        summary: str,
        relevance_decay: str,
        created_at: Optional[datetime] = None,
    ) -> str:
        """
        Create YAML frontmatter for an entry.

        Args:
            author: Entry author.
            tags: Tags for the entry.
            summary: Entry summary.
            relevance_decay: Decay rate for relevance.
            created_at: Creation timestamp.

        Returns:
            YAML frontmatter as a string.
        """
        ts = created_at or datetime.now()
        tags_yaml = "\n".join(f"  - {tag}" for tag in tags)

        frontmatter = f"""---
title: "{summary}"
author: {author}
created_at: {ts.isoformat()}
relevance_decay: {relevance_decay}
tags:
{tags_yaml}
---
"""
        return frontmatter

    def append_to_entry(self, filepath: Path, additional_content: str) -> None:
        """
        Append content to an existing journal entry.

        Args:
            filepath: Path to the existing entry.
            additional_content: Content to append.
        """
        if not filepath.exists():
            raise FileNotFoundError(f"Journal entry not found: {filepath}")

        current_content = filepath.read_text(encoding="utf-8")
        new_content = f"{current_content}\n\n{additional_content}"
        filepath.write_text(new_content, encoding="utf-8")

    def read_entry(self, filename: str) -> dict[str, Any]:
        """
        Read a journal entry and parse its content.

        Args:
            filename: Name of the entry file.

        Returns:
            Dictionary with 'metadata' and 'content' keys.
        """
        filepath = self.journal_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Journal entry not found: {filepath}")

        content = filepath.read_text(encoding="utf-8")

        # Parse frontmatter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_raw = parts[1].strip()
                body = parts[2].strip()

                # Simple YAML parsing for our known structure
                metadata = self._parse_frontmatter(frontmatter_raw)
                return {"metadata": metadata, "content": body}

        # No frontmatter found
        return {"metadata": {}, "content": content}

    def _parse_frontmatter(self, frontmatter: str) -> dict[str, Any]:
        """
        Parse YAML frontmatter into a dictionary.

        Args:
            frontmatter: Raw YAML frontmatter string.

        Returns:
            Parsed metadata dictionary.
        """
        metadata: dict[str, Any] = {}
        current_key = None
        current_list: list[str] = []

        for line in frontmatter.split("\n"):
            line = line.rstrip()

            # Handle list items
            if line.startswith("  - "):
                if current_key:
                    current_list.append(line[4:].strip())
                continue

            # Save previous list if we were building one
            if current_key and current_list:
                metadata[current_key] = current_list
                current_list = []
                current_key = None

            # Parse key-value pairs
            if ":" in line and not line.startswith(" "):
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()

                if value:
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    metadata[key] = value
                else:
                    # This might be the start of a list
                    current_key = key

        # Don't forget the last list
        if current_key and current_list:
            metadata[current_key] = current_list

        return metadata
