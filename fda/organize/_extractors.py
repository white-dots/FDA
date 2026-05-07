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


def _extract_xlsx(path: Path) -> ExtractionResult:
    """Extract text + sections from an .xlsx workbook.

    Two passes are required: pass 1 (data_only=True, read_only=True) for cached
    values and the row-1 column headers; pass 2 (data_only=False, read_only=False)
    for formula counts and merged-range counts. Workbook is NOT a context manager
    in openpyxl 3.0.9 — close via try/finally.

    Sections layout: schema labels first (Sheet:<name> + row-1 headers, in
    workbook order, deduped, length-guarded, capped to
    MAX_SECTIONS_PER_FILE - len(synthesized)), then synthesized labels in the
    tail.

    Text layout: per sheet, "Sheet: <name>\\n", then up to
    _XLSX_TEXT_ROWS_PER_SHEET rows of \\t-joined cells (left-trimmed to
    _XLSX_TEXT_COLS_PER_ROW), then a blank line. No extractor-side byte cap.
    """
    import openpyxl

    # Pass 2 — formula counts, merged-range counts, formula-string lookup.
    # Filled in Task 6; for this task we just do pass 1 and leave synthesized=[].
    synthesized: list[str] = []

    schema_labels: list[str] = []
    schema_seen: dict[str, None] = {}
    text_parts: list[str] = []

    wb1 = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for sheet_name in wb1.sheetnames:
            ws = wb1[sheet_name]
            sheet_label = f"Sheet:{sheet_name}"
            if sheet_label not in schema_seen:
                schema_seen[sheet_label] = None
                schema_labels.append(sheet_label)
            row1 = next(ws.iter_rows(values_only=True, max_row=1), ())
            for v in row1:
                if v is None:
                    continue
                s = " ".join(str(v).split())
                if not s:
                    continue
                if not (SECTION_HEADER_MIN_CHARS <= len(s) <= SECTION_HEADER_MAX_CHARS):
                    continue
                if s in schema_seen:
                    continue
                schema_seen[s] = None
                schema_labels.append(s)
            text_parts.append(f"Sheet: {sheet_name}\n")
            # Text-row serialization stub — Task 5 fills this in.
            text_parts.append("\n")
    finally:
        wb1.close()

    schema_cap = MAX_SECTIONS_PER_FILE - len(synthesized)
    schema_capped = schema_labels[: max(0, schema_cap)]
    # Final ordered-set pass over `schema_capped + synthesized` so a column
    # header literally named "FormulaHeavy" or "MergedCells" doesn't appear
    # twice when its synthesized counterpart triggers.
    final_seen: dict[str, None] = {}
    sections_list: list[str] = []
    for label in schema_capped + synthesized:
        if label not in final_seen:
            final_seen[label] = None
            sections_list.append(label)
    sections = tuple(sections_list)

    return ExtractionResult(
        text="".join(text_parts),
        status="ok",
        sections=sections,
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
    ".xlsx": _extract_xlsx,
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
