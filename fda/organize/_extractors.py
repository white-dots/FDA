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

import csv
import io
import itertools
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

# pptx serialization caps (memory-bound the in-process slide/shape/notes walk;
# do NOT cap `text` — Reader owns the 64 KiB contract cap via
# READER_TEXT_CAP_BYTES).
_PPTX_SLIDES_MAX = 100
_PPTX_SHAPES_PER_SLIDE_MAX = 50
_PPTX_NOTES_CHARS_PER_SLIDE_MAX = 2000

# csv extractor caps and fallback label. The five _CSV_* numeric caps
# memory-bound the in-process read/sniff/scan/serialize walk; do NOT cap
# `text` — Reader owns the 64 KiB contract cap via READER_TEXT_CAP_BYTES.
# _CSV_NO_HEADER_LABEL is the synthesized fallback section label appended
# when no usable header is found (parallel to xlsx's "FormulaHeavy").
_CSV_READ_BYTES_MAX = 4 * 1024 * 1024
_CSV_SNIFF_SAMPLE_CHARS = 8 * 1024
_CSV_HEADER_SCAN_ROWS = 5
_CSV_TEXT_ROWS_MAX = 20
_CSV_TEXT_COLS_PER_ROW = 32
_CSV_NO_HEADER_LABEL = "NoHeader"

# Per-script length-guard floor for csv column headers. SECTION_HEADER_MIN_CHARS
# (= 3) was chosen for English where 1- and 2-char strings are usually noise.
# Korean column headers are syllable-dense — `이름` (name), `나이` (age), `부서`
# (department) are 2-char and routine. Apply this floor when the label contains
# Hangul. Mirrors the design pattern in the in-flight Korean sections sub-project
# (`KOREAN_LABEL_MIN_CHARS = 2` in `_sections.py`); replace this private csv copy
# with an import once that work merges.
_CSV_KOREAN_LABEL_MIN_CHARS = 2

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

    # Pass 2 — formula counts, merged-range counts. ReadOnlyWorksheet does
    # NOT expose merged_cells in openpyxl 3.0.9, so this pass uses the
    # default read_only=False.
    non_empty_cells = 0
    formula_cells = 0
    merged_count = 0
    formulas_by_addr: dict[tuple[str, int, int], str] = {}
    wb2 = openpyxl.load_workbook(path, data_only=False, read_only=False)
    try:
        for ws in wb2.worksheets:
            merged_count += len(ws.merged_cells.ranges)
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    non_empty_cells += 1
                    if cell.data_type == "f":
                        formula_cells += 1
                        # openpyxl returns formula values that already start
                        # with "=" (e.g., "=A2*10"). Defensive prefix only if
                        # missing — never f"={cell.value}" blindly, which
                        # would double-prefix and produce "==A2*10".
                        raw = str(cell.value)
                        formula_str = raw if raw.startswith("=") else f"={raw}"
                        formulas_by_addr[(ws.title, cell.row, cell.column)] = (
                            formula_str
                        )
    finally:
        wb2.close()

    synthesized: list[str] = []
    if (
        non_empty_cells > 0
        and formula_cells / non_empty_cells > _XLSX_FORMULA_DENSITY_THRESHOLD
    ):
        synthesized.append("FormulaHeavy")
    if merged_count >= _XLSX_MERGED_CELLS_MIN:
        synthesized.append("MergedCells")

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
            for r_idx, row in enumerate(
                ws.iter_rows(values_only=True, max_row=_XLSX_TEXT_ROWS_PER_SHEET),
                start=1,
            ):
                row_slice = list(row[:_XLSX_TEXT_COLS_PER_ROW])
                cells_str: list[str] = []
                for c_idx, value in enumerate(row_slice, start=1):
                    if value is None:
                        formula = formulas_by_addr.get(
                            (sheet_name, r_idx, c_idx)
                        )
                        cells_str.append(formula if formula is not None else "")
                    else:
                        cells_str.append(str(value))
                while cells_str and cells_str[-1] == "":
                    cells_str.pop()
                text_parts.append("\t".join(cells_str) + "\n")
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


