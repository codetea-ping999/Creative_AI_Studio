"""Tests for scene visual job fan-out (#251).

Two layers are covered:

- ``fan_out_scene_visuals`` in isolation, against a real ``JobService`` (so
  every child job actually lands in the job repository) but with no
  ``JobRunner`` -- these tests assert on *submission*: what request each
  scene produced, and how strategy/model failures are recorded without
  aborting the rest of the fan-out.
- A fake-generator integration path (parent issue #65's acceptance
  criterion) that also runs a ``JobRunner`` against fake image/video
  generators, so partial *generation* failure (as opposed to submission
  failure) is exercised end to end and successful scene outputs are proven
  not to be discarded.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.assets import AssetRepository  # noqa: E402
from core.jobs import EventBus, JobQueue, JobRunner, JobService  # noqa: E402
from core.schemas import GenerationRequest, GenerationResult  # noqa: E402
from core.storage.repositories.job_repository import JobRepository  # noqa: E402
from core.story.binding import (  # noqa: E402
    SCENE_ID_PARAM,
    SCENE_ROLE_PARAM,
    STORY_ID_PARAM,
)
from core.story.visual_fanout import (  # noqa: E402
    SceneVisualFanoutResult,
    fan_out_scene_visuals,
)
from core.story.visual_manifest import (  # noqa: E402
    BibleSnapshotRef,
    SceneVisualManifest,
    SceneVisualRequest,
)
from core.story.visual_strategy import (  # noqa: E402
    IMAGE_TO_VIDEO,
    KEN_BURNS,
    STILL,
    TEXT_TO_VIDEO,
    VisualCapabilities,
    VisualResourceBudget,
)
from generators.base import BaseGenerator  # noqa: E402
from generators.registry import GeneratorRegistry  # noqa: E402


def _request(**overrides) -> SceneVisualRequest:
    defaults = {
        "id": "visreq_1",
        "story_id": "story_1",
        "scene_id": "scene_01",
        "order": 0,
        "prompt": "a rooftop at dawn",
    }
    defaults.update(overrides)
    return SceneVisualRequest(**defaults)


def _manifest(*requests: SceneVisualRequest, story_id: str = "story_1") -> SceneVisualManifest:
    return SceneVisualManifest(story_id=story_id, requests=list(requests))


_THREE_SCENES = _manifest(
    _request(id="visreq_1", scene_id="scene_01", order=0, prompt="a rooftop at dawn"),
    _request(
        id="visreq_2",
        scene_id="scene_02",
        order=1,
        prompt="a narrow alley at night",
        visual_intent="ken burns",
    ),
    _request(
        id="visreq_3",
        scene_id="scene_03",
        order=2,
        prompt="a car chase through downtown",
        visual_intent="cinematic",
    ),
)

_FULL_CAPABILITIES = VisualCapabilities(
    image_generation_ready=True,
    text_to_video_ready=True,
    image_to_video_ready=True,
)

_STRATEGY_MODELS = {
    STILL: "sdxl",
    KEN_BURNS: "sdxl",
    TEXT_TO_VIDEO: "storyboard-video",
    IMAGE_TO_VIDEO: "learned-video",
}


class SubmissionTests(unittest.TestCase):
    """`fan_out_scene_visuals` against a real `JobService`, no runner."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)

        self.job_repository = JobRepository(root / "jobs.db")
        self.job_queue = JobQueue()
        self.event_bus = EventBus()
        self.job_service = JobService(
            self.job_repository, self.job_queue, self.event_bus
        )

    def test_one_call_launches_at_least_three_scene_jobs(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )

        self.assertEqual(len(result.items), 3)
        self.assertTrue(all(item.job_id for item in result.items))
        self.assertEqual(self.job_queue.size(), 3)

    def test_every_child_job_is_traceable_to_story_scene_strategy_and_context(
        self,
    ) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )

        for item, scene_request in zip(result.items, _THREE_SCENES.requests_in_order()):
            job = self.job_repository.get(item.job_id)
            self.assertIsNotNone(job)
            params = job.request.params

            self.assertEqual(params[STORY_ID_PARAM], "story_1")
            self.assertEqual(params[SCENE_ID_PARAM], scene_request.scene_id)
            self.assertEqual(params[SCENE_ROLE_PARAM], "visual")
            self.assertEqual(params["visual_strategy"], item.strategy.strategy)
            self.assertEqual(
                params["visual_strategy_decision"]["strategy"], item.strategy.strategy
            )
            # The frozen manifest context travels with the job wholesale.
            self.assertEqual(
                params["scene_visual_request"]["prompt"], scene_request.prompt
            )
            self.assertEqual(
                params["scene_visual_request"]["id"], scene_request.id
            )
            self.assertEqual(job.request.prompt, scene_request.prompt)
            self.assertEqual(job.request.model_id, _STRATEGY_MODELS[item.strategy.strategy])

    def test_strategy_and_media_type_follow_visual_intent(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )
        by_scene = {item.scene_id: item for item in result.items}

        self.assertEqual(by_scene["scene_01"].strategy.strategy, TEXT_TO_VIDEO)
        self.assertEqual(by_scene["scene_01"].request.media_type, "video")
        self.assertEqual(by_scene["scene_02"].strategy.strategy, KEN_BURNS)
        self.assertEqual(by_scene["scene_02"].request.media_type, "image")
        self.assertEqual(by_scene["scene_03"].strategy.strategy, TEXT_TO_VIDEO)
        self.assertEqual(by_scene["scene_03"].request.media_type, "video")

    def test_bible_snapshot_travels_into_the_child_job(self) -> None:
        manifest = _manifest(
            _request(
                id="visreq_1",
                scene_id="scene_01",
                order=0,
                bible_snapshot=[
                    BibleSnapshotRef(
                        bible_id="bible_hero",
                        kind="character",
                        name="Hero",
                        version="2024-01-01T00:00:00+00:00",
                    )
                ],
            )
        )

        result = fan_out_scene_visuals(
            manifest, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )

        job = self.job_repository.get(result.items[0].job_id)
        snapshot = job.request.params["scene_visual_request"]["bible_snapshot"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["bible_id"], "bible_hero")

    def test_project_id_is_bound_to_every_child(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES,
            self.job_service,
            _FULL_CAPABILITIES,
            _STRATEGY_MODELS,
            project_id="proj_1",
        )

        for item in result.items:
            job = self.job_repository.get(item.job_id)
            self.assertEqual(job.project_id, "proj_1")

    def test_task_type_map_overrides_the_default_generator_route(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES,
            self.job_service,
            _FULL_CAPABILITIES,
            _STRATEGY_MODELS,
            strategy_task_types={TEXT_TO_VIDEO: "text-to-video"},
        )
        by_scene = {item.scene_id: item for item in result.items}

        self.assertEqual(
            self.job_repository.get(by_scene["scene_01"].job_id).request.task_type,
            "text-to-video",
        )
        # A strategy absent from the map submits with task_type=None.
        self.assertIsNone(
            self.job_repository.get(by_scene["scene_02"].job_id).request.task_type
        )

    def test_no_capability_at_all_fails_every_scene_without_raising(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, VisualCapabilities(), _STRATEGY_MODELS
        )

        self.assertEqual(len(result.items), 3)
        self.assertTrue(all(item.job_id is None for item in result.items))
        self.assertTrue(all(item.status == "failed" for item in result.items))
        self.assertTrue(all(item.error_message for item in result.items))
        self.assertEqual(self.job_queue.size(), 0)

    def test_missing_model_for_one_strategy_fails_only_that_scene(self) -> None:
        models_missing_ken_burns = {
            key: value for key, value in _STRATEGY_MODELS.items() if key != KEN_BURNS
        }

        result = fan_out_scene_visuals(
            _THREE_SCENES,
            self.job_service,
            _FULL_CAPABILITIES,
            models_missing_ken_burns,
        )
        by_scene = {item.scene_id: item for item in result.items}

        # scene_02 selects ken_burns and has no model configured for it.
        self.assertIsNone(by_scene["scene_02"].job_id)
        self.assertEqual(by_scene["scene_02"].status, "failed")
        self.assertIn("ken_burns", by_scene["scene_02"].error_message)
        self.assertIn("scene_02", by_scene["scene_02"].error_message)

        # scene_01 and scene_03 (text_to_video) are unaffected.
        self.assertIsNotNone(by_scene["scene_01"].job_id)
        self.assertEqual(by_scene["scene_01"].status, "queued")
        self.assertIsNotNone(by_scene["scene_03"].job_id)
        self.assertEqual(by_scene["scene_03"].status, "queued")
        self.assertEqual(self.job_queue.size(), 2)

    def test_budget_can_force_every_scene_onto_the_cheap_path(self) -> None:
        budget = VisualResourceBudget(allow_text_to_video=False, allow_image_to_video=False)

        result = fan_out_scene_visuals(
            _THREE_SCENES,
            self.job_service,
            _FULL_CAPABILITIES,
            _STRATEGY_MODELS,
            budget,
        )

        self.assertTrue(
            all(item.strategy.strategy in (STILL, KEN_BURNS) for item in result.items)
        )
        self.assertTrue(all(item.request.media_type == "image" for item in result.items))

    def test_result_round_trips_through_json(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )

        payload = result.model_dump(mode="json")
        restored = SceneVisualFanoutResult.model_validate(payload)

        self.assertEqual(restored, result)


class SceneVisualFanoutResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)

        self.job_repository = JobRepository(root / "jobs.db")
        self.job_queue = JobQueue()
        self.event_bus = EventBus()
        self.job_service = JobService(
            self.job_repository, self.job_queue, self.event_bus
        )

    def _succeed(self, job_id: str, outputs: list[str]) -> None:
        for status in ("preparing", "running", "postprocessing"):
            assert self.job_repository.update_status(job_id, status) is not None
        self.job_service.mark_succeeded(
            job_id,
            GenerationResult(job_id=job_id, status="succeeded", outputs=outputs),
        )

    def test_refresh_reads_live_status_without_mutating_the_original(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )
        first_job_id = result.items[0].job_id
        self._succeed(first_job_id, ["a.mp4"])

        refreshed = result.refresh(self.job_repository)

        self.assertEqual(refreshed.items[0].status, "succeeded")
        # The original result is untouched (pure merge, same contract as
        # core.story.merge.apply_text_result).
        self.assertEqual(result.items[0].status, "queued")

    def test_is_complete_true_only_once_every_item_is_terminal(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )
        self.assertFalse(result.refresh(self.job_repository).is_complete())

        for item in result.items:
            self._succeed(item.job_id, ["x"])

        self.assertTrue(result.refresh(self.job_repository).is_complete())

    def test_a_submission_failure_item_counts_as_already_complete(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, VisualCapabilities(), _STRATEGY_MODELS
        )
        self.assertTrue(result.is_complete())

    def test_succeeded_scene_ids_and_failed_items_partition_a_mixed_result(self) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )
        succeeding, failing, _ = result.items
        self._succeed(succeeding.job_id, ["x"])
        self.job_service.mark_failed(failing.job_id, "stub render failure")

        refreshed = result.refresh(self.job_repository)

        self.assertEqual(refreshed.succeeded_scene_ids(), [succeeding.scene_id])
        self.assertEqual(
            [item.scene_id for item in refreshed.failed_items()], [failing.scene_id]
        )


