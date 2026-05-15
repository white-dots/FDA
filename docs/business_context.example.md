# [Your Company Name] — Business Context for FDA Metadata Layer

> **Setup:** Copy this file to `~/.fda/business_context.md` and edit each section.
> Delete sections that do not apply to your company. Add your own sections if useful.
> See `docs/business_context_guide.md` for what makes good context.

---

## Departments

List each department in BOTH Korean and English (English is the database code).
One short line describing what work they do.

- 영업 (sales) — 매출 활동, 고객 계약, 제안서
- 재무 (finance) — 회계, 청구서, 예산
- 인사 (hr) — 채용, 인사 평가, 급여 정보
- 생산 (production) — 제조 공정, 품질 관리, 생산 일정
- 연구개발 (rd) — 실험 보고서, 특허, 신제품 개발
- 법무 (legal) — 계약 검토, 컴플라이언스
- 운영 (operations) — 시설 관리, 물류
- 마케팅 (marketing) — 외부 자료, 캠페인
- 경영진 (executive) — 임원 회의록, 전략 문서

## Confidentiality Rules

Company-specific rules. Generic rules (e.g., "salary is sensitive") are already
in the metadata-classifier skill — only put YOUR specific rules here.

- [Example] Catalyst formulations and process parameters → restricted (trade secret)
- [Example] Files referencing Process [Internal Project Name] → restricted
- [Example] Customer contracts with named third parties → confidential
- [Example] Internal executive meeting minutes → confidential
- [Example] Marketing materials approved for public release → public
- When in doubt → restricted (fail-closed; never under-classify)

## Internal Codes

Codes and abbreviations the model would not otherwise recognize.

- PR-#### → Production Run number (e.g., PR-2026-042)
- BOM → Bill of Materials
- COA → Certificate of Analysis
- MSDS → Material Safety Data Sheet
- [Add your company's internal codes here]

## Document Type Clarifications

Only fill in where your usage differs from generic meaning.

- [Example] "보고서" in our company refers to quarterly summaries, not one-off memos.
- [Example] "제안서" with a customer name → proposal; without one → memo.

## Folder Granularity (organize-only)

How coarse or fine the organize command's folder taxonomy should be.
The organize classifier honors these preferences; the metadata classifier
ignores them. Plain-prose statements work best — write what you want, not
how to compute it.

- [Example] Treat order-shaped documents (purchase orders, sales orders,
  delivery orders) as a single `Sales/Orders` bucket. Do not split by
  template variation.
- [Example] Keep all client-visit reports in `영업/거래처방문보고서`
  regardless of which sales rep filed them.
- [Example] Separate quarterly reports (`YYYY_분기_*`) from one-off memos
  even when the title says "보고서."
- [Example] Prefer fewer, broader buckets when in doubt — under 10 top-level
  folders for a corpus under 200 files.

## Retention Rules (optional)

How long different types should be kept. The model uses this as a hint for
confidentiality and storage routing.

- Invoices: 7 years (legal requirement)
- Employment contracts: 5 years after termination
- Production run records: 10 years (regulatory)
- Marketing drafts: 1 year

## Naming Conventions (optional)

Consistent file-naming patterns at your company.

- "YYYY_분기_*" pattern → quarterly report
- "PR-####_*" pattern → production run record
- "Confidential_*" prefix → respect existing confidentiality marking

---

## Notes

- This file is loaded by FDA's metadata classifier on every run.
- It is NOT committed to the FDA repository. Keep your company's version at `~/.fda/business_context.md`.
- Total size cap: ~50KB. Beyond that, the file is truncated with a warning.
- Edit any time. The next `fda organize` or `fda metadata` run picks up the change.
