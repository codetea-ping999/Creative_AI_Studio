"""Extended API coverage for gallery, projects, feedback, and video generation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
    def test_api_cors_uses_the_configured_web_port(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            with patch.dict(os.environ, {"WEB_PORT": "5174"}):
                client = TestClient(create_app(services, start_job_runner=False))

            response = client.options(
                "/health",
                headers={
                    "Origin": "http://127.0.0.1:5174",
                    "Access-Control-Request-Method": "GET",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], "http://127.0.0.1:5174")

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
            summary_payload = summary_response.json()
            self.assertEqual(summary_payload["total_feedback"], 1)
            self.assertEqual(summary_payload["average_quality_rating"], 4.0)
            self.assertEqual(summary_payload["average_semantic_rating"], 5.0)
            self.assertEqual(summary_payload["average_creative_rating"], None)
            self.assertEqual(summary_payload["comment_count"], 1)
            self.assertEqual(summary_payload["export_ready_rate"], 0.0)
            self.assertEqual(summary_payload["reuse_intent_rate"], 0.0)
            self.assertEqual(summary_payload["issue_tag_counts"], {})
            self.assertEqual(summary_payload["human_quality_score"], 80.0)
            self.assertEqual(summary_payload["human_semantic_alignment_score"], 100.0)
            self.assertEqual(summary_payload["human_creative_alignment_score"], None)
            self.assertEqual(summary_payload["latest_feedback_at"], feedback_response.json()["created_at"])

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

    def test_remove_job_rejects_foreign_project_and_preserves_bindings(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            source_project_id = client.post("/projects", json={"name": "Source"}).json()["id"]
            other_project_id = client.post("/projects", json={"name": "Other"}).json()["id"]

            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "nighttime city storyboard",
                    "model_id": "storyboard-video",
                    "project_id": source_project_id,
                    "output_format": "gif",
                    "params": {"duration_seconds": 2, "fps": 6},
                },
            )
            self.assertEqual(create_response.status_code, 201)
            job_id = create_response.json()["job_id"]

            services.job_runner.run_once()
            asset = services.asset_repository.get_primary_by_job(job_id)

            self.assertIsNotNone(asset)
            assert asset is not None

            remove_response = client.delete(f"/projects/{other_project_id}/jobs/{job_id}")
            self.assertEqual(remove_response.status_code, 404)
            self.assertEqual(remove_response.json()["detail"], "Job not found in project")

            job_payload = client.get(f"/jobs/{job_id}").json()
            self.assertEqual(job_payload["project_id"], source_project_id)

            asset_payload = client.get(f"/gallery/{asset.id}").json()
            self.assertEqual(asset_payload["project_id"], source_project_id)

            source_project = client.get(f"/projects/{source_project_id}/jobs").json()
            other_project = client.get(f"/projects/{other_project_id}/jobs").json()
            self.assertEqual(source_project["job_count"], 1)
            self.assertEqual(source_project["jobs"][0]["id"], job_id)
            self.assertEqual(other_project["job_count"], 0)

    def test_remove_job_from_project_clears_job_and_asset_bindings(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            project_id = client.post("/projects", json={"name": "Bound Project"}).json()["id"]
            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "graphic opening scene",
                    "model_id": "storyboard-video",
                    "project_id": project_id,
                    "output_format": "gif",
                    "params": {"duration_seconds": 2, "fps": 6},
                },
            )
            self.assertEqual(create_response.status_code, 201)
            job_id = create_response.json()["job_id"]

            services.job_runner.run_once()
            asset = services.asset_repository.get_primary_by_job(job_id)

            self.assertIsNotNone(asset)
            assert asset is not None

            remove_response = client.delete(f"/projects/{project_id}/jobs/{job_id}")
            self.assertEqual(remove_response.status_code, 200)
            self.assertEqual(remove_response.json()["job_count"], 0)

            job_payload = client.get(f"/jobs/{job_id}").json()
            self.assertEqual(job_payload["project_id"], None)

            asset_payload = client.get(f"/gallery/{asset.id}").json()
            self.assertEqual(asset_payload["project_id"], None)

            project_assets = client.get(f"/projects/{project_id}/assets").json()
            self.assertEqual(project_assets, [])

    def test_sync_job_is_idempotent_for_existing_assets(self) -> None:
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
                    "prompt": "abstract motion board",
                    "model_id": "storyboard-video",
                    "output_format": "gif",
                    "params": {"duration_seconds": 2, "fps": 6},
                },
            )
            self.assertEqual(create_response.status_code, 201)
            job_id = create_response.json()["job_id"]

            services.job_runner.run_once()
            asset = services.asset_repository.get_primary_by_job(job_id)
            job = services.job_repository.get(job_id)

            self.assertIsNotNone(asset)
            self.assertIsNotNone(job)
            assert asset is not None
            assert job is not None

            updated_at_before = asset.updated_at.isoformat()

            first_sync = services.asset_repository.sync_job(job)
            second_sync = services.asset_repository.sync_job(job)
            asset_after = services.asset_repository.get(asset.id)

            self.assertEqual(first_sync[0].id, asset.id)
            self.assertEqual(second_sync[0].id, asset.id)
            self.assertIsNotNone(asset_after)
            assert asset_after is not None
            self.assertEqual(asset_after.updated_at.isoformat(), updated_at_before)

    def test_gallery_and_project_reads_do_not_mutate_asset_timestamps_or_order(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            project_id = client.post("/projects", json={"name": "Read Only"}).json()["id"]
            for prompt in ("first storyboard frame", "second storyboard frame"):
                create_response = client.post(
                    "/generate/video",
                    json={
                        "prompt": prompt,
                        "model_id": "storyboard-video",
                        "project_id": project_id,
                        "output_format": "gif",
                        "params": {"duration_seconds": 2, "fps": 6},
                    },
                )
                self.assertEqual(create_response.status_code, 201)
                services.job_runner.run_once()

            assets_before = services.asset_repository.list_all(project_id=project_id)
            order_before = [asset.id for asset in assets_before]
            updated_before = {
                asset.id: asset.updated_at.isoformat()
                for asset in assets_before
            }

            gallery_response = client.get(f"/gallery?project_id={project_id}&media_type=video")
            project_assets_response = client.get(f"/projects/{project_id}/assets")

            self.assertEqual(gallery_response.status_code, 200)
            self.assertEqual(project_assets_response.status_code, 200)
            self.assertEqual(
                [item["asset_id"] for item in gallery_response.json()],
                order_before,
            )
            self.assertEqual(
                [item["asset_id"] for item in project_assets_response.json()],
                order_before,
            )

            assets_after = services.asset_repository.list_all(project_id=project_id)
            order_after = [asset.id for asset in assets_after]
            updated_after = {
                asset.id: asset.updated_at.isoformat()
                for asset in assets_after
            }

            self.assertEqual(order_after, order_before)
            self.assertEqual(updated_after, updated_before)

    def test_gallery_asset_reuse_export_and_project_binding_workflows(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            source_project_id = client.post("/projects", json={"name": "Source Project"}).json()["id"]
            target_project_id = client.post("/projects", json={"name": "Target Project"}).json()["id"]

            create_response = client.post(
                "/generate/video",
                json={
                    "prompt": "graphic storyboard for a launch trailer",
                    "model_id": "storyboard-video",
                    "project_id": source_project_id,
                    "output_format": "gif",
                    "seed": 42,
                    "params": {"duration_seconds": 2, "fps": 6, "visual_style": "animatic"},
                },
            )
            self.assertEqual(create_response.status_code, 201)
            source_job_id = create_response.json()["job_id"]

            processed_job = services.job_runner.run_once()
            self.assertIsNotNone(processed_job)

            gallery_items = client.get("/gallery?media_type=video").json()
            self.assertEqual(len(gallery_items), 1)
            source_asset_id = gallery_items[0]["asset_id"]
            self.assertEqual(gallery_items[0]["project_id"], source_project_id)

            detail_response = client.get(f"/gallery/{source_asset_id}")
            self.assertEqual(detail_response.status_code, 200)
            detail_payload = detail_response.json()
            self.assertEqual(detail_payload["request_snapshot"]["prompt"], "graphic storyboard for a launch trailer")
            self.assertEqual(detail_payload["project_id"], source_project_id)

            reuse_response = client.post(
                f"/gallery/{source_asset_id}/reuse",
                json={
                    "action": "variation",
                    "prompt": "graphic storyboard for a launch trailer, brighter color pass",
                    "project_id": target_project_id,
                    "params": {
                        "duration_seconds": 3,
                        "camera_motion": "orbit",
                        "review_issue_tags": ["color_lighting"],
                        "review_source": "quick-review",
                    },
                },
            )
            self.assertEqual(reuse_response.status_code, 201)
            reuse_payload = reuse_response.json()
            self.assertEqual(reuse_payload["project_id"], target_project_id)

            reused_job = client.get(f"/jobs/{reuse_payload['job_id']}").json()
            self.assertEqual(reused_job["project_id"], target_project_id)
            self.assertEqual(reused_job["request"]["seed"], 42)
            self.assertEqual(reused_job["request"]["params"]["reuse_action"], "variation")
            self.assertEqual(
                reused_job["request"]["params"]["source_asset_id"],
                source_asset_id,
            )
            self.assertEqual(
                reused_job["request"]["params"]["reference_asset_path"],
                detail_payload["output_path"],
            )
            self.assertEqual(
                reused_job["request"]["params"]["review_issue_tags"],
                ["color_lighting"],
            )
            self.assertEqual(
                reused_job["request"]["params"]["review_source"],
                "quick-review",
            )

            with patch("apps.api.routes.gallery.secrets.randbits", return_value=987654321):
                rerun_response = client.post(
                    f"/gallery/{source_asset_id}/reuse",
                    json={
                        "action": "rerun",
                        "project_id": target_project_id,
                    },
                )
            self.assertEqual(rerun_response.status_code, 201)
            rerun_job = client.get(f"/jobs/{rerun_response.json()['job_id']}").json()
            self.assertEqual(rerun_job["request"]["seed"], 987654321)
            self.assertEqual(rerun_job["request"]["params"]["reuse_action"], "rerun")

            with patch("apps.api.routes.gallery.secrets.randbits", return_value=123456789):
                default_reuse_response = client.post(f"/gallery/{source_asset_id}/reuse", json={})
            self.assertEqual(default_reuse_response.status_code, 201)
            default_reuse_job = client.get(
                f"/jobs/{default_reuse_response.json()['job_id']}"
            ).json()
            self.assertEqual(default_reuse_job["project_id"], source_project_id)
            self.assertEqual(default_reuse_job["request"]["seed"], 123456789)
            self.assertEqual(default_reuse_job["request"]["params"]["reuse_action"], "rerun")

            invalid_action_response = client.post(
                f"/gallery/{source_asset_id}/reuse",
                json={"action": "repeat"},
            )
            self.assertEqual(invalid_action_response.status_code, 422)

            original_after_reuse = client.get(f"/gallery/{source_asset_id}").json()
            self.assertEqual(original_after_reuse["project_id"], source_project_id)
            self.assertEqual(original_after_reuse["reuse_count"], 3)

            services.job_runner.run_once()
            rerun_processed_job = services.job_runner.run_once()
            self.assertIsNotNone(rerun_processed_job)
            assert rerun_processed_job is not None
            self.assertEqual(rerun_processed_job.id, rerun_response.json()["job_id"])
            self.assertNotEqual(
                Path(processed_job.result.outputs[0]).read_bytes(),
                Path(rerun_processed_job.result.outputs[0]).read_bytes(),
            )
            target_gallery = client.get(f"/gallery?project_id={target_project_id}&media_type=video").json()
            self.assertEqual(len(target_gallery), 2)
            self.assertTrue(all(item["project_id"] == target_project_id for item in target_gallery))

            export_response = client.post(
                f"/gallery/{source_asset_id}/export",
                json={
                    "destination_dir": str(root / "exports" / "video"),
                    "destination_name": "launch-trailer-source.gif",
                    "include_metadata": True,
                },
            )
            self.assertEqual(export_response.status_code, 200)
            export_payload = export_response.json()
            self.assertTrue(Path(export_payload["export_path"]).exists())
            self.assertTrue(Path(export_payload["metadata_path"]).exists())

            rebound_response = client.patch(
                f"/gallery/{source_asset_id}/project",
                json={"project_id": target_project_id},
            )
            self.assertEqual(rebound_response.status_code, 200)
            rebound_payload = rebound_response.json()
            self.assertEqual(rebound_payload["project_id"], target_project_id)

            source_project_jobs = client.get(f"/projects/{source_project_id}/jobs").json()
            target_project_jobs = client.get(f"/projects/{target_project_id}/jobs").json()
            self.assertEqual(source_project_jobs["job_count"], 1)
            self.assertEqual(target_project_jobs["job_count"], 3)

            feedback_response = client.post(
                "/feedback",
                json={
                    "job_id": source_job_id,
                    "asset_id": source_asset_id,
                    "project_id": target_project_id,
                    "quality_rating": 5,
                    "semantic_rating": 4,
                    "creative_rating": 4,
                    "comments": "ready for export",
                },
            )
            self.assertEqual(feedback_response.status_code, 201)

            project_export_response = client.post(
                f"/projects/{target_project_id}/export",
                json={"destination_dir": str(root / "exports" / "project-bundle")},
            )
            self.assertEqual(project_export_response.status_code, 200)
            project_manifest_path = Path(project_export_response.json()["manifest_path"])
            self.assertTrue(project_manifest_path.exists())
            project_manifest = json.loads(project_manifest_path.read_text(encoding="utf-8"))
            self.assertIn("quality_summary", project_manifest["project"])
            self.assertIn("feedback_summary", project_manifest["project"])
            self.assertEqual(project_manifest["project"]["feedback_summary"]["total_feedback"], 1)
            self.assertEqual(project_manifest["project"]["asset_count"], 3)


if __name__ == "__main__":
    unittest.main()
