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