class _FakeImageGenerator(BaseGenerator):
    """Always succeeds; records every request it was handed."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.seen_requests: list[GenerationRequest] = []

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest, context=None) -> GenerationResult:
        self.seen_requests.append(request)
        output_path = self.output_dir / f"image-{len(self.seen_requests):03d}.png"
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        return GenerationResult(
            job_id="pending",
            status="succeeded",
            outputs=[str(output_path)],
            previews=[str(output_path)],
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None


class _FakeVideoGenerator(BaseGenerator):
    """Fails any scene whose frozen prompt contains "crash"; succeeds otherwise.

    A deterministic stand-in for a real video model, so the integration test
    can exercise a *generation-time* partial failure (as opposed to the
    submission-time failures covered by ``SubmissionTests``) without any
    model weights.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.seen_requests: list[GenerationRequest] = []

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest, context=None) -> GenerationResult:
        self.seen_requests.append(request)
        if "crash" in request.prompt:
            raise RuntimeError("stub render failure: crash scene")
        output_path = self.output_dir / f"video-{len(self.seen_requests):03d}.mp4"
        output_path.write_bytes(b"stub-mp4")
        return GenerationResult(
            job_id="pending",
            status="succeeded",
            outputs=[str(output_path)],
            previews=[str(output_path)],
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None


class FakeGeneratorIntegrationTests(unittest.TestCase):
    """End-to-end: manifest -> fan_out_scene_visuals -> JobRunner -> fake
    image/video generators, with no real model weights involved (#251).
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)

        self.job_repository = JobRepository(root / "jobs.db")
        self.job_queue = JobQueue()
        self.event_bus = EventBus()
        self.asset_repository = AssetRepository(root / "assets")
        self.job_service = JobService(
            self.job_repository,
            self.job_queue,
            self.event_bus,
            asset_repository=self.asset_repository,
        )
        self.image_generator = _FakeImageGenerator(root / "images")
        self.video_generator = _FakeVideoGenerator(root / "videos")
        self.runner = JobRunner(
            self.job_repository,
            self.job_queue,
            GeneratorRegistry({"image": self.image_generator, "video": self.video_generator}),
            event_bus=self.event_bus,
            asset_repository=self.asset_repository,
            job_service=self.job_service,
        )

    def _run_all_queued(self) -> None:
        while self.job_queue.size():
            self.runner.run_once()

    def test_partial_generation_failure_does_not_discard_successful_scene_outputs(
        self,
    ) -> None:
        manifest = _manifest(
            _request(
                id="visreq_1",
                scene_id="scene_01",
                order=0,
                prompt="a rooftop at dawn",
                visual_intent="still",
            ),
            _request(
                id="visreq_2",
                scene_id="scene_02",
                order=1,
                prompt="a car crash on a downtown crash bridge",
                visual_intent="cinematic",
            ),
            _request(
                id="visreq_3",
                scene_id="scene_03",
                order=2,
                prompt="a quiet harbor at dusk",
                visual_intent="cinematic",
            ),
        )

        result = fan_out_scene_visuals(
            manifest, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )
        self.assertEqual(len(result.items), 3)
        self._run_all_queued()
        reconciled = result.refresh(self.job_repository)

        by_scene = {item.scene_id: item for item in reconciled.items}
        self.assertEqual(by_scene["scene_01"].status, "succeeded")
        self.assertEqual(by_scene["scene_02"].status, "failed")
        self.assertEqual(by_scene["scene_03"].status, "succeeded")

        # The failing scene does not wipe out the others: their jobs and
        # synced assets are fully intact and independently inspectable.
        for scene_id in ("scene_01", "scene_03"):
            job = self.job_repository.get(by_scene[scene_id].job_id)
            self.assertEqual(job.status, "succeeded")
            assets = self.asset_repository.get_by_job(job.id)
            self.assertEqual(len(assets), 1)

        failed_job = self.job_repository.get(by_scene["scene_02"].job_id)
        self.assertIn("crash", failed_job.error_message)
        self.assertEqual(self.asset_repository.get_by_job(failed_job.id), [])

        self.assertEqual(reconciled.succeeded_scene_ids(), ["scene_01", "scene_03"])
        self.assertEqual(
            [item.scene_id for item in reconciled.failed_items()], ["scene_02"]
        )
        self.assertTrue(reconciled.is_complete())

    def test_still_and_video_strategies_route_to_their_respective_fake_generator(
        self,
    ) -> None:
        result = fan_out_scene_visuals(
            _THREE_SCENES, self.job_service, _FULL_CAPABILITIES, _STRATEGY_MODELS
        )
        self._run_all_queued()

        self.assertEqual(len(self.image_generator.seen_requests), 1)  # scene_02: ken_burns
        self.assertEqual(len(self.video_generator.seen_requests), 2)  # scene_01, scene_03
        reconciled = result.refresh(self.job_repository)
        self.assertTrue(all(item.status == "succeeded" for item in reconciled.items))


if __name__ == "__main__":
    unittest.main()
