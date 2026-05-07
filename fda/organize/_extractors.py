# fda/organize/_extractors.py
"""Text extraction registry.

Each registered extractor is a function `(path: Path) -> ExtractionResult`.
Reader is the only caller. The registry is the single seam for "add a new
document format" — Reader has no knowledge of which extensions exist beyond
what's in EXTRACTORS, and there is no separate binary-extension denylist.

`extract()` is the top-level entry point. It dispatches by suffix, catches
any exception the extractor raises (so a misbehaving extractor never aborts
a run), and returns ExtractionResult(None, status="no_extractor") for
unknown extensions.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from fda.organize.models import ExtractionResult
from fda.organize._sections import (
    MAX_SECTIONS_PER_FILE,
    SECTION_HEADER_MAX_CHARS,
    SECTION_HEADER_MIN_CHARS,
    extract_sections_from_text,
)

TextExtractor = Callable[[Path], ExtractionResult]

# Internal: extractor's memory-safety cap on `pdftotext` stdout. Reader applies
# its own 64 KB cap on the contract side; this cap protects the subprocess
# pipe from runaway output.
_PDF_PIPE_CAP_BYTES = 64 * 1024
_PDF_TIMEOUT_SECONDS = 10

# xlsx serialization caps (memory-bound the in-process row/col walk; do NOT cap
# `text` — Reader owns the 64 KiB contract cap via READER_TEXT_CAP_BYTES).
_XLSX_TEXT_ROWS_PER_SHEET = 20
_XLSX_TEXT_COLS_PER_ROW = 32
# Synthesized-label thresholds.
_XLSX_FORMULA_DENSITY_THRESHOLD = 0.05
_XLSX_MERGED_CELLS_MIN = 3

_DOCX_HEADING_RE = re.compile(r"^Heading [1-9]$")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _read_text(path: Path) -> ExtractionResult:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ExtractionResult(text=None, status="failed", note=str(e))
    return ExtractionResult(
        text=text,
        status="ok",
        sections=extract_sections_from_text(text),
    )


def _run_pdftotext(path: Path) -> bytes:
    """Run `pdftotext -layout -l 3 <path> -` and return up to _PDF_PIPE_CAP_BYTES.
    Raises RuntimeError on non-zero exit."""
    pdftotext = _which("pdftotext")
    if pdftotext is None:
        raise RuntimeError("pdftotext vanished from PATH between check and use")
    proc = subprocess.Popen(
        [pdftotext, "-layout", "-l", "3", str(path), "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + _PDF_TIMEOUT_SECONDS
    try:
        while total < _PDF_PIPE_CAP_BYTES and time.monotonic() < deadline:
            chunk = proc.stdout.read1(8192) if proc.stdout else b""
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)[:_PDF_PIPE_CAP_BYTES]
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if proc.returncode not in (0, None) and not raw:
        raise RuntimeError(f"pdftotext exited {proc.returncode}")
    return raw


def _extract_pdf_text(path: Path) -> ExtractionResult:
    if _which("pdftotext") is None:
        return ExtractionResult(
            text=None,
            status="tool_missing",
            note="pdftotext not on PATH (install poppler)",
        )
    raw = _run_pdftotext(path)
    text = raw[:_PDF_PIPE_CAP_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        return ExtractionResult(
            text=None,
            status="failed",
            note="pdftotext produced no text (image-only PDF?)",
        )
    return ExtractionResult(
        text=text,
        status="ok",
        sections=extract_sections_from_text(text),
    )


def _extract_docx(path: Path) -> ExtractionResult:
    """Extract text + heading-style sections from a .docx file.

    Sections: paragraphs whose style name is "Title" or matches "Heading [1-9]",
    in document order, deduped, length-guarded, capped at MAX_SECTIONS_PER_FILE.

    Text: every paragraph body plus every table cell body, joined by "\n".
    No extractor-side byte cap — Reader owns the 64 KiB contract cap.
    """
    from docx import Document

    doc = Document(str(path))
    sections: list[str] = []
    seen: dict[str, None] = {}
    text_parts: list[str] = []

    for p in doc.paragraphs:
        body = p.text
        if body:
            text_parts.append(body)
        if len(sections) >= MAX_SECTIONS_PER_FILE:
            continue
        style_name = getattr(p.style, "name", "") or ""
        if style_name != "Title" and not _DOCX_HEADING_RE.match(style_name):
            continue
        label = " ".join(body.split())
        if not (SECTION_HEADER_MIN_CHARS <= len(label) <= SECTION_HEADER_MAX_CHARS):
            continue
        if label in seen:
            continue
        seen[label] = None
        sections.append(label)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell_text = cell.text
                if cell_text:
                    text_parts.append(cell_text)

    return ExtractionResult(
        text="\n".join(text_parts),
        status="ok",
        sections=tuple(sections),
    )


EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _read_text,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
}


def extract(path: Path) -> ExtractionResult:
    """Top-level entry point. Dispatch by extension; catch extractor exceptions."""
    extractor = EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return ExtractionResult(text=None, status="no_extractor")
    try:
        return extractor(path)
    except Exception as e:  # noqa: BLE001 — extractor must never abort a run
        return ExtractionResult(text=None, status="failed", note=str(e))
