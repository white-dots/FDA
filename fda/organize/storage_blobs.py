# fda/organize/storage_blobs.py
"""Storage-blob taxonomy + deterministic grouping.

A "storage blob" is a file FDA has no text extractor for but whose type we
recognize with certainty (video/image/audio, archives, DB backups). These
belong in S3. They are carved out of what would otherwise be the quarantine
pile and emitted as ordinary Grouping objects (no LLM call — there is no
text to classify).

IMPORTANT: this module imports ONLY from fda.organize.models. Importing from
fda.organize.__init__, router, or classifier would create an import cycle
(__init__ imports router, router imports this module).
"""
from __future__ import annotations

from collections.abc import Sequence

from fda.organize.models import CatalogEntry, Grouping, quarantine_bucket

# Exactly the four public names the spec permits — keeps `import *` from
# re-exporting Sequence/CatalogEntry/Grouping/quarantine_bucket.
__all__ = [
    "STORAGE_BLOB_BUCKETS",
    "STORAGE_BLOB_CATEGORY_NAMES",
    "storage_blob_bucket",
    "build_groupings",
]

# Ordered: (stable english id, on-disk subpath, extensions). Order is the
# emission order of build_groupings (deterministic plans). Editing an
# extension set is a one-line change here; nothing else needs to change.
STORAGE_BLOB_BUCKETS: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "StorageBlobMedia",
        "미디어_Media",
        frozenset({
            ".mp4", ".mov", ".avi", ".mkv",
            ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".mp3", ".wav", ".m4a", ".flac",
        }),
    ),
    (
        "StorageBlobArchive",
        "압축파일_Archives",
        frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}),
    ),
    (
        "StorageBlobBackup",
        "백업_Backups",
        frozenset({".bak", ".sql", ".dump", ".dmp"}),
    ),
)

# Single source of truth for the router's deterministic S3 rule.
STORAGE_BLOB_CATEGORY_NAMES: frozenset[str] = frozenset(
    category_id for category_id, _, _ in STORAGE_BLOB_BUCKETS
)


def storage_blob_bucket(entry: CatalogEntry) -> tuple[str, str] | None:
    """Return (category_id, subpath) for a storage-blob entry, else None.

    Both conditions must hold:
      1. quarantine_bucket(entry) is not None — the file is unreadable and
         would otherwise be quarantined. This guarantees we never divert a
         file the classifier could read (and excludes junk, whose
         quarantine_bucket is None).
      2. entry.ext is in exactly one bucket's extension set.
    """
    if quarantine_bucket(entry) is None:
        return None
    for category_id, subpath, exts in STORAGE_BLOB_BUCKETS:
        if entry.ext in exts:
            return (category_id, subpath)
    return None


def build_groupings(entries: Sequence[CatalogEntry]) -> list[Grouping]:
    """One Grouping per non-empty bucket, deterministic order.

    Bucket order follows STORAGE_BLOB_BUCKETS; file_ids are sorted. Entries
    that are not storage blobs are skipped. Empty input → [].
    """
    by_bucket: dict[str, list[str]] = {}
    for entry in entries:
        bucket = storage_blob_bucket(entry)
        if bucket is None:
            continue
        category_id, _ = bucket
        by_bucket.setdefault(category_id, []).append(entry.path_id)

    groups: list[Grouping] = []
    for category_id, subpath, _ in STORAGE_BLOB_BUCKETS:
        path_ids = by_bucket.get(category_id)
        if not path_ids:
            continue
        groups.append(Grouping(
            category=category_id,
            subpath=subpath,
            file_ids=tuple(sorted(path_ids)),
            reason="미디어/압축/백업 — 저장소(S3) 대상 파일 유형",
        ))
    return groups
