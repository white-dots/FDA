#!/usr/bin/env python3
"""Evaluate an FDA test fixture after it has been organized.

Joins the post-organize tree against manifest.csv (filename first,
SHA-256 fallback if FDA renamed) and writes a one-page evaluation
report plus stdout summary.

Usage:
    python scripts/evaluate_fda_fixture.py \
        /private/tmp/fda-test-sets/randomized-mixed-corpus-2026-05-12

What you see:
- File-count delta (manifest vs on-disk).
- Per-source landing: where did `company`, `napierone`, `hwp` files end up.
- Per-bucket origin mix: who landed in each FDA-created folder.
- Hwp coverage check.
- Anomalies (missing files, on-disk files not in manifest).
- Side-by-side preview of the router's destination counts if
  routing-report.json is present.

The report is written to <fixture>/evaluation-report.md so it survives
later runs and can be diffed across seeds.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

SPECIAL_FILES = {
    "manifest.csv",
    "routing-report.md",
    "routing-report.json",
    "evaluation-report.md",
    "_FDA_EVAL.md",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    by_name: dict[str, dict] = {}
    by_sha: dict[str, dict] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            name = Path(row["randomized_path"]).name
            by_name[name] = row
            by_sha[row["sha256"]] = row
    return by_name, by_sha


def walk_fixture(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name in SPECIAL_FILES:
            continue
        yield p


def relpath(root: Path, p: Path) -> str:
    return str(p.relative_to(root))


def write_lines(buf: list[str], *xs: str) -> None:
    for x in xs:
        buf.append(x)


def bucket_histogram(by_bucket: dict) -> list[tuple[str, int]]:
    """(bucket, file_count) sorted by count desc, then name. The #4
    over-fragmentation signal: a human reads this list."""
    return sorted(
        ((b, len(v)) for b, v in by_bucket.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )


def blob_s3_check(routing_data: dict | None) -> dict:
    """#5(a): a StorageBlob* category reached s3 at high confidence.

    Returns {"verdict": "yes"|"no"|"INCONCLUSIVE ...",
             "blob_s3": [(name, low_confidence), ...]}.
    None => routing-report.json missing/unreadable => the run cannot
    answer #5 (router failures are swallowed by organize(); spec
    precondition)."""
    if routing_data is None:
        return {"verdict": "INCONCLUSIVE (no routing-report.json)",
                "blob_s3": []}
    blob_s3 = [
        (c.get("name", ""), bool(c.get("low_confidence")))
        for c in routing_data.get("categories", []) or []
        if c.get("destination") == "s3"
        and str(c.get("name", "")).startswith("StorageBlob")
    ]
    ok = any(lc is False for _, lc in blob_s3)
    return {"verdict": "yes" if ok else "no", "blob_s3": blob_s3}


def s3_honesty_ok(
    routing_data: dict | None,
    *,
    expect_ext: tuple[str, ...] = ("pdf", "xyz", "bin"),
) -> dict:
    """#5(b): the non-blob unreadables stayed quarantined (not S3-routed).

    Proven by their quarantine buckets being present in the report:
    password .pdf -> _ExtractionFailed/pdf, mystery.xyz ->
    _NoExtractor/xyz, zero-byte .bin -> _NoExtractor/bin. Read from the
    report alone. We do NOT inspect which categories reached s3 — the LLM
    router and the all_extraction_failed short-circuit can legitimately
    send normal categories there, so that is not a honesty signal."""
    if routing_data is None:
        return {"verdict": "INCONCLUSIVE (no routing-report.json)",
                "present": [], "missing": list(expect_ext)}
    q_exts = {
        str(g.get("ext", "")).lower()
        for g in routing_data.get("quarantine", []) or []
    }
    present = [e for e in expect_ext if e in q_exts]
    missing = [e for e in expect_ext if e not in q_exts]
    return {"verdict": "yes" if not missing else "no",
            "present": present, "missing": missing}


def metadata_check(fda_home: Path, *, query: str = "계약서") -> dict:
    """Open <fda_home>/metadata.db; report classified-doc count, the run's
    seen/classified/failed (+ derived status) from the `runs` audit row,
    and Korean FTS hit count. Never raises — a missing/locked DB is a
    reported finding, not a crash."""
    out = {"rows": 0, "korean_hits": 0, "seen": 0, "classified": 0,
           "failed": 0, "status": "unknown", "error": ""}
    db = Path(fda_home) / "metadata.db"
    if not db.exists():
        out["error"] = f"metadata.db not found at {db}"
        out["status"] = "no metadata.db"
        return out
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            out["rows"] = conn.execute(
                "SELECT count(*) FROM documents"
            ).fetchone()[0]
            out["korean_hits"] = conn.execute(
                "SELECT count(*) FROM documents_fts "
                "WHERE documents_fts MATCH ?",
                (query,),
            ).fetchone()[0]
            row = conn.execute(
                "SELECT files_seen, files_classified, files_failed "
                "FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                out["status"] = "no run row"
            else:
                out["seen"] = int(row[0] or 0)
                out["classified"] = int(row[1] or 0)
                out["failed"] = int(row[2] or 0)
                out["status"] = (
                    "ok"
                    if out["failed"] == 0
                    and out["classified"] == out["seen"]
                    else "partial"
                )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def has_hangul(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in s or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", help="Path to the organized fixture folder.")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print to stdout only; skip evaluation-report.md.",
    )
    parser.add_argument(
        "--fda-home",
        default=None,
        help="Sandbox FDA_HOME dir holding metadata.db (Korean-search check).",
    )
    args = parser.parse_args()

    root = Path(args.fixture).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    manifest_path = root / "manifest.csv"
    if not manifest_path.exists():
        # FDA may have organized the manifest itself into a sub-bucket.
        # Fall back to the first manifest.csv found anywhere under root.
        found = sorted(root.rglob("manifest.csv"))
        if not found:
            print(f"manifest.csv missing under {root}", file=sys.stderr)
            return 2
        manifest_path = found[0]
        print(f"Note: using manifest at {manifest_path}", file=sys.stderr)

    # Sweep stale _FDA_EVAL.md so a re-run starts clean and the FDA
    # executor never sees our evaluator artifacts as input files.
    for stale in root.rglob("_FDA_EVAL.md"):
        stale.unlink()

    by_name, by_sha = load_manifest(manifest_path)
    on_disk = list(walk_fixture(root))

    matched: list[tuple[Path, dict]] = []
    unknown: list[Path] = []
    for p in on_disk:
        row = by_name.get(p.name)
        if row is None:
            row = by_sha.get(sha256_file(p))
        if row is None:
            unknown.append(p)
        else:
            matched.append((p, row))

    matched_shas = {row["sha256"] for _, row in matched}
    missing = [row for sha, row in by_sha.items() if sha not in matched_shas]

    by_bucket: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    by_source_bucket: dict[str, Counter] = defaultdict(Counter)
    hwp_buckets: Counter = Counter()
    for p, row in matched:
        bucket = relpath(root, p.parent) or "."
        source = row["source"]
        original_name = Path(row["original_path"]).name
        by_bucket[bucket].append((source, original_name, p.name))
        by_source_bucket[source][bucket] += 1
        if source == "hwp":
            hwp_buckets[bucket] += 1

    report: list[str] = []
    write_lines(report, "# FDA Fixture Evaluation Report", "")
    write_lines(report, f"Fixture: `{root}`", "")

    write_lines(report, "## File-count check", "")
    write_lines(
        report,
        f"- manifest: {len(by_name)}",
        f"- on-disk:  {len(on_disk)}",
        f"- matched:  {len(matched)}",
        f"- unknown (on-disk, not in manifest): {len(unknown)}",
        f"- missing (in manifest, not on-disk): {len(missing)}",
        "",
    )

    write_lines(report, "## Per-source: where did files land?", "")
    for source in sorted(by_source_bucket):
        total = sum(by_source_bucket[source].values())
        write_lines(report, f"### `{source}` — {total} files", "")
        for bucket, n in by_source_bucket[source].most_common():
            write_lines(report, f"- `{bucket}`: {n}")
        write_lines(report, "")

    write_lines(report, "## Per-bucket: origin mix", "")
    for bucket in sorted(by_bucket):
        rows = by_bucket[bucket]
        src_counts = Counter(src for src, _, _ in rows)
        write_lines(
            report,
            f"### `{bucket}` — {len(rows)} files",
            "",
            f"- sources: {dict(src_counts)}",
        )
        sample = sorted(rows)[:8]
        for src, original, _ in sample:
            write_lines(report, f"  - [{src}] {original}")
        if len(rows) > 8:
            write_lines(report, f"  - …and {len(rows)-8} more")
        write_lines(report, "")

    write_lines(report, "## Hwp coverage", "")
    hwp_total = sum(hwp_buckets.values())
    write_lines(report, f"- hwp files placed: {hwp_total} / 10")
    if hwp_buckets:
        write_lines(report, "- buckets containing hwp:")
        for b, n in hwp_buckets.most_common():
            write_lines(report, f"  - `{b}`: {n}")
    write_lines(report, "")

    routing_json = root / "routing-report.json"
    router_by_subpath: dict[str, dict] = {}
    if routing_json.exists():
        try:
            data = json.loads(routing_json.read_text(encoding="utf-8"))
            router_by_subpath = _index_router_categories(data)
            dest_counts: Counter = Counter()
            low_conf = 0
            for c in data.get("categories", []):
                dest_counts[c.get("destination", "unknown")] += c.get(
                    "signals", {}
                ).get("file_count", 0)
                if c.get("low_confidence"):
                    low_conf += 1
            write_lines(report, "## Router destination summary", "")
            for dest in ("sharepoint", "s3", "rdbms"):
                write_lines(report, f"- {dest}: {dest_counts.get(dest, 0)}")
            write_lines(
                report,
                f"- low-confidence categories: {low_conf}",
                "",
            )
        except Exception as e:  # noqa: BLE001
            write_lines(report, f"## Router report unreadable: {e}", "")

    if unknown:
        write_lines(report, "## Anomalies — files not in manifest", "")
        for p in unknown[:20]:
            write_lines(report, f"- `{relpath(root, p)}`")
        if len(unknown) > 20:
            write_lines(report, f"- …and {len(unknown)-20} more")
        write_lines(report, "")

    if missing:
        write_lines(report, "## Anomalies — manifest files missing from disk", "")
        for row in missing[:20]:
            write_lines(
                report,
                f"- [{row['source']}] {Path(row['original_path']).name} "
                f"(sha {row['sha256'][:12]})",
            )
        if len(missing) > 20:
            write_lines(report, f"- …and {len(missing)-20} more")
        write_lines(report, "")

    write_lines(report, "## Bucket-size histogram (#4 signal)", "")
    hist = bucket_histogram(by_bucket)
    _BLOB_FOLDERS = {"미디어_Media", "압축파일_Archives", "백업_Backups"}
    write_lines(report, f"- buckets: {len(hist)}")
    if hist:
        for _name, _n in hist:
            _tag = "  (blob — exclude #4)" if _name in _BLOB_FOLDERS else ""
            write_lines(report, f"- {_name}: {_n}{_tag}")
    else:
        write_lines(report, "- (none)")
    write_lines(report, "")

    rdata = None
    if routing_json.exists():
        try:
            rdata = json.loads(routing_json.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rdata = None
    a = blob_s3_check(rdata)
    b = s3_honesty_ok(rdata)
    write_lines(
        report, "## #5 — storage-blob -> S3 + honesty", "",
        f"- (a) blob->s3 high-confidence: {a['verdict']}"
        + (f"  [{', '.join(f'{n}:lc={lc}' for n, lc in a['blob_s3'])}]"
           if a["blob_s3"] else ""),
        f"- (b) honesty (non-blob unreadables quarantined): {b['verdict']}"
        f"  present={b['present']}"
        + (f" missing={b['missing']}" if b.get("missing") else ""),
        "",
    )

    mc = metadata_check(Path(args.fda_home)) if args.fda_home else None
    if mc is not None:
        write_lines(
            report, "## Metadata layer", "",
            f"- classified rows: {mc['rows']}",
            f"- run success: {mc['classified']}/{mc['seen']} classified, "
            f"{mc['failed']} failed (status: {mc['status']})",
            f"- Korean search '계약서' hits: {mc['korean_hits']}",
            (f"- error: {mc['error']}" if mc["error"] else ""),
            "",
        )

    ko_buckets = sum(1 for b in by_bucket if has_hangul(b))
    md_path = root / "routing-report.md"
    md_ko = md_path.exists() and has_hangul(
        md_path.read_text(encoding="utf-8", errors="ignore")
    )
    write_lines(
        report, "## Korean-label presence (#goal 4)", "",
        f"- buckets with Hangul names: {ko_buckets}/{len(by_bucket)}",
        f"- routing-report.md contains Hangul: {'yes' if md_ko else 'no'}",
        "",
    )

    # --- Consolidated Summary scoreboard, PREPENDED so the scorecard
    #     opens with it (spec: "scorecard opens with a ## Summary"). All
    #     pieces above are already computed; we just aggregate + prepend.
    dest_counts: dict[str, int] = {}
    for c in (rdata or {}).get("categories", []) or []:
        d = c.get("destination", "?")
        dest_counts[d] = dest_counts.get(d, 0) + 1
    dest_str = ", ".join(
        f"{k}={dest_counts[k]}" for k in sorted(dest_counts)
    ) or "(none)"
    md_line = (
        f"{mc['classified']}/{mc['seen']} classified, {mc['failed']} "
        f"failed (status {mc['status']}), searchable "
        f"{'yes' if mc['korean_hits'] else 'no'}"
        if mc is not None else "(no --fda-home; metadata not checked)"
    )
    summary = [
        "## Summary", "",
        f"- organized: {len(hist)} folders, "
        f"{len(unknown) + len(missing)} discrepancies "
        f"(incl. up to 3 expected blob folders — exclude for #4)",
        f"- routed to cloud: {dest_str} | #5(a) blob->s3 {a['verdict']} | "
        f"#5(b) honesty {b['verdict']}",
        f"- metadata: {md_line}",
        f"- Korean labels: {ko_buckets}/{len(by_bucket)} buckets, "
        f"routing-report.md Hangul {'yes' if md_ko else 'no'}",
        "",
    ]
    report[:0] = summary

    output = "\n".join(report) + "\n"
    print(output)
    written: list[Path] = []
    if not args.no_write:
        out_path = root / "evaluation-report.md"
        out_path.write_text(output, encoding="utf-8")
        written.append(out_path)

        for bucket in sorted(by_bucket):
            bucket_dir = root / bucket if bucket != "." else root
            rows = by_bucket[bucket]
            src_counts = Counter(src for src, _, _ in rows)
            buf: list[str] = []
            buf.append(f"# FDA bucket: `{bucket}`")
            buf.append("")
            buf.append(f"- files: {len(rows)}")
            buf.append(f"- sources: {dict(src_counts)}")
            cat = _match_router_category(bucket, router_by_subpath)
            if cat is not None:
                dest = cat.get("destination", "?")
                low = " (low confidence)" if cat.get("low_confidence") else ""
                buf.append(f"- router destination: {dest}{low}")
                reason = (cat.get("reason") or "").strip()
                if reason:
                    buf.append("")
                    buf.append("**Claude's reason:**")
                    buf.append("")
                    buf.append("> " + reason.replace("\n", "\n> "))
            buf.append("")
            buf.append("## Sample originals (up to 12)")
            buf.append("")
            for src, original, _ in sorted(rows)[:12]:
                buf.append(f"- [{src}] {original}")
            if len(rows) > 12:
                buf.append(f"- …and {len(rows)-12} more")
            buf.append("")
            eval_path = bucket_dir / "_FDA_EVAL.md"
            eval_path.write_text("\n".join(buf), encoding="utf-8")
            written.append(eval_path)

    for p in written:
        print(f"Wrote: {p}")
    return 0 if not unknown and not missing else 1


def _index_router_categories(data: dict) -> dict[str, dict]:
    """Build a subpath → category dict from a routing-report.json payload.

    The bucket relpath on disk equals the router's `subpath` exactly, so
    a subpath-keyed index makes the evaluator's lookup a direct dict hit.
    Categories with no `subpath` are silently skipped (defensive — the
    current router always emits it). The legacy code keyed by `name`,
    which silently dropped categories whose `name` differed from
    `subpath` (e.g. Korean subpaths under an English name).
    """
    index: dict[str, dict] = {}
    for c in data.get("categories", []) or []:
        sub = c.get("subpath")
        if sub:
            index[sub] = c
    return index


def _match_router_category(
    bucket: str, router_by_subpath: dict[str, dict],
) -> dict | None:
    """Look up the routing-report category whose `subpath` equals the
    on-disk bucket (relpath under the evaluation root).

    Router `subpath` is the exact relative path the executor created. The
    evaluator builds `bucket` from the same relpath, so a direct dict
    lookup is sufficient. No dash/case fallbacks: those existed only to
    paper over a now-removed `name`-keyed lookup.
    """
    return router_by_subpath.get(bucket)


if __name__ == "__main__":
    sys.exit(main())
