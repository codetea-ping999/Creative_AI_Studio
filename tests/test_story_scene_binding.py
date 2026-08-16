"""Scene binding: generated media finding its way back to its scene."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.assets import AssetRepository  # noqa: E402
from core.jobs import EventBus, JobQueue, JobService  # noqa: E402
from core.schemas import GenerationRequest, GenerationResult  # noqa: E402
from core.storage.repositories.job_repository import JobRepository  # noqa: E402
from core.story import (  # noqa: E402
    SceneBinder,
    StoryRepository,
    apply_text_result,
    build_timeline,
    scene_binding_params,
)

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

_SCENES = {
    "scenes": [
        {
            "heading": "屋上の朝",
            "narration": "朝の光が街を照らしていた。",
            "image_prompt": "rooftop at dawn",
            "bgm_mood": "hopeful",
            "duration_seconds": 4,
        },
        {
            "heading": "路地の追跡",
            "narration": "",
            "image_prompt": "narrow alley at night",
            "bgm_mood": "hopeful",
            "duration_seconds": 3,
        },
    ]
}


class SceneBindingParamsTests(unittest.TestCase):
    def test_params_carry_the_route_home(self) -> None:
        params = scene_binding_params("story_1", "scene_01", "visual")
        self.assertEqual(
            params,
            {"story_id": "story_1", "scene_id": "scene_01", "scene_role": "visual"},
        )

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            scene_binding_params("story_1", "scene_01", "poster")
        self.assertIn("visual", str(context.exception))


class SceneBinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)

        self.job_repository = JobRepository(root / "jobs.db")
        self.asset_repository = AssetRepository(root / "assets")
        self.story_repository = StoryRepository(root / "stories")
        self.event_bus = EventBus()
        self.job_service = JobService(
            self.job_repository,
            JobQueue(),
            self.event_bus,
            asset_repository=self.asset_repository,
        )
        self.binder = SceneBinder(
            self.story_repository,
            self.job_repository,
            self.asset_repository,
            event_bus=self.event_bus,
        )

        story = self.story_repository.create(title="Rewind", premise="p")
        self.story = self.story_repository.save(
            apply_text_result(story, "scene_list", _SCENES)
        )

    def _succeed_scene_job(
        self,
        scene_id: str,
        role: str,
        *,
        output: str = "outputs/images/a.png",
        story_id: str | None = None,
    ) -> str:
        request = GenerationRequest(
            media_type="image",
            prompt="rooftop at dawn",
            model_id="sdxl",
            params=scene_binding_params(
                story_id or self.story.id, scene_id, role
            ),
        )
        job = self.job_service.create_job(request)
        self.job_service.mark_succeeded(
            job.id,
            GenerationResult(
                job_id=job.id,
                status="succeeded",
                outputs=[output],
                previews=[output],
                metadata={"quality_report": {"quality_score": 70.0}},
            ),
        )
        return job.id

    def test_binding_attaches_the_asset_to_its_scene(self) -> None:
        job_id = self._succeed_scene_job("scene_01", "visual")
        story = self.binder.bind_job(job_id)

        self.assertIsNotNone(story)
        scene = story.scenes[0]
        self.assertIn("visual", scene.asset_ids)
        self.assertIn(job_id, scene.job_ids)
        # The untouched scene keeps nothing.
        self.assertEqual(story.scenes[1].asset_ids, {})

    def test_event_subscription_binds_without_an_explicit_call(self) -> None:
        self.binder.attach_to_event_bus()
        self._succeed_scene_job("scene_02", "visual")

        story = self.story_repository.get(self.story.id)
        self.assertIn("visual", story.scenes[1].asset_ids)

    def test_roles_accumulate_on_one_scene(self) -> None:
        self.binder.attach_to_event_bus()
        self._succeed_scene_job("scene_01", "visual")
        self._succeed_scene_job(
            "scene_01", "narration", output="outputs/audio/n.wav"
        )
        self._succeed_scene_job("scene_01", "music", output="outputs/audio/m.wav")

        scene = self.story_repository.get(self.story.id).scenes[0]
        self.assertEqual(set(scene.asset_ids), {"visual", "narration", "music"})
        self.assertEqual(len(scene.job_ids), 3)

    def test_regenerating_a_role_replaces_the_previous_asset(self) -> None:
        self.binder.attach_to_event_bus()
        self._succeed_scene_job("scene_01", "visual", output="outputs/images/a.png")
        first = self.story_repository.get(self.story.id).scenes[0].asset_ids["visual"]

        self._succeed_scene_job("scene_01", "visual", output="outputs/images/b.png")
        second = self.story_repository.get(self.story.id).scenes[0].asset_ids["visual"]

        self.assertNotEqual(first, second)
        self.assertEqual(len(self.story_repository.get(self.story.id).scenes[0].job_ids), 2)

    def test_unbound_job_is_ignored(self) -> None:
        request = GenerationRequest(
            media_type="image", prompt="anything", model_id="sdxl", params={}
        )
        job = self.job_service.create_job(request)
        self.job_service.mark_succeeded(
            job.id,
            GenerationResult(
                job_id=job.id,
                status="succeeded",
                outputs=["outputs/images/x.png"],
                previews=[],
                metadata={},
            ),
        )
        self.assertIsNone(self.binder.bind_job(job.id))

    def test_unknown_story_and_scene_are_survivable(self) -> None:
        self.assertIsNone(
            self.binder.bind_job(
                self._succeed_scene_job("scene_01", "visual", story_id="story_missing")
            )
        )
        self.assertIsNone(self.binder.bind_job(self._succeed_scene_job("scene_99", "visual")))

    def test_failed_job_is_not_bound(self) -> None:
        request = GenerationRequest(
            media_type="image",
            prompt="rooftop",
            model_id="sdxl",
            params=scene_binding_params(self.story.id, "scene_01", "visual"),
        )
        job = self.job_service.create_job(request)
        self.job_service.mark_failed(job.id, "out of memory")

        self.assertIsNone(self.binder.bind_job(job.id))
        self.assertEqual(self.story_repository.get(self.story.id).scenes[0].asset_ids, {})

    def test_event_handler_never_raises(self) -> None:
        self.binder.attach_to_event_bus()
        # A terminal event for a job that does not exist must not propagate.
        self.event_bus.publish("job_succeeded", {"job_id": "job_missing"})
        self.event_bus.publish("job_succeeded", {})

    def test_binding_makes_the_timeline_buildable(self) -> None:
        self.binder.attach_to_event_bus()
        for scene_id in ("scene_01", "scene_02"):
            self._succeed_scene_job(scene_id, "visual")
        self._succeed_scene_job(
            "scene_01", "narration", output="outputs/audio/n.wav"
        )

        story = self.story_repository.get(self.story.id)
        timeline = build_timeline(story)
        self.assertEqual(len(timeline["tracks"]["visual"]), 2)
        self.assertEqual(len(timeline["tracks"]["narration"]), 1)
        self.assertAlmostEqual(timeline["total_duration_seconds"], 7.0)


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class SceneGenerationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.services = create_application_services(
            db_path=root / "jobs.db", output_dir=root / "outputs" / "images"
        )
        self.client = TestClient(create_app(self.services, start_job_runner=False))

        self.story_id = self.client.post(
            "/stories", json={"title": "Rewind", "premise": "p"}
        ).json()["id"]
        story = self.services.story_repository.get(self.story_id)
        self.services.story_repository.save(
            apply_text_result(story, "scene_list", _SCENES)
        )

    def _drain(self, limit: int = 32) -> None:
        processed = 0
        while processed < limit and self.services.job_runner.run_once() is not None:
            processed += 1

    def test_scene_visual_request_uses_the_scene_prompt(self) -> None:
        response = self.client.post(
            f"/stories/{self.story_id}/scenes/scene_01/generate",
            json={"role": "visual", "model_id": "sdxl"},
        )
        self.assertEqual(response.status_code, 201, response.text)

        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["media_type"], "image")
        self.assertEqual(job["request"]["prompt"], "rooftop at dawn")
        self.assertEqual(job["request"]["params"]["scene_id"], "scene_01")
        self.assertEqual(job["request"]["params"]["scene_role"], "visual")

    def test_scene_narration_request_targets_speech(self) -> None:
        response = self.client.post(
            f"/stories/{self.story_id}/scenes/scene_01/generate",
            json={"role": "narration"},
        )
        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["media_type"], "audio")
        self.assertEqual(job["request"]["task_type"], "text-to-speech")
        self.assertEqual(job["request"]["prompt"], "朝の光が街を照らしていた。")

    def test_scene_music_request_uses_the_mood_and_scene_length(self) -> None:
        response = self.client.post(
            f"/stories/{self.story_id}/scenes/scene_01/generate",
            json={"role": "music"},
        )
        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["request"]["task_type"], "text-to-music")
        self.assertEqual(job["request"]["prompt"], "hopeful")
        self.assertEqual(job["request"]["params"]["duration_seconds"], 4)

    def test_scene_without_the_needed_text_is_a_conflict(self) -> None:
        response = self.client.post(
            f"/stories/{self.story_id}/scenes/scene_02/generate",
            json={"role": "narration"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("narration", response.json()["detail"])

    def test_unknown_role_scene_and_story_are_rejected(self) -> None:
        self.assertEqual(
            self.client.post(
                f"/stories/{self.story_id}/scenes/scene_01/generate",
                json={"role": "poster"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/stories/{self.story_id}/scenes/scene_99/generate",
                json={"role": "visual"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/stories/story_missing/scenes/scene_01/generate",
                json={"role": "visual"},
            ).status_code,
            404,
        )

    def test_assemble_before_any_visual_is_a_conflict(self) -> None:
        response = self.client.post(f"/stories/{self.story_id}/assemble", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("scene_01", response.json()["detail"])

    def test_assemble_queues_a_job_once_scenes_carry_visuals(self) -> None:
        # Bind a stand-in asset per scene without needing an image model.
        story = self.services.story_repository.get(self.story_id)
        for scene in story.scenes:
            job = self.services.job_service.create_job(
                GenerationRequest(
                    media_type="image",
                    prompt="x",
                    model_id="sdxl",
                    params=scene_binding_params(story.id, scene.id, "visual"),
                )
            )
            self.services.job_service.mark_succeeded(
                job.id,
                GenerationResult(
                    job_id=job.id,
                    status="succeeded",
                    outputs=[f"outputs/images/{scene.id}.png"],
                    previews=[f"outputs/images/{scene.id}.png"],
                    metadata={},
                ),
            )

        response = self.client.post(
            f"/stories/{self.story_id}/assemble",
            json={"width": 1080, "height": 1920, "fps": 24},
        )
        self.assertEqual(response.status_code, 201, response.text)

        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["media_type"], "video")
        self.assertEqual(job["request"]["task_type"], "assembly")
        timeline = job["request"]["params"]["timeline"]
        self.assertEqual(timeline["resolution"], [1080, 1920])
        self.assertEqual(len(timeline["tracks"]["visual"]), 2)
        # Timelines travel as asset ids, and this route resolved every one of
        # them against the story's project before queueing; a raw path would
        # have bypassed that check.
        self.assertNotIn("path", timeline["tracks"]["visual"][0])

    def test_assemble_accepts_media_that_shares_the_story_project(self) -> None:
        """A project-bound story assembles its own project's media.

        The project boundary is an exact match, so this is the branch that the
        project-less story above never reaches: without it, the only green
        assemble coverage would be ``None == None``.
        """

        project_id = self.client.post(
            "/projects", json={"name": "Short film"}
        ).json()["id"]
        story_id = self.client.post(
            "/stories",
            json={"title": "Bound", "premise": "p", "project_id": project_id},
        ).json()["id"]
        story = self.services.story_repository.save(
            apply_text_result(
                self.services.story_repository.get(story_id), "scene_list", _SCENES
            )
        )

        for scene in story.scenes:
            job = self.services.job_service.create_job(
                GenerationRequest(
                    media_type="image",
                    prompt="x",
                    model_id="sdxl",
                    params=scene_binding_params(story.id, scene.id, "visual"),
                ),
                project_id=project_id,
            )
            self.services.job_service.mark_succeeded(
                job.id,
                GenerationResult(
                    job_id=job.id,
                    status="succeeded",
                    outputs=[f"outputs/images/{scene.id}.png"],
                    previews=[f"outputs/images/{scene.id}.png"],
                    metadata={},
                ),
            )

        response = self.client.post(f"/stories/{story_id}/assemble", json={})
        self.assertEqual(response.status_code, 201, response.text)

        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        timeline = job["request"]["params"]["timeline"]
        self.assertEqual(len(timeline["tracks"]["visual"]), 2)
        # Asserting that each asset resolves and matches the project would be dead
        # weight: resolve_timeline_assets already 404s on either, so the 201 above
        # proves both. Pin instead what the 201 does not imply — that the assembly
        # job is itself filed under the story's project, and that the timeline
        # travels as asset ids with no host path (issue #105).
        self.assertEqual(job["project_id"], project_id)
        for entry in timeline["tracks"]["visual"]:
            self.assertNotIn("path", entry)

    def test_scene_jobs_join_the_story_project(self) -> None:
        project_id = self.client.post(
            "/projects", json={"name": "Short film"}
        ).json()["id"]
        story_id = self.client.post(
            "/stories", json={"title": "Bound", "premise": "p", "project_id": project_id}
        ).json()["id"]
        story = self.services.story_repository.get(story_id)
        self.services.story_repository.save(
            apply_text_result(story, "scene_list", _SCENES)
        )

        job_id = self.client.post(
            f"/stories/{story_id}/scenes/scene_01/generate",
            json={"role": "visual"},
        ).json()["job_id"]

        project_jobs = self.client.get(f"/projects/{project_id}/jobs").json()
        self.assertIn(job_id, [entry["id"] for entry in project_jobs["jobs"]])

    def test_end_to_end_scene_binding_through_the_running_studio(self) -> None:
        """A narration job generated from the UI path binds itself to its scene."""

        response = self.client.post(
            f"/stories/{self.story_id}/scenes/scene_01/generate",
            json={"role": "narration", "model_id": "template-writer"},
        )
        self.assertEqual(response.status_code, 201)
        self._drain()

        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        detail = self.client.get(f"/stories/{self.story_id}").json()
        scene = detail["story"]["scenes"][0]

        if job["status"] == "succeeded":
            self.assertIn("narration", scene["asset_ids"])
            self.assertIn(job["id"], scene["job_ids"])
            self.assertNotIn(
                {"scene_id": "scene_01", "role": "narration"},
                detail["missing_assets"],
            )
        else:
            # No TTS weights in this environment: the contract under test is that
            # a failed job leaves the scene unbound rather than half-bound.
            self.assertEqual(scene["asset_ids"], {})


if __name__ == "__main__":
    unittest.main()
