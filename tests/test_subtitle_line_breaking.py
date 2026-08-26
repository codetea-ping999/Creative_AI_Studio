"""Tests for the minimum Japanese subtitle line-breaking rules (#240 / #60).

Each acceptance criterion from the issue gets its own group of tests:

- "Japanese punctuation does not begin a line in the covered minimum-rule
  set" -> ``LineStartProhibitionTests``.
- Line-end prohibition (opening brackets), the other half of the "minimum
  prohibited line-start/line-end punctuation rules" task -> ``LineEndProhibitionTests``.
- "Wrapping is deterministic for the same text/measure" -> ``DeterminismTests``.
- "Explicit valid line breaks are preserved" -> ``ExplicitLineBreakTests``.
- "Mixed-language text does not crash or overflow unboundedly" -> ``MixedLanguageAndOverflowTests``.
- "Tests document the intentionally limited kinsoku scope" -> ``LimitedScopeTests``.

``KinsokuFixupUnitTests`` exercises ``apply_kinsoku_rules`` directly on
hand-built line lists, independent of the wrap step, so the pull/push/cascade
behaviour is pinned down without depending on where ``_wrap_cjk`` happens to
place a cut.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generators.video.subtitle_line_breaking import (  # noqa: E402
    LINE_END_PROHIBITED,
    LINE_START_PROHIBITED,
    apply_kinsoku_rules,
    break_subtitle_lines,
    is_line_end_prohibited,
    is_line_start_prohibited,
)


def _no_line_starts_prohibited(lines: list[str]) -> bool:
    return all(line and line[0] not in LINE_START_PROHIBITED for line in lines)


def _no_line_ends_prohibited(lines: list[str]) -> bool:
    return all(line and line[-1] not in LINE_END_PROHIBITED for line in lines)


class LineStartProhibitionTests(unittest.TestCase):
    """行頭禁則: Japanese punctuation never opens a line."""

    def test_small_kana_pulled_back_across_a_hard_cut(self) -> None:
        # 20 plain characters exhaust the default CJK budget with no soft
        # break recorded, forcing a hard cut; the next character is a small
        # kana, which is line-start prohibited.
        text = "あ" * 20 + "ょろん"
        lines = break_subtitle_lines(text)

        self.assertTrue(_no_line_starts_prohibited(lines))
        # The kana moved onto the previous line rather than being dropped.
        self.assertEqual(lines, ["あ" * 20 + "ょ", "ろん"])

    def test_closing_bracket_never_opens_a_line(self) -> None:
        text = "本文はここまで」続きは次のシーンで。"
        lines = break_subtitle_lines(text, max_chars=8)

        self.assertTrue(_no_line_starts_prohibited(lines))

    def test_full_line_start_prohibited_set_never_appears_at_a_line_start(self) -> None:
        # A representative sample from every category in LINE_START_PROHIBITED,
        # each forced right after a hard-cut boundary.
        for character in "、。」』ぁっーー！？：":
            with self.subTest(character=character):
                text = "字" * 20 + character + "続き"
                lines = break_subtitle_lines(text)
                self.assertTrue(
                    _no_line_starts_prohibited(lines),
                    f"{character!r} started a line in {lines!r}",
                )


class LineEndProhibitionTests(unittest.TestCase):
    """行末禁則: opening brackets never close a line."""

    def test_opening_bracket_pushed_forward_across_a_hard_cut(self) -> None:
        # 19 plain characters plus an opening bracket lands the bracket
        # exactly on the hard-cut boundary.
        text = "あ" * 19 + "「" + "ろんぶん"
        lines = break_subtitle_lines(text)

        self.assertTrue(_no_line_ends_prohibited(lines))
        self.assertEqual(lines, ["あ" * 19, "「ろんぶん"])

    def test_full_line_end_prohibited_set_never_appears_at_a_line_end(self) -> None:
        for character in "「『（【〈（":
            with self.subTest(character=character):
                text = "字" * 19 + character + "続きの文章です"
                lines = break_subtitle_lines(text)
                self.assertTrue(
                    _no_line_ends_prohibited(lines),
                    f"{character!r} ended a line in {lines!r}",
                )


class DeterminismTests(unittest.TestCase):
    """Wrapping the same text/measure twice gives identical output."""

    def test_repeated_calls_agree(self) -> None:
        text = "吾輩は猫である。名前はまだ無い。どこで生まれたかとんと見当がつかぬ。"
        first = break_subtitle_lines(text, max_chars=10)
        second = break_subtitle_lines(text, max_chars=10)
        self.assertEqual(first, second)

    def test_mixed_and_latin_text_also_deterministic(self) -> None:
        text = "Ship it before Friday, please -- the demo cannot slip again."
        first = break_subtitle_lines(text, max_chars=15)
        second = break_subtitle_lines(text, max_chars=15)
        self.assertEqual(first, second)


class ExplicitLineBreakTests(unittest.TestCase):
    """User-authored ``\\n`` breaks are kept as line boundaries."""

    def test_valid_explicit_break_is_preserved_as_is(self) -> None:
        # Both halves fit comfortably under a generous budget, so a caller
        # that merges Japanese without an explicit break would collapse this
        # to one line; the explicit break must survive instead.
        lines = break_subtitle_lines("おはよう\nございます", max_chars=100)
        self.assertEqual(lines, ["おはよう", "ございます"])

    def test_explicit_break_before_prohibited_start_is_adjusted_not_dropped(self) -> None:
        # The user's break lands right before a comma. It is still honoured
        # as a two-line structure -- the comma is pulled across the boundary
        # instead of the break being discarded and the lines rejoined.
        lines = break_subtitle_lines("赤\n、青", max_chars=100)

        self.assertEqual(len(lines), 2)
        self.assertTrue(_no_line_starts_prohibited(lines))
        # No character was lost or reordered, only moved across the boundary.
        self.assertEqual("".join(lines), "赤、青")

    def test_explicit_break_preserved_for_latin_text(self) -> None:
        lines = break_subtitle_lines("first line\nsecond line", max_chars=100)
        self.assertEqual(lines, ["first line", "second line"])

    def test_blank_explicit_segments_do_not_produce_empty_lines(self) -> None:
        lines = break_subtitle_lines("one\n\ntwo", max_chars=100)
        self.assertEqual(lines, ["one", "two"])


class MixedLanguageAndOverflowTests(unittest.TestCase):
    """Mixed Japanese/Latin input, and long unbroken tokens, never crash or overflow."""

    def test_long_url_like_token_is_hard_broken(self) -> None:
        token = "https://example.com/" + "a" * 200
        lines = break_subtitle_lines(f"See {token} for details", max_chars=42)

        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), 42)

    def test_mixed_japanese_and_url_does_not_crash_or_overflow(self) -> None:
        text = "詳細は https://example.com/" + "x" * 100 + " をご覧ください。"
        lines = break_subtitle_lines(text, max_chars=20)

        self.assertTrue(lines)
        for line in lines:
            # Character-wise CJK wrapping bounds every line to the measure
            # plus at most one kinsoku-pulled character.
            self.assertLessEqual(len(line), 21)

    def test_empty_and_whitespace_only_text_returns_no_lines(self) -> None:
        self.assertEqual(break_subtitle_lines(""), [])
        self.assertEqual(break_subtitle_lines("   \n\n  "), [])

    def test_non_positive_max_chars_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            break_subtitle_lines("text", max_chars=0)
        with self.assertRaises(ValueError):
            break_subtitle_lines("text", max_chars=-5)


class LimitedScopeTests(unittest.TestCase):
    """Document what the minimum rule set intentionally does not cover.

    Non-goal, per the module docstring: a line's own leading/trailing
    character is only ever adjusted when it is the *product of a wrap
    point*. Text that itself opens with a prohibited character, with no
    earlier line to move it onto, is left as authored -- this is not a full
    kinsoku shori (typesetting) engine, only the minimum wrap-time rule set.
    """

    def test_text_authored_with_a_leading_prohibited_character_is_unchanged(self) -> None:
        # A leading "。" is a sentence break, so the wrap step itself splits
        # it into its own first line before kinsoku correction ever runs.
        # That first line has no *earlier* line to pull the "。" onto, so it
        # is intentionally left exactly as the caller wrote it -- the same
        # scope limit as ``KinsokuFixupUnitTests
        # .test_single_line_is_never_adjusted_against_itself``, just reached
        # through the wrap step instead of being fed in directly.
        text = "。これは注釈です"
        lines = break_subtitle_lines(text, max_chars=100)

        self.assertEqual(lines, ["。", "これは注釈です"])
        self.assertTrue(lines[0].startswith("。"))

    def test_wraps_per_character_not_by_morphological_word_boundary(self) -> None:
        # "分かち書き" (word segmentation) is not attempted: CJK text wraps
        # per character like the rest of this codebase, so a cut can land
        # inside what a human would consider one word.
        text = "あいうえおかきくけこさしすせそ"
        lines = break_subtitle_lines(text, max_chars=5)
        self.assertEqual(lines, ["あいうえお", "かきくけこ", "さしすせそ"])


class KinsokuFixupUnitTests(unittest.TestCase):
    """Exercise ``apply_kinsoku_rules`` directly against hand-built line lists."""

    def test_helper_predicates_match_the_membership_sets(self) -> None:
        self.assertTrue(is_line_start_prohibited("」"))
        self.assertFalse(is_line_start_prohibited("あ"))
        self.assertTrue(is_line_end_prohibited("「"))
        self.assertFalse(is_line_end_prohibited("あ"))

    def test_no_change_when_already_valid(self) -> None:
        lines = ["これはいい感じの", "行分割です。"]
        self.assertEqual(apply_kinsoku_rules(lines), lines)

    def test_multiple_prohibited_start_characters_all_pulled_back(self) -> None:
        # A run of two prohibited-start characters ("」" then "、") both need
        # to move, and the second line is fully absorbed and dropped.
        result = apply_kinsoku_rules(["abc", "」、", "def"])
        self.assertEqual(result, ["abc」、", "def"])

    def test_prohibited_end_character_pushed_to_the_immediate_neighbour_only(self) -> None:
        result = apply_kinsoku_rules(["x「", "y", "z"])
        self.assertEqual(result, ["x", "「y", "z"])

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(apply_kinsoku_rules([]), [])

    def test_single_line_is_never_adjusted_against_itself(self) -> None:
        # There is no neighbouring line, so a lone prohibited-start character
        # is left in place -- this is the same limited-scope behaviour
        # documented in ``LimitedScopeTests``.
        self.assertEqual(apply_kinsoku_rules(["」starts here"]), ["」starts here"])


if __name__ == "__main__":
    unittest.main()
