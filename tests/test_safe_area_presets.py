"""Tests for the 9:16 / 16:9 / 1:1 safe-area presets (issue #239 / #60).

Each acceptance criterion from the issue gets its own test group:

- "All three aspect-ratio presets exist as inspectable data" -> ``PresetCatalogTests``.
- "Insets remain valid when output resolution changes within the same aspect
  ratio" -> ``ResolutionScalingTests``.
- "No safe region extends outside the frame" -> ``FrameBoundsTests``.
- "Presets have stable names/IDs and tests" -> ``PresetCatalogTests`` (ids) plus
  this whole file (tests).

``ValidationTests`` additionally exercises the schema itself (not just the three
shipped presets), since a preset catalog is only as trustworthy as the
validation that would catch a bad entry added later.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generators.video.safe_area import (  # noqa: E402
    HORIZONTAL_16_9,
    SAFE_AREA_PRESETS,
    SQUARE_1_1,
    VERTICAL_9_16,
    SafeAreaInsets,
    SafeAreaPreset,
    SafeAreaRegion,
    get_safe_area_preset,
    resolve_preset_for_resolution,
)


class PresetCatalogTests(unittest.TestCase):
    """All three presets exist, are inspectable, and have stable ids."""

    def test_all_three_aspect_ratios_present(self) -> None:
        by_ratio = {preset.aspect_ratio: preset for preset in SAFE_AREA_PRESETS.values()}
        self.assertEqual(set(by_ratio), {"9:16", "16:9", "1:1"})

    def test_preset_ids_are_stable_literals(self) -> None:
        # These strings are the contract: anything downstream (subtitle
        # rendering in #241, a preview overlay in #242) will reference a
        # preset by this id, so a rename here is a breaking change.
        self.assertEqual(VERTICAL_9_16.preset_id, "vertical_9x16")
        self.assertEqual(HORIZONTAL_16_9.preset_id, "horizontal_16x9")
        self.assertEqual(SQUARE_1_1.preset_id, "square_1x1")

    def test_catalog_keyed_by_own_preset_id(self) -> None:
        for preset_id, preset in SAFE_AREA_PRESETS.items():
            self.assertEqual(preset_id, preset.preset_id)
        self.assertEqual(len(SAFE_AREA_PRESETS), 3)

    def test_orientation_matches_aspect_ratio(self) -> None:
        self.assertEqual(VERTICAL_9_16.orientation, "vertical")
        self.assertEqual(HORIZONTAL_16_9.orientation, "horizontal")
        self.assertEqual(SQUARE_1_1.orientation, "square")

    def test_get_safe_area_preset_looks_up_by_id(self) -> None:
        self.assertIs(get_safe_area_preset("vertical_9x16"), VERTICAL_9_16)
        self.assertIs(get_safe_area_preset("horizontal_16x9"), HORIZONTAL_16_9)
        self.assertIs(get_safe_area_preset("square_1x1"), SQUARE_1_1)

    def test_get_safe_area_preset_names_the_unknown_id(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            get_safe_area_preset("vertical_4x5")
        message = str(ctx.exception)
        self.assertIn("vertical_4x5", message)
        # The error should help a caller self-correct, not just say "invalid".
        self.assertIn("vertical_9x16", message)

    def test_ratio_property_matches_declared_aspect_ratio(self) -> None:
        self.assertAlmostEqual(VERTICAL_9_16.ratio, 9 / 16)
        self.assertAlmostEqual(HORIZONTAL_16_9.ratio, 16 / 9)
        self.assertAlmostEqual(SQUARE_1_1.ratio, 1.0)


class FrameBoundsTests(unittest.TestCase):
    """No content_safe/subtitle_safe region may extend outside the frame."""

    def test_content_and_subtitle_regions_stay_within_unit_frame(self) -> None:
        for preset in SAFE_AREA_PRESETS.values():
            for region in (preset.content_safe.region(), preset.subtitle_safe.region()):
                self.assertGreaterEqual(region.x, 0.0, preset.preset_id)
                self.assertGreaterEqual(region.y, 0.0, preset.preset_id)
                self.assertLessEqual(region.x + region.width, 1.0, preset.preset_id)
                self.assertLessEqual(region.y + region.height, 1.0, preset.preset_id)

    def test_subtitle_safe_nested_inside_content_safe(self) -> None:
        for preset in SAFE_AREA_PRESETS.values():
            content_region = preset.content_safe.region()
            subtitle_region = preset.subtitle_safe.region()
            self.assertTrue(
                content_region.contains(subtitle_region),
                f"{preset.preset_id}: subtitle_safe escapes content_safe",
            )

    def test_resolved_pixel_regions_never_exceed_frame(self) -> None:
        resolutions = [(1080, 1920), (720, 1280), (2160, 3840), (1088, 1920), (61, 109)]
        for preset in SAFE_AREA_PRESETS.values():
            for width, height in resolutions:
                resolved = preset.resolve(width, height)
                for region in (resolved.content, resolved.subtitle):
                    self.assertGreaterEqual(region.x, 0)
                    self.assertGreaterEqual(region.y, 0)
                    self.assertLessEqual(region.right, width)
                    self.assertLessEqual(region.bottom, height)

    def test_resolve_rejects_non_positive_resolution(self) -> None:
        with self.assertRaises(ValueError):
            VERTICAL_9_16.resolve(0, 1920)
        with self.assertRaises(ValueError):
            VERTICAL_9_16.resolve(1080, -1)


class ResolutionScalingTests(unittest.TestCase):
    """Insets stay valid/consistent as output resolution changes within a ratio."""

    def test_multiple_9_16_resolutions_resolve_to_the_same_preset(self) -> None:
        for width, height in [(1080, 1920), (720, 1280), (2160, 3840)]:
            self.assertIs(resolve_preset_for_resolution(width, height), VERTICAL_9_16)

    def test_multiple_16_9_resolutions_resolve_to_the_same_preset(self) -> None:
        for width, height in [(1920, 1080), (1280, 720), (3840, 2160)]:
            self.assertIs(resolve_preset_for_resolution(width, height), HORIZONTAL_16_9)

    def test_multiple_1_1_resolutions_resolve_to_the_same_preset(self) -> None:
        for width, height in [(1080, 1080), (512, 512), (2048, 2048)]:
            self.assertIs(resolve_preset_for_resolution(width, height), SQUARE_1_1)

    def test_even_dimension_rounding_still_matches_its_ratio(self) -> None:
        # generators.video.assembly._resolve_resolution rounds to even pixel
        # dimensions, which can nudge the exact ratio by a fraction of a
        # percent (e.g. 1920x1080 -> 1088x1920 territory). That drift must
        # still resolve to the same preset, not fall through to "no match".
        self.assertIs(resolve_preset_for_resolution(1088, 1920), VERTICAL_9_16)

    def test_unmatched_aspect_ratio_is_rejected_by_name(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_preset_for_resolution(1600, 1200)  # 4:3, not one of the three
        self.assertIn("1600x1200", str(ctx.exception))

    def test_resolve_preset_for_resolution_rejects_non_positive_dims(self) -> None:
        with self.assertRaises(ValueError):
            resolve_preset_for_resolution(0, 1920)
        with self.assertRaises(ValueError):
            resolve_preset_for_resolution(1080, 0)

    def test_normalized_insets_scale_proportionally_across_resolutions(self) -> None:
        # Same aspect ratio, very different pixel counts: the resolved content
        # region's width/height fraction of the frame should match the
        # normalized inset definition to within one-pixel floor/rounding
        # error at the smaller resolution.
        small = VERTICAL_9_16.resolve(720, 1280)
        large = VERTICAL_9_16.resolve(2160, 3840)
        small_width_fraction = small.content.width / 720
        large_width_fraction = large.content.width / 2160
        self.assertAlmostEqual(small_width_fraction, large_width_fraction, delta=0.01)
        small_height_fraction = small.content.height / 1280
        large_height_fraction = large.content.height / 3840
        self.assertAlmostEqual(small_height_fraction, large_height_fraction, delta=0.01)


class ValidationTests(unittest.TestCase):
    """The schema itself rejects malformed regions/insets/presets."""

    def test_region_extending_past_right_edge_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafeAreaRegion(x=0.6, y=0.0, width=0.5, height=0.5)

    def test_region_extending_past_bottom_edge_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafeAreaRegion(x=0.0, y=0.6, width=0.5, height=0.5)

    def test_insets_leaving_zero_or_negative_width_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafeAreaInsets(top=0.1, right=0.5, bottom=0.1, left=0.5)

    def test_insets_leaving_zero_or_negative_height_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafeAreaInsets(top=0.5, right=0.1, bottom=0.5, left=0.1)

    def test_preset_with_subtitle_safe_escaping_content_safe_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SafeAreaPreset(
                preset_id="broken",
                aspect_ratio="9:16",
                orientation="vertical",
                content_safe=SafeAreaInsets(top=0.5, right=0.1, bottom=0.1, left=0.1),
                # top=0.1 puts the subtitle region's top above the content
                # region's top (0.5), i.e. outside it.
                subtitle_safe=SafeAreaInsets(top=0.1, right=0.1, bottom=0.1, left=0.1),
            )
        self.assertIn("broken", str(ctx.exception))

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafeAreaRegion(x=0.0, y=0.0, width=0.5, height=0.5, extra="nope")


if __name__ == "__main__":
    unittest.main()
