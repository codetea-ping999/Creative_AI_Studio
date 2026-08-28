"""API coverage for the v0.3 story, bible, and batch endpoints."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services
    from core.schemas import GenerationRequest, GenerationResult
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

    def test_preview_rejects_a_bible_entry_referencing_a_missing_asset(self) -> None:
        # #199: PromptComposer's asset resolution is wired only if
        # bootstrap/factories.py constructs it with an asset_repository.
        # This drives the real app (create_application_services), not a
        # hand-built PromptComposer, so it would have passed silently (no
        # 422, references dropped) if that wiring were ever missing again.
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            entry = self._create_entry(
                studio, reference_asset_ids=["asset_does_not_exist"]
            )

            response = studio.client.post(
                "/bible/preview",
                json={
                    "base_prompt": "rooftop at dawn",
                    "bible_refs": [entry["id"]],
                    "template": "image",
                },
            )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("asset_does_not_exist", response.text)

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

    def _mark_character_sheet_winner_succeeded(
        self,
        studio: _Studio,
        tmp_dir: Path,
        *,
        entry_id: str,
        seed: int | None,
        model_id: str = "sdxl",
        attributes: dict | None = None,
        output_name: str = "winner.png",
    ) -> str:
        """Simulate one completed character-sheet cell without SDXL weights.

        Mirrors what `generators/image/generator.py` would have written into
        `GenerationResult.metadata` for a batch item (see #194): a
        `prompt_composition` audit trail plus the runtime-resolved
        `model_id`/`seed`/`params`, alongside per-cell batch bookkeeping
        (`bible_refs`/`axis_values`/`batch_axis_labels`) in the *request*
        params -- the exact shape `_effective_promotion_settings` and
        `BibleRepository.promote_winner` (#195) must read from and filter.
        """

        output_path = tmp_dir / "outputs" / "images" / output_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-png")

        job = studio.services.job_service.create_job(
            GenerationRequest(
                media_type="image",
                prompt="Mina, front view, neutral expression",
                model_id="sdxl",
                params={
                    "bible_refs": [entry_id],
                    "axis_values": {"angle": {"prompt_suffix": "front view"}},
                    "batch_axis_labels": {"angle": "front-view"},
                },
            )
        )
        metadata: dict = {
            "model_id": model_id,
            "params": {
                "width": 832,
                "height": 1024,
                "num_inference_steps": 26,
                "guidance_scale": 7.0,
            },
            "prompt_composition": {
                "prompt": "Mina, front view, neutral expression",
                "negative_prompt": None,
                "seed": seed,
                "lora": None,
                "reference_asset_ids": [],
                "resolved_references": [],
                "palette": [],
                "attributes": attributes or {},
                "applied": [],
                "conflicts": [],
            },
        }
        if seed is not None:
            metadata["seed"] = seed
        studio.services.job_service.mark_succeeded(
            job.id,
            GenerationResult(
                job_id=job.id,
                status="succeeded",
                outputs=[str(output_path)],
                previews=[str(output_path)],
                metadata=metadata,
            ),
        )
        asset = studio.services.asset_repository.get_by_job(job.id)[0]
        return asset.id

    def test_promote_winner_locks_seed_merges_attributes_and_registers_reference(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            studio = _Studio(tmp_path)
            entry = self._create_entry(
                studio,
                attributes={"hair": "long black straight"},
                seed_policy={},
            )

            asset_id = self._mark_character_sheet_winner_succeeded(
                studio,
                tmp_path,
                entry_id=entry["id"],
                seed=777,
                attributes={"hair": "long black straight", "eyes": "purple"},
            )

            response = studio.client.post(
                f"/bible/{entry['id']}/promote", json={"asset_id": asset_id}
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()

            self.assertEqual(payload["entry"]["id"], entry["id"])
            self.assertEqual(
                payload["entry"]["attributes"],
                {"hair": "long black straight", "eyes": "purple"},
            )
            self.assertEqual(
                payload["entry"]["seed_policy"], {"mode": "locked", "seed": 777}
            )
            self.assertEqual(payload["entry"]["reference_asset_ids"], [asset_id])
            self.assertEqual(payload["promotion"]["asset_id"], asset_id)
            self.assertEqual(payload["promotion"]["applied"]["model_id"], "sdxl")
            self.assertEqual(payload["promotion"]["applied"]["seed"], 777)
            # Per-cell batch bookkeeping must not leak into the promoted params.
            applied_params = payload["promotion"]["applied"]["params"]
            self.assertNotIn("bible_refs", applied_params)
            self.assertNotIn("axis_values", applied_params)
            self.assertNotIn("batch_axis_labels", applied_params)
            self.assertEqual(applied_params["width"], 832)

            # The change is persisted, not just returned once.
            refetched = studio.client.get(f"/bible/{entry['id']}").json()
            self.assertEqual(refetched["seed_policy"], {"mode": "locked", "seed": 777})

    def test_promote_winner_rejects_unknown_asset_or_entry(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            studio = _Studio(tmp_path)
            entry = self._create_entry(studio)
            asset_id = self._mark_character_sheet_winner_succeeded(
                studio, tmp_path, entry_id=entry["id"], seed=1
            )

            missing_asset = studio.client.post(
                f"/bible/{entry['id']}/promote", json={"asset_id": "asset_missing"}
            )
            self.assertEqual(missing_asset.status_code, 404)

            missing_entry = studio.client.post(
                "/bible/bible_missing/promote", json={"asset_id": asset_id}
            )
            self.assertEqual(missing_entry.status_code, 404)

    def test_promote_winner_requires_a_recorded_seed(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            studio = _Studio(tmp_path)
            entry = self._create_entry(studio)
            asset_id = self._mark_character_sheet_winner_succeeded(
                studio, tmp_path, entry_id=entry["id"], seed=None
            )

            response = studio.client.post(
                f"/bible/{entry['id']}/promote", json={"asset_id": asset_id}
            )
            self.assertEqual(response.status_code, 422, response.text)

    def test_promote_winner_only_updates_the_targeted_entry(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            studio = _Studio(tmp_path)
            target = self._create_entry(studio, name="Mina")
            other = self._create_entry(studio, name="Rio")
            asset_id = self._mark_character_sheet_winner_succeeded(
                studio, tmp_path, entry_id=target["id"], seed=555
            )

            response = studio.client.post(
                f"/bible/{target['id']}/promote", json={"asset_id": asset_id}
            )
            self.assertEqual(response.status_code, 200, response.text)

            untouched = studio.client.get(f"/bible/{other['id']}").json()
            self.assertEqual(untouched["seed_policy"], other["seed_policy"])
            self.assertEqual(untouched["reference_asset_ids"], [])


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

    def test_apply_refuses_a_job_written_for_another_story(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            written_for = self._create_story(studio)
            other = self._create_story(studio, title="Another film")

            job_id = studio.client.post(
                f"/stories/{written_for['id']}/expand",
                json={"task": "logline", "model_id": "template-writer"},
            ).json()["job_id"]
            studio.drain()

            stolen = studio.client.post(
                f"/stories/{other['id']}/apply", json={"job_id": job_id}
            )
            self.assertEqual(stolen.status_code, 409, stolen.text)
            self.assertIn(written_for["id"], stolen.json()["detail"])
            self.assertIn(other["id"], stolen.json()["detail"])
            self.assertEqual(
                studio.client.get(f"/stories/{other['id']}").json()["story"]["logline"],
                "",
            )

            # The story the job was written for still takes it.
            owned = studio.client.post(
                f"/stories/{written_for['id']}/apply", json={"job_id": job_id}
            )
            self.assertEqual(owned.status_code, 200, owned.text)
            self.assertTrue(owned.json()["story"]["logline"])

    def test_expand_ignores_a_caller_supplied_story_id(self) -> None:
        """The queueing story owns the job, whatever ``params`` claims.

        ``params`` is otherwise passed through to the writer, so a caller could
        set ``story_id`` there and hand its own job's result to another story.
        """

        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            written_for = self._create_story(studio)
            other = self._create_story(studio, title="Another film")

            job_id = studio.client.post(
                f"/stories/{written_for['id']}/expand",
                json={
                    "task": "logline",
                    "model_id": "template-writer",
                    "params": {"story_id": other["id"], "task": "prose"},
                },
            ).json()["job_id"]
            studio.drain()

            job_params = studio.client.get(f"/jobs/{job_id}").json()["request"]["params"]
            self.assertEqual(job_params["story_id"], written_for["id"])
            self.assertEqual(job_params["task"], "logline")

            # The story that queued it can still apply it, and the named story
            # cannot: the ownership check reads the server's value.
            hijacked = studio.client.post(
                f"/stories/{other['id']}/apply", json={"job_id": job_id}
            )
            self.assertEqual(hijacked.status_code, 409, hijacked.text)
            self.assertEqual(
                studio.client.get(f"/stories/{other['id']}").json()["story"]["logline"],
                "",
            )

            owned = studio.client.post(
                f"/stories/{written_for['id']}/apply", json={"job_id": job_id}
            )
            self.assertEqual(owned.status_code, 200, owned.text)
            self.assertTrue(owned.json()["story"]["logline"])

    def test_apply_accepts_a_hand_built_text_job_that_names_no_story(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "時を巻き戻せる少女",
                    "model_id": "template-writer",
                    "params": {"task": "logline"},
                },
            ).json()["job_id"]
            studio.drain()

            applied = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": job_id}
            )
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertTrue(applied.json()["story"]["logline"])

    def test_assemble_refuses_media_from_another_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            home = studio.client.post(
                "/projects", json={"name": "Short film"}
            ).json()["id"]
            elsewhere = studio.client.post(
                "/projects", json={"name": "Client work"}
            ).json()["id"]
            story = self._create_story(studio, project_id=home)

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

            foreign_job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "another client's brief",
                    "model_id": "template-writer",
                    "project_id": elsewhere,
                    "params": {"task": "logline"},
                },
            ).json()["job_id"]
            studio.drain()
            foreign = studio.services.asset_repository.get_primary_by_job(
                foreign_job_id
            )
            self.assertIsNotNone(foreign)

            stored = studio.services.story_repository.get(story["id"])
            studio.services.story_repository.save(
                stored.model_copy(
                    update={
                        "scenes": [
                            scene.model_copy(
                                update={"asset_ids": {"visual": foreign.id}}
                            )
                            for scene in stored.scenes
                        ]
                    }
                )
            )

            response = studio.client.post(f"/stories/{story['id']}/assemble", json={})
            self.assertEqual(response.status_code, 404, response.text)
            detail = response.json()["detail"]
            self.assertIn(foreign.id, detail)
            # The same refusal fires after PATCH /stories/{id} {project_id} moves a
            # story away from media that was generated before the move, so the
            # message has to name the way back instead of only saying "not found".
            # The ids are interpolated, not left as {placeholders}, so the line can
            # be pasted straight into a request.
            self.assertIn(f"POST /projects/{home}/assets/{foreign.id}", detail)
            self.assertIn(elsewhere, detail)
            self.assertIn(home, detail)

    def test_assemble_refusal_names_a_recovery_that_exists_when_the_story_leaves(
        self,
    ) -> None:
        """The refusal must not prescribe an impossible fix.

        Attaching an asset to a project is a route; detaching it is not. So when a
        story is moved *out* of a project after its media was generated, telling the
        user to re-parent the asset names a step they cannot take — there is no
        target project id to POST to. The message has to point the other way.
        """

        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            home = studio.client.post(
                "/projects", json={"name": "Short film"}
            ).json()["id"]
            story = self._create_story(studio, project_id=home)

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

            media_job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "a shot description",
                    "model_id": "template-writer",
                    "project_id": home,
                    "params": {"task": "logline"},
                },
            ).json()["job_id"]
            studio.drain()
            asset = studio.services.asset_repository.get_primary_by_job(media_job_id)
            self.assertIsNotNone(asset)
            self.assertEqual(asset.project_id, home)

            stored = studio.services.story_repository.get(story["id"])
            studio.services.story_repository.save(
                stored.model_copy(
                    update={
                        "scenes": [
                            scene.model_copy(
                                update={"asset_ids": {"visual": asset.id}}
                            )
                            for scene in stored.scenes
                        ]
                    }
                )
            )

            moved_out = studio.client.patch(
                f"/stories/{story['id']}", json={"project_id": None}
            )
            self.assertEqual(moved_out.status_code, 200, moved_out.text)

            response = studio.client.post(f"/stories/{story['id']}/assemble", json={})
            self.assertEqual(response.status_code, 404, response.text)
            detail = response.json()["detail"]
            self.assertIn(asset.id, detail)
            self.assertIn(home, detail)
            self.assertIn("PATCH /stories/{story_id}", detail)
            # The route it would send them to does not exist in this direction.
            self.assertNotIn(
                "POST /projects/",
                detail,
                "the refusal must not prescribe a step the API cannot perform",
            )

    def test_script_stage_writes_into_the_named_scene(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 3},
                },
            ).json()["job_id"]
            studio.drain()
            studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            )

            queued = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "script",
                    "model_id": "template-writer",
                    "params": {"scene_id": "scene_02"},
                },
            )
            self.assertEqual(queued.status_code, 201, queued.text)
            studio.drain()

            applied = studio.client.post(
                f"/stories/{story['id']}/apply",
                json={"job_id": queued.json()["job_id"]},
            )
            self.assertEqual(applied.status_code, 200, applied.text)
            scenes = applied.json()["story"]["scenes"]
            self.assertEqual(len(scenes), 3)
            self.assertEqual(scenes[0]["dialogue"], [])
            self.assertTrue(scenes[1]["dialogue"])
            self.assertEqual(scenes[2]["dialogue"], [])
            # Nothing was parked out of reach.
            self.assertNotIn(
                "unassigned_script_lines", applied.json()["story"]["metadata"]
            )

    def test_apply_rejects_a_script_job_whose_scene_was_regenerated_away(
        self,
    ) -> None:
        """A stale scene target fails loudly instead of parking the dialogue.

        ``docs/api-contract.md`` promises a 400 here, but the only coverage was at
        the library level (``core.story.apply_text_result`` raising ValueError).
        Nothing pinned the HTTP boundary, so a regression turning this back into a
        silent park-in-metadata would not have been caught — which is exactly the
        failure #106 exists to prevent.
        """

        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            def rebuild_scenes(count: int) -> None:
                job_id = studio.client.post(
                    f"/stories/{story['id']}/expand",
                    json={
                        "task": "scene_list",
                        "model_id": "template-writer",
                        "params": {"scene_count": count},
                    },
                ).json()["job_id"]
                studio.drain()
                studio.client.post(
                    f"/stories/{story['id']}/apply", json={"job_id": job_id}
                )

            rebuild_scenes(3)
            queued = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "script",
                    "model_id": "template-writer",
                    "params": {"scene_id": "scene_03"},
                },
            )
            self.assertEqual(queued.status_code, 201, queued.text)
            studio.drain()

            # The writer trims the story down before applying the queued script.
            rebuild_scenes(2)

            applied = studio.client.post(
                f"/stories/{story['id']}/apply",
                json={"job_id": queued.json()["job_id"]},
            )
            self.assertEqual(applied.status_code, 400, applied.text)
            detail = applied.json()["detail"]
            self.assertIn("scene_03", detail)

            after = studio.client.get(f"/stories/{story['id']}").json()["story"]
            self.assertNotIn("unassigned_script_lines", after["metadata"])
            self.assertEqual([scene["dialogue"] for scene in after["scenes"]], [[], []])

    def test_apply_refuses_script_lines_with_no_scene_to_land_in(self) -> None:
        """Dialogue is never accepted into a place nothing can read it back from.

        A hand-built ``POST /generate/text`` script job names no scene, and the
        merge would park its lines in ``metadata.unassigned_script_lines`` — where
        no route returns them to a scene. The API refuses and names the stage that
        does bind a target.
        """

        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 3},
                },
            ).json()["job_id"]
            studio.drain()
            studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            )

            loose_job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "屋上で二人が言い争う",
                    "model_id": "template-writer",
                    "params": {"task": "script"},
                },
            ).json()["job_id"]
            studio.drain()
            self.assertEqual(
                studio.client.get(f"/jobs/{loose_job_id}").json()["status"],
                "succeeded",
            )

            refused = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": loose_job_id}
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            detail = refused.json()["detail"]
            self.assertIn(f"POST /stories/{story['id']}/expand", detail)
            self.assertIn("params.scene_id", detail)

            stored = studio.client.get(f"/stories/{story['id']}").json()["story"]
            self.assertNotIn("unassigned_script_lines", stored["metadata"])
            self.assertEqual(
                [scene["dialogue"] for scene in stored["scenes"]], [[], [], []]
            )

    def test_apply_refuses_script_lines_when_the_story_has_no_scenes(self) -> None:
        """The same refusal, with the scene_list stage named instead."""

        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            loose_job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "屋上で二人が言い争う",
                    "model_id": "template-writer",
                    "params": {"task": "script"},
                },
            ).json()["job_id"]
            studio.drain()

            refused = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": loose_job_id}
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertIn("scene_list", refused.json()["detail"])
            self.assertNotIn(
                "unassigned_script_lines",
                studio.client.get(f"/stories/{story['id']}").json()["story"]["metadata"],
            )

    def test_apply_still_binds_script_lines_on_a_single_scene_story(self) -> None:
        """One scene is unambiguous, so an unnamed payload still lands in it."""

        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 1},
                },
            ).json()["job_id"]
            studio.drain()
            studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            )

            loose_job_id = studio.client.post(
                "/generate/text",
                json={
                    "prompt": "屋上で二人が言い争う",
                    "model_id": "template-writer",
                    "params": {"task": "script"},
                },
            ).json()["job_id"]
            studio.drain()

            applied = studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": loose_job_id}
            )
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertTrue(applied.json()["story"]["scenes"][0]["dialogue"])

    def test_script_stage_requires_a_scene_it_can_write_into(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            studio = _Studio(Path(tmp_dir))
            story = self._create_story(studio)

            before_scenes = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={"task": "script", "model_id": "template-writer"},
            )
            self.assertEqual(before_scenes.status_code, 409, before_scenes.text)
            self.assertIn("scene_list", before_scenes.json()["detail"])

            scene_job_id = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "scene_list",
                    "model_id": "template-writer",
                    "params": {"scene_count": 3},
                },
            ).json()["job_id"]
            studio.drain()
            studio.client.post(
                f"/stories/{story['id']}/apply", json={"job_id": scene_job_id}
            )

            unnamed = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={"task": "script", "model_id": "template-writer"},
            )
            self.assertEqual(unnamed.status_code, 400, unnamed.text)
            self.assertIn("scene_02", unnamed.json()["detail"])

            unknown = studio.client.post(
                f"/stories/{story['id']}/expand",
                json={
                    "task": "script",
                    "model_id": "template-writer",
                    "params": {"scene_id": "scene_99"},
                },
            )
            self.assertEqual(unknown.status_code, 404, unknown.text)

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

            response = studio.client.get(
                f"/stories/{story['id']}/timeline",
                params={"width": 1080, "height": 1920, "fps": 24},
            )
            timeline = response.json()["timeline"]

            self.assertEqual(timeline["resolution"], [1080, 1920])
            self.assertEqual(timeline["fps"], 24)
            self.assertEqual(len(timeline["tracks"]["visual"]), 2)

            # The client is served through the /outputs mount, so it gets a URL
            # it can fetch — never where the file lives on this machine.
            entry = timeline["tracks"]["visual"][0]
            self.assertNotIn("path", entry)
            self.assertEqual(entry["asset_id"], asset.id)
            self.assertTrue(
                entry["preview_url"].startswith("/outputs/"), entry["preview_url"]
            )
            self.assertTrue(entry["preview_url"].endswith(Path(asset.path).name))
            self.assertNotIn(tmp_dir, response.text)
            self.assertNotIn(str(Path(asset.path).parent), response.text)

    def test_timeline_omits_the_preview_of_an_unserved_asset(self) -> None:
        """An asset outside the served root has no URL, so the key is absent.

        ``asset_id`` always identifies the entry; ``preview_url`` is the optional
        half, and a consumer has to handle it missing rather than assume every
        entry is fetchable.
        """

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

            asset = studio.services.asset_repository.get_primary_by_job(scene_job_id)
            self.assertIsNotNone(asset)
            # An asset imported from elsewhere on disk is not under /outputs.
            outside = Path(tmp_dir) / "imported" / "elsewhere.png"
            asset.path = str(outside)
            studio.services.asset_repository.create_or_update(asset)

            stored = studio.services.story_repository.get(story["id"])
            studio.services.story_repository.save(
                stored.model_copy(
                    update={
                        "scenes": [
                            scene.model_copy(update={"asset_ids": {"visual": asset.id}})
                            for scene in stored.scenes
                        ]
                    }
                )
            )

            response = studio.client.get(f"/stories/{story['id']}/timeline")
            self.assertEqual(response.status_code, 200, response.text)
            entry = response.json()["timeline"]["tracks"]["visual"][0]
            self.assertEqual(entry["asset_id"], asset.id)
            self.assertNotIn("preview_url", entry)
            self.assertNotIn("path", entry)
            self.assertNotIn(str(outside), response.text)

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
