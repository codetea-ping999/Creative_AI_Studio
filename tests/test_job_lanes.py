"""Tests for JOB_LANES configuration parsing and lane assignment (#179)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.jobs as jobs_package  # noqa: E402
from core.jobs.lanes import (  # noqa: E402
    DEFAULT_JOB_LANES,
    LANE_HEAVY,
    LANE_LIGHT,
    LaneConfig,
    assign_lane,
    parse_job_lanes,
    resolve_lane,
)


class ParseJobLanesTests(unittest.TestCase):
    def test_default_value_is_heavy_1_light_1(self) -> None:
        self.assertEqual(DEFAULT_JOB_LANES, "heavy:1,light:1")

    def test_explicit_value_is_parsed_without_touching_env(self) -> None:
        config = parse_job_lanes("heavy:2,light:3")
        self.assertEqual(config.concurrency, {"heavy": 2, "light": 3})
        self.assertEqual(config.lane_names, ("heavy", "light"))

    def test_falls_back_to_default_when_no_value_or_env_is_given(self) -> None:
        os.environ.pop("JOB_LANES", None)
        config = parse_job_lanes()
        self.assertEqual(config.concurrency, {"heavy": 1, "light": 1})

    def test_reads_job_lanes_from_the_environment(self) -> None:
        os.environ["JOB_LANES"] = "heavy:4,light:2"
        try:
            config = parse_job_lanes()
            self.assertEqual(config.concurrency, {"heavy": 4, "light": 2})
        finally:
            os.environ.pop("JOB_LANES", None)

    def test_single_lane_configuration_is_representable(self) -> None:
        config = parse_job_lanes("heavy:1")
        self.assertTrue(config.is_single_lane)
        self.assertEqual(config.lane_names, ("heavy",))

    def test_custom_lane_names_are_allowed(self) -> None:
        config = parse_job_lanes("default:3")
        self.assertTrue(config.is_single_lane)
        self.assertEqual(config.concurrency, {"default": 3})

    def test_multi_lane_configuration_is_not_single_lane(self) -> None:
        config = parse_job_lanes("heavy:1,light:1")
        self.assertFalse(config.is_single_lane)

    def test_whitespace_around_entries_is_tolerated(self) -> None:
        config = parse_job_lanes(" heavy : 2 , light : 1 ")
        self.assertEqual(config.concurrency, {"heavy": 2, "light": 1})

    def test_empty_value_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("")
        self.assertIn("JOB_LANES", str(ctx.exception))
        self.assertIn("empty", str(ctx.exception))

    def test_missing_colon_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("heavy1")
        message = str(ctx.exception)
        self.assertIn("heavy1", message)
        self.assertIn("JOB_LANES", message)

    def test_non_integer_concurrency_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("heavy:many")
        message = str(ctx.exception)
        self.assertIn("heavy", message)
        self.assertIn("many", message)

    def test_zero_concurrency_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("heavy:0")
        self.assertIn("heavy", str(ctx.exception))

    def test_negative_concurrency_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("heavy:-1")
        self.assertIn("heavy", str(ctx.exception))

    def test_duplicate_lane_name_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("heavy:1,heavy:2")
        message = str(ctx.exception)
        self.assertIn("heavy", message)
        self.assertIn("more than once", message)

    def test_empty_lane_name_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes(":1")
        self.assertIn("JOB_LANES", str(ctx.exception))

    def test_trailing_comma_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_job_lanes("heavy:1,")
        self.assertIn("JOB_LANES", str(ctx.exception))


class ResolveLaneTests(unittest.TestCase):
    def test_image_routes_to_heavy(self) -> None:
        self.assertEqual(resolve_lane("image", "text-to-image"), LANE_HEAVY)

    def test_video_routes_to_heavy_by_default(self) -> None:
        self.assertEqual(resolve_lane("video", "text-to-video"), LANE_HEAVY)

    def test_music_routes_to_heavy(self) -> None:
        self.assertEqual(resolve_lane("audio", "text-to-music"), LANE_HEAVY)

    def test_text_routes_to_light(self) -> None:
        self.assertEqual(resolve_lane("text", "story"), LANE_LIGHT)

    def test_text_to_speech_routes_to_light_despite_audio_media_type(self) -> None:
        self.assertEqual(resolve_lane("audio", "text-to-speech"), LANE_LIGHT)

    def test_assembly_routes_to_light_despite_video_media_type(self) -> None:
        self.assertEqual(resolve_lane("video", "assembly"), LANE_LIGHT)

    def test_none_task_type_uses_the_media_type_default(self) -> None:
        self.assertEqual(resolve_lane("audio", None), LANE_HEAVY)
        self.assertEqual(resolve_lane("text", None), LANE_LIGHT)

    def test_unknown_task_type_falls_back_to_the_media_type_default(self) -> None:
        # An unrecognized task_type under a heavy media type stays heavy
        # rather than being guessed into light.
        self.assertEqual(resolve_lane("audio", "some-future-task"), LANE_HEAVY)
        self.assertEqual(resolve_lane("video", "some-future-task"), LANE_HEAVY)
        self.assertEqual(resolve_lane("text", "some-future-task"), LANE_LIGHT)

    def test_unknown_media_type_raises_actionable_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_lane("holographic", "anything")
        message = str(ctx.exception)
        self.assertIn("holographic", message)
        self.assertIn("image", message)  # names a known media type


class AssignLaneTests(unittest.TestCase):
    def test_single_lane_config_collapses_every_job_onto_it(self) -> None:
        lanes = LaneConfig(concurrency={"heavy": 1})
        self.assertEqual(assign_lane("text", "story", lanes), "heavy")
        self.assertEqual(assign_lane("image", "text-to-image", lanes), "heavy")
        self.assertEqual(assign_lane("audio", "text-to-speech", lanes), "heavy")

    def test_single_lane_config_with_custom_name_still_collapses(self) -> None:
        lanes = LaneConfig(concurrency={"default": 2})
        self.assertEqual(assign_lane("image", "text-to-image", lanes), "default")
        self.assertEqual(assign_lane("text", "story", lanes), "default")

    def test_multi_lane_config_routes_by_media_and_task_type(self) -> None:
        lanes = LaneConfig(concurrency={"heavy": 1, "light": 1})
        self.assertEqual(assign_lane("image", "text-to-image", lanes), "heavy")
        self.assertEqual(assign_lane("text", "story", lanes), "light")
        self.assertEqual(assign_lane("audio", "text-to-speech", lanes), "light")
        self.assertEqual(assign_lane("video", "assembly", lanes), "light")

    def test_missing_target_lane_raises_actionable_error(self) -> None:
        lanes = LaneConfig(concurrency={"heavy": 1, "extra": 1})
        with self.assertRaises(ValueError) as ctx:
            assign_lane("text", "story", lanes)
        message = str(ctx.exception)
        self.assertIn("light", message)
        self.assertIn("JOB_LANES", message)

    def test_end_to_end_default_configuration_matches_the_epic_contract(self) -> None:
        lanes = parse_job_lanes(DEFAULT_JOB_LANES)
        self.assertEqual(assign_lane("image", "text-to-image", lanes), LANE_HEAVY)
        self.assertEqual(assign_lane("video", "text-to-video", lanes), LANE_HEAVY)
        self.assertEqual(assign_lane("audio", "text-to-music", lanes), LANE_HEAVY)
        self.assertEqual(assign_lane("text", "story", lanes), LANE_LIGHT)
        self.assertEqual(assign_lane("audio", "text-to-speech", lanes), LANE_LIGHT)
        self.assertEqual(assign_lane("video", "assembly", lanes), LANE_LIGHT)


class PackageExportsTests(unittest.TestCase):
    def test_core_jobs_package_re_exports_the_lane_contract(self) -> None:
        self.assertIs(jobs_package.DEFAULT_JOB_LANES, DEFAULT_JOB_LANES)
        self.assertIs(jobs_package.LANE_HEAVY, LANE_HEAVY)
        self.assertIs(jobs_package.LANE_LIGHT, LANE_LIGHT)
        self.assertIs(jobs_package.LaneConfig, LaneConfig)
        self.assertIs(jobs_package.assign_lane, assign_lane)
        self.assertIs(jobs_package.parse_job_lanes, parse_job_lanes)
        self.assertIs(jobs_package.resolve_lane, resolve_lane)


if __name__ == "__main__":
    unittest.main()
