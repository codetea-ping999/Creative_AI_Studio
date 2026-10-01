"""Tests for CJK font fallback on procedural storyboard frames (#449)."""

from __future__ import annotations

import random
import unittest
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from generators.video import fonts as fonts_module
from generators.video import runtime as runtime_module
from generators.video.fonts import (
    STORYBOARD_FONT_ENV,
    font_covers_text,
    font_for_text,
    resolve_cjk_font_path,
)

_AVAILABLE_CJK_FONT = resolve_cjk_font_path()


def _render_glyph(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, char: str) -> bytes:
    image = Image.new("L", (48, 48), 0)
    ImageDraw.Draw(image).text((4, 4), char, fill=255, font=font)
    return image.tobytes()


def _render_frame(prompt: str, negative_prompt: str | None = None) -> Image.Image:
    return runtime_module.ProceduralStoryboardRuntime()._render_frame(
        index=0,
        total_frames=12,
        width=576,
        height=320,
        prompt=prompt,
        negative_prompt=negative_prompt,
        palette=[],
        camera_motion="push-in",
        visual_style="storyboard",
        rng=random.Random(7),
    )


class ResolveCjkFontPathTests(unittest.TestCase):
    def test_env_override_wins_when_the_file_exists(self) -> None:
        resolved = resolve_cjk_font_path(
            env={STORYBOARD_FONT_ENV: "/custom/font.ttc"},
            candidates=("/system/cjk.ttc",),
            is_file=lambda path: path in {"/custom/font.ttc", "/system/cjk.ttc"},
        )
        self.assertEqual(resolved, "/custom/font.ttc")

    def test_missing_override_falls_back_to_first_existing_candidate(self) -> None:
        resolved = resolve_cjk_font_path(
            env={STORYBOARD_FONT_ENV: "/missing/font.ttc"},
            candidates=("/absent.ttc", "/second.ttc", "/third.ttc"),
            is_file=lambda path: path in {"/second.ttc", "/third.ttc"},
        )
        self.assertEqual(resolved, "/second.ttc")

    def test_returns_none_when_nothing_is_found(self) -> None:
        resolved = resolve_cjk_font_path(
            env={},
            candidates=("/absent-a.ttc", "/absent-b.ttc"),
            is_file=lambda path: False,
        )
        self.assertIsNone(resolved)


class FontForTextTests(unittest.TestCase):
    def test_latin_text_keeps_the_primary_font(self) -> None:
        primary = ImageFont.load_default()
        with mock.patch.object(fonts_module, "_load_truetype") as load:
            chosen = font_for_text("SHOT 01 STORYBOARD", primary, 12, fallback_path="/x.ttc")
        self.assertIs(chosen, primary)
        load.assert_not_called()

    def test_default_font_does_not_cover_japanese(self) -> None:
        self.assertFalse(font_covers_text(ImageFont.load_default(), "夕暮れ"))

    def test_no_cjk_font_found_keeps_the_primary_font(self) -> None:
        primary = ImageFont.load_default()
        with mock.patch.object(fonts_module, "_default_cjk_font_path", return_value=None):
            self.assertIs(font_for_text("夕暮れの海辺", primary, 12), primary)

    def test_unloadable_fallback_keeps_the_primary_font(self) -> None:
        primary = ImageFont.load_default()
        chosen = font_for_text(
            "夕暮れの海辺", primary, 12, fallback_path="/definitely/not/a/font.ttc"
        )
        self.assertIs(chosen, primary)

    def test_frame_renders_without_any_cjk_font(self) -> None:
        with mock.patch.object(fonts_module, "_default_cjk_font_path", return_value=None):
            frame = _render_frame("夕暮れの海辺を歩く少女", "ぼやけた画像")
        self.assertEqual(frame.size, (576, 320))

    def test_latin_frame_is_unchanged_by_fallback_availability(self) -> None:
        prompt = "a lighthouse at dusk, slow waves, cinematic"
        with mock.patch.object(fonts_module, "_default_cjk_font_path", return_value=None):
            without = _render_frame(prompt, "blurry").tobytes()
        with_fallback = _render_frame(prompt, "blurry").tobytes()
        self.assertEqual(without, with_fallback)


@unittest.skipIf(_AVAILABLE_CJK_FONT is None, "no CJK-capable system font installed")
class CjkGlyphRenderingTests(unittest.TestCase):
    def test_distinct_cjk_characters_render_distinct_glyphs(self) -> None:
        # Tofu glyphs are identical for every missing character, so two
        # different kanji rendering differently proves real glyphs are drawn.
        chosen = font_for_text("海空", ImageFont.load_default(), 12)
        self.assertNotEqual(_render_glyph(chosen, "海"), _render_glyph(chosen, "空"))

    def test_default_font_renders_cjk_as_identical_tofu(self) -> None:
        primary = ImageFont.load_default()
        self.assertEqual(_render_glyph(primary, "海"), _render_glyph(primary, "空"))

    def test_japanese_frames_are_deterministic_and_prompt_dependent(self) -> None:
        first = _render_frame("夕暮れの海辺").tobytes()
        again = _render_frame("夕暮れの海辺").tobytes()
        other = _render_frame("夕暮れの山道").tobytes()
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)


class ColumnWrapTests(unittest.TestCase):
    def test_latin_wrap_matches_textwrap(self) -> None:
        import textwrap

        text = "a lighthouse at dusk with slow rolling waves and a cinematic sky"
        self.assertEqual(runtime_module._wrap_columns(text, 34), textwrap.wrap(text, width=34))

    def test_cjk_text_wraps_by_display_columns(self) -> None:
        lines = runtime_module._wrap_columns("あ" * 40, 34)
        self.assertEqual(lines, ["あ" * 17, "あ" * 17, "あ" * 6])

    def test_cjk_wrap_keeps_closing_punctuation_off_line_start(self) -> None:
        lines = runtime_module._wrap_columns("あ" * 17 + "。続き", 34)
        self.assertEqual(lines, ["あ" * 17 + "。", "続き"])

    def test_cjk_wrap_keeps_opening_bracket_off_line_end(self) -> None:
        lines = runtime_module._wrap_columns("あ" * 16 + "「続き」", 34)
        self.assertEqual(lines, ["あ" * 16, "「続き」"])

    def test_cjk_shorten_truncates_with_placeholder(self) -> None:
        shortened = runtime_module._shorten_columns("avoid: " + "ぼ" * 40, 54)
        self.assertTrue(shortened.endswith("..."))
        self.assertLessEqual(runtime_module._display_columns(shortened), 54)
        self.assertTrue(shortened.startswith("avoid: ぼ"))


if __name__ == "__main__":
    unittest.main()
