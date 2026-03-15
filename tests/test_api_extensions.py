"""Extended API coverage for gallery, projects, feedback, and video generation."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ApiExtensionTests(unittest.TestCase):
    def test_generate_image_and_audio_accept_project_binding(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            project_id = client.post(
                "/projects",
                json={"name": "Cross Media Project"},
            ).json()["id"]

            image_response = client.post(
                "/generate/image",
                json={
                    "prompt": "clean product shot",
                    "model_id": "sdxl",
                    "project_id": project_id,
                    "output_format": "png",
                    "params": {"width": 512, "height": 512, "steps": 8},
                },
            )
            audio_response = client.post(
                "/generate/audio",
                json={
                    "prompt": "bright synth loop",
                    "model_id": "musicgen-small",
                    "project_id": project_id,
                    "output_format": "wav",
                    "params": {"duration_seconds": 4, "bpm": 120, "mood": "bright"},
                },
            )

            self.assertEqual(image_response.status_code, 201)
            self.assertEqual(audio_response.status_code, 201)

            project_jobs = client.get(f"/projects/{project_id}/jobs").json()
            self.assertEqual(project_jobs["job_count"], 2)
            self.assertEqual(project_jobs["media_breakdown"], {"audio": 1, "image": 1})

    def test_generate_video_job_runs_end_to_end_and_appears_in_gallery(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "cinematic aerial shot of tokyo at dusk",
                    "model_id": "storyboard-video",
                    "output_format": "gif",
                    "params": {
                        "duration_seconds": 3,
                        "fps": 6,
                        "camera_motion": "push-in",
                    },
                },
            )

            self.assertEqual(create_response.status_code, 201)
            job_id = create_response.json()["job_id"]

            processed_job = services.job_runner.run_once()

            self.assertIsNotNone(processed_job)
            assert processed_job is not None
            self.assertEqual(processed_job.id, job_id)
            self.assertEqual(processed_job.status, "succeeded")
            self.assertTrue(Path(processed_job.result.outputs[0]).exists())
            self.assertEqual(processed_job.result.metadata["output_format"], "gif")

            gallery_response = client.get("/gallery?media_type=video")
            self.assertEqual(gallery_response.status_code, 200)
            gallery_items = gallery_response.json()
            self.assertEqual(len(gallery_items), 1)
            self.assertEqual(gallery_items[0]["job_id"], job_id)
            self.assertIsInstance(gallery_items[0]["quality_level"], str)
            self.assertGreater(gallery_items[0]["quality_score"], 0)
            self.assertEqual(gallery_items[0]["preview_path"], processed_job.result.outputs[0])

    def test_project_job_binding_and_feedback_summary(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            project_response = client.post(
                "/projects",
                json={"name": "Storyboard Sprint", "description": "motion exploration"},
            )
            self.assertEqual(project_response.status_code, 201)
            project_id = project_response.json()["id"]

            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "editorial storyboard for a fashion campaign",
                    "model_id": "storyboard-video",
                    "project_id": project_id,
                    "output_format": "gif",
                    "params": {
                        "duration_seconds": 2,
                        "fps": 6,
                        "visual_style": "editorial-board",
                    },
                },
            )
            self.assertEqual(create_response.status_code, 201)
            job_id = create_response.json()["job_id"]

            processed_job = services.job_runner.run_once()
            self.assertIsNotNone(processed_job)

            project_jobs_response = client.get(f"/projects/{project_id}/jobs")
            self.assertEqual(project_jobs_response.status_code, 200)
            payload = project_jobs_response.json()
            self.assertEqual(payload["job_count"], 1)
            self.assertEqual(payload["media_breakdown"], {"video": 1})
            self.assertEqual(payload["jobs"][0]["project_id"], project_id)

            job_response = client.get(f"/jobs/{job_id}")
            self.assertEqual(job_response.status_code, 200)
            self.assertEqual(job_response.json()["project_id"], project_id)

            feedback_response = client.post(
                "/feedback",
                json={
                    "job_id": job_id,
                    "quality_rating": 4,
                    "semantic_rating": 5,
                    "comments": "usable storyboard pass",
                },
            )
            self.assertEqual(feedback_response.status_code, 201)

            summary_response = client.get(f"/feedback/summary?job_id={job_id}")
            self.assertEqual(summary_response.status_code, 200)
            self.assertEqual(
                summary_response.json(),
                {
                    "total_feedback": 1,
                    "average_quality_rating": 4.0,
                    "average_semantic_rating": 5.0,
                    "comment_count": 1,
                    "latest_feedback_at": feedback_response.json()["created_at"],
                },
            )

    def test_feedback_requires_known_job_and_gallery_supports_project_filter(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            missing_feedback_response = client.post(
                "/feedback",
                json={"job_id": "job_missing", "quality_rating": 3},
            )
            self.assertEqual(missing_feedback_response.status_code, 404)

            project_id = client.post("/projects", json={"name": "Filter Test"}).json()["id"]
            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "moody alleyway storyboard",
                    "model_id": "storyboard-video",
                    "project_id": project_id,
                    "output_format": "gif",
                    "params": {"duration_seconds": 2, "fps": 6},
                },
            )
            self.assertEqual(create_response.status_code, 201)
            job_id = create_response.json()["job_id"]
            services.job_runner.run_once()

            filtered_gallery = client.get(f"/gallery?project_id={project_id}&q=alleyway")
            self.assertEqual(filtered_gallery.status_code, 200)
            payload = filtered_gallery.json()
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["project_id"], project_id)

    def test_rerun_job_clones_request_and_can_move_to_another_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            source_project_id = client.post("/projects", json={"name": "Source"}).json()["id"]
            target_project_id = client.post("/projects", json={"name": "Target"}).json()["id"]

            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "foggy harbor storyboard",
                    "model_id": "storyboard-video",
                    "project_id": source_project_id,
                    "output_format": "gif",
                    "params": {"duration_seconds": 2, "fps": 6},
                },
            )
            self.assertEqual(create_response.status_code, 201)
            source_job_id = create_response.json()["job_id"]
            services.job_runner.run_once()

            rerun_response = client.post(
                f"/jobs/{source_job_id}/rerun",
                json={
                    "prompt": "foggy harbor storyboard, sunrise variation",
                    "project_id": target_project_id,
                    "params": {"duration_seconds": 3, "fps": 6, "visual_style": "animatic"},
                },
            )
            self.assertEqual(rerun_response.status_code, 201)
            rerun_job_id = rerun_response.json()["job_id"]

            rerun_job = client.get(f"/jobs/{rerun_job_id}").json()
            self.assertEqual(rerun_job["project_id"], target_project_id)
            self.assertEqual(rerun_job["request"]["prompt"], "foggy harbor storyboard, sunrise variation")
            self.assertEqual(rerun_job["request"]["params"]["duration_seconds"], 3)
            self.assertEqual(rerun_job["request"]["params"]["visual_style"], "animatic")

            source_project = client.get(f"/projects/{source_project_id}/jobs").json()
            target_project = client.get(f"/projects/{target_project_id}/jobs").json()
            self.assertEqual(source_project["job_count"], 1)
            self.assertEqual(target_project["job_count"], 1)


if __name__ == "__main__":
    unittest.main()
