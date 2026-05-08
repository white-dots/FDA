"""Tests for fda.organize._sections.extract_sections_from_text."""

import pytest


class TestExtractSectionsFromText:
    def test_colon_only_on_line_headers(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Some preamble.\n"
            "\n"
            "Shipping Details:\n"
            "  ACME Corp\n"
            "\n"
            "Customer Details:\n"
            "  Hanna Moos\n"
        )
        assert extract_sections_from_text(text) == (
            "Shipping Details",
            "Customer Details",
        )

    def test_labeled_field_with_value_on_same_line_does_not_match(self):
        """Lines like 'Order ID: 10488' have content after the colon and
        deliberately don't match — that's how we distinguish section
        dividers from labeled fields."""
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Order ID: 10488\n"
            "Order Date: 2024-03-15\n"
            "\n"
            "Products:\n"
        )
        assert extract_sections_from_text(text) == ("Products",)

    def test_allcaps_short_lines_are_title_cased(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "INVOICE\n"
            "\n"
            "Some content here.\n"
            "\n"
            "TOTAL DUE\n"
        )
        assert extract_sections_from_text(text) == ("Invoice", "Total Due")

    def test_source_order_preserved_across_patterns(self):
        """ALL-CAPS line precedes colon-headers — pins the single-pass
        algorithm. A two-pass implementation would emit colon-headers
        first and break this test."""
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Some intro paragraph\n"
            "\n"
            "INVOICE\n"
            "\n"
            "Bill To:\n"
            "Acme Corp\n"
            "\n"
            "Ship To:\n"
            "Customer Address\n"
        )
        assert extract_sections_from_text(text) == (
            "Invoice",
            "Bill To",
            "Ship To",
        )

    def test_duplicates_deduplicated_in_first_seen_order(self):
        from fda.organize._sections import extract_sections_from_text

        text = (
            "Shipping Details:\n"
            "...\n"
            "Customer Details:\n"
            "...\n"
            "Shipping Details:\n"
            "...\n"
        )
        assert extract_sections_from_text(text) == (
            "Shipping Details",
            "Customer Details",
        )

    def test_capped_at_max_sections_per_file(self):
        from fda.organize._sections import (
            MAX_SECTIONS_PER_FILE,
            extract_sections_from_text,
        )

        # 30 distinct colon-headers — well past the cap of 15.
        lines = [f"Section {i}:\n  body\n" for i in range(30)]
        result = extract_sections_from_text("\n".join(lines))
        assert len(result) == MAX_SECTIONS_PER_FILE
        # First MAX_SECTIONS_PER_FILE in source order.
        assert result[0] == "Section 0"
        assert result[-1] == f"Section {MAX_SECTIONS_PER_FILE - 1}"

    def test_empty_input_returns_empty_tuple(self):
        from fda.organize._sections import extract_sections_from_text

        assert extract_sections_from_text("") == ()
        assert extract_sections_from_text("   \n  \t\n") == ()

    def test_no_headers_returns_empty_tuple(self):
        from fda.organize._sections import extract_sections_from_text

        text = "just a sentence with a colon: see?\nno headers anywhere here.\n"
        assert extract_sections_from_text(text) == ()

    def test_too_short_label_rejected(self):
        from fda.organize._sections import extract_sections_from_text

        # "Ab" is shorter than SECTION_HEADER_MIN_CHARS (3), so the line
        # "Ab:" is rejected even though the regex matches.
        # (The regex's own minimum is 3 chars including the leading capital,
        # so "Ab:" doesn't match the regex anyway — this test pins the
        # SECTION_HEADER_MIN_CHARS guard as a defense in depth.)
        text = "Ab:\n\nLonger Header:\n"
        assert extract_sections_from_text(text) == ("Longer Header",)

    def test_too_long_label_rejected(self):
        from fda.organize._sections import (
            SECTION_HEADER_MAX_CHARS,
            extract_sections_from_text,
        )

        long = "A" + "b" * (SECTION_HEADER_MAX_CHARS + 5)
        text = f"{long}:\n\nShort Header:\n"
        # Long label is rejected by the SECTION_HEADER_MAX_CHARS guard.
        assert extract_sections_from_text(text) == ("Short Header",)

    def test_scan_chars_cap_bounds_work(self):
        from fda.organize._sections import (
            SECTION_SCAN_CHARS,
            extract_sections_from_text,
        )

        # Header inside the scan window, decoy header past the cap.
        head = "Real Header:\n" + ("x\n" * 10)
        padding = "y\n" * (SECTION_SCAN_CHARS + 1024)
        decoy = "Decoy Header:\n"
        result = extract_sections_from_text(head + padding + decoy)
        assert "Real Header" in result
        assert "Decoy Header" not in result


# ---------------------------------------------------------------------------
# contains_hangul — public helper for cross-module per-script logic
# ---------------------------------------------------------------------------


class TestContainsHangul:
    def test_pure_hangul_returns_true(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("이름") is True

    def test_mixed_ascii_and_hangul_returns_true(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("KPI 지표") is True

    def test_pure_ascii_returns_false(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("name") is False

    def test_empty_string_returns_false(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("") is False

    def test_punctuation_only_returns_false(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("[Q&A]") is False

    def test_full_width_roman_returns_false(self):
        """Full-width Roman characters are not Hangul. Pins the precise
        Hangul-only intent of HANGUL_RANGE = U+AC00..U+D7A3."""
        from fda.organize._sections import contains_hangul

        assert contains_hangul("ＫＰＩ") is False

    def test_hanja_returns_false(self):
        """CJK Hanja excluded per v1 Non-goals."""
        from fda.organize._sections import contains_hangul

        assert contains_hangul("漢字") is False

    def test_full_width_roman_plus_hangul_returns_true(self):
        from fda.organize._sections import contains_hangul

        assert contains_hangul("ＫＰＩ 지표") is True
