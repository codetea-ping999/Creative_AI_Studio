"""Tests for batch fan-out expansion, tracking, and stage advancement."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.batches import (  # noqa: E402
    Axis,
    AxisValue,
    BatchRepository,
    BatchService,
    BatchSpec,
    Stage,
    build_batch_template,
    expand_items,
    list_batch_templates,
    resolve_max_items_limit,
)
from core.jobs import EventBus, JobQueue, JobService  # noqa: E402
from core.schemas import GenerationResult  # noqa: E402
from core.storage.repositories.job_repository import JobRepository  # noqa: E402


def _spec(**overrides) -> BatchSpec:
    base = {
        "name": "test batch",
        "media_type": "image",
        "model_id": "sdxl",
        "prompt": "acme coffee logo",
        "params": {"width": 1024, "height": 1024},
        "axes": [
            Axis(
                name="structure",
                values=[
                    AxisValue(label="wordmark", patch={"prompt_suffix": "wordmark"}),
                    AxisValue(label="monogram", patch={"prompt_suffix": "monogram"}),
                ],
            ),
            Axis(
                name="tone",
                values=[
                    AxisValue(label="minimal", patch={"prompt_suffix": "minimal"}),
                    AxisValue(label="premium", patch={"prompt_suffix": "premium"}),
                ],
            ),
        ],
    }
    base.update(overrides)
    return BatchSpec(**base)


class ExpansionTests(unittest.TestCase):
    def test_grid_expands_the_cartesian_product(self) -> None:
        items = expand_items(_spec(), stage=Stage(name="single"), stage_index=0)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            sorted(item.label for item in items),
            ["monogram__minimal", "monogram__premium", "wordmark__minimal", "wordmark__premium"],
        )

    def test_labels_are_unique_and_filesystem_safe(self) -> None:
        items = expand_items(_spec(), stage=Stage(name="single"), stage_index=0)
        labels = [item.label for item in items]
        self.assertEqual(len(labels), len(set(labels)))
        for label in labels:
            self.assertNotIn("/", label)
            self.assertNotIn(" ", label)

    def test_axis_patches_append_to_the_prompt(self) -> None:
        items = expand_items(_spec(), stage=Stage(name="single"), stage_index=0)
        prompts = {item.label: item.request.prompt for item in items}
        self.assertEqual(
            prompts["wordmark__minimal"], "acme coffee logo, wordmark, minimal"
        )

    def test_axis_patch_merges_into_params(self) -> None:
        spec = _spec(
            axes=[
                Axis(
                    name="size",
                    values=[
                        AxisValue(label="tall", patch={"height": 1536}),
                        AxisValue(
                            label="detailed",
                            patch={"params": {"guidance_scale": 9.0}},
                        ),
                    ],
                )
            ]
        )
        items = expand_items(spec, stage=Stage(name="single"), stage_index=0)
        by_label = {item.label: item for item in items}
        self.assertEqual(by_label["tall"].request.params["height"], 1536)
        self.assertEqual(by_label["tall"].request.params["width"], 1024)
        self.assertEqual(
            by_label["detailed"].request.params["guidance_scale"], 9.0
        )

    def test_stage_overrides_apply_on_top_of_axis_patches(self) -> None:
        items = expand_items(
            _spec(),
            stage=Stage(name="probe", param_overrides={"width": 640, "steps": 14}),
            stage_index=0,
        )
        self.assertEqual(items[0].request.params["width"], 640)
        self.assertEqual(items[0].request.params["steps"], 14)

    def test_declarative_context_is_carried_in_params(self) -> None:
        items = expand_items(
            _spec(bible_refs=["bible_1"]), stage=Stage(name="single"), stage_index=0
        )
        params = items[0].request.params
        self.assertEqual(params["bible_refs"], ["bible_1"])
        self.assertIn("structure", params["axis_values"])
        self.assertIn("structure", params["batch_axis_labels"])
        self.assertEqual(params["batch_stage"], "single")

    def test_spec_is_not_mutated_by_expansion(self) -> None:
        spec = _spec()
        expand_items(spec, stage=Stage(name="single"), stage_index=0)
        self.assertEqual(spec.params, {"width": 1024, "height": 1024})
        self.assertEqual(spec.prompt, "acme coffee logo")

    def test_limit_error_names_the_count_and_cap(self) -> None:
        spec = _spec(limit=3)
        with self.assertRaises(ValueError) as context:
            expand_items(spec, stage=Stage(name="single"), stage_index=0)
        message = str(context.exception)
        self.assertIn("4 items", message)
        self.assertIn("limit of 3", message)

    def test_sample_strategy_is_deterministic_for_a_seed(self) -> None:
        spec = _spec(strategy="sample", max_items=2, sample_seed=11)
        first = expand_items(spec, stage=Stage(name="single"), stage_index=0)
        second = expand_items(spec, stage=Stage(name="single"), stage_index=0)
        self.assertEqual(len(first), 2)
        self.assertEqual(
            [item.label for item in first], [item.label for item in second]
        )

    def test_sample_strategy_requires_max_items(self) -> None:
        with self.assertRaises(ValueError):
            expand_items(
                _spec(strategy="sample"), stage=Stage(name="single"), stage_index=0
            )

    def test_seed_policies(self) -> None:
        shared = expand_items(
            _spec(seed=100, seed_policy="shared"),
            stage=Stage(name="single"),
            stage_index=0,
        )
        self.assertEqual({item.request.seed for item in shared}, {100})

        per_item = expand_items(
            _spec(seed=100, seed_policy="per_item"),
            stage=Stage(name="single"),
            stage_index=0,
        )
        self.assertEqual(
            sorted(item.request.seed for item in per_item), [100, 101, 102, 103]
        )

    def test_items_are_grouped_by_model_id(self) -> None:
        spec = _spec(
            axes=[
                Axis(
                    name="model",
                    values=[
                        AxisValue(label="a", patch={"model_id": "sdxl"}),
                        AxisValue(label="b", patch={"model_id": "anime-sdxl"}),
                        AxisValue(label="c", patch={"model_id": "sdxl"}),
                    ],
                )
            ]
        )
        items = expand_items(spec, stage=Stage(name="single"), stage_index=0)
        model_order = [item.request.model_id for item in items]
        self.assertEqual(model_order, sorted(model_order))

    def test_no_axes_produces_one_item(self) -> None:
        items = expand_items(
            _spec(axes=[]), stage=Stage(name="single"), stage_index=0
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].label, "item-000")

    def test_task_type_flows_into_the_child_request(self) -> None:
        items = expand_items(
            _spec(media_type="text", task_type="story", axes=[]),
            stage=Stage(name="single"),
            stage_index=0,
        )
        self.assertEqual(items[0].request.task_type, "story")


class BatchServiceTests(unittest.TestCase):
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
        self.batch_repository = BatchRepository(root / "batches")
        self.service = BatchService(
            self.batch_repository,
            self.job_service,
            self.job_repository,
            event_bus=self.event_bus,
        )

    def _succeed(self, job_id: str, score: float) -> None:
        self.job_service.mark_succeeded(
            job_id,
            GenerationResult(
                job_id=job_id,
                status="succeeded",
                outputs=[f"outputs/images/{job_id}.png"],
                previews=[f"outputs/images/{job_id}.png"],
                metadata={"quality_report": {"quality_score": score}},
            ),
        )

    def test_create_batch_enqueues_every_child(self) -> None:
        record = self.service.create_batch(_spec())
        self.assertEqual(len(record.items), 4)
        self.assertTrue(all(item.job_id for item in record.items))
        self.assertEqual(self.job_queue.size(), 4)
        self.assertEqual(record.status, "running")
        self.assertEqual(record.aggregate.total, 4)

    def test_project_id_is_bound_to_children(self) -> None:
        record = self.service.create_batch(_spec(project_id="proj_1"))
        job = self.job_repository.get(record.items[0].job_id)
        self.assertEqual(job.project_id, "proj_1")

    def test_reconcile_reads_scores_and_paths_from_jobs(self) -> None:
        record = self.service.create_batch(_spec())
        self._succeed(record.items[0].job_id, 82.0)

        reconciled = self.service.get_batch(record.id)
        first = reconciled.items[0]
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(first.score, 82.0)
        self.assertTrue(first.output_path.endswith(".png"))
        self.assertEqual(reconciled.aggregate.succeeded, 1)
        self.assertEqual(reconciled.aggregate.best_item_id, first.id)

    def test_partial_status_when_some_children_fail(self) -> None:
        record = self.service.create_batch(_spec())
        self._succeed(record.items[0].job_id, 70.0)
        self._succeed(record.items[1].job_id, 60.0)
        self.job_service.mark_failed(record.items[2].job_id, "out of memory")
        self.job_service.mark_failed(record.items[3].job_id, "out of memory")

        reconciled = self.service.get_batch(record.id)
        self.assertEqual(reconciled.status, "partial")
        self.assertEqual(reconciled.aggregate.failed, 2)
        self.assertEqual(reconciled.aggregate.average_score, 65.0)
        self.assertEqual(
            reconciled.items[2].error_message, "out of memory"
        )

    def test_failed_status_when_every_child_fails(self) -> None:
        record = self.service.create_batch(_spec())
        for item in record.items:
            self.job_service.mark_failed(item.job_id, "boom")
        self.assertEqual(self.service.get_batch(record.id).status, "failed")

    def test_reconcile_is_idempotent(self) -> None:
        record = self.service.create_batch(_spec())
        self._succeed(record.items[0].job_id, 80.0)
        first = self.service.reconcile(record.id)
        second = self.service.reconcile(record.id)
        self.assertEqual(
            first.model_dump(exclude={"updated_at"}),
            second.model_dump(exclude={"updated_at"}),
        )

    def test_reconcile_survives_a_fresh_service_instance(self) -> None:
        record = self.service.create_batch(_spec())
        self._succeed(record.items[0].job_id, 91.0)

        # A restart keeps no events; state must come back from the job repository.
        fresh_service = BatchService(
            BatchRepository(self.batch_repository.batch_dir),
            self.job_service,
            self.job_repository,
        )
        reconciled = fresh_service.get_batch(record.id)
        self.assertEqual(reconciled.items[0].score, 91.0)

    def test_advance_keeps_only_the_top_scores(self) -> None:
        spec = _spec(
            stages=[
                Stage(name="probe", param_overrides={"width": 640}, keep_top_n=2),
                Stage(name="refine", param_overrides={"width": 1024}),
            ]
        )
        record = self.service.create_batch(spec)
        scores = [55.0, 91.0, 73.0, 12.0]
        for item, score in zip(record.items, scores):
            self._succeed(item.job_id, score)

        advanced = self.service.advance(record.id)
        refine_items = advanced.items_for_stage(1)
        self.assertEqual(len(refine_items), 2)
        self.assertEqual(advanced.stage_index, 1)
        self.assertEqual(refine_items[0].request.params["width"], 1024)

        # The two best probes (91.0 and 73.0) are the ones carried forward.
        expected_labels = {
            f"{record.items[1].label}__refine",
            f"{record.items[2].label}__refine",
        }
        self.assertEqual({item.label for item in refine_items}, expected_labels)

    def test_advance_waits_for_the_stage_to_finish(self) -> None:
        spec = _spec(
            stages=[
                Stage(name="probe", keep_top_n=1),
                Stage(name="refine"),
            ]
        )
        record = self.service.create_batch(spec)
        self._succeed(record.items[0].job_id, 80.0)

        advanced = self.service.advance(record.id)
        self.assertEqual(advanced.stage_index, 0)
        self.assertEqual(len(advanced.items_for_stage(1)), 0)

    def test_advance_is_a_noop_on_the_last_stage(self) -> None:
        record = self.service.create_batch(_spec())
        for item in record.items:
            self._succeed(item.job_id, 70.0)
        advanced = self.service.advance(record.id)
        self.assertEqual(advanced.stage_index, 0)
        self.assertEqual(advanced.status, "succeeded")

    def test_event_subscription_advances_the_stage_automatically(self) -> None:
        spec = _spec(
            stages=[
                Stage(name="probe", param_overrides={"width": 640}, keep_top_n=1),
                Stage(name="refine", param_overrides={"width": 1024}),
            ]
        )
        self.service.attach_to_event_bus()
        record = self.service.create_batch(spec)
        for index, item in enumerate(record.items):
            self._succeed(item.job_id, 50.0 + index)

        reconciled = self.service.get_batch(record.id)
        self.assertEqual(reconciled.stage_index, 1)
        self.assertEqual(len(reconciled.items_for_stage(1)), 1)

    def test_event_handler_ignores_unrelated_jobs(self) -> None:
        self.service.attach_to_event_bus()
        # Publishing a terminal event for an unknown job must not raise.
        self.event_bus.publish("job_succeeded", {"job_id": "job_unknown"})

    def test_cancel_stops_every_pending_child(self) -> None:
        record = self.service.create_batch(_spec())
        self._succeed(record.items[0].job_id, 80.0)

        cancelled = self.service.cancel(record.id)
        statuses = [item.status for item in cancelled.items]
        self.assertEqual(statuses.count("cancelled"), 3)
        self.assertEqual(statuses.count("succeeded"), 1)
        self.assertEqual(cancelled.status, "partial")

    def test_promote_marks_the_winner(self) -> None:
        record = self.service.create_batch(_spec())
        promoted = self.service.promote(record.id, record.items[1].id)
        self.assertTrue(promoted.items[1].promoted)
        self.assertFalse(promoted.items[0].promoted)

    def test_promote_unknown_item_raises(self) -> None:
        record = self.service.create_batch(_spec())
        with self.assertRaises(LookupError):
            self.service.promote(record.id, "item_nope")

    def test_missing_batch_reads_return_none(self) -> None:
        self.assertIsNone(self.service.get_batch("batch_missing"))
        self.assertIsNone(self.service.advance("batch_missing"))
        self.assertIsNone(self.service.cancel("batch_missing"))
        self.assertIsNone(self.service.promote("batch_missing", "x"))

    def test_operator_limit_overrides_an_over_ambitious_spec(self) -> None:
        service = BatchService(
            self.batch_repository,
            self.job_service,
            self.job_repository,
            max_items_limit=2,
        )
        with self.assertRaises(ValueError):
            service.create_batch(_spec(limit=64))

    def test_list_batches_filters_by_project(self) -> None:
        self.service.create_batch(_spec(project_id="proj_1"))
        self.service.create_batch(_spec())
        self.assertEqual(len(self.service.list_batches()), 2)
        self.assertEqual(len(self.service.list_batches(project_id="proj_1")), 1)

    def test_corrupt_batch_file_is_isolated(self) -> None:
        self.service.create_batch(_spec())
        (self.batch_repository.batch_dir / "broken.json").write_text(
            "{nope", encoding="utf-8"
        )
        self.assertEqual(len(self.service.list_batches()), 1)


class LimitResolutionTests(unittest.TestCase):
    def test_env_override(self) -> None:
        import os

        os.environ["BATCH_MAX_ITEMS"] = "12"
        try:
            self.assertEqual(resolve_max_items_limit(), 12)
        finally:
            os.environ.pop("BATCH_MAX_ITEMS", None)

    def test_invalid_env_falls_back(self) -> None:
        import os

        os.environ["BATCH_MAX_ITEMS"] = "many"
        try:
            self.assertEqual(resolve_max_items_limit(), 64)
        finally:
            os.environ.pop("BATCH_MAX_ITEMS", None)

    def test_explicit_value_wins(self) -> None:
        self.assertEqual(resolve_max_items_limit(8), 8)


class TemplateTests(unittest.TestCase):
    def test_logo_30_has_thirty_probe_items_and_two_stages(self) -> None:
        spec = build_batch_template("logo-30", prompt="acme logo", model_id="sdxl")
        self.assertEqual(len(spec.axes[0].values), 30)
        stages = spec.resolved_stages()
        self.assertEqual([stage.name for stage in stages], ["probe", "refine"])
        self.assertEqual(stages[0].keep_top_n, 6)

        items = expand_items(spec, stage=stages[0], stage_index=0)
        self.assertEqual(len(items), 30)
        self.assertEqual(items[0].request.params["width"], 640)
        self.assertIn("acme logo", items[0].request.prompt)

    def test_thumbnail_grid_is_thirty_cells(self) -> None:
        spec = build_batch_template("thumbnail-tone-grid", prompt="episode 12")
        items = expand_items(spec, stage=spec.resolved_stages()[0], stage_index=0)
        self.assertEqual(len(items), 30)

    def test_character_sheet_shares_one_seed(self) -> None:
        spec = build_batch_template("character-sheet", prompt="mina", seed=99)
        items = expand_items(spec, stage=spec.resolved_stages()[0], stage_index=0)
        self.assertEqual(len(items), 16)
        self.assertEqual({item.request.seed for item in items}, {99})

    def test_logline_template_targets_text(self) -> None:
        spec = build_batch_template("logline-candidates", prompt="a premise")
        self.assertEqual(spec.media_type, "text")
        items = expand_items(spec, stage=spec.resolved_stages()[0], stage_index=0)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0].request.params["task"], "logline")
        self.assertIn(items[0].request.params["tone"], {"hopeful", "melancholic", "tense", "playful", "epic"})

    def test_overrides_are_applied(self) -> None:
        spec = build_batch_template("logo-30", name="custom", limit=40)
        self.assertEqual(spec.name, "custom")
        self.assertEqual(spec.limit, 40)

    def test_unknown_template_raises(self) -> None:
        with self.assertRaises(LookupError) as context:
            build_batch_template("mood-board")
        self.assertIn("logo-30", str(context.exception))

    def test_listing_reports_counts_and_stages(self) -> None:
        templates = {entry["name"]: entry for entry in list_batch_templates()}
        self.assertEqual(templates["logo-30"]["first_stage_items"], 30)
        self.assertEqual(templates["thumbnail-tone-grid"]["first_stage_items"], 30)
        self.assertEqual(
            templates["logo-30"]["stages"][0], {"name": "probe", "keep_top_n": 6}
        )


if __name__ == "__main__":
    unittest.main()
