"""Tests for v0.2 new features."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime

try:
    from core.assets import AssetRepository
    from core.jobs import JobRecord
    from core.projects import Project, ProjectRepository
    from core.feedback import Feedback, FeedbackRepository
    from core.schemas import GenerationRequest, GenerationResult
except ModuleNotFoundError as exc:
    import sys
    print(f"Import error: {exc}")
    sys.exit(1)


class ProjectRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self.repo = ProjectRepository(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_create_project(self):
        project = self.repo.create("Test Project", "A test project")
        self.assertEqual(project.name, "Test Project")
        self.assertEqual(project.description, "A test project")
        self.assertEqual(project.job_ids, [])

    def test_get_project(self):
        created = self.repo.create("Test", "")
        retrieved = self.repo.get(created.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, created.id)

    def test_add_job_to_project(self):
        project = self.repo.create("Test", "")
        updated = self.repo.add_job(project.id, "job123")
        self.assertIn("job123", updated.job_ids)

    def test_list_projects_sorted(self):
        p1 = self.repo.create("Project 1", "")
        p2 = self.repo.create("Project 2", "")
        projects = self.repo.list_all()
        # Should be sorted by updated_at, newest first
        self.assertEqual(projects[0].id, p2.id)
        self.assertEqual(projects[1].id, p1.id)

    def test_delete_project(self):
        project = self.repo.create("To Delete", "")
        self.assertTrue(self.repo.delete(project.id))
        self.assertIsNone(self.repo.get(project.id))

    def test_invalid_project_json_is_skipped(self):
        project = self.repo.create("Valid", "")
        (Path(self.tmpdir.name) / "broken.json").write_text("{", encoding="utf-8")

        projects = self.repo.list_all()

        self.assertEqual([item.id for item in projects], [project.id])

    def test_get_invalid_project_json_returns_none(self):
        project_file = Path(self.tmpdir.name) / "project-broken.json"
        project_file.write_text("", encoding="utf-8")

        self.assertIsNone(self.repo.get("project-broken"))

    def test_remove_job_returns_none_when_project_does_not_include_it(self):
        source = self.repo.create("Source", "")
        other = self.repo.create("Other", "")
        self.repo.add_job(source.id, "job123")

        removed = self.repo.remove_job(other.id, "job123")

        self.assertIsNone(removed)
        self.assertEqual(self.repo.get(source.id).job_ids, ["job123"])
        self.assertEqual(self.repo.get(other.id).job_ids, [])


class AssetRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self.asset_dir = Path(self.tmpdir.name) / "assets"
        self.output_dir = Path(self.tmpdir.name) / "outputs"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.repo = AssetRepository(self.asset_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_sync_job_is_idempotent_when_asset_state_is_unchanged(self):
        job = self._make_job("job-1", self.output_dir / "job-1.gif")

        first_asset = self.repo.sync_job(job)[0]
        second_asset = self.repo.sync_job(job)[0]
        reloaded_asset = self.repo.get(first_asset.id)

        self.assertEqual(second_asset.id, first_asset.id)
        self.assertIsNotNone(reloaded_asset)
        self.assertEqual(second_asset.updated_at, first_asset.updated_at)
        self.assertEqual(reloaded_asset.updated_at, first_asset.updated_at)

    def test_sync_jobs_preserves_asset_order_when_nothing_changed(self):
        first_job = self._make_job("job-1", self.output_dir / "job-1.gif")
        second_job = self._make_job("job-2", self.output_dir / "job-2.gif")

        self.repo.sync_job(first_job)
        self.repo.sync_job(second_job)

        order_before = [asset.id for asset in self.repo.list_all()]
        updated_before = {asset.id: asset.updated_at for asset in self.repo.list_all()}

        self.repo.sync_jobs([second_job, first_job])

        assets_after = self.repo.list_all()
        order_after = [asset.id for asset in assets_after]
        updated_after = {asset.id: asset.updated_at for asset in assets_after}

        self.assertEqual(order_after, order_before)
        self.assertEqual(updated_after, updated_before)

    def test_invalid_asset_json_is_skipped_and_can_be_resynced(self):
        job = self._make_job("job-1", self.output_dir / "job-1.gif")
        asset = self.repo.sync_job(job)[0]
        asset_path = self.asset_dir / f"{asset.id}.json"
        asset_path.write_text("", encoding="utf-8")

        self.assertIsNone(self.repo.get(asset.id))
        self.assertEqual(self.repo.list_all(), [])

        resynced = self.repo.sync_job(job)[0]

        self.assertEqual(resynced.id, asset.id)
        self.assertIsNotNone(self.repo.get(asset.id))

    def test_list_all_skips_unrelated_invalid_asset_json(self):
        job = self._make_job("job-1", self.output_dir / "job-1.gif")
        asset = self.repo.sync_job(job)[0]
        (self.asset_dir / "broken.json").write_text("{", encoding="utf-8")

        assets = self.repo.list_all()

        self.assertEqual([item.id for item in assets], [asset.id])

    def _make_job(self, job_id: str, output_path: Path) -> JobRecord:
        output_path.write_bytes(b"GIF89a")
        now = datetime.now()
        return JobRecord(
            id=job_id,
            project_id="project-1",
            media_type="video",
            status="succeeded",
            request=GenerationRequest(
                media_type="video",
                prompt=f"Prompt for {job_id}",
                model_id="storyboard-video",
                output_format="gif",
                params={"fps": 6},
            ),
            result=GenerationResult(
                job_id=job_id,
                status="succeeded",
                outputs=[str(output_path)],
                previews=[str(output_path)],
                metadata={
                    "output_format": "gif",
                    "quality_report": {"quality_score": 75.0},
                },
                error_message=None,
            ),
            progress=1.0,
            error_message=None,
            created_at=now,
            updated_at=now,
        )


class FeedbackRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self.repo = FeedbackRepository(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_create_feedback(self):
        feedback = self.repo.create("job123", quality_rating=4)
        self.assertEqual(feedback.job_id, "job123")
        self.assertEqual(feedback.quality_rating, 4)

    def test_create_feedback_with_semantic_rating(self):
        feedback = self.repo.create("job123", quality_rating=4, semantic_rating=5)
        self.assertEqual(feedback.semantic_rating, 5)

    def test_invalid_rating(self):
        with self.assertRaises(ValueError):
            self.repo.create("job123", quality_rating=6)  # Out of range

    def test_list_by_job(self):
        f1 = self.repo.create("job1", quality_rating=4)
        f2 = self.repo.create("job2", quality_rating=3)
        f3 = self.repo.create("job1", quality_rating=5)

        job1_feedbacks = self.repo.list_by_job("job1")
        self.assertEqual(len(job1_feedbacks), 2)

    def test_feedback_persistence(self):
        feedback = self.repo.create("job123", quality_rating=4, comments="Great!")
        retrieved = self.repo.get(feedback.id)
        self.assertEqual(retrieved.comments, "Great!")

    def test_delete_feedback(self):
        feedback = self.repo.create("job123", quality_rating=4)
        self.assertTrue(self.repo.delete(feedback.id))
        self.assertIsNone(self.repo.get(feedback.id))

    def test_invalid_feedback_json_is_skipped(self):
        feedback = self.repo.create("job123", quality_rating=4)
        (Path(self.tmpdir.name) / "broken.json").write_text("{", encoding="utf-8")

        feedbacks = self.repo.list_all()

        self.assertEqual([item.id for item in feedbacks], [feedback.id])

    def test_get_invalid_feedback_json_returns_none(self):
        feedback_file = Path(self.tmpdir.name) / "feedback-broken.json"
        feedback_file.write_text("", encoding="utf-8")

        self.assertIsNone(self.repo.get("feedback-broken"))


if __name__ == "__main__":
    unittest.main()
