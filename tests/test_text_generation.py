"""Tests for the text runtimes, story tasks, and text quality scoring."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import ModelRegistry  # noqa: E402
from core.models.loader import TemplateTextLoader  # noqa: E402
from core.models.text_runtimes import (  # noqa: E402
    build_template_runtime,
    extract_json_object,
    parse_brief,
    resolve_text_endpoint,
)
from core.quality import evaluate_text_output  # noqa: E402
from core.schemas import GenerationRequest  # noqa: E402
from generators.text import STORY_TASKS, TextGenerator, get_story_task  # noqa: E402


class _FakeModelService:
    """Minimal ModelService stand-in returning a fixed manifest and runtime."""

    def __init__(self, manifest, runtime_obj) -> None:
        self._manifest = manifest
        self._runtime_obj = runtime_obj
        self.resolved_with: tuple | None = None

    def resolve_runtime(self, model_id, media_type, task_type=None):
        self.resolved_with = (model_id, media_type, task_type)
        return self._manifest, self._runtime_obj


def _template_manifest():
    registry = ModelRegistry()
    registry.load_all()
    return registry.get("template-writer-local")


def _template_runtime():
    return TemplateTextLoader().load(_template_manifest())


def _generator(output_dir: Path, runtime_obj=None) -> TextGenerator:
    manifest = _template_manifest()
    runtime = runtime_obj if runtime_obj is not None else _template_runtime()
    return TextGenerator(
        _FakeModelService(manifest, runtime),
        output_dir=output_dir,
    )


def _request(task: str, **params) -> GenerationRequest:
    return GenerationRequest(
        media_type="text",
        prompt="時を巻き戻せる少女が最後の一日を選び直す",
        model_id="template-writer",
        params={"task": task, **params},
    )


class TemplateRuntimeTests(unittest.TestCase):
    def test_brief_block_is_parsed(self) -> None:
        prompt = "Write 3 loglines.\n\n### BRIEF\n- premise: a heist\n- count: 3\n"
        self.assertEqual(
            parse_brief(prompt), {"premise": "a heist", "count": "3"}
        )

    def test_generation_is_deterministic_for_a_seed(self) -> None:
        generate = build_template_runtime(seed_salt="test")
        schema = get_story_task("logline").json_schema()
        first = generate("### BRIEF\n- premise: p\n", seed=7, json_schema=schema)
        second = generate("### BRIEF\n- premise: p\n", seed=7, json_schema=schema)
        self.assertEqual(first, second)

    def test_array_length_follows_the_requested_count(self) -> None:
        generate = build_template_runtime()
        payload = json.loads(
            generate(
                "### BRIEF\n- premise: p\n- count: 5\n",
                json_schema=get_story_task("logline").json_schema(),
            )
        )
        self.assertEqual(len(payload["loglines"]), 5)

    def test_plain_text_generation_without_a_schema(self) -> None:
        generate = build_template_runtime()
        text = generate("### BRIEF\n- subject: 少女\n")
        self.assertIn("少女", text)


class StoryTaskTests(unittest.TestCase):
    def test_every_task_round_trips_through_the_template_runtime(self) -> None:
        runtime = _template_runtime()
        for name, task in STORY_TASKS.items():
            with self.subTest(task=name):
                raw = runtime["generate"](
                    task.build_prompt({"premise": "a premise", "subject": "少女"}),
                    system=task.system_prompt,
                    max_tokens=task.default_max_tokens,
                    temperature=0.8,
                    top_p=0.95,
                    seed=1,
                    json_schema=task.json_schema(),
                )
                payload = extract_json_object(raw)
                validated = task.response_model.model_validate(payload)
                markdown = task.render_markdown(validated.model_dump(mode="json"))
                self.assertTrue(markdown.startswith("#"))
                self.assertGreater(len(markdown), 30)

    def test_unknown_task_lists_valid_tasks(self) -> None:
        with self.assertRaises(ValueError) as context:
            get_story_task("haiku")
        self.assertIn("scene_list", str(context.exception))

    def test_beat_structure_names_appear_in_the_prompt(self) -> None:
        prompt = get_story_task("beat_sheet").build_prompt(
            {"logline": "l", "structure": "kishotenketsu"}
        )
        self.assertIn("kishotenketsu", prompt)
        self.assertIn("ten (twist)", prompt)


class JsonExtractionTests(unittest.TestCase):
    def test_plain_object(self) -> None:
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_object(self) -> None:
        self.assertEqual(
            extract_json_object('```json\n{"a": 1}\n```'), {"a": 1}
        )

    def test_object_after_preamble(self) -> None:
        self.assertEqual(
            extract_json_object('Sure! Here you go:\n{"a": {"b": 2}}\nHope that helps.'),
            {"a": {"b": 2}},
        )

    def test_braces_inside_strings_are_ignored(self) -> None:
        self.assertEqual(
            extract_json_object('{"a": "a } brace"}'), {"a": "a } brace"}
        )

    def test_empty_response_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            extract_json_object("   ")

    def test_non_object_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            extract_json_object("[1, 2]")

    def test_unclosed_object_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            extract_json_object('{"a": 1')


class TextGeneratorTests(unittest.TestCase):
    def test_validate_request_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            generator = _generator(Path(root))
            with self.assertRaises(ValueError):
                generator.validate_request(
                    GenerationRequest(
                        media_type="image", prompt="x", model_id="template-writer"
                    )
                )
            with self.assertRaises(ValueError):
                generator.validate_request(
                    GenerationRequest(
                        media_type="text", prompt="   ", model_id="template-writer"
                    )
                )
            with self.assertRaises(ValueError) as context:
                generator.validate_request(_request("sonnet"))
            self.assertIn("scene_list", str(context.exception))
            with self.assertRaises(ValueError):
                generator.validate_request(
                    GenerationRequest(
                        media_type="text",
                        prompt="x",
                        model_id="template-writer",
                        output_format="png",
                    )
                )
            # md and json are both accepted
            generator.validate_request(
                GenerationRequest(
                    media_type="text",
                    prompt="x",
                    model_id="template-writer",
                    output_format="json",
                )
            )

    def test_scene_list_run_writes_markdown_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "text"
            generator = _generator(output_dir)
            result = generator.run(_request("scene_list", scene_count=3))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.outputs), 1)
            markdown_path = Path(result.outputs[0])
            self.assertTrue(markdown_path.exists())
            self.assertEqual(markdown_path.suffix, ".md")

            structured_path = Path(result.metadata["structured_path"])
            self.assertTrue(structured_path.exists())
            self.assertEqual(
                json.loads(structured_path.read_text(encoding="utf-8")),
                result.metadata["structured"],
            )
            self.assertEqual(len(result.metadata["structured"]["scenes"]), 3)
            self.assertEqual(result.metadata["generation_attempts"], 1)
            self.assertEqual(result.metadata["story_task"], "scene_list")

    def test_metadata_matches_the_shared_generator_contract(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = _generator(Path(root)).run(_request("logline", count=2))
            for key in (
                "generator",
                "media_type",
                "task_type",
                "prompt",
                "model_id",
                "manifest_id",
                "model_display_name",
                "model_runtime",
                "loader",
                "device",
                "seed",
                "output_format",
                "default_params",
                "quality_report",
                "params",
            ):
                with self.subTest(key=key):
                    self.assertIn(key, result.metadata)
            self.assertEqual(result.metadata["media_type"], "text")
            self.assertEqual(result.metadata["output_format"], "md")

    def test_lineage_params_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = _generator(Path(root)).run(
                _request("logline", story_id="story_1", source_job_id="job_1")
            )
            self.assertEqual(result.metadata["story_id"], "story_1")
            self.assertEqual(result.metadata["source_job_id"], "job_1")

    def test_broken_json_is_repaired_on_the_second_attempt(self) -> None:
        calls: list[str] = []

        def flaky_generate(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return "I think the answer is: not json at all"
            return json.dumps({"loglines": [{"text": "ok", "hook": "h", "tone": "t"}]})

        runtime = {**_template_runtime(), "generate": flaky_generate}
        with tempfile.TemporaryDirectory() as root:
            result = _generator(Path(root), runtime).run(_request("logline"))

            self.assertEqual(result.metadata["generation_attempts"], 2)
            self.assertEqual(result.metadata["structured"]["loglines"][0]["text"], "ok")
            self.assertIn("### CORRECTION", calls[1])

    def test_persistent_schema_failure_is_actionable_and_saves_raw(self) -> None:
        def broken_generate(prompt, **kwargs):
            return "still not json"

        runtime = {**_template_runtime(), "generate": broken_generate}
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "text"
            generator = _generator(output_dir, runtime)
            with self.assertRaises(ValueError) as context:
                generator.run(_request("logline"))

            message = str(context.exception)
            self.assertIn("logline", message)
            self.assertIn("Raw response saved to", message)
            raw_files = list(output_dir.glob("failed_logline_*.txt"))
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(
                raw_files[0].read_text(encoding="utf-8"), "still not json"
            )

    def test_schema_is_not_passed_to_runtimes_without_support(self) -> None:
        seen: list[object] = []

        def recording_generate(prompt, **kwargs):
            seen.append(kwargs.get("json_schema"))
            return json.dumps({"loglines": [{"text": "ok"}]})

        runtime = {
            **_template_runtime(),
            "generate": recording_generate,
            "supports_json_schema": False,
        }
        with tempfile.TemporaryDirectory() as root:
            _generator(Path(root), runtime).run(_request("logline"))
        self.assertEqual(seen, [None])

    def test_runtime_wiring_params_do_not_leak_into_the_brief(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = _generator(Path(root)).run(_request("logline"))
            self.assertNotIn("context_window", result.metadata["resolved_prompt"])
            self.assertNotIn("context_window", result.metadata["params"])


class EndpointGuardTests(unittest.TestCase):
    def test_loopback_endpoints_are_allowed(self) -> None:
        for base_url in (
            "http://127.0.0.1:11434/v1",
            "http://localhost:1234/v1/",
            "http://[::1]:8000/v1",
        ):
            with self.subTest(base_url=base_url):
                self.assertTrue(resolve_text_endpoint(base_url))

    def test_remote_endpoint_is_refused_by_default(self) -> None:
        import os

        os.environ.pop("ALLOW_REMOTE_TEXT_ENDPOINTS", None)
        with self.assertRaises(ValueError) as context:
            resolve_text_endpoint("https://api.example.com/v1")
        self.assertIn("ALLOW_REMOTE_TEXT_ENDPOINTS", str(context.exception))

    def test_remote_endpoint_allowed_with_explicit_opt_in(self) -> None:
        import os

        os.environ["ALLOW_REMOTE_TEXT_ENDPOINTS"] = "true"
        try:
            self.assertEqual(
                resolve_text_endpoint("https://api.example.com/v1/"),
                "https://api.example.com/v1",
            )
        finally:
            os.environ.pop("ALLOW_REMOTE_TEXT_ENDPOINTS", None)

    def test_non_http_scheme_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            resolve_text_endpoint("ftp://127.0.0.1/v1")

    def test_empty_base_url_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            resolve_text_endpoint("   ")


class TextQualityTests(unittest.TestCase):
    def _write(self, root: Path, text: str) -> Path:
        path = root / "sample.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_report_shape_matches_other_evaluators(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self._write(
                Path(root),
                "# Chapter\n\nThe rain stopped before dawn. "
                "She counted the coins twice, then walked out into the wet street "
                "without looking back at the lamp still burning in the window.\n",
            )
            report = evaluate_text_output(path, task="prose")

            for key in (
                "method",
                "quality_score",
                "quality_level",
                "business_readiness_score",
                "business_readiness_level",
                "checks",
                "metrics",
                "notes",
            ):
                with self.subTest(key=key):
                    self.assertIn(key, report)
            self.assertEqual(report["method"], "heuristic_local_v1")
            self.assertEqual(report["metrics"]["task"], "prose")

    def test_degenerate_repetition_scores_lower_than_varied_prose(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            good = self._write(
                Path(root) / "" if False else Path(root),
                "The rain stopped before dawn and the market began to stir. "
                "She counted coins, hid them, and left without a word. "
                "Somewhere behind her a shutter opened onto the grey light.\n",
            )
            good_report = evaluate_text_output(good)

            bad_path = Path(root) / "bad.md"
            bad_path.write_text("she walked " * 60, encoding="utf-8")
            bad_report = evaluate_text_output(bad_path)

            self.assertLess(bad_report["quality_score"], good_report["quality_score"])
            self.assertGreater(bad_report["metrics"]["repetition_ratio"], 0.5)

    def test_placeholder_leftovers_are_penalized(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clean = evaluate_text_output(
                self._write(
                    Path(root),
                    "The bell rang twice before anyone moved toward the door.\n",
                )
            )
            dirty_path = Path(root) / "dirty.md"
            dirty_path.write_text(
                "The bell rang twice before anyone moved toward the door.\n"
                "TODO: finish this scene. Insert description here. {subject}\n",
                encoding="utf-8",
            )
            dirty = evaluate_text_output(dirty_path)

            self.assertGreater(dirty["metrics"]["placeholder_hits"], 0)
            self.assertLess(dirty["quality_score"], clean["quality_score"])
            self.assertTrue(
                any("placeholder" in check for check in dirty["checks"])
            )

    def test_incomplete_structured_payload_lowers_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self._write(Path(root), "# Loglines\n\n1. A girl rewinds a day.\n")
            complete = evaluate_text_output(
                path, structured={"loglines": [{"text": "a", "hook": "b", "tone": "c"}]}
            )
            incomplete = evaluate_text_output(
                path, structured={"loglines": [{"text": "a", "hook": "", "tone": None}]}
            )
            self.assertEqual(complete["metrics"]["structure_completeness"], 1.0)
            self.assertLess(
                incomplete["metrics"]["structure_completeness"],
                complete["metrics"]["structure_completeness"],
            )
            self.assertLess(incomplete["quality_score"], complete["quality_score"])

    def test_japanese_repetition_uses_shingles(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            varied = self._write(
                Path(root),
                "朝の光が街を照らしていた。彼女は静かに息を吸い、歩き出した。"
                "遠くで鐘が鳴り、路地に人影が増えていく。\n",
            )
            varied_report = evaluate_text_output(varied)

            looped_path = Path(root) / "looped.md"
            looped_path.write_text("彼女は歩いた。" * 30, encoding="utf-8")
            looped_report = evaluate_text_output(looped_path)

            self.assertLess(
                varied_report["metrics"]["repetition_ratio"],
                looped_report["metrics"]["repetition_ratio"],
            )

    def test_generated_output_is_scored(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = _generator(Path(root)).run(_request("scene_list", scene_count=3))
            report = result.metadata["quality_report"]
            self.assertEqual(report["method"], "heuristic_local_v1")
            self.assertGreater(report["quality_score"], 0)


if __name__ == "__main__":
    unittest.main()
