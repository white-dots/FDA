"""
File Indexer — builds a semantic index of local files using a local embedding model.

Runs daily (via scheduler) or on-demand. Walks user directories (Documents,
Downloads, Desktop), skips noise (.git, node_modules, etc), and stores a
vector embedding for each file's path + metadata in SQLite.

Uses fastembed with multilingual-MiniLM-L12-v2 (384 dim) by default — runs
100% locally, no API key, no cost, supports Korean/English/50+ languages.

Incremental: only re-embeds files whose mtime changed since last run.
Deleted files are pruned from the index.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from fda.config import (
    FILE_INDEXER_BATCH_SIZE,
    FILE_INDEXER_DEFAULT_ROOTS,
    FILE_INDEXER_EMBEDDING_DIM,
    FILE_INDEXER_EMBEDDING_MODEL,
    FILE_INDEXER_MAX_FILES,
)
from fda.state.project_state import ProjectState

logger = logging.getLogger(__name__)

# Directories to skip when walking
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".venv", "venv", "env",
    ".next", ".nuxt", "dist", "build", ".cache",
    "Library", ".Trash", ".DS_Store",
    ".idea", ".vscode",
    "site-packages",
})

# File extensions we index (skip binary blobs, videos, large images)
INDEXABLE_EXTENSIONS = frozenset({
    # Docs & text
    ".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt",
    ".xls", ".xlsx", ".csv", ".tsv", ".ods",
    ".ppt", ".pptx", ".key", ".odp",
    # Web & code
    ".html", ".htm", ".xml", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".sh", ".zsh", ".bash", ".ps1",
    ".sql", ".env",
    # Design / media metadata
    ".fig", ".sketch", ".psd", ".ai",
    # Images (embed only path/metadata, not content)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic",
})

# Module-level singleton — the fastembed model takes ~5s to load, avoid
# reloading it for every indexer instance or search call.
_EMBEDDER_CACHE: dict = {}


def _get_embedder(model_name: str):
    """Lazy-load and cache the fastembed TextEmbedding model."""
    if model_name in _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE[model_name]
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        raise RuntimeError(
            "fastembed is not installed. Run: pip install fastembed"
        ) from e
    logger.info(f"[indexer] Loading embedding model: {model_name} (first run downloads ~100MB)")
    t0 = time.time()
    embedder = TextEmbedding(model_name=model_name)
    logger.info(f"[indexer] Embedder ready in {time.time()-t0:.1f}s")
    _EMBEDDER_CACHE[model_name] = embedder
    return embedder


@dataclass
class ScannedFile:
    path: str
    filename: str
    parent_dir: str
    extension: str
    size: int
    mtime: float


@dataclass
class IndexerStats:
    scanned: int = 0
    embedded: int = 0
    skipped: int = 0
    deleted: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "embedded": self.embedded,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "errors": self.errors,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class FileIndexer:
    """
    Builds and maintains a semantic file index using local embeddings.

    No API key required — the embedding model runs locally via ONNX.

    Usage:
        indexer = FileIndexer(state)
        stats = indexer.run()                        # full incremental index
        hits = indexer.search("AX proposal", k=10)   # semantic search
    """

    def __init__(
        self,
        state: ProjectState,
        model: str = FILE_INDEXER_EMBEDDING_MODEL,
        roots: Optional[list[str]] = None,
    ):
        self.state = state
        self.model = model
        self.roots = roots or FILE_INDEXER_DEFAULT_ROOTS
        self._embedder = None  # lazy

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def run(self, *, force: bool = False, progress_cb: Optional[Callable[[str], None]] = None) -> IndexerStats:
        """
        Run an incremental index update.

        Args:
            force: If True, re-embed everything regardless of mtime.
            progress_cb: Optional callback(str) for progress messages.

        Returns:
            IndexerStats with counts.
        """
        stats = IndexerStats()
        run_id = self.state.record_indexer_run_start()
        started = time.time()
        error_msg = None

        def log(msg: str) -> None:
            logger.info(f"[indexer] {msg}")
            if progress_cb:
                progress_cb(msg)

        try:
            log(f"Scanning roots: {self.roots}")
            scanned = list(self._scan_files())
            stats.scanned = len(scanned)
            log(f"Found {stats.scanned} files")

            # Figure out which files need re-embedding
            to_embed: list[ScannedFile] = []
            seen_paths: set[str] = set()
            for f in scanned:
                seen_paths.add(f.path)
                if force:
                    to_embed.append(f)
                    continue
                existing_mtime = self.state.get_file_embedding_mtime(f.path)
                if existing_mtime is None or existing_mtime < f.mtime:
                    to_embed.append(f)
                else:
                    stats.skipped += 1

            log(f"To embed: {len(to_embed)} (skipped {stats.skipped} unchanged)")

            # Prune deleted files
            existing_paths = self.state.get_existing_file_paths()
            deleted_paths = [p for p in existing_paths if p not in seen_paths]
            if deleted_paths:
                stats.deleted = self.state.delete_file_embeddings(deleted_paths)
                log(f"Pruned {stats.deleted} deleted files")

            # Batch embed
            if to_embed:
                log(f"Loading embedding model: {self.model}...")
                for batch_start in range(0, len(to_embed), FILE_INDEXER_BATCH_SIZE):
                    batch = to_embed[batch_start : batch_start + FILE_INDEXER_BATCH_SIZE]
                    try:
                        self._embed_and_store_batch(batch)
                        stats.embedded += len(batch)
                        if batch_start % (FILE_INDEXER_BATCH_SIZE * 10) == 0 or batch_start + len(batch) >= len(to_embed):
                            log(f"Embedded {stats.embedded}/{len(to_embed)}")
                    except Exception as e:
                        logger.warning(f"[indexer] batch failed: {e}")
                        stats.errors += len(batch)

        except Exception as e:
            logger.exception(f"[indexer] run failed: {e}")
            error_msg = str(e)
            stats.errors += 1

        stats.elapsed_seconds = time.time() - started
        self.state.record_indexer_run_finish(
            run_id,
            files_scanned=stats.scanned,
            files_embedded=stats.embedded,
            files_skipped=stats.skipped,
            files_deleted=stats.deleted,
            error=error_msg,
        )
        return stats

    def search(self, query: str, k: int = 20) -> list[dict]:
        """
        Semantic search: find the top-k files most similar to the query.

        Args:
            query: Natural language query (any language — Korean, English, etc).
            k: Number of results to return.

        Returns:
            List of dicts with path, filename, parent_dir, score.
        """
        # Embed the query
        query_vec = self._embed_texts([query])
        if query_vec is None or len(query_vec) == 0:
            return []
        query_vec = query_vec[0].astype(np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []
        query_vec = query_vec / query_norm

        # Load all embeddings (up to the configured cap for in-memory search)
        rows = self.state.get_all_file_embeddings(limit=FILE_INDEXER_MAX_FILES)
        if not rows:
            return []

        # Stack into a matrix
        matrix = np.stack([
            np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
        ])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

        scores = matrix @ query_vec  # cosine similarity (both unit vectors)
        top_k_idx = np.argsort(-scores)[:k]

        results = []
        for idx in top_k_idx:
            r = rows[int(idx)]
            results.append({
                "path": r["path"],
                "filename": r["filename"],
                "parent_dir": r["parent_dir"],
                "extension": r["extension"],
                "size": r["size"],
                "mtime": r["mtime"],
                "score": float(scores[int(idx)]),
            })
        return results

    def stats(self) -> dict:
        """Return indexing stats."""
        return self.state.get_file_embeddings_stats()

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _get_embedder(self):
        if self._embedder is None:
            self._embedder = _get_embedder(self.model)
        return self._embedder

    def _scan_files(self) -> Iterable[ScannedFile]:
        """Walk the configured roots and yield ScannedFile records."""
        count = 0
        for root in self.roots:
            root_path = Path(root).expanduser()
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                # Prune skip dirs in-place
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    ext = Path(name).suffix.lower()
                    if ext not in INDEXABLE_EXTENSIONS:
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    if st.st_size == 0:
                        continue
                    yield ScannedFile(
                        path=full,
                        filename=name,
                        parent_dir=dirpath,
                        extension=ext,
                        size=st.st_size,
                        mtime=st.st_mtime,
                    )
                    count += 1
                    if count >= FILE_INDEXER_MAX_FILES:
                        return

    def _build_embedding_text(self, f: ScannedFile) -> str:
        """Build the text to embed for a file — filename + path segments + ext."""
        path_words = " ".join(
            part for part in f.path.replace("/", " ").replace("_", " ").replace("-", " ").split()
            if len(part) > 1
        )
        return f"{f.filename} | {path_words} | type:{f.extension[1:] if f.extension else 'unknown'}"

    def _embed_texts(self, texts: list[str]) -> Optional[np.ndarray]:
        """Embed a list of texts. Returns shape (N, dim) numpy array, or None on error."""
        try:
            embedder = self._get_embedder()
            vectors = list(embedder.embed(texts))
            return np.array(vectors, dtype=np.float32)
        except Exception as e:
            logger.warning(f"[indexer] embedding failed: {e}")
            return None

    def _embed_and_store_batch(self, batch: list[ScannedFile]) -> None:
        """Embed a batch of files and upsert to SQLite."""
        texts = [self._build_embedding_text(f) for f in batch]
        vectors = self._embed_texts(texts)
        if vectors is None:
            raise RuntimeError("Embedding call returned no vectors")

        for f, text, vec in zip(batch, texts, vectors):
            vec = vec.astype(np.float32)
            self.state.upsert_file_embedding(
                path=f.path,
                filename=f.filename,
                parent_dir=f.parent_dir,
                extension=f.extension,
                size=f.size,
                mtime=f.mtime,
                embedding=vec.tobytes(),
                embedding_text=text,
                model=self.model,
            )


def get_file_indexer(state: ProjectState) -> FileIndexer:
    """Factory for a FileIndexer with the current project state."""
    return FileIndexer(state)
