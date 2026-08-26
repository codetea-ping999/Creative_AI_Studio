"""Tests for JOB_LANES configuration/assignment (#179) and lane routing (#180)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
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
from core.jobs.queue import JobQueue  # noqa: E402
from core.jobs.runner import JobRunner  # noqa: E402
from core.jobs.service import JobService  # noqa: E402
from core.schemas import GenerationRequest  # noqa: E402
from core.storage.repositories.job_repository import JobRepository  # noqa: E402
from generators.base import BaseGenerator  # noqa: E402
from generators.registry import GeneratorRegistry  # noqa: E402


class _ImmediateGenerator(BaseGenerator):
    """Minimal generator that succeeds without touching the filesystem."""

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        return None

    def generate(self, request: GenerationRequest, context=None):
        from core.schemas import GenerationResult

        return GenerationResult(
            job_id="lane-stub",
            status="succeeded",
            outputs=[],
            previews=[],
            metadata={"media_type": request.media_type},
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None


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


class JobQueueLaneRoutingTests(unittest.TestCase):
    """Tests for `core/jobs/queue.py`'s lane-partitioned FIFO (#180)."""

    def test_default_construction_is_single_lane_and_backward_compatible(self) -> None:
        # No `lanes` argument at all: enqueue/dequeue/size take no lane
        # argument, exactly as before #180 -- this is the "single-lane
        # configuration matches current behavior" acceptance criterion.
        queue = JobQueue()
        self.assertTrue(queue.is_single_lane)
        self.assertEqual(queue.size(), 0)

        queue.enqueue("job-1")
        queue.enqueue("job-2")
        self.assertEqual(queue.size(), 2)
        self.assertEqual(queue.dequeue(), "job-1")
        self.assertEqual(queue.dequeue(), "job-2")
        self.assertIsNone(queue.dequeue())

    def test_multi_lane_queues_preserve_fifo_independently(self) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))

        queue.enqueue("heavy-1", lane=LANE_HEAVY)
        queue.enqueue("light-1", lane=LANE_LIGHT)
        queue.enqueue("heavy-2", lane=LANE_HEAVY)
        queue.enqueue("light-2", lane=LANE_LIGHT)

        # Each lane's own FIFO order is unaffected by activity in the other
        # lane -- a long run of heavy jobs never reorders light jobs (or vice
        # versa).
        self.assertEqual(queue.dequeue(lane=LANE_HEAVY), "heavy-1")
        self.assertEqual(queue.dequeue(lane=LANE_LIGHT), "light-1")
        self.assertEqual(queue.dequeue(lane=LANE_HEAVY), "heavy-2")
        self.assertEqual(queue.dequeue(lane=LANE_LIGHT), "light-2")

    def test_a_job_enqueued_to_one_lane_cannot_be_consumed_from_another(self) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        queue.enqueue("heavy-only", lane=LANE_HEAVY)

        # The job is not visible to the light lane at all...
        self.assertIsNone(queue.dequeue(lane=LANE_LIGHT))
        # ...and dequeuing it from its actual lane returns it exactly once.
        self.assertEqual(queue.dequeue(lane=LANE_HEAVY), "heavy-only")
        self.assertIsNone(queue.dequeue(lane=LANE_HEAVY))
        self.assertIsNone(queue.dequeue(lane=LANE_LIGHT))

    def test_enqueuing_the_same_job_into_a_different_lane_raises(self) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        queue.enqueue("job-1", lane=LANE_HEAVY)

        with self.assertRaises(ValueError) as ctx:
            queue.enqueue("job-1", lane=LANE_LIGHT)
        message = str(ctx.exception)
        self.assertIn("job-1", message)
        self.assertIn(LANE_HEAVY, message)
        self.assertIn(LANE_LIGHT, message)

        # The job must still be consumable exactly once, from its original
        # lane -- the rejected call must not have partially mutated state.
        self.assertIsNone(queue.dequeue(lane=LANE_LIGHT))
        self.assertEqual(queue.dequeue(lane=LANE_HEAVY), "job-1")

    def test_enqueuing_the_same_job_into_the_same_lane_twice_is_idempotent(self) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        queue.enqueue("job-1", lane=LANE_HEAVY)
        queue.enqueue("job-1", lane=LANE_HEAVY)

        self.assertEqual(queue.size(lane=LANE_HEAVY), 1)
        self.assertEqual(queue.dequeue(lane=LANE_HEAVY), "job-1")
        self.assertIsNone(queue.dequeue(lane=LANE_HEAVY))

    def test_dequeue_without_a_lane_on_a_multi_lane_queue_raises_actionable_error(
        self,
    ) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        with self.assertRaises(ValueError) as ctx:
            queue.dequeue()
        message = str(ctx.exception)
        self.assertIn(LANE_HEAVY, message)
        self.assertIn(LANE_LIGHT, message)

    def test_enqueue_without_a_lane_on_a_multi_lane_queue_raises_actionable_error(
        self,
    ) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        with self.assertRaises(ValueError):
            queue.enqueue("job-1")

    def test_unknown_lane_raises_actionable_error(self) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        with self.assertRaises(ValueError) as ctx:
            queue.enqueue("job-1", lane="mystery")
        self.assertIn("mystery", str(ctx.exception))

    def test_size_reports_per_lane_and_aggregate(self) -> None:
        queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
        queue.enqueue("heavy-1", lane=LANE_HEAVY)
        queue.enqueue("heavy-2", lane=LANE_HEAVY)
        queue.enqueue("light-1", lane=LANE_LIGHT)

        self.assertEqual(queue.size(lane=LANE_HEAVY), 2)
        self.assertEqual(queue.size(lane=LANE_LIGHT), 1)
        self.assertEqual(queue.size(), 3)

    def test_duplicate_lane_names_raise_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            JobQueue(lanes=(LANE_HEAVY, LANE_HEAVY))


