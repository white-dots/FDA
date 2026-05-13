# tests/test_routing_report.py
"""Snapshot test for _write_md_report (Korean labels, mixed-language categories).

Spec: docs/superpowers/specs/2026-05-13-korean-bucket-names-design.md
"""
from __future__ import annotations


def test_markdown_writer_renders_korean_labels_and_mixed_categories(tmp_path):
    from fda.organize.models import (
        CatalogEntry, RoutedCategory, RoutingReport,
    )
    from fda.organize.router import _aggregate_signals, _write_md_report

    entry = CatalogEntry(
        path_id="f000",
        path=str(tmp_path / "영업/거래처방문보고서/v.hwp"),
        ext=".hwp",
        size_bytes=1000,
        summary="거래처 방문 결과를 기록한 영업팀 보고서.",
        type_label="visit-report",
        is_junk=False,
        summary_failed=False,
        extract_status="ok",
        sections=(),
    )
    sig = _aggregate_signals([entry])
    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T14:22:00Z",
        target_root=str(tmp_path),
        categories=(
            RoutedCategory(
                name="Client-Visit-Reports",
                subpath="영업/거래처방문보고서",
                destination="sharepoint",
                reason="영업팀이 SharePoint에서 자주 조회하는 거래처 방문 보고서.",
                low_confidence=False, signals=sig, misfits=(),
            ),
            RoutedCategory(
                name="Business-Rates",
                subpath="Finance/Business-Rates",
                destination="sharepoint",
                reason="Finance team reference rates accessed via M365.",
                low_confidence=False, signals=sig, misfits=(),
            ),
        ),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")

    assert "# 라우팅 보고서" in md
    assert "## 카테고리별 라우팅" in md
    assert "대상:" in md
    assert "파일 수:" in md
    assert "이유:" in md
    assert "영업/거래처방문보고서" in md
    assert "영업팀이 SharePoint에서 자주 조회하는 거래처 방문 보고서." in md
    assert "Finance/Business-Rates" in md
    assert "Finance team reference rates accessed via M365." in md


def test_json_report_always_includes_quarantine_key(tmp_path):
    """Even when empty, the quarantine key appears as []."""
    import json

    from fda.organize.models import RoutingReport
    from fda.organize.router import _report_to_dict, _write_json_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
    )
    out = tmp_path / "routing-report.json"
    _write_json_report(report, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["quarantine"] == []


def test_json_report_quarantine_shape_matches_spec(tmp_path):
    import json

    from fda.organize.models import (
        QuarantineEntry, QuarantineGroup, RoutingReport,
    )
    from fda.organize.router import _write_json_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
        quarantine=(
            QuarantineGroup(
                bucket="_NoExtractor", ext="doc",
                entries=(QuarantineEntry(
                    relative_path="_NoExtractor/doc/old-quote.doc",
                    size_bytes=24576,
                    note="no extractor registered for .doc",
                ),),
            ),
        ),
    )
    out = tmp_path / "routing-report.json"
    _write_json_report(report, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["quarantine"] == [
        {
            "bucket": "_NoExtractor",
            "ext": "doc",
            "entries": [
                {
                    "relative_path": "_NoExtractor/doc/old-quote.doc",
                    "size_bytes": 24576,
                    "note": "no extractor registered for .doc",
                },
            ],
        }
    ]


def test_md_report_omits_quarantine_sections_when_empty(tmp_path):
    from fda.organize.models import RoutingReport
    from fda.organize.router import _write_md_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")
    assert "건너뜀" not in md
    assert "건너뜀:" not in md  # header summary should not show skip count


def test_md_report_renders_both_quarantine_sections(tmp_path):
    from fda.organize.models import (
        QuarantineEntry, QuarantineGroup, RoutingReport,
    )
    from fda.organize.router import _write_md_report

    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(),
        quarantine=(
            QuarantineGroup(
                bucket="_NoExtractor", ext="doc",
                entries=(
                    QuarantineEntry(
                        relative_path="_NoExtractor/doc/old-quote.doc",
                        size_bytes=24576,
                        note="no extractor registered for .doc",
                    ),
                ),
            ),
            QuarantineGroup(
                bucket="_ExtractionFailed", ext="pdf",
                entries=(
                    QuarantineEntry(
                        relative_path="_ExtractionFailed/pdf/scan.pdf",
                        size_bytes=180_000,
                        note="image-only PDF?",
                    ),
                ),
            ),
        ),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")
    # Header summary gains skip count
    assert "건너뜀: 2 (추출기 없음 1, 추출 실패 1)" in md
    # Sections present in fixed order
    assert "## 건너뜀 — 추출기 없음 (No Extractor)" in md
    assert "## 건너뜀 — 추출 실패 (Extraction Failed)" in md
    # Per-extension subheaders
    assert "### .doc (1개)" in md
    assert "### .pdf (1개)" in md
    # Per-file entries: backtick path + comma-grouped bytes + 바이트 + reason
    assert "`_NoExtractor/doc/old-quote.doc` (24,576 바이트) — no extractor registered for .doc" in md
    assert "`_ExtractionFailed/pdf/scan.pdf` (180,000 바이트) — image-only PDF?" in md


def test_md_report_header_summary_includes_skip_count_only_when_nonzero(tmp_path):
    from fda.organize.models import (
        CatalogEntry, RoutedCategory, RoutingReport,
    )
    from fda.organize.router import _aggregate_signals, _write_md_report

    entry = CatalogEntry(
        path_id="f000", path="/tmp/x/a.txt",
        ext=".txt", size_bytes=10, summary="s", type_label="doc",
        is_junk=False, summary_failed=False, extract_status="ok",
    )
    sig = _aggregate_signals([entry])
    report = RoutingReport(
        version="1.0",
        generated_at="2026-05-13T00:00:00Z",
        target_root=str(tmp_path),
        categories=(
            RoutedCategory(
                name="Texts", subpath="Texts", destination="sharepoint",
                reason="r", low_confidence=False, signals=sig, misfits=(),
            ),
        ),
    )
    out = tmp_path / "routing-report.md"
    _write_md_report(report, out)
    md = out.read_text(encoding="utf-8")
    assert "건너뜀:" not in md  # no quarantine → no skip count segment
    assert "총 카테고리: 1 · 총 파일: 1" in md
