"""API coverage for the v0.3 story, bible, and batch endpoints."""

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


class _Studio:
    """A live app plus its services, driven without a background runner."""

    def __init__(self, root: Path) -> None:
        self.services = create_application_services(
            db_path=root / "jobs.db",
            output_dir=root / "outputs" / "images",
        )
        self.client = TestClient(create_app(self.services, start_job_runner=False))

    def drain(self, limit: int = 64) -> int:
        """Run queued jobs synchronously so tests stay deterministic."""

        processed = 0
        while processed < limit and self.services.job_runner.run_once() is not None:
            processed += 1
        return processed


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class TextGenerationApiTests(unittest.TestCase):
    def test_generate_text_runs_end_to_end(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            response = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "時を巻き戻せる少女が最後の一日を選び直す",
                    "model_id": "template-writer",
                    "params": {"task": "scene_list", "scene_count": 3},
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            job_id = response.json()["job_id"]

            studio.drain()
            job = studio.client.get(f"/jobs/{job_id}").json()
            self.assertEqual(job["status"], "succeeded", job.get("error_message"))
            self.assertEqual(job["media_type"], "text")
            self.assertEqual(job["request"]["task_type"], "story")

            metadata = job["result"]["metadata"]
            self.assertEqual(metadata["story_task"], "scene_list")
            self.assertEqual(len(metadata["structured"]["scenes"]), 3)
            self.assertTrue(Path(job["result"]["outputs"][0]).exists())
            self.assertTrue(Path(metadata["structured_path"]).exists())

    def test_text_job_appears_in_gallery_and_metrics(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            studio.client.post(
                "/generate/text",
                json={
                    "prompt": "a heist in a rainy city",
                    "model_id": "template-writer",
                    "params": {"task": "logline", "count": 2},
                },
            )
            studio.drain()

            gallery = studio.client.get("/gallery", params={"media_type": "text"}).json()
            self.assertEqual(len(gallery), 1)
            self.assertEqual(gallery[0]["media_type"], "text")
            self.assertTrue(gallery[0]["output_path"].endswith(".md"))

            metrics = studio.client.get("/metrics/summary").json()
            self.assertIn("text", metrics["by_media"])
            self.assertEqual(metrics["by_media"]["text"]["total_jobs"], 1)

    def test_unknown_story_task_fails_the_job_with_a_useful_message(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "a premise",
                    "model_id": "template-writer",
                    "params": {"task": "sonnet"},
                },
            ).json()["job_id"]
            studio.drain()

            job = studio.client.get(f"/jobs/{job_id}").json()
            self.assertEqual(job["status"], "failed")
            self.assertIn("scene_list", job["error_message"])


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class BibleApiTests(unittest.TestCase):
    def _create_entry(self, studio: _Studio, **overrides) -> dict:
        payload = {
            "kind": "character",
            "name": "Mina",
            "prompt_fragment": "long black straight hair, purple eyes",
            "attributes": {"hair": "long black straight"},
            "locked_fields": ["hair"],
            "seed_policy": {"mode": "locked", "seed": 4242},
        }
        payload.update(overrides)
        response = studio.client.post("/bible", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_crud_round_trip(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            entry = self._create_entry(studio)

            fetched = studio.client.get(f"/bible/{entry['id']}").json()
            self.assertEqual(fetched["name"], "Mina")
            self.assertEqual(fetched["seed_policy"]["seed"], 4242)

            patched = studio.client.patch(
                f"/bible/{entry['id']}", json={"summary": "protagonist"}
            ).json()
            self.assertEqual(patched["summary"], "protagonist")

            listing = studio.client.get("/bible", params={"kind": "character"}).json()
            self.assertEqual(len(listing["items"]), 1)
            self.assertIn("character", listing["kinds"])

            self.assertEqual(
                studio.client.delete(f"/bible/{entry['id']}").status_code, 204
            )
            self.assertEqual(
                studio.client.get(f"/bible/{entry['id']}").status_code, 404
            )

    def test_invalid_kind_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            response = studio.client.post(
                "/bible", json={"kind": "soundtrack", "name": "x"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                studio.client.get("/bible", params={"kind": "soundtrack"}).status_code,
                400,
            )

    def test_preview_composes_without_generating(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            entry = self._create_entry(studio)

            preview = studio.client.post(
                "/bible/preview",
                json={
                    "base_prompt": "rooftop at dawn",
                    "bible_refs": [entry["id"], "bible_missing"],
                    "axis_values": {"look": {"attributes": {"hair": "short pink bob"}}},
                    "template": "image",
                    "seed": 1,
                },
            ).json()

            self.assertIn("rooftop at dawn", preview["prompt"])
            self.assertIn("purple eyes", preview["prompt"])
            self.assertEqual(preview["seed"], 4242)
            self.assertEqual(preview["attributes"]["hair"], "long black straight")
            self.assertTrue(
                any("unknown bible entry" in message for message in preview["conflicts"])
            )
            self.assertTrue(
                any("locked attribute" in message for message in preview["conflicts"])
            )

    def test_axis_catalogs_are_served(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            catalogs = studio.client.get("/bible/catalogs").json()
            self.assertIn("logo_structure", catalogs)

            logo = studio.client.get("/bible/catalogs/logo_structure").json()
            self.assertEqual(len(logo["values"]), 30)
            self.assertEqual(
                studio.client.get("/bible/catalogs/vibes").status_code, 404
            )

    def test_bible_refs_reach_the_image_request(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            entry = self._create_entry(studio)

            job_id = studio.client.post(
                "/generate/image",
                json={
                    "prompt": "rooftop at dawn",
                    "model_id": "sdxl",
                    "params": {"bible_refs": [entry["id"]]},
                },
            ).json()["job_id"]

            # The image model is not present in this environment, so the request
            # is asserted at the contract level rather than by running it.
            job = studio.client.get(f"/jobs/{job_id}").json()
            self.assertEqual(job["request"]["params"]["bible_refs"], [entry["id"]])


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class BatchApiTests(unittest.TestCase):
    def test_templates_are_listed_with_counts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            templates = {
                entry["name"]: entry
                for entry in studio.client.get("/batches/templates").json()
            }
            self.assertEqual(templates["logo-30"]["first_stage_items"], 30)
            self.assertEqual(
                templates["logo-30"]["stages"][0]["keep_top_n"], 6
            )

    def test_create_from_template_enqueues_children(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            response = studio.client.post(
                "/batches",
                json={
                    "template": "logo-30",
                    "overrides": {
                        "prompt": "acme coffee roasters logo",
                        "model_id": "sdxl",
                    },
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            batch = response.json()

            self.assertEqual(len(batch["items"]), 30)
            self.assertEqual(batch["stage_names"], ["probe", "refine"])
            self.assertEqual(batch["status"], "running")
            self.assertTrue(all(item["job_id"] for item in batch["items"]))
            self.assertEqual(studio.services.job_queue.size(), 30)

    def test_create_from_explicit_spec_and_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            batch = studio.client.post(
                "/batches",
                json={
                    "spec": {
                        "name": "logline sweep",
                        "media_type": "text",
                        "task_type": "story",
                        "model_id": "template-writer",
                        "prompt": "a heist in a rainy city",
                        "params": {"task": "logline", "count": 2},
                        "axes": [
                            {
                                "name": "tone",
                                "values": [
                                    {"label": "tense", "patch": {"params": {"tone": "tense"}}},
                                    {"label": "playful", "patch": {"params": {"tone": "playful"}}},
                                ],
                            }
                        ],
                        "seed_policy": "per_item",
                    }
                },
            ).json()
            self.assertEqual(len(batch["items"]), 2)

            studio.drain()
            reconciled = studio.client.get(f"/batches/{batch['id']}").json()
            self.assertEqual(reconciled["status"], "succeeded")
            self.assertEqual(reconciled["aggregate"]["succeeded"], 2)
            self.assertIsNotNone(reconciled["aggregate"]["average_score"])
            self.assertTrue(
                all(item["output_path"] for item in reconciled["items"])
            )

            item_id = reconciled["items"][0]["id"]
            promoted = studio.client.post(
                f"/batches/{batch['id']}/items/{item_id}/promote"
            ).json()
            self.assertTrue(promoted["items"][0]["promoted"])

            listing = studio.client.get("/batches").json()
            self.assertEqual(len(listing["items"]), 1)

    def test_gallery_items_carry_their_batch_id_and_support_batch_filtering(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            batch = studio.client.post(
                "/batches",
                json={
                    "spec": {
                        "name": "logline sweep",
                        "media_type": "text",
                        "task_type": "story",
                        "model_id": "template-writer",
                        "prompt": "a heist in a rainy city",
                        "params": {"task": "logline", "count": 1},
                        "axes": [
                            {
                                "name": "tone",
                                "values": [
                                    {"label": "tense", "patch": {"params": {"tone": "tense"}}},
                                    {"label": "playful", "patch": {"params": {"tone": "playful"}}},
                                ],
                            }
                        ],
                        "seed_policy": "per_item",
                    }
                },
            ).json()
            studio.drain()

            # An unrelated, non-batch job must not be swept into the batch filter.
            studio.client.post(
                "/generate/text",
                json={
                    "prompt": "a lone standing job",
                    "model_id": "template-writer",
                    "params": {"task": "logline", "count": 1},
                },
            )
            studio.drain()

            gallery = studio.client.get("/gallery", params={"media_type": "text"}).json()
            self.assertEqual(len(gallery), 3)
            batch_items = [item for item in gallery if item["batch_id"] == batch["id"]]
            self.assertEqual(len(batch_items), 2)
            self.assertTrue(all(item["batch_label"] for item in batch_items))
            standalone_items = [item for item in gallery if item["batch_id"] is None]
            self.assertEqual(len(standalone_items), 1)

            filtered = studio.client.get(
                "/gallery",
                params={"media_type": "text", "batch_id": batch["id"]},
            ).json()
            self.assertEqual(len(filtered), 2)
            self.assertTrue(all(item["batch_id"] == batch["id"] for item in filtered))

            detail = studio.client.get(f"/gallery/{batch_items[0]['asset_id']}").json()
            self.assertEqual(detail["batch_id"], batch["id"])

    def test_two_stage_batch_advances_after_the_probe(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            batch = studio.client.post(
                "/batches",
                json={
                    "spec": {
                        "name": "staged text sweep",
                        "media_type": "text",
                        "task_type": "story",
                        "model_id": "template-writer",
                        "prompt": "a heist in a rainy city",
                        "params": {"task": "logline"},
                        "axes": [
                            {
                                "name": "tone",
                                "values": [
                                    {"label": "a", "patch": {"params": {"tone": "a"}}},
                                    {"label": "b", "patch": {"params": {"tone": "b"}}},
                                    {"label": "c", "patch": {"params": {"tone": "c"}}},
                                ],
                            }
                        ],
                        "stages": [
                            {"name": "probe", "param_overrides": {"count": 1}, "keep_top_n": 1},
                            {"name": "refine", "param_overrides": {"count": 3}},
                        ],
                    }
                },
            ).json()
            self.assertEqual(len(batch["items"]), 3)

            # Draining the probe children triggers the automatic advance, which
            # enqueues the refine child; draining again completes it.
            studio.drain()
            studio.drain()

            advanced = studio.client.get(f"/batches/{batch['id']}").json()
            self.assertEqual(advanced["stage_index"], 1)
            refine_items = [
                item for item in advanced["items"] if item["stage_index"] == 1
            ]
            self.assertEqual(len(refine_items), 1)
            self.assertTrue(refine_items[0]["label"].endswith("__refine"))

    def test_cancel_stops_pending_children(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            batch = studio.client.post(
                "/batches",
                json={
                    "template": "logo-30",
                    "overrides": {"prompt": "acme logo", "model_id": "sdxl"},
                },
            ).json()

            cancelled = studio.client.post(f"/batches/{batch['id']}/cancel").json()
            statuses = {item["status"] for item in cancelled["items"]}
            self.assertEqual(statuses, {"cancelled"})
            self.assertEqual(cancelled["status"], "cancelled")

    def test_oversized_sweep_is_a_client_error(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            response = studio.client.post(
                "/batches",
                json={
                    "template": "logo-30",
                    "overrides": {"prompt": "acme logo", "limit": 4},
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("exceeds the limit", response.json()["detail"])

    def test_request_must_name_exactly_one_source(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            self.assertEqual(studio.client.post("/batches", json={}).status_code, 400)
            self.assertEqual(
                studio.client.post("/batches", json={"template": "nope"}).status_code,
                404,
            )

    def test_unknown_batch_is_not_found(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            for path in (
                "/batches/batch_missing",
                "/batches/batch_missing/advance",
                "/batches/batch_missing/cancel",
            ):
                with self.subTest(path=path):
                    method = studio.client.get if path.count("/") == 2 else studio.client.post
                    self.assertEqual(method(path).status_code, 404)

    def test_batch_binds_children_to_a_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            project_id = studio.client.post(
                "/projects", json={"name": "Brand sprint"}
            ).json()["id"]

            batch = studio.client.post(
                "/batches",
                json={
                    "spec": {
                        "name": "bound",
                        "media_type": "text",
                        "task_type": "story",
                        "model_id": "template-writer",
                        "prompt": "premise",
                        "project_id": project_id,
                        "params": {"task": "logline"},
                    }
                },
            ).json()

            project_jobs = studio.client.get(f"/projects/{project_id}/jobs").json()
            self.assertEqual(len(project_jobs["jobs"]), len(batch["items"]))

            missing = studio.client.post(
                "/batches",
                json={
                    "spec": {
                        "name": "bad project",
                        "media_type": "text",
                        "prompt": "premise",
                        "project_id": "proj_missing",
                    }
                },
            )
            self.assertEqual(missing.status_code, 404)


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class StoryApiTests(unittest.TestCase):
    def _create_story(self, studio: _Studio, **overrides) -> dict:
        payload = {
            "title": "Rewind",
            "premise": "時を巻き戻せる少女が最後の一日を選び直す",
            "language": "ja",
        }
        payload.update(overrides)
        response = studio.client.post("/stories", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_crud_and_task_listing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            detail = studio.client.get(f"/stories/{story['id']}").json()
            self.assertEqual(detail["story"]["title"], "Rewind")
            self.assertEqual(detail["missing_assets"], [])

            patched = studio.client.patch(
                f"/stories/{story['id']}", json={"tone": "melancholic"}
            ).json()
            self.assertEqual(patched["id"], story["id"])

            self.assertIn("scene_list", studio.client.get("/stories/tasks").json())
            self.assertEqual(len(studio.client.get("/stories").json()["items"]), 1)

            self.assertEqual(
                studio.client.delete(f"/stories/{story['id']}").status_code, 204
            )
            self.assertEqual(
                studio.client.get(f"/stories/{story['id']}").status_code, 404
            )

    def test_expand_then_apply_builds_the_story(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={"task": "logline", "model_id": "template-writer", "params": {"count": 2}},
            ).json()["job_id"]
            studio.drain()

            applied = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": job_id}
            ).json()
            self.assertTrue(applied["story"]["logline"])
            self.assertEqual(
                len(applied["story"]["metadata"]["logline_candidates"]), 2
            )
            self.assertEqual(applied["story"]["source_job_ids"], [job_id])

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 3},
                },
            ).json()["job_id"]
            studio.drain()

            with_scenes = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            ).json()
            self.assertEqual(len(with_scenes["story"]["scenes"]), 3)
            # Every scene still needs a visual, and the ones with narration text
            # need audio too.
            roles = {entry["role"] for entry in with_scenes["missing_assets"]}
            self.assertIn("visual", roles)

    def test_expand_requires_something_to_write_about(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio, premise="", title="")
            response = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={"task": "logline", "model_id": "template-writer"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("premise", response.json()["detail"])

    def test_apply_rejects_unfinished_and_non_story_jobs(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={"task": "logline", "model_id": "template-writer"},
            ).json()["job_id"]

            pending = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": job_id}
            )
            self.assertEqual(pending.status_code, 409)

            self.assertEqual(
                studio.client.post(
                    f"/stories/{story['id']}/apply", json={"job_id": "job_missing"}
                ).status_code,
                404,
            )

    def test_timeline_requires_generated_visuals(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 2},
                },
            ).json()["job_id"]
            studio.drain()
            studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            )

            conflict = studio.client.get(f"/stories/{story['id']}/timeline")
            self.assertEqual(conflict.status_code, 409)
            self.assertIn("scene_01", conflict.json()["detail"])

    def test_timeline_is_built_once_scenes_carry_assets(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 2},
                },
            ).json()["job_id"]
            studio.drain()
            studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            )

            # Attach the text job's own asset as a stand-in visual so the timeline
            # contract can be exercised without an image model present.
            asset = studio.services.asset_repository.get_primary_by_job(scene_job_id)
            self.assertIsNotNone(asset)
            stored = studio.services.story_repository.get(story["id"])
            scenes = [
                scene.model_copy(update={"asset_ids": {"visual": asset.id}})
                for scene in stored.scenes
            ]
            studio.services.story_repository.save(
                stored.model_copy(update={"scenes": scenes})
            )

            timeline = studio.client.get(
                f"/stories/{story['id']}/timeline",
                params={"width": 1080, "height": 1920, "fps": 24},
            ).json()["timeline"]

            self.assertEqual(timeline["resolution"], [1080, 1920])
            self.assertEqual(timeline["fps"], 24)
            self.assertEqual(len(timeline["tracks"]["visual"]), 2)
            self.assertEqual(timeline["tracks"]["visual"][0]["path"], asset.path)

    def test_story_can_be_bound_to_a_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            project_id = studio.client.post(
                "/projects", json={"name": "Short film"}
            ).json()["id"]
            story = self._create_story(studio, project_id=project_id)

            job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={"task": "logline", "model_id": "template-writer"},
            ).json()["job_id"]

            project_jobs = studio.client.get(f"/projects/{project_id}/jobs").json()
            self.assertEqual(
                [entry["id"] for entry in project_jobs["jobs"]], [job_id]
            )

            self.assertEqual(
                studio.client.post(
                    "/stories", json={"title": "x", "project_id": "proj_missing"}
                ).status_code,
                404,
            )

    def test_unknown_format_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            response = studio.client.post(
                "/stories", json={"title": "x", "format": "hologram"}
            )
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
