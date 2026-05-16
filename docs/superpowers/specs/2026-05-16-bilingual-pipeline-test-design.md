# Bilingual real+synthetic pipeline test — design

Date: 2026-05-16
Branch: dev_branch
Status: design approved (brainstorm), pending Codex + user spec review

## Purpose

Run the FDA organize pipeline once, end to end, on a realistic messy bilingual
corpus, and produce evidence for four things:

1. **Organization works** — sensible folders, no lost/misplaced files,
   verifier discrepancies = 0, bucket count is sane.
2. **Cloud router works** — all three destinations
   (SharePoint / S3 / RDBMS) actually fire.
3. **Metadata layer is built** — the sandbox `metadata.db` has classified
   rows and a Korean search query returns hits.
4. **Korean handling succeeds** — Korean folder names, Korean `reason` text
   in `routing-report.md`, and the Korean `business_context.md` rules are
   honored.

This run also resolves two still-open backlog items
(`project_organize_test_followups.md`):

- **#4 — over-fragmentation.** A 10-file sample once produced four separate
  2-file Korean buckets. A larger run is needed to judge whether buckets stay
  sane. Verdict is made by **eyeballing a bucket-size histogram** on a
  ~300-file run (the runbook's own evaluation method), not by an automated
  cohesion score.
- **#5 — is the `s3` router branch dead?** It has never fired in prior tests.
  The router sends a category to `s3` deterministically when the category is
  the catch-all `Misc` **or** when every file in it failed text extraction
  (`all_extraction_failed` in `fda/organize/router.py:_short_circuit`). The
  corpus deliberately includes a realistic junk pile that produces at least
  one all-unreadable bucket, so `s3` must fire — or it is genuinely broken
  and we drop to two destinations.

  **Precondition for the verdict:** the router stage swallows its own
  failures (`fda/organize/__init__.py` logs `router stage failed` and
  continues). "S3 did not fire" is only a valid #5 finding if the run
  *actually routed*: `routing-report.json` must exist and be readable, and
  the organize log must contain **no** `router stage failed` line. If either
  check fails the run is inconclusive for #5 — rerun, do not record a
  verdict.

## Background that shaped this design

Two beliefs from an earlier session were wrong and are corrected here:

- macOS privacy does **not** block this terminal from reading
  `~/Documents` or `~/Downloads`. No copy-out workaround is needed.
- A large real corpus already exists at
  `/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data/`, but it is
  **English-heavy and low-diversity**: `02_company_documents` is 2,676
  English `invoice_*.pdf` (one document type, zero Korean);
  `03_napierone/extracted` is 1,020 mixed-extension English/synthetic files;
  `hwp_samples/` is 10 real Korean `.hwp` with generic names.

Consequence: **every Korean business-purpose document must be generated.**
Real files supply file-type diversity, English noise, and a little real
Korean `.hwp`; generated files supply the Korean business content the test
actually needs.

## Corpus design (~300 files, three streams)

All three streams are pooled by the existing
`scripts/randomize_fda_fixture.py`, which copies (never moves), hash-renames
while preserving extensions, scatters files into random `folder_1..N`
buckets, and writes one `manifest.csv` with a `source` column.

### Stream 1 — generated Korean business docs (~120)

Persistent, reusable source folder: `doc_agent_test_data/04_mock_korean/`.

- Six document types, realistic Korean content:
  계약서 (contracts), 견적서 (quotes), 인사문서 (HR documents),
  분기보고서 (quarterly reports), 거래처방문보고서 (client-visit reports),
  회의록 (meeting minutes).
- Formats: `.docx`, `.pdf`, `.txt`. **No `.hwpx`** — generating valid OWPML
  is fiddly and redundant given 20 real `.hwp` files exist; a malformed
  generated file would silently become quarantine noise and produce a false
  negative.
- Korean `.pdf` uses a **real embedded Korean TrueType font**, **not**
  reportlab's built-in CID font. CID-font PDFs frequently extract as garbage
  through the pdfminer/pypdf-class extractors FDA uses; an embedded TTF with
  a ToUnicode map extracts reliably. The generator takes a
  `--korean-font PATH` argument and resolves a usable single-face `.ttf` in
  this order: (a) the explicit `--korean-font` path, (b) a repo-vendored
  `tests/assets/NanumGothic.ttf` if present, (c) a documented macOS fallback.
  `.ttc` collection files are rejected (reportlab `TTFont` needs a single
  face). If no usable `.ttf` is found, the generator fails loudly naming the
  expected paths — it never emits tofu/garbled PDFs.
- The generator writes `04_mock_korean/ground_truth.csv` mapping each
  generated file's absolute path → `doc_type`, `business_purpose`. This is a
  **human reference for eyeballing only**; no script consumes it for
  automated scoring (per the approved decision to grade #4 by looking).

### Stream 1b — realistic junk / S3-bait (~40)

Persistent source folder: `doc_agent_test_data/05_mock_s3bait_junk/`.

A realistic messy-folder junk mix: `.log`, `.mp4` (tiny valid stub), `.zip`,
`.sql` / `.bak` (DB-dump-like), `.tar.gz`, password-locked `.pdf`,
zero-byte files, `.DS_Store`.

The `s3` short-circuit does **not** depend on file extension. It fires when
a router category is the catch-all `Misc` **or** when every file in that
category failed text extraction (`signals.all_extraction_failed`,
`fda/organize/router.py:_short_circuit`). The quarantine layer groups
unextractable files by extension into `_NoExtractor/<ext>/` and
`_ExtractionFailed/<ext>/` buckets; any such bucket whose files all failed
extraction is, by construction, an `all_extraction_failed` category — so it
trips the `s3` short-circuit. The junk pile only needs **enough genuinely
unreadable files of one extension to form one such bucket** to resolve #5.
The broader extension *variety* is a separate, user-chosen goal: exercising
the quarantine layer across multiple extensions. It is not what triggers
`s3`.

### Stream 2 — real files (~140)

- One-time staging (documented commands, not a script): unzip
  `~/Downloads/MYBOX.zip` (10 real Korean `.hwp`) and copy the real Korean
  `~/Downloads` docs (e.g. `조직도 및 연락처_라이온켐텍.xlsx`,
  `lion chemtech sas concept kr.docx`) into a new persistent source
  `doc_agent_test_data/06_real_korean/`.
- Pooled via `randomize_fda_fixture.py --source` from: `06_real_korean`
  (~12 staged files), `03_napierone/extracted`, a small slice of
  `02_company_documents` (English invoices — proves FDA does not lump all
  PDFs together), and `hwp_samples` (10 real Korean `.hwp`). Per-source
  counts are set with `--quota`. **A `--quota` must never exceed its
  source's available file count**; the staged/generated counts are the
  source of truth, so quotas are derived from them, not assumed. With
  `--count 300`, `napierone` is left un-quota'd and absorbs the remainder
  (~98), landing total real ≈ 140.

## Components

| Artifact | Type | Responsibility |
|---|---|---|
| `scripts/build_korean_test_sources.py` | new | Deterministic-seed generator. Builds `04_mock_korean/` (+ `ground_truth.csv`) and `05_mock_s3bait_junk/`. **Self-verifies**: each generated Korean `.pdf` is re-extracted and must contain its Korean text; failure is a hard error (non-zero exit), so a broken fixture cannot silently pass. |
| `doc_agent_test_data/06_real_korean/` | one-time setup | Staged real Korean: `MYBOX.zip` contents + `~/Downloads` Korean docs. Created by documented `unzip`/`cp` commands. |
| `scripts/randomize_fda_fixture.py` | existing, reused | Pools all sources (`--source NAME=PATH` ×N, `--quota NAME=N`) → `/private/tmp/fda-test-sets/randomized-bilingual-2026-05-16-001/` + `manifest.csv`. No code change. |
| `docs/superpowers/specs/2026-05-16-business-context.draft.ko.md` | new draft | Korean folder-granularity + doc-type rules. User edits it, then it is copied to `$FDA_HOME/business_context.md` (inside the throwaway sandbox) before the run. |
| `scripts/diag_organize.py` | existing, reused | The run harness: `<fixture> --apply` runs the full pipeline (route + metadata on by default) with the standard Korean organize instructions. No code change. |
| `scripts/evaluate_fda_fixture.py` | extend | Add: bucket-size histogram; explicit `S3 fired? yes/no`; `metadata.db` row count + Korean-search-returns-hits check (via a new `--fda-home` arg); Korean-label presence check on bucket subpaths / `routing-report.md`. Existing per-source landing and router-destination counts are kept. |
| eyeball-notes file | new, post-run | Filled after the run; records the written verdict on #4 and #5. |

## Exact commands (pinned, not hand-wavy)

This block shows the **post-implementation** invocation. Step 1
(`build_korean_test_sources.py`) and step 6's `--fda-home` flag, the
"S3 fired? yes/no" line, the `metadata.db` Korean-search check and the
Korean-label scan are **deliverables of this work**, not current behavior;
today `scripts/evaluate_fda_fixture.py` accepts only `fixture` /
`--no-write`. `randomize_fda_fixture.py` and `diag_organize.py` are reused
unchanged.

```bash
cd /Users/hogyeongkim/Desktop/Projects/FDA/FDA
DATA=/Users/hogyeongkim/Desktop/Projects/doc_agent_test_data
FIX=/private/tmp/fda-test-sets/randomized-bilingual-2026-05-16-001
HOME_TMP=$(mktemp -d /tmp/fda-test-home-XXXX)

# 1. Generate the two mock source folders (idempotent, fixed seed).
.venv/bin/python scripts/build_korean_test_sources.py \
  --korean-out "$DATA/04_mock_korean" \
  --junk-out   "$DATA/05_mock_s3bait_junk" \
  --seed bilingual-2026-05-16

# 2. One-time real-Korean staging.
mkdir -p "$DATA/06_real_korean"
unzip -o ~/Downloads/MYBOX.zip -d "$DATA/06_real_korean"
cp ~/Downloads/"조직도 및 연락처_라이온켐텍.xlsx" \
   ~/Downloads/"lion chemtech sas concept kr.docx" "$DATA/06_real_korean/"

# 2b. VERIFY real source counts before pooling. randomize_fda_fixture.py
#     silently caps an over-quota source (take = min(quota, available)) and
#     back-fills the shortfall from un-quota'd sources — so a too-high
#     real_korean quota would quietly swap real Korean for napierone noise
#     and fake the Korean-coverage result. Set each --quota from the number
#     printed here; never assume it.
for s in 04_mock_korean 05_mock_s3bait_junk 06_real_korean hwp_samples; do
  printf '%-22s %s files\n' "$s" \
    "$(find "$DATA/$s" -type f ! -name .DS_Store ! -name ground_truth.csv | wc -l | tr -d ' ')"
done

# 3. Pool into one shuffled, hash-renamed fixture + manifest.
#    Each --quota below MUST be <= the count printed by step 2b.
.venv/bin/python scripts/randomize_fda_fixture.py \
  --source mock_korean="$DATA/04_mock_korean" \
  --source junk="$DATA/05_mock_s3bait_junk" \
  --source real_korean="$DATA/06_real_korean" \
  --source napierone="$DATA/03_napierone/extracted" \
  --source invoices="$DATA/02_company_documents" \
  --source hwp="$DATA/hwp_samples" \
  --quota mock_korean=120 --quota junk=40 --quota real_korean=12 \
  --quota hwp=10 --quota invoices=20 \
  --output "$FIX" --count 300 --seed bilingual-2026-05-16
# (real_korean=12 ≈ all staged real Korean; adjust to the actual staged
#  count. napierone is un-quota'd and absorbs the remainder.)

# 4. Place the Korean business_context.md inside the sandbox FDA_HOME.
cp docs/superpowers/specs/2026-05-16-business-context.draft.ko.md \
   "$HOME_TMP/business_context.md"   # after user edits the draft

# 5. PREFLIGHT then one sandboxed pipeline run. diag_organize.py does NOT
#    set or validate FDA_HOME itself; without the env prefix organize()
#    reads the REAL ~/.fda/business_context.md and writes the real
#    metadata.db. Abort unless the sandbox is in force.
case "$HOME_TMP" in
  /tmp/fda-test-home-*|/private/tmp/fda-test-home-*) : ;;
  *) echo "ABORT: HOME_TMP not a sandbox dir: $HOME_TMP" >&2; exit 1 ;;
esac
[ -f "$HOME_TMP/business_context.md" ] || { echo "ABORT: no sandbox business_context.md" >&2; exit 1; }
FDA_HOME="$HOME_TMP" .venv/bin/python scripts/diag_organize.py "$FIX" --apply

# 6. Grade by looking.
.venv/bin/python scripts/evaluate_fda_fixture.py "$FIX" --fda-home "$HOME_TMP"
```

## Data flow

```
build_korean_test_sources.py ─► 04_mock_korean (+ground_truth.csv), 05_mock_s3bait_junk
MYBOX.zip + Downloads KR     ─► 06_real_korean
all sources ─► randomize_fda_fixture.py ─► $FIX/ (hash-named, folder_N) + manifest.csv
$FIX ─► diag_organize.py --apply (FDA_HOME=$HOME_TMP) ─► organized tree
                                                       + $FIX/routing-report.{json,md}
                                                       + $HOME_TMP/metadata.db
manifest.csv + routing-report.json + metadata.db ─► evaluate_fda_fixture.py ─► scorecard
scorecard + manual eyeball ─► written verdict on #4 and #5
```

## Grading (by looking — no automated cohesion scorer)

`evaluate_fda_fixture.py` prints a plain scorecard. The #4 over-fragmentation
judgement is made by a human reading the histogram, matching the runbook's
own method. The scorecard adds, on top of today's output:

- **Bucket-size histogram** — number of FDA-created folders and the file
  count of each, sorted (the #4 signal).
- **`S3 fired? yes/no`** — explicit one-liner derived from the router
  destination counts (the #5 verdict at a glance), alongside the existing
  per-destination counts.
- **Metadata check** — open `$FDA_HOME/metadata.db`, report classified-row
  count, and run one Korean FTS query (e.g. `계약`) that must return ≥1 hit.
- **Korean-label presence** — scan bucket subpaths and `routing-report.md`
  for Hangul; report yes/no.

The file-match join is unchanged: the evaluator already matches on
randomized filename with a SHA-256 fallback, so it is robust to FDA
renaming. `ground_truth.csv` is keyed by the generator's absolute source
path, which equals `manifest.csv`'s `original_path` for stream-1 files;
it is consulted by a human, not joined by the evaluator.

## State hygiene & safety

- `randomize_fda_fixture.py` copies, never moves. Real source folders are
  preserved byte-for-byte. The `04`/`05`/`06` mock folders are **persistent
  and reusable**; only the `/private/tmp` fixture is disposable.
- `FDA_HOME` points at a throwaway `mktemp -d` dir. `business_context.md`
  and `metadata.db` live there. The real `~/.fda` is never read or written.
  Because `diag_organize.py` neither sets nor validates `FDA_HOME`, the
  step-5 preflight (sandbox-path check + sandbox `business_context.md`
  presence) is mandatory — running without it silently falls back to the
  real `~/.fda`.
- `reportlab` is installed into `.venv` only — **not** added to
  `pyproject.toml` (it is test-fixture tooling, not product code).
- Git repositories are never modified by the organize tools (existing
  guarantee).
- A fixture is single-use: once FDA has run on `$FIX`, generate a fresh
  numbered fixture for any re-test (runbook rule).

## Testing approach

- After changing `scripts/evaluate_fda_fixture.py`, the full suite must
  pass: `.venv/bin/python -m pytest tests/ -x -q --tb=short`.
- TDD on the one piece of new logic with real bug-risk: the
  `metadata.db` Korean-search check and the bucket-size histogram get
  focused unit tests.
- The generator gets one smoke test proving its self-verification **fails
  loudly**: feed it a deliberately broken (non-Korean) PDF path and assert a
  non-zero exit / raised error. A generator that can silently emit
  unreadable Korean PDFs would invalidate goal #4.

## Risks

- **Korean PDF extraction.** Even an embedded TTF may not extract cleanly
  through FDA's PDF path. Mitigation: the generator's self-verify gate hard-
  fails; if it cannot produce extractable Korean PDFs, we drop `.pdf` from
  stream 1 and stand on `.docx`/`.txt` (still a valid Korean test).
- **Korean font availability.** The generator's font resolution order
  (`--korean-font` → vendored `tests/assets/NanumGothic.ttf` → documented
  macOS fallback) must end in a hard, message-bearing failure if no
  single-face `.ttf` is usable — never tofu/garbled PDFs. Vendoring
  `NanumGothic.ttf` makes the test reproducible off this machine and is the
  preferred default.
- **`s3` still does not fire** even with a guaranteed all-unreadable bucket
  — *and* the verdict precondition holds (`routing-report.json` present, no
  `router stage failed` in the log). Only then is it the definitive #5
  finding (branch is dead → simplify the router to two destinations in
  follow-up work). If the precondition fails, the run is inconclusive, not a
  verdict.

## Out of scope

- Generating `.hwpx` (cut — redundant given real `.hwp`).
- Any automated cluster-cohesion / togetherness scoring (cut — #4 graded by
  eye per the approved decision).
- Fixing #4 or #5 in this pass — this run produces *evidence*; fixes are
  separate work driven by the verdict.
- Changes to `randomize_fda_fixture.py` or `diag_organize.py` (reused as-is).
