# fda/metadata/cli.py
"""argparse handlers for `fda metadata <folder>` and `fda search <query>`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def handle_metadata(args: argparse.Namespace) -> int:
    """Run the metadata stage standalone on an already-organized tree."""
    from fda.claude_backend import get_claude_backend
    from fda.metadata import run as metadata_run
    from fda.metadata.store import LockBusy
    from fda.organize import reader
    from fda.organize._logger import OrganizeLogger

    target = Path(args.path).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 1

    backend = get_claude_backend()
    olog = OrganizeLogger(log_path=None, target_basename=target.name,
                          progress_callback=lambda m: print(m))
    try:
        catalog = reader.read(target, backend=backend, logger=olog)
        report = metadata_run(
            target_path=target, catalog=catalog, backend=backend,
            logger=olog, progress_callback=lambda m: print(m),
        )
        print(
            f"metadata: {report.files_classified}/{report.files_seen} "
            f"classified, {report.files_failed} failed "
            f"(run_id={report.run_id})"
        )
        return 0
    except LockBusy as e:
        print(f"⚠ {e}", file=sys.stderr)
        return 1
    except Exception as e:
        logger.exception("fda metadata crashed")
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def handle_search(args: argparse.Namespace) -> int:
    """FTS5 + filter search over ~/.fda/metadata.db."""
    from fda.metadata.search import search
    from fda.metadata.store import connect
    from fda.metadata.vocab import (
        ko_label_for_confidentiality,
        ko_label_for_department,
        ko_label_for_document_type,
    )

    db = Path.home() / ".fda" / "metadata.db"
    if not db.exists():
        print(f"error: no metadata DB at {db}; run `fda metadata <folder>` first",
              file=sys.stderr)
        return 1
    try:
        conn = connect(db)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        hits = search(
            conn,
            query=" ".join(args.query) if args.query else "",
            department=args.department,
            document_type=args.document_type,
            confidentiality=args.confidentiality,
            language=args.language,
            since=args.since,
            fail_closed_only=args.fail_closed_only,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        import json as _json
        print(_json.dumps([{
            "sha256": h.sha256, "path": h.path,
            "department": h.department, "document_type": h.document_type,
            "confidentiality": h.confidentiality, "language": h.language,
            "summary": h.summary, "confidence": h.confidence,
            "fail_closed_override": h.fail_closed_override,
            "mtime": h.mtime,
        } for h in hits], ensure_ascii=False, indent=2))
        return 0

    if not hits:
        print("(no matches)")
        return 0

    use_ko = not args.english
    for h in hits:
        dep = ko_label_for_department(h.department) if use_ko else h.department
        dt = ko_label_for_document_type(h.document_type) if use_ko else h.document_type
        conf = (
            ko_label_for_confidentiality(h.confidentiality)
            if use_ko else h.confidentiality
        )
        marker = " ⚠fail-closed" if h.fail_closed_override else ""
        print(f"{h.path}")
        print(f"  {dep} / {dt} / {conf}{marker}  (confidence={h.confidence:.2f})")
        if h.summary:
            print(f"  {h.summary[:200]}")
    return 0


def register_metadata_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "metadata",
        help="Run the metadata stage on an already-organized tree",
    )
    p.add_argument("path", help="Already-organized directory to index")
    p.set_defaults(func=handle_metadata)


def register_search_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "search",
        help="Keyword-search the metadata index (~/.fda/metadata.db)",
    )
    p.add_argument("query", nargs="*", help="Search terms (Korean OK)")
    p.add_argument("--department", help="Filter by department code")
    p.add_argument("--document-type", dest="document_type",
                   help="Filter by document_type code")
    p.add_argument("--confidentiality", help="Filter by confidentiality code")
    p.add_argument("--language", help="Filter by language (ko|en|unknown)")
    p.add_argument("--since", help="ISO mtime cutoff (rows with mtime >= since)")
    p.add_argument("--fail-closed-only", dest="fail_closed_only",
                   action="store_true",
                   help="Only rows where fail_closed_override = True")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--english", action="store_true",
                   help="Output English codes instead of Korean labels")
    p.add_argument("--json", action="store_true",
                   help="Output machine-readable JSON")
    p.set_defaults(func=handle_search)
