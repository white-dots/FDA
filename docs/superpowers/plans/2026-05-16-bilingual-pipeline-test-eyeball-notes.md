# Bilingual pipeline test — eyeball notes

Spec: docs/superpowers/specs/2026-05-16-bilingual-pipeline-test-design.md
Fixture: /private/tmp/fda-test-sets/randomized-bilingual-2026-05-16-001
Seed: bilingual-2026-05-16   FDA_HOME: <sandbox dir>

> Filled by the ASSISTANT after the operator runs the pipeline and pastes
> back the `evaluate_fda_fixture.py` scorecard (which opens with its own
> `## Summary`). The operator does not hand-fill the verdicts.

## Assistant summary (plain-language, written post-run)
- organized: ___
- routed to cloud: ___
- metadata layer: ___
- overall: ___

## Preconditions (must hold before recording any verdict)
- [ ] routing-report.json present and readable
- [ ] organize log has NO "router stage failed" line
- [ ] step-2b real-source counts verified; no --quota exceeded its source

## Goal 1 — organization quality (#4)
- buckets: ___  sizes: ___
  (exclude 미디어_Media / 압축파일_Archives / 백업_Backups — expected
   S3 folders by design, NOT over-fragmentation)
- verifier discrepancies: ___ (must be 0)
- VERDICT #4 (over-fragmentation): ___

## Goal 2 — router (#5: storage-blob -> S3 + honesty)
- sharepoint ___ | s3 ___ | rdbms ___
- (a) StorageBlob* at s3, low_confidence=false? ___  (list: ___)
- (b) non-blob unreadables quarantined — buckets pdf/xyz/bin present? ___
      (missing: ___)
- VERDICT #5: ___

## Goal 3 — metadata layer
- classified rows: ___  | Korean '계약서' hits: ___
- run success: ___/___ classified, ___ failed (status: ___)

## Goal 4 — Korean handling
- Hangul bucket names: ___/___  | routing-report.md Hangul: ___
- business_context.md rules honored? ___