def _extract_pptx(path: Path) -> ExtractionResult:
    """Extract text + slide-title sections from a .pptx file.

    Sections: slide titles read from the title placeholder, in slide order,
    deduped, length-guarded, capped at MAX_SECTIONS_PER_FILE. Slides whose
    layout has no title placeholder contribute nothing to sections.

    Text: per slide, "Slide N: <title>\n" banner + shape text from every
    text-bearing shape + speaker notes (when present, with all guards).
    Banner omits title when title fails the length guard or is empty after
    trim. No extractor-side byte cap — Reader owns the 64 KiB contract cap.
    """
    from pptx import Presentation

    prs = Presentation(str(path))
    sections: list[str] = []
    seen: dict[str, None] = {}
    text_parts: list[str] = []

    for slide_idx, slide in enumerate(prs.slides, start=1):
        if slide_idx > _PPTX_SLIDES_MAX:
            break

        title_shape = slide.shapes.title
        title_for_banner = ""  # set ONLY when title passes length guard
        if title_shape is not None:
            raw = title_shape.text or ""
            candidate = " ".join(raw.split())
            if SECTION_HEADER_MIN_CHARS <= len(candidate) <= SECTION_HEADER_MAX_CHARS:
                title_for_banner = candidate
                if candidate not in seen and len(sections) < MAX_SECTIONS_PER_FILE:
                    seen[candidate] = None
                    sections.append(candidate)

        if title_for_banner:
            text_parts.append(f"Slide {slide_idx}: {title_for_banner}\n")
        else:
            text_parts.append(f"Slide {slide_idx}:\n")

        for shape_idx, shape in enumerate(slide.shapes, start=1):
            if shape_idx > _PPTX_SHAPES_PER_SLIDE_MAX:
                break
            if not shape.has_text_frame:
                continue
            body = shape.text_frame.text
            if body:
                text_parts.append(body + "\n")

        if slide.has_notes_slide:
            notes_tf = slide.notes_slide.notes_text_frame
            if notes_tf is not None:
                notes_text = notes_tf.text or ""
                if notes_text.strip():
                    text_parts.append(
                        "Notes: "
                        + notes_text[:_PPTX_NOTES_CHARS_PER_SLIDE_MAX]
                        + "\n"
                    )

        text_parts.append("\n")  # blank line between slides

    return ExtractionResult(
        text="".join(text_parts),
        status="ok",
        sections=tuple(sections),
    )