class JobServiceLaneRoutingTests(unittest.TestCase):
    """Tests that `JobService.enqueue_job` applies the lane assignment
    contract without duplication (#180)."""

    def test_enqueue_job_without_lane_config_uses_the_queues_implicit_lane(
        self,
    ) -> None:
        # No `lane_config` at all -- must reproduce pre-#180 JobService
        # behavior exactly, since this is what every existing caller
        # (bootstrap/factories.py, most tests) still does.
        with TemporaryDirectory() as tmp_dir:
            repository = JobRepository(Path(tmp_dir) / "jobs.db")
            queue = JobQueue()
            service = JobService(repository, queue)

            job = service.create_job(
                GenerationRequest(media_type="text", prompt="a story beat", model_id="")
            )

            self.assertEqual(queue.size(), 1)
            self.assertEqual(queue.dequeue(), job.id)

    def test_enqueue_job_with_a_single_lane_config_collapses_onto_the_implicit_lane(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            repository = JobRepository(Path(tmp_dir) / "jobs.db")
            queue = JobQueue()
            service = JobService(repository, queue, lane_config=parse_job_lanes("heavy:1"))

            image_job = service.create_job(
                GenerationRequest(media_type="image", prompt="a skyline", model_id="sdxl")
            )
            text_job = service.create_job(
                GenerationRequest(media_type="text", prompt="a beat", model_id="")
            )

            # Both land in the queue's one implicit lane, in FIFO order.
            self.assertEqual(queue.size(), 2)
            self.assertEqual(queue.dequeue(), image_job.id)
            self.assertEqual(queue.dequeue(), text_job.id)

    def test_enqueue_job_with_a_multi_lane_config_routes_without_duplication(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            repository = JobRepository(Path(tmp_dir) / "jobs.db")
            queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
            service = JobService(
                repository, queue, lane_config=parse_job_lanes(DEFAULT_JOB_LANES)
            )

            image_job = service.create_job(
                GenerationRequest(media_type="image", prompt="a skyline", model_id="sdxl")
            )
            text_job = service.create_job(
                GenerationRequest(media_type="text", prompt="a beat", model_id="")
            )
            speech_job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="narration",
                    model_id="",
                    task_type="text-to-speech",
                )
            )

            self.assertEqual(queue.size(lane=LANE_HEAVY), 1)
            self.assertEqual(queue.size(lane=LANE_LIGHT), 2)

            # Each job is consumable from exactly the lane it was routed to,
            # never both -- proving no job is duplicated across lanes.
            self.assertEqual(queue.dequeue(lane=LANE_HEAVY), image_job.id)
            self.assertIsNone(queue.dequeue(lane=LANE_HEAVY))
            self.assertEqual(queue.dequeue(lane=LANE_LIGHT), text_job.id)
            self.assertEqual(queue.dequeue(lane=LANE_LIGHT), speech_job.id)
            self.assertIsNone(queue.dequeue(lane=LANE_LIGHT))


class JobRunnerLaneRoutingTests(unittest.TestCase):
    """End-to-end proof that a lane-routed job is processed by exactly one
    lane's worker (#180)."""

    def test_run_once_scoped_to_a_lane_only_drains_that_lanes_jobs(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repository = JobRepository(Path(tmp_dir) / "jobs.db")
            queue = JobQueue(lanes=(LANE_HEAVY, LANE_LIGHT))
            lane_config = parse_job_lanes(DEFAULT_JOB_LANES)
            service = JobService(repository, queue, lane_config=lane_config)
            registry = GeneratorRegistry(
                {"image": _ImmediateGenerator(), "text": _ImmediateGenerator()}
            )
            runner = JobRunner(repository, queue, registry)

            heavy_job = service.create_job(
                GenerationRequest(media_type="image", prompt="a skyline", model_id="sdxl")
            )
            light_job = service.create_job(
                GenerationRequest(media_type="text", prompt="a beat", model_id="")
            )

            # Draining the light lane must not touch the heavy job.
            light_result = runner.run_once(lane=LANE_LIGHT)
            self.assertIsNotNone(light_result)
            assert light_result is not None
            self.assertEqual(light_result.id, light_job.id)
            self.assertEqual(light_result.status, "succeeded")
            self.assertIsNone(runner.run_once(lane=LANE_LIGHT))

            # The heavy job is still pending, untouched by the light drain,
            # and only the heavy lane can produce it.
            self.assertIsNone(runner.run_once(lane=LANE_LIGHT))
            heavy_result = runner.run_once(lane=LANE_HEAVY)
            self.assertIsNotNone(heavy_result)
            assert heavy_result is not None
            self.assertEqual(heavy_result.id, heavy_job.id)
            self.assertEqual(heavy_result.status, "succeeded")


if __name__ == "__main__":
    unittest.main()
