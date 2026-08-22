"""API and service-graph coverage for speech and timeline assembly."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import (
        create_application_services,
        create_default_assembly_generator,
        create_default_speech_generator,
        create_default_text_generator,
        create_default_video_generator,
    )
    from core.assets import Asset
    from core.models import (
        BaseSpeechLoader,
        KokoroTtsLoader,
        VoicevoxHttpLoader,
    )
    from generators.audio import AudioGenerator, SpeechGenerator
    from generators.video import AssemblyGenerator, VideoGenerator
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class MediaGenerationApiTests(unittest.TestCase):
    def test_registry_keeps_defaults_and_adds_dedicated_generators(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )

            registry = services.generator_registry
            self.assertIsInstance(registry.get("audio"), AudioGenerator)
            self.assertIsInstance(
                registry.get("audio", "text-to-speech"),
                SpeechGenerator,
            )
            self.assertIsInstance(registry.get("video"), VideoGenerator)
            assembly = registry.get("video", "assembly")
            self.assertIsInstance(assembly, AssemblyGenerator)
            self.assertFalse(assembly.allow_direct_paths)

            self.assertIs(registry.get("audio"), registry.get("audio", "text-to-music"))
            self.assertIs(registry.get("video"), registry.get("video", "text-to-video"))
            self.assertIs(registry.get("text"), registry.get("text", "story"))
            self.assertIs(registry.get("image"), registry.get("image", "text-to-image"))
            with self.assertRaisesRegex(ValueError, "text-to-spech"):
                registry.get("audio", "text-to-spech")

            source = root / "source.png"
            source.write_bytes(b"registered source")
            services.asset_repository.create_or_update(
                Asset(
                    id="asset_visual",
                    job_id="job_visual",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="source",
                    prompt="source",
                    model_id="test",
                    path=str(source),
                )
            )
            self.assertEqual(assembly.asset_path_lookup("asset_visual"), str(source))
            self.assertIsNone(assembly.asset_path_lookup("asset_missing"))

    def test_speech_endpoint_rejects_invalid_postprocess_before_enqueueing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            for invalid_value in (None, "false", 1):
                with self.subTest(postprocess=invalid_value):
                    response = client.post(
                        "/generate/speech",
                        json={
                            "prompt": "静かな夜明けだった。",
                            "model_id": "kokoro-tts-local",
                            "output_format": "wav",
                            "params": {"postprocess": invalid_value},
                        },
                    )
                    self.assertEqual(response.status_code, 422, response.text)

            # None of the invalid requests should have reached the job queue.
            self.assertEqual(services.job_repository.list(), [])

    def test_speech_and_assembly_endpoints_bind_jobs_to_a_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))
            project_id = client.post(
                "/projects",
                json={"name": "Narrated short"},
            ).json()["id"]
            visual_path = root / "registered-visual.png"
            visual_path.write_bytes(b"registered source")
            services.asset_repository.create_or_update(
                Asset(
                    id="asset_visual",
                    job_id="job_visual",
                    project_id=project_id,
                    media_type="image",
                    kind="output",
                    title="source",
                    prompt="source",
                    model_id="test",
                    path=str(visual_path),
                )
            )

            speech = client.post(
                "/generate/speech",
                json={
                    "prompt": "静かな夜明けだった。",
                    "model_id": "kokoro-tts-local",
                    "project_id": project_id,
                    "output_format": "wav",
                    "params": {"voice": "jm_kumo"},
                },
            )
            self.assertEqual(speech.status_code, 201, speech.text)

            assembly = client.post(
                "/generate/assembly",
                json={
                    "prompt": "assemble the narrated short",
                    "project_id": project_id,
                    "output_format": "mp4",
                    "params": {
                        "timeline": {
                            "fps": 8,
                            "tracks": {
                                "visual": [
                                    {
                                        "scene_id": "scene_1",
                                        "asset_id": "asset_visual",
                                        "path": "/tmp/caller-controlled.png",
                                        "duration_seconds": 1,
                                    }
                                ],
                                "narration": [],
                                "music": [],
                                "subtitles": [],
                            },
                        }
                    },
                },
            )
            self.assertEqual(assembly.status_code, 201, assembly.text)

            speech_job = client.get(
                f"/jobs/{speech.json()['job_id']}"
            ).json()
            self.assertEqual(speech_job["media_type"], "audio")
            self.assertEqual(speech_job["request"]["task_type"], "text-to-speech")
            self.assertEqual(speech_job["project_id"], project_id)

            assembly_job = client.get(
                f"/jobs/{assembly.json()['job_id']}"
            ).json()
            self.assertEqual(assembly_job["media_type"], "video")
            self.assertEqual(assembly_job["request"]["task_type"], "assembly")
            self.assertEqual(assembly_job["project_id"], project_id)
            visual_entry = assembly_job["request"]["params"]["timeline"]["tracks"]["visual"][0]
            self.assertNotIn("path", visual_entry)

            project = client.get(f"/projects/{project_id}").json()
            self.assertCountEqual(
                project["job_ids"],
                [speech_job["id"], assembly_job["id"]],
            )

    def test_assembly_rejects_cross_project_and_unassigned_assets(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))
            project_a = client.post("/projects", json={"name": "A"}).json()["id"]
            project_b = client.post("/projects", json={"name": "B"}).json()["id"]
            source = root / "source.png"
            source.write_bytes(b"source")

            for asset_id, project_id in (
                ("asset_project_a", project_a),
                ("asset_unassigned", None),
            ):
                services.asset_repository.create_or_update(
                    Asset(
                        id=asset_id,
                        job_id=f"job_{asset_id}",
                        project_id=project_id,
                        media_type="image",
                        kind="output",
                        title=asset_id,
                        prompt="source",
                        model_id="test",
                        path=str(source),
                    )
                )

            for asset_id in ("asset_project_a", "asset_unassigned"):
                with self.subTest(asset_id=asset_id):
                    response = client.post(
                        "/generate/assembly",
                        json=_assembly_payload(asset_id, project_id=project_b),
                    )
                    self.assertEqual(response.status_code, 404, response.text)

            self.assertEqual(services.job_repository.list(), [])

    def test_unassigned_assembly_accepts_only_repo_assets_and_strips_paths(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))
            source = root / "source.png"
            source.write_bytes(b"source")
            services.asset_repository.create_or_update(
                Asset(
                    id="asset_unassigned",
                    job_id="job_source",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="source",
                    prompt="source",
                    model_id="test",
                    path=str(source),
                )
            )

            response = client.post(
                "/generate/assembly",
                json=_assembly_payload(
                    "asset_unassigned",
                    direct_path="/etc/passwd",
                ),
            )
            self.assertEqual(response.status_code, 201, response.text)
            job = services.job_repository.get(response.json()["job_id"])
            self.assertIsNotNone(job)
            visual = job.request.params["timeline"]["tracks"]["visual"][0]
            self.assertNotIn("path", visual)
            self.assertEqual(visual["asset_id"], "asset_unassigned")

            raw_only = _assembly_payload("asset_unassigned")
            raw_only["params"]["timeline"]["tracks"]["visual"][0].pop("asset_id")
            raw_only["params"]["timeline"]["tracks"]["visual"][0]["path"] = str(source)
            response = client.post("/generate/assembly", json=raw_only)
            self.assertEqual(response.status_code, 422, response.text)

            response = client.post(
                "/generate/assembly",
                json=_assembly_payload("asset_missing"),
            )
            self.assertEqual(response.status_code, 404, response.text)

    def test_new_factories_and_speech_loaders_are_public(self) -> None:
        self.assertTrue(callable(create_default_assembly_generator))
        self.assertTrue(callable(create_default_speech_generator))
        self.assertTrue(callable(create_default_text_generator))
        self.assertTrue(callable(create_default_video_generator))
        self.assertTrue(issubclass(KokoroTtsLoader, BaseSpeechLoader))
        self.assertTrue(issubclass(VoicevoxHttpLoader, BaseSpeechLoader))


def _assembly_payload(
    asset_id: str,
    *,
    project_id: str | None = None,
    direct_path: str | None = None,
) -> dict:
    visual: dict[str, object] = {
        "scene_id": "scene_1",
        "asset_id": asset_id,
        "duration_seconds": 1,
    }
    if direct_path is not None:
        visual["path"] = direct_path
    return {
        "prompt": "assemble",
        "project_id": project_id,
        "output_format": "mp4",
        "params": {
            "timeline": {
                "fps": 8,
                "tracks": {
                    "visual": [visual],
                    "narration": [],
                    "music": [],
                    "subtitles": [],
                },
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
