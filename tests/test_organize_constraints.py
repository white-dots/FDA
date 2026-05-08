# tests/test_organize_constraints.py
"""Lint-style tests enforcing the spec's 'must NOT' rules."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ORGANIZE_DIR = Path(__file__).resolve().parent.parent / "fda" / "organize"


def _py_files() -> list[Path]:
    return [p for p in ORGANIZE_DIR.rglob("*.py") if "__pycache__" not in p.parts]


class TestNoHardcodedModelNames:
    @pytest.mark.parametrize("pattern", [
        r"claude-sonnet",
        r"claude-haiku",
        r"claude-opus",
    ])
    def test_no_hardcoded_model_id_in_organize(self, pattern):
        rx = re.compile(pattern)
        for path in _py_files():
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    pytest.fail(
                        f"hardcoded model id {pattern!r} found at "
                        f"{path.relative_to(ORGANIZE_DIR.parent.parent)}:{line_no} — "
                        "model ids must come from SKILL.md frontmatter."
                    )


class TestNoBinaryDenylistInReader:
    def test_no_BINARY_EXTS(self):
        reader = (ORGANIZE_DIR / "reader.py").read_text(encoding="utf-8")
        assert "_BINARY_EXTS" not in reader, (
            "reader.py must not maintain a binary-extension denylist; "
            "extension dispatch lives in _extractors.EXTRACTORS only."
        )


class TestEachConstantHasOneHome:
    """For each named tunable, the literal value appears at most once across
    the package (in the module that owns it)."""

    CONSTS = {
        # constant -> (owning module relative path, literal source as in code)
        "READER_TEXT_CAP_BYTES": ("reader.py", "64 * 1024"),
        "READER_PER_FILE_TIMEOUT_SECONDS": ("reader.py", "30"),
        "READER_TOTAL_TIMEOUT_SECONDS": ("reader.py", "300"),
        "READER_WORKER_COUNT": ("reader.py", "8"),
        "VERBATIM_HEAD_CHARS": ("reader.py", "300"),
        "MAX_SECTIONS_PER_FILE": ("_sections.py", "15"),
        "SECTION_HEADER_MAX_CHARS": ("_sections.py", "40"),
        "SECTION_SCAN_CHARS": ("_sections.py", "16 * 1024"),
        "TAXONOMY_SAMPLE_SHAPE_BUDGET": ("classifier.py", "10"),
        "TAXONOMY_SAMPLE_FULL_THRESHOLD": ("classifier.py", "150"),
        "TAXONOMY_SAMPLE_TARGET_SIZE": ("classifier.py", "100"),
        "TAXONOMY_SAMPLE_FALLBACK_BUDGET": ("classifier.py", "100"),
        "ASSIGNER_BATCH_TARGET_TOKENS": ("classifier.py", "45_000"),
        "MAX_ASSIGNER_INPUT_TOKENS": ("classifier.py", "50_000"),
        "MAX_CLASSIFIER_CONCURRENCY": ("classifier.py", "4"),
        "MAX_FALLBACK_RATE": ("classifier.py", "0.20"),
        "MIN_FALLBACK_REFINE_COUNT": ("classifier.py", "25"),
        "MAX_TAXONOMY_REFINEMENTS": ("classifier.py", "1"),
        "ASSIGNER_BAD_RESPONSE_FALLBACK_RATE": ("classifier.py", "0.02"),
        "ASSIGNER_BAD_RESPONSE_FALLBACK_MAX": ("classifier.py", "10"),
        "CLASSIFIER_INPUT_TOKEN_BUDGET": ("classifier.py", "60_000"),
        "READER_FAILED_SUMMARY_THRESHOLD": ("classifier.py", "0.25"),
        "SUMMARY_TRUNCATE_CHARS": ("classifier.py", "200"),
        "SECTION_HEADER_MIN_CHARS": ("_sections.py", "3"),
        "KOREAN_LABEL_MIN_CHARS": ("_sections.py", "2"),
        "HANGUL_RANGE": ("_sections.py", '"가-힣"'),
        "_XLSX_TEXT_ROWS_PER_SHEET": ("_extractors.py", "20"),
        "_XLSX_TEXT_COLS_PER_ROW": ("_extractors.py", "32"),
        "_XLSX_FORMULA_DENSITY_THRESHOLD": ("_extractors.py", "0.05"),
        "_XLSX_MERGED_CELLS_MIN": ("_extractors.py", "3"),
        "_PPTX_SLIDES_MAX": ("_extractors.py", "100"),
        "_PPTX_SHAPES_PER_SLIDE_MAX": ("_extractors.py", "50"),
        "_PPTX_NOTES_CHARS_PER_SLIDE_MAX": ("_extractors.py", "2000"),
        "_CSV_READ_BYTES_MAX": ("_extractors.py", "4 * 1024 * 1024"),
        "_CSV_SNIFF_SAMPLE_CHARS": ("_extractors.py", "8 * 1024"),
        "_CSV_HEADER_SCAN_ROWS": ("_extractors.py", "5"),
        "_CSV_TEXT_ROWS_MAX": ("_extractors.py", "20"),
        "_CSV_TEXT_COLS_PER_ROW": ("_extractors.py", "32"),
        "_CSV_NO_HEADER_LABEL": ("_extractors.py", '"NoHeader"'),
        "_CSV_KOREAN_LABEL_MIN_CHARS": ("_extractors.py", "2"),
    }

    def test_constant_defined_in_owning_module(self):
        for name, (rel, _literal) in self.CONSTS.items():
            owner = ORGANIZE_DIR / rel
            text = owner.read_text(encoding="utf-8")
            assert re.search(rf"^{re.escape(name)}\s*=", text, re.MULTILINE), (
                f"{name} missing from {rel}"
            )

    # Distinctive literals (unlikely to appear by coincidence elsewhere) must
    # appear ONLY in the owning module. Generic small numbers like 4, 100,
    # 0.20, 0.25 are excluded — they'd produce too many false positives.
    #
    # SPEC DEVIATION (Chunk G, Option A): the spec also listed "64 * 1024":
    # "reader.py", but `_extractors._PDF_PIPE_CAP_BYTES = 64 * 1024` legitimately
    # uses the same literal for a semantically distinct cap (PDF subprocess pipe
    # vs. extracted-text size). The two cannot share a constant without a
    # circular import (reader -> _extractors). Dropped from this dict.
    DISTINCTIVE_LITERALS = {
        "45_000": "classifier.py",
        "50_000": "classifier.py",
        "60_000": "classifier.py",
    }

    def test_distinctive_literals_have_one_home(self):
        for literal, owner_rel in self.DISTINCTIVE_LITERALS.items():
            owner = ORGANIZE_DIR / owner_rel
            for path in _py_files():
                if path.samefile(owner):
                    continue
                text = path.read_text(encoding="utf-8")
                assert literal not in text, (
                    f"distinctive literal {literal!r} appears in "
                    f"{path.relative_to(ORGANIZE_DIR.parent.parent)} as well as "
                    f"{owner_rel} — replace it with the named constant import."
                )


class TestPlanBuilderDoesNotImportCatalog:
    def test_no_Catalog_import(self):
        text = (ORGANIZE_DIR / "plan_builder.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module \
               and node.module.endswith("models"):
                names = {a.name for a in node.names}
                assert "Catalog" not in names, "plan_builder must not import Catalog"
                assert "CatalogEntry" not in names, "plan_builder must not import CatalogEntry"


class TestSkillContents:
    def test_assigner_emits_path_ids(self):
        skill = (
            ORGANIZE_DIR / "skills" / "taxonomy-assigner" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "path_id" in skill
        assert "EXACTLY ONCE" in skill or "exactly once" in skill

    def test_proposer_does_not_reference_path_ids_for_assignments(self):
        skill = (
            ORGANIZE_DIR / "skills" / "taxonomy-proposer" / "SKILL.md"
        ).read_text(encoding="utf-8")
        # Mentioning the field name to declare the input shape is fine; the
        # rule is that the proposer must not be asked to produce assignments.
        assert "Do NOT emit any `path_id` references" in skill or \
            "DO NOT emit" in skill


class TestClassifierPublicSurface:
    def test_only_classify_is_public(self):
        text = (ORGANIZE_DIR / "classifier.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        publics = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")
        ]
        assert publics == ["classify"], (
            f"classifier.py public functions must be exactly ['classify']; got {publics}"
        )
