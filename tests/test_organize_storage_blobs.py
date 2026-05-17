# tests/test_organize_storage_blobs.py
"""Tests for fda.organize.storage_blobs."""
from __future__ import annotations

import pytest

from fda.organize.models import CatalogEntry


def _entry(idx, *, ext, extract_status="no_extractor", is_junk=False):
    """Build a CatalogEntry with the minimum fields the taxonomy reads.

    Default extract_status="no_extractor" mirrors how the reader tags a
    non-junk file it has no extractor for (built via reader._quarantine_entry
    with is_junk=False, summary_failed=False).
    """
    return CatalogEntry(
        path_id=f"f{idx:03d}",
        path=f"/tmp/target/{idx:03d}{ext}",
        ext=ext,
        size_bytes=1000,
        summary="",
        type_label="",
        is_junk=is_junk,
        summary_failed=False,
        extract_status=extract_status,
    )


class TestStorageBlobBucket:
    def test_media_extensions_map_to_media_bucket(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        for ext in (".mp4", ".mov", ".png", ".jpg", ".mp3", ".flac"):
            assert storage_blob_bucket(_entry(0, ext=ext)) == (
                "StorageBlobMedia", "미디어_Media"
            ), ext

    def test_archive_extensions_map_to_archive_bucket(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        for ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"):
            assert storage_blob_bucket(_entry(0, ext=ext)) == (
                "StorageBlobArchive", "압축파일_Archives"
            ), ext

    def test_backup_extensions_map_to_backup_bucket(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        for ext in (".bak", ".sql", ".dump", ".dmp"):
            assert storage_blob_bucket(_entry(0, ext=ext)) == (
                "StorageBlobBackup", "백업_Backups"
            ), ext

    def test_non_blob_unreadable_extension_returns_none(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        assert storage_blob_bucket(_entry(0, ext=".xyz")) is None

    def test_junk_entry_returns_none(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        # Junk wins even when the extension is a storage-blob type.
        assert storage_blob_bucket(
            _entry(0, ext=".zip", is_junk=True)
        ) is None

    def test_readable_file_with_blob_extension_returns_none(self):
        from fda.organize.storage_blobs import storage_blob_bucket
        # Load-bearing guard: never steal a file the classifier could read.
        assert storage_blob_bucket(
            _entry(0, ext=".zip", extract_status="ok")
        ) is None


class TestCategoryNames:
    def test_category_names_match_bucket_spec(self):
        from fda.organize.storage_blobs import (
            STORAGE_BLOB_BUCKETS,
            STORAGE_BLOB_CATEGORY_NAMES,
        )
        assert STORAGE_BLOB_CATEGORY_NAMES == frozenset(
            cat for cat, _, _ in STORAGE_BLOB_BUCKETS
        )
        assert STORAGE_BLOB_CATEGORY_NAMES == frozenset({
            "StorageBlobMedia", "StorageBlobArchive", "StorageBlobBackup",
        })
