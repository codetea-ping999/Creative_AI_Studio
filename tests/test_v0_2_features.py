"""Tests for v0.2 new features."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime

try:
    from core.projects import Project, ProjectRepository
    from core.feedback import Feedback, FeedbackRepository
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


if __name__ == "__main__":
    unittest.main()
