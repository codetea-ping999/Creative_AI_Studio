"""Tests for the deterministic scene-to-visual request manifest contract."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from core.prompting.composer import ComposedPrompt  # noqa: E402
from core.storage.json_files import utc_now  # noqa: E402
from core.story.schemas import Scene, StoryDocument  # noqa: E402
from core.story.visual_manifest import (  # noqa: E402
    BibleSnapshotRef,
    SceneVisualManifest,
    SceneVisualRequest,
    build_visual_manifest,
)


def _story(scenes: list[Scene], **fields) -> StoryDocument:
    now = utc_now()
    return StoryDocument(id="story_1", scenes=scenes, created_at=now, updated_at=now, **fields)


def _scene(scene_id: str, order: int, **fields) -> Scene:
    return Scene(id=scene_id, order=order, **fields)


def _composed(prompt: str, **fields) -> ComposedPrompt:
    defaults = {
        "prompt": prompt,
        "negative_prompt": None,
        "seed": None,
        "lora": None,
    }
    defaults.update(fields)
    return ComposedPrompt(**defaults)


class BuildVisualManifestTests(unittest.TestCase):
    def test_one_request_per_scene_traceable_to_its_scene(self) -> None:
        story = _story(
            [
                _scene("scene_01", order=0, duration_seconds=3.0),
                _scene("scene_02", order=1, duration_seconds=5.0),
            ]
        )
        composed = {
            "scene_01": _composed("a rooftop at dawn"),
            "scene_02": _composed("a narrow alley at night"),
        }

        manifest = build_visual_manifest(story, composed)

        self.assertEqual(manifest.story_id, "story_1")
        self.assertEqual(len(manifest.requests), 2)
        for request in manifest.requests:
            self.assertEqual(request.story_id, "story_1")
            self.assertIn(request.scene_id, {"scene_01", "scene_02"})

    def test_requests_in_order_matches_scene_order_even_when_input_is_reversed(
        self,
    ) -> None:
        story = _story(
            [
                _scene("scene_02", order=1),
                _scene("scene_01", order=0),
            ]
        )
        composed = {
            "scene_01": _composed("first"),
            "scene_02": _composed("second"),
        }

        manifest = build_visual_manifest(story, composed)
        ordered = manifest.requests_in_order()

        self.assertEqual([request.scene_id for request in ordered], ["scene_01", "scene_02"])

    def test_same_story_and_composed_prompts_produce_identical_manifest(self) -> None:
        story = _story([_scene("scene_01", order=0)])
        composed = {"scene_01": _composed("a quiet street")}

        first = build_visual_manifest(story, composed)
        second = build_visual_manifest(story, composed)

        self.assertEqual(first, second)
        self.assertEqual(first.requests[0].id, second.requests[0].id)

    def test_request_id_is_deterministic_not_random(self) -> None:
        story = _story([_scene("scene_01", order=0)])
        composed = {"scene_01": _composed("a quiet street")}

        manifest = build_visual_manifest(story, composed)
        rebuilt = build_visual_manifest(story, composed)

        self.assertEqual(manifest.requests[0].id, rebuilt.requests[0].id)
        self.assertTrue(manifest.requests[0].id.startswith("visreq_"))

    def test_missing_composed_prompt_for_a_scene_raises(self) -> None:
        story = _story([_scene("scene_01", order=0), _scene("scene_02", order=1)])
        composed = {"scene_01": _composed("only one scene composed")}

        with self.assertRaises(ValueError) as ctx:
            build_visual_manifest(story, composed)
        self.assertIn("scene_02", str(ctx.exception))

    def test_bible_snapshot_splits_into_character_location_style_refs(self) -> None:
        story = _story([_scene("scene_01", order=0)])
        composed = {"scene_01": _composed("hero in a forest")}
        snapshots = {
            "scene_01": [
                BibleSnapshotRef(
                    bible_id="bible_hero", kind="character", name="Hero", version="v1"
                ),
                BibleSnapshotRef(
                    bible_id="bible_forest", kind="location", name="Forest", version="v1"
                ),
                BibleSnapshotRef(
                    bible_id="bible_style", kind="style", name="Watercolor", version="v1"
                ),
            ]
        }

        manifest = build_visual_manifest(story, composed, snapshots)
        request = manifest.requests[0]

        self.assertEqual(request.character_refs, ["bible_hero"])
        self.assertEqual(request.location_refs, ["bible_forest"])
        self.assertEqual(request.style_refs, ["bible_style"])
        self.assertEqual(len(request.bible_snapshot), 3)

    def test_composed_prompt_provenance_is_preserved(self) -> None:
        story = _story([_scene("scene_01", order=0)])
        composed = {
            "scene_01": _composed(
                "hero in a forest",
                negative_prompt="blurry",
                seed=42,
                reference_asset_ids=["asset_1"],
                conflicts=["unknown bible entry: bible_ghost"],
            )
        }

        manifest = build_visual_manifest(story, composed)
        request = manifest.requests[0]

        self.assertEqual(request.negative_prompt, "blurry")
        self.assertEqual(request.seed, 42)
        self.assertEqual(request.reference_asset_ids, ["asset_1"])
        self.assertEqual(request.conflicts, ["unknown bible entry: bible_ghost"])

    def test_round_trips_through_json(self) -> None:
        story = _story([_scene("scene_01", order=0)])
        composed = {"scene_01": _composed("a quiet street")}

        manifest = build_visual_manifest(story, composed)
        payload = manifest.model_dump(mode="json")
        restored = SceneVisualManifest.model_validate(payload)

        self.assertEqual(restored, manifest)


class SceneVisualManifestValidationTests(unittest.TestCase):
    def _request(self, **fields) -> SceneVisualRequest:
        defaults = {
            "id": "visreq_1",
            "story_id": "story_1",
            "scene_id": "scene_01",
            "order": 0,
            "prompt": "a prompt",
        }
        defaults.update(fields)
        return SceneVisualRequest(**defaults)

    def test_two_requests_for_the_same_scene_are_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            SceneVisualManifest(
                story_id="story_1",
                requests=[
                    self._request(id="visreq_1", scene_id="scene_01"),
                    self._request(id="visreq_2", scene_id="scene_01"),
                ],
            )
        self.assertIn("scene_01", str(ctx.exception))

    def test_request_with_mismatched_story_id_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SceneVisualManifest(
                story_id="story_1",
                requests=[self._request(story_id="story_other")],
            )

    def test_duplicate_request_id_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SceneVisualManifest(
                story_id="story_1",
                requests=[
                    self._request(id="visreq_1", scene_id="scene_01"),
                    self._request(id="visreq_1", scene_id="scene_02"),
                ],
            )

    def test_unknown_bible_kind_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            BibleSnapshotRef(
                bible_id="bible_1", kind="not-a-real-kind", name="X", version="v1"
            )


if __name__ == "__main__":
    unittest.main()
