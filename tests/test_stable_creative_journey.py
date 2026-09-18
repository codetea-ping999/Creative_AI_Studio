"""v1.0 Stable primary creative journey, driven through the public API.

Project -> Template Story -> procedural scene visuals -> Gallery/reuse ->
Assembly MP4, with no model weights installed and nothing pre-bound: every scene
visual in the critical proof is produced by ``POST /stories/{id}/scenes/{scene}/
generate`` (``media_type="video"``) and finds its scene through the ordinary job
lifecycle and scene binder.

SDXL stays Preview: nothing here names an image model.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from apps.api.routes.stories import PROCEDURAL_VISUAL_MODEL_ID
    from bootstrap import create_application_services
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as exc:  # pragma: no cover - environment guard
    FFMPEG = None
    FFMPEG_ERROR = exc

SCENE_COUNT = 2


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class StableCreativeJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.services = create_application_services(
            db_path=self.root / "jobs.db",
            output_dir=self.root / "outputs" / "images",
        )
        self.client = TestClient(
            create_app(self.services, start_job_runner=False)
        )

    def _drain(self, limit: int = 64) -> int:
        processed = 0
        while (
            processed < limit and self.services.job_runner.run_once() is not None
        ):
            processed += 1
        return processed

    def _project_story_with_template_scenes(self) -> tuple[str, str]:
        project = self.client.post("/projects", json={"name": "Stable film"})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]

        story = self.client.post(
            "/stories",
            json={
                "title": "Rewind",
                "premise": "時を巻き戻せる少女が最後の一日を選び直す",
                "language": "ja",
                "project_id": project_id,
            },
        )
        self.assertEqual(story.status_code, 201, story.text)
        story_id = story.json()["id"]

        job_id = self.client.post(
            f"/stories/{story_id}/expand",
            json={
                "task": "scene_list",
                "model_id": "template-writer",
                "params": {"scene_count": SCENE_COUNT},
            },
        ).json()["job_id"]
        self._drain()
        applied = self.client.post(
            f"/stories/{story_id}/apply", json={"job_id": job_id}
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(len(applied.json()["story"]["scenes"]), SCENE_COUNT)
        return project_id, story_id

    def _generate_procedural_visual(self, story_id: str, scene_id: str) -> str:
        response = self.client.post(
            f"/stories/{story_id}/scenes/{scene_id}/generate",
            json={"role": "visual", "media_type": "video"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["job_id"]

    def _scenes(self, story_id: str) -> list[dict]:
        return self.client.get(f"/stories/{story_id}").json()["story"]["scenes"]

    def test_scene_visual_request_is_a_procedural_video_job(self) -> None:
        _, story_id = self._project_story_with_template_scenes()
        scene = self._scenes(story_id)[0]

        job_id = self._generate_procedural_visual(story_id, scene["id"])
        job = self.client.get(f"/jobs/{job_id}").json()

        self.assertEqual(job["media_type"], "video")
        self.assertEqual(job["request"]["task_type"], "text-to-video")
        self.assertEqual(job["request"]["model_id"], PROCEDURAL_VISUAL_MODEL_ID)
        self.assertEqual(job["request"]["prompt"], scene["image_prompt"])
        params = job["request"]["params"]
        self.assertEqual(params["story_id"], story_id)
        self.assertEqual(params["scene_id"], scene["id"])
        self.assertEqual(params["scene_role"], "visual")
        # Reproducible across server restarts: never the per-process hash().
        self.assertIsInstance(job["request"]["seed"], int)

    def test_scene_seed_is_stable_and_an_explicit_seed_wins(self) -> None:
        _, story_id = self._project_story_with_template_scenes()
        scene_id = self._scenes(story_id)[0]["id"]

        seeds = [
            self.client.get(
                f"/jobs/{self._generate_procedural_visual(story_id, scene_id)}"
            ).json()["request"]["seed"]
            for _ in range(2)
        ]
        self.assertEqual(seeds[0], seeds[1])

        explicit = self.client.post(
            f"/stories/{story_id}/scenes/{scene_id}/generate",
            json={"role": "visual", "media_type": "video", "seed": 7},
        )
        self.assertEqual(
            self.client.get(f"/jobs/{explicit.json()['job_id']}").json()["request"][
                "seed"
            ],
            7,
        )

    def test_still_image_path_is_unchanged_without_media_type(self) -> None:
        _, story_id = self._project_story_with_template_scenes()
        scene = self._scenes(story_id)[0]

        response = self.client.post(
            f"/stories/{story_id}/scenes/{scene['id']}/generate",
            json={"role": "visual"},
        )
        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["media_type"], "image")

    def test_media_type_is_refused_for_audio_roles(self) -> None:
        _, story_id = self._project_story_with_template_scenes()
        scene = self._scenes(story_id)[0]

        response = self.client.post(
            f"/stories/{story_id}/scenes/{scene['id']}/generate",
            json={"role": "narration", "media_type": "video"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("visual", response.json()["detail"])

        unknown = self.client.post(
            f"/stories/{story_id}/scenes/{scene['id']}/generate",
            json={"role": "visual", "media_type": "hologram"},
        )
        self.assertEqual(unknown.status_code, 422)

    @unittest.skipIf(FFMPEG is None, "bundled ffmpeg unavailable")
    def test_project_to_assembly_mp4_with_no_model_weights(self) -> None:
        project_id, story_id = self._project_story_with_template_scenes()
        scene_ids = [scene["id"] for scene in self._scenes(story_id)]

        # Nothing is bound yet, and assembly refuses to guess.
        early = self.client.post(f"/stories/{story_id}/assemble", json={})
        self.assertEqual(early.status_code, 409)
        for scene_id in scene_ids:
            self.assertIn(scene_id, early.json()["detail"])

        job_ids = {
            scene_id: self._generate_procedural_visual(story_id, scene_id)
            for scene_id in scene_ids
        }
        self._drain()

        # Every job succeeded through the ordinary lifecycle...
        for scene_id, job_id in job_ids.items():
            job = self.client.get(f"/jobs/{job_id}").json()
            self.assertEqual(job["status"], "succeeded", job.get("error_message"))

        # ...and each asset is bound to its own scene's visual role.
        detail = self.client.get(f"/stories/{story_id}").json()
        self.assertEqual(
            [
                entry
                for entry in detail["missing_assets"]
                if entry["role"] == "visual"
            ],
            [],
        )
        gallery = self.client.get(
            "/gallery", params={"project_id": project_id, "media_type": "video"}
        ).json()
        gallery_by_job = {item["job_id"]: item for item in gallery}
        visual_asset_ids: dict[str, str] = {}
        for scene in detail["story"]["scenes"]:
            asset_id = scene["asset_ids"]["visual"]
            visual_asset_ids[scene["id"]] = asset_id
            self.assertIn(job_ids[scene["id"]], scene["job_ids"])
            # Gallery sees exactly this asset, as a procedural clip in the
            # story's project.
            item = gallery_by_job[job_ids[scene["id"]]]
            self.assertEqual(item["asset_id"], asset_id)
            self.assertEqual(item["model_id"], PROCEDURAL_VISUAL_MODEL_ID)
            self.assertTrue(item["output_path"].endswith(".gif"))
            self.assertTrue(Path(item["output_path"]).is_file())
        self.assertEqual(len(set(visual_asset_ids.values())), SCENE_COUNT)

        # Assembly consumes the bound visuals and produces the MP4.
        assembled = self.client.post(
            f"/stories/{story_id}/assemble",
            json={"width": 320, "height": 180, "fps": 8},
        )
        self.assertEqual(assembled.status_code, 201, assembled.text)
        timeline = self.client.get(
            f"/jobs/{assembled.json()['job_id']}"
        ).json()["request"]["params"]["timeline"]
        self.assertEqual(
            [entry["asset_id"] for entry in timeline["tracks"]["visual"]],
            [visual_asset_ids[scene_id] for scene_id in scene_ids],
        )
        self._drain()

        job = self.client.get(f"/jobs/{assembled.json()['job_id']}").json()
        self.assertEqual(job["status"], "succeeded", job.get("error_message"))
        mp4_items = [
            item
            for item in self.client.get(
                "/gallery", params={"project_id": project_id, "media_type": "video"}
            ).json()
            if item["job_id"] == assembled.json()["job_id"]
        ]
        self.assertEqual(len(mp4_items), 1)
        output = Path(mp4_items[0]["output_path"])
        self.assertEqual(output.suffix, ".mp4")
        self.assertGreater(output.stat().st_size, 0)
        probe = subprocess.run(
            [FFMPEG, "-i", str(output), "-hide_banner"],
            capture_output=True,
            text=True,
        )
        self.assertIn("Video:", probe.stderr)

    def test_gallery_rerun_of_a_scene_visual_rebinds_that_scene(self) -> None:
        project_id, story_id = self._project_story_with_template_scenes()
        scene_id = self._scenes(story_id)[0]["id"]
        first_job = self._generate_procedural_visual(story_id, scene_id)
        self._drain()
        first_asset = next(
            scene["asset_ids"]["visual"]
            for scene in self._scenes(story_id)
            if scene["id"] == scene_id
        )

        reuse = self.client.post(
            f"/gallery/{first_asset}/reuse", json={"action": "rerun"}
        )
        self.assertEqual(reuse.status_code, 201, reuse.text)
        self.assertNotEqual(reuse.json()["job_id"], first_job)
        self._drain()

        rerun = self.client.get(f"/jobs/{reuse.json()['job_id']}").json()
        self.assertEqual(rerun["status"], "succeeded", rerun.get("error_message"))
        scene = next(s for s in self._scenes(story_id) if s["id"] == scene_id)
        # Newest attempt wins under the existing binding semantics.
        self.assertNotEqual(scene["asset_ids"]["visual"], first_asset)
        self.assertIn(reuse.json()["job_id"], scene["job_ids"])

    def test_unavailable_procedural_model_fails_and_binds_nothing(self) -> None:
        _, story_id = self._project_story_with_template_scenes()
        scene_id = self._scenes(story_id)[0]["id"]

        response = self.client.post(
            f"/stories/{story_id}/scenes/{scene_id}/generate",
            json={
                "role": "visual",
                "media_type": "video",
                "model_id": "no-such-storyboard-model",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self._drain()

        job = self.client.get(f"/jobs/{response.json()['job_id']}").json()
        # It really was the procedural video path that failed, not a stray image.
        self.assertEqual(job["media_type"], "video")
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["error_message"])
        detail = self.client.get(f"/stories/{story_id}").json()
        scene = next(s for s in detail["story"]["scenes"] if s["id"] == scene_id)
        self.assertEqual(scene["asset_ids"], {})
        status_entry = next(
            entry
            for entry in detail["asset_status"]
            if entry["scene_id"] == scene_id and entry["role"] == "visual"
        )
        self.assertEqual(status_entry["state"], "failed")
        self.assertIn(
            {"scene_id": scene_id, "role": "visual"}, detail["missing_assets"]
        )

    def test_a_failed_render_does_not_bind_and_assembly_still_refuses(self) -> None:
        _, story_id = self._project_story_with_template_scenes()
        scene_ids = [scene["id"] for scene in self._scenes(story_id)]

        # Scene 1 renders; scene 2's render is forced to fail.
        good = self._generate_procedural_visual(story_id, scene_ids[0])
        self._drain()
        self.assertEqual(self.client.get(f"/jobs/{good}").json()["status"], "succeeded")

        generator = self.services.generator_registry.get("video", "text-to-video")
        with mock.patch.object(
            type(generator), "generate", side_effect=RuntimeError("render exploded")
        ):
            bad = self._generate_procedural_visual(story_id, scene_ids[1])
            self._drain()
        self.assertEqual(self.client.get(f"/jobs/{bad}").json()["status"], "failed")

        scenes = {scene["id"]: scene for scene in self._scenes(story_id)}
        self.assertIn("visual", scenes[scene_ids[0]]["asset_ids"])
        self.assertEqual(scenes[scene_ids[1]]["asset_ids"], {})

        refused = self.client.post(f"/stories/{story_id}/assemble", json={})
        self.assertEqual(refused.status_code, 409)
        self.assertIn(scene_ids[1], refused.json()["detail"])
        self.assertNotIn(scene_ids[0], refused.json()["detail"])


if __name__ == "__main__":
    unittest.main()
