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


class TestBuildGroupings:
    def test_empty_input_returns_empty_list(self):
        from fda.organize.storage_blobs import build_groupings
        assert build_groupings([]) == []

    def test_mixed_entries_one_group_per_nonempty_bucket(self):
        from fda.organize.storage_blobs import build_groupings
        entries = [
            _entry(2, ext=".mp4"),
            _entry(0, ext=".zip"),
            _entry(1, ext=".png"),
            _entry(3, ext=".xyz"),        # non-blob → skipped
            _entry(4, ext=".zip", is_junk=True),  # junk → skipped
        ]
        groups = build_groupings(entries)
        # StorageBlobBackup has no files → no group for it.
        assert [(g.category, g.subpath) for g in groups] == [
            ("StorageBlobMedia", "미디어_Media"),
            ("StorageBlobArchive", "압축파일_Archives"),
        ]
        media, archive = groups
        # file_ids sorted; only f001 (.png) + f002 (.mp4) are media.
        assert media.file_ids == ("f001", "f002")
        assert archive.file_ids == ("f000",)
        assert media.reason == "미디어/압축/백업 — 저장소(S3) 대상 파일 유형"
        assert archive.reason == "미디어/압축/백업 — 저장소(S3) 대상 파일 유형"

    def test_bucket_order_is_deterministic_regardless_of_input_order(self):
        from fda.organize.storage_blobs import build_groupings
        # Backup entry first, media last: output order must still follow
        # STORAGE_BLOB_BUCKETS (media, archive, backup).
        entries = [
            _entry(0, ext=".sql"),
            _entry(1, ext=".tar"),
            _entry(2, ext=".jpg"),
        ]
        groups = build_groupings(entries)
        # Lock category AND subpath for all three buckets (incl. backup).
        assert [(g.category, g.subpath) for g in groups] == [
            ("StorageBlobMedia", "미디어_Media"),
            ("StorageBlobArchive", "압축파일_Archives"),
            ("StorageBlobBackup", "백업_Backups"),
        ]
        # Spec §4: EVERY emitted Grouping carries the fixed reason string.
        assert all(
            g.reason == "미디어/압축/백업 — 저장소(S3) 대상 파일 유형"
            for g in groups
        )