def _extract_csv(path: Path) -> ExtractionResult:
    """Extract text + column-header sections from a .csv file.

    Sections: row-1 column headers under csv.Sniffer.has_header(), or the
    first row in the first _CSV_HEADER_SCAN_ROWS that satisfies all five
    header-shape rules when has_header() is False. Length-guarded, deduped,
    capped at MAX_SECTIONS_PER_FILE. When no usable header is found,
    sections=(_CSV_NO_HEADER_LABEL,).

    Text: up to _CSV_TEXT_ROWS_MAX physical rows from the start, each
    truncated to _CSV_TEXT_COLS_PER_ROW cells, "\\t"-joined with a trailing
    "\\n". Sniffed delimiter is normalized to tab so the classifier sees a
    uniform shape across .csv and .xlsx. No banner. No extractor-side byte
    cap — Reader owns the 64 KiB contract cap.

    Encoding: utf-8-sig (BOM-tolerant) → cp949 (Excel-Korean exports).
    Both decode failures → status="failed".
    """
    with path.open("rb") as f:
        raw = f.read(_CSV_READ_BYTES_MAX)

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("cp949")
        except UnicodeDecodeError as e:
            return ExtractionResult(text=None, status="failed", note=str(e))

    if not decoded:
        return ExtractionResult(text="", status="ok", sections=())
    if not decoded.strip():
        # Whitespace-only decoded content: csv.reader would yield rows of
        # whitespace cells, but spec requires text="" and sections=("NoHeader",).
        return ExtractionResult(
            text="", status="ok", sections=(_CSV_NO_HEADER_LABEL,)
        )

    sample = decoded[:_CSV_SNIFF_SAMPLE_CHARS]
    try:
        # Restrict candidate delimiters so the sniffer cannot pick a stray
        # alphabetic byte (e.g. "n" from "name") on short single-column samples.
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = False

    # Bounded two-pass iteration. Pass 1: read up to _CSV_HEADER_SCAN_ROWS + 1
    # rows for header detection (the +1 is rule 5's lookahead). Pass 2:
    # re-iterate from the start and read up to _CSV_TEXT_ROWS_MAX rows for
    # serialization. A malformed row past those windows does not affect
    # extraction — only mid-window corruption propagates as status="failed".
    header_iter = csv.reader(io.StringIO(decoded), dialect)
    scan_window = list(itertools.islice(header_iter, _CSV_HEADER_SCAN_ROWS + 1))

    header_cells: list[str] | None = None
    if has_header and scan_window:
        header_cells = scan_window[0]
    elif scan_window:
        scan_limit = min(_CSV_HEADER_SCAN_ROWS, len(scan_window))
        for i in range(scan_limit):
            r = scan_window[i]
            non_empty = [c for c in r if c and c.strip()]
            if len(non_empty) < 2:
                continue
            normalized = [" ".join(c.split()) for c in non_empty]
            if not all(
                _label_min_chars(s) <= len(s) <= SECTION_HEADER_MAX_CHARS
                for s in normalized
            ):
                continue
            if any(_is_pure_number(s) for s in normalized):
                continue
            if len(set(normalized)) != len(normalized):
                continue
            # rule 5: cell count matches the next row, OR R is the final row.
            # The scan_window size is _CSV_HEADER_SCAN_ROWS + 1, so for i in
            # 0..scan_limit-1 the lookahead scan_window[i+1] is in-bounds
            # whenever the file has more rows than the scan window. When
            # i + 1 == len(scan_window), the file had ≤ scan+1 rows total and
            # R is the final row — accept it (no cell-count constraint).
            if i + 1 < len(scan_window) and len(r) != len(scan_window[i + 1]):
                continue
            header_cells = r
            break

    if header_cells is None:
        sections = (_CSV_NO_HEADER_LABEL,) if scan_window else ()
    else:
        labels: list[str] = []
        seen: dict[str, None] = {}
        for cell in header_cells:
            if cell is None:
                continue
            label = " ".join(str(cell).split())
            if not label:
                continue
            if not (_label_min_chars(label) <= len(label) <= SECTION_HEADER_MAX_CHARS):
                continue
            if label in seen:
                continue
            seen[label] = None
            labels.append(label)
            if len(labels) >= MAX_SECTIONS_PER_FILE:
                break
        sections = tuple(labels) if labels else (_CSV_NO_HEADER_LABEL,)

    text_iter = csv.reader(io.StringIO(decoded), dialect)
    text_parts: list[str] = []
    for row in itertools.islice(text_iter, _CSV_TEXT_ROWS_MAX):
        cells = [str(c) if c is not None else "" for c in row[:_CSV_TEXT_COLS_PER_ROW]]
        text_parts.append("\t".join(cells) + "\n")

    return ExtractionResult(
        text="".join(text_parts),
        status="ok",
        sections=sections,
    )


def _is_pure_number(s: str) -> bool:
    """True iff s parses as int or float (no exception). Used by the
    has_header-False fallback scan to reject all-numeric rows."""
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        return False


def _label_min_chars(s: str) -> int:
    """Per-script length-guard floor for csv header labels.

    Returns _CSV_KOREAN_LABEL_MIN_CHARS (= 2) when s contains any Hangul
    syllable (U+AC00..U+D7A3), else SECTION_HEADER_MIN_CHARS (= 3). The
    Hangul Syllables block matches the range used by the in-flight Korean
    sections sub-project for `_sections.py`.
    """
    for c in s:
        if "가" <= c <= "힣":
            return _CSV_KOREAN_LABEL_MIN_CHARS
    return SECTION_HEADER_MIN_CHARS


EXTRACTORS: dict[str, TextExtractor] = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _extract_csv,
    ".log": _read_text,
    ".json": _read_text,
    ".xml": _read_text,
    ".pdf": _extract_pdf_text,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
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
