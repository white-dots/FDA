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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", help="Path to the organized fixture folder.")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print to stdout only; skip evaluation-report.md.",
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
