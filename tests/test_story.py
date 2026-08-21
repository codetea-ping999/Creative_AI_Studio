"""Tests for story documents, text merging, and timeline building."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.story import (  # noqa: E402
    Scene,
    StoryDocument,
    StoryRepository,
    apply_text_result,
    build_timeline,
    count_words,
    missing_scene_assets,
    split_subtitle_lines,
)

_SCENE_PAYLOAD = {
    "scenes": [
        {
            "heading": "屋上の朝",
            "summary": "主人公が決意する",
            "narration": "朝の光が街を照らしていた。\n彼女は静かに息を吸った。",
            "image_prompt": "rooftop at dawn, city skyline",
            "image_negative": "blurry",
            "bgm_mood": "hopeful",
            "duration_seconds": 5,
            "camera": "ken_burns_in",
        },
        {
            "heading": "路地の追跡",
            "summary": "追われる",
            "narration": "足音が近づく。",
            "image_prompt": "narrow alley, night",
            "bgm_mood": "hopeful",
            "duration_seconds": 3,
        },
    ]
}


def _story(**fields) -> StoryDocument:
    from core.storage.json_files import utc_now

    now = utc_now()
    return StoryDocument(id="story_test", created_at=now, updated_at=now, **fields)


class StoryRepositoryTests(unittest.TestCase):
    def test_create_get_update_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = StoryRepository(Path(root) / "stories")
            created = repository.create(title="First", premise="a premise")

            self.assertEqual(repository.get(created.id).title, "First")

            updated = repository.update(created.id, title="Renamed", genre="SF")
            self.assertEqual(updated.title, "Renamed")
            self.assertEqual(updated.genre, "SF")
            self.assertGreaterEqual(updated.updated_at, created.updated_at)

            repository.create(title="Second", project_id="proj_1")
            self.assertEqual(len(repository.list_all()), 2)
            self.assertEqual(len(repository.list_all(project_id="proj_1")), 1)
            self.assertEqual(len(repository.list_all(query_text="renamed")), 1)
            self.assertEqual(len(repository.list_all(limit=1)), 1)

            self.assertTrue(repository.delete(created.id))
            self.assertFalse(repository.delete(created.id))
            self.assertIsNone(repository.get(created.id))

    def test_unknown_field_and_format_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = StoryRepository(Path(root) / "stories")
            with self.assertRaises(ValueError):
                repository.create(title="x", nonsense=1)
            with self.assertRaises(ValueError):
                repository.create(title="x", format="hologram")

    def test_corrupt_file_is_isolated_from_listing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            story_dir = Path(root) / "stories"
            repository = StoryRepository(story_dir)
            repository.create(title="Healthy")
            (story_dir / "broken.json").write_text("{not json", encoding="utf-8")

            self.assertEqual(len(repository.list_all()), 1)
            self.assertIsNone(repository.get("broken"))

    def test_update_ignores_reserved_fields(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = StoryRepository(Path(root) / "stories")
            created = repository.create(title="Keep id")
            updated = repository.update(created.id, id="hijacked", title="Keep id")
            self.assertEqual(updated.id, created.id)


class ApplyTextResultTests(unittest.TestCase):
    def test_logline_keeps_all_candidates(self) -> None:
        story = _story()
        merged = apply_text_result(
            story,
            "logline",
            {
                "loglines": [
                    {"text": "A", "hook": "h", "tone": "t"},
                    {"text": "B", "hook": "h", "tone": "t"},
                ]
            },
            job_id="job_1",
        )

        self.assertEqual(merged.logline, "A")
        self.assertEqual(len(merged.metadata["logline_candidates"]), 2)
        self.assertEqual(merged.source_job_ids, ["job_1"])
        # input untouched
        self.assertEqual(story.logline, "")
        self.assertEqual(story.source_job_ids, [])

    def test_beat_sheet_assigns_stable_ids_and_order(self) -> None:
        merged = apply_text_result(
            _story(),
            "beat_sheet",
            {
                "beats": [
                    {"act": "1", "purpose": "setup", "summary": "s1"},
                    {"act": "2", "purpose": "confront", "summary": "s2"},
                ]
            },
        )
        self.assertEqual([beat.id for beat in merged.beats], ["beat_01", "beat_02"])
        self.assertEqual([beat.order for beat in merged.beats], [0, 1])

    def test_scene_list_preserves_generated_asset_lineage(self) -> None:
        story = _story(
            scenes=[
                Scene(
                    id="scene_01",
                    order=0,
                    asset_ids={"visual": "asset_a", "music": "asset_m"},
                    job_ids=["job_old"],
                    bible_refs=["bible_1"],
                )
            ]
        )
        merged = apply_text_result(story, "scene_list", _SCENE_PAYLOAD)

        self.assertEqual(len(merged.scenes), 2)
        self.assertEqual(merged.scenes[0].asset_ids["visual"], "asset_a")
        self.assertEqual(merged.scenes[0].job_ids, ["job_old"])
        self.assertEqual(merged.scenes[0].bible_refs, ["bible_1"])
        self.assertEqual(merged.scenes[0].heading, "屋上の朝")
        # A newly added scene starts with no lineage.
        self.assertEqual(merged.scenes[1].asset_ids, {})

    def test_scene_narration_line_breaks_are_collapsed(self) -> None:
        merged = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        self.assertNotIn("\n", merged.scenes[0].narration)
        self.assertIn("朝の光が街を照らしていた。彼女は", merged.scenes[0].narration)

    def test_scene_duration_falls_back_when_invalid(self) -> None:
        merged = apply_text_result(
            _story(),
            "scene_list",
            {"scenes": [{"heading": "h", "duration_seconds": "nope"}]},
        )
        self.assertEqual(merged.scenes[0].duration_seconds, 4.0)

    def test_prose_replaces_chapter_matched_by_title(self) -> None:
        story = apply_text_result(
            _story(),
            "prose",
            {"title": "第一章", "prose_markdown": "吾輩は猫である。"},
        )
        self.assertEqual(len(story.chapters), 1)
        first_word_count = story.chapters[0].word_count

        story = apply_text_result(
            story,
            "prose",
            {"title": "第一章", "prose_markdown": "吾輩は猫である。名前はまだ無い。"},
        )
        self.assertEqual(len(story.chapters), 1)
        self.assertGreater(story.chapters[0].word_count, first_word_count)

        story = apply_text_result(
            story,
            "prose",
            {"title": "第二章", "prose_markdown": "その後の話。"},
        )
        self.assertEqual(len(story.chapters), 2)
        self.assertEqual(story.chapters[1].order, 1)

    def test_script_attaches_dialogue_to_named_scene(self) -> None:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        merged = apply_text_result(
            story,
            "script",
            {
                "scene_id": "scene_02",
                "lines": [{"speaker": "ミナ", "text": "こっちへ", "direction": "小声で"}],
            },
        )
        self.assertEqual(merged.scenes[0].dialogue, [])
        self.assertEqual(merged.scenes[1].dialogue[0].speaker, "ミナ")
        self.assertEqual(merged.scenes[1].dialogue[0].direction, "小声で")

    def test_script_naming_a_missing_scene_is_rejected(self) -> None:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        with self.assertRaises(ValueError) as context:
            apply_text_result(
                story,
                "script",
                {
                    "scene_id": "scene_99",
                    "lines": [{"speaker": "ミナ", "text": "こっちへ"}],
                },
            )
        # Parking the lines in metadata would look like a successful merge while
        # the named scene stayed empty.
        self.assertIn("scene_99", str(context.exception))
        self.assertIn("scene_01", str(context.exception))

    def test_script_naming_a_missing_scene_index_is_rejected(self) -> None:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        with self.assertRaises(ValueError) as context:
            apply_text_result(
                story,
                "script",
                {"scene_index": 7, "lines": [{"speaker": "A", "text": "x"}]},
            )
        self.assertIn("7", str(context.exception))

    def test_script_without_target_is_parked_in_metadata(self) -> None:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        merged = apply_text_result(
            story,
            "script",
            {"lines": [{"speaker": "A", "text": "x"}]},
        )
        self.assertEqual(len(merged.metadata["unassigned_script_lines"]), 1)

    def test_character_sheet_drafts_are_deduplicated_by_name(self) -> None:
        story = apply_text_result(
            _story(),
            "character_sheet",
            {"name": "ミナ", "summary": "v1"},
        )
        story = apply_text_result(
            story,
            "character_sheet",
            {"name": "ミナ", "summary": "v2"},
        )
        self.assertEqual(len(story.metadata["character_drafts"]), 1)
        self.assertEqual(story.metadata["character_drafts"][0]["summary"], "v2")

    def test_unknown_task_lists_supported_tasks(self) -> None:
        with self.assertRaises(ValueError) as context:
            apply_text_result(_story(), "screenplay", {})
        self.assertIn("scene_list", str(context.exception))

    def test_empty_payloads_are_rejected(self) -> None:
        for task, payload in (
            ("logline", {"loglines": []}),
            ("beat_sheet", {"beats": []}),
            ("scene_list", {"scenes": []}),
            ("prose", {"prose_markdown": "  "}),
            ("script", {"lines": []}),
            ("character_sheet", {"name": ""}),
        ):
            with self.subTest(task=task):
                with self.assertRaises(ValueError):
                    apply_text_result(_story(), task, payload)

    def test_source_job_ids_are_deduplicated(self) -> None:
        story = apply_text_result(
            _story(), "scene_list", _SCENE_PAYLOAD, job_id="job_1"
        )
        story = apply_text_result(story, "scene_list", _SCENE_PAYLOAD, job_id="job_1")
        self.assertEqual(story.source_job_ids, ["job_1"])


class TimelineTests(unittest.TestCase):
    def _ready_story(self) -> StoryDocument:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        scenes = []
        for index, scene in enumerate(story.scenes):
            scenes.append(
                scene.model_copy(
                    update={
                        "asset_ids": {
                            "visual": f"asset_v{index}",
                            "narration": f"asset_n{index}",
                            "music": "asset_music",
                        }
                    }
                )
            )
        return story.model_copy(update={"scenes": scenes})

    def test_timeline_structure_and_duration_invariant(self) -> None:
        story = self._ready_story()
        timeline = build_timeline(story, resolution=(1080, 1920), fps=24)

        self.assertEqual(timeline["resolution"], [1080, 1920])
        self.assertEqual(timeline["fps"], 24)
        self.assertEqual(len(timeline["tracks"]["visual"]), 2)
        self.assertAlmostEqual(
            timeline["total_duration_seconds"],
            story.total_duration_seconds(),
        )
        self.assertAlmostEqual(
            sum(entry["duration_seconds"] for entry in timeline["tracks"]["visual"]),
            story.total_duration_seconds(),
        )

    def test_narration_start_times_accumulate(self) -> None:
        timeline = build_timeline(self._ready_story())
        starts = [entry["start_seconds"] for entry in timeline["tracks"]["narration"]]
        self.assertEqual(starts, [0.0, 5.0])

    def test_shared_music_becomes_one_spanning_entry(self) -> None:
        timeline = build_timeline(self._ready_story())
        music = timeline["tracks"]["music"]
        self.assertEqual(len(music), 1)
        self.assertAlmostEqual(music[0]["duration_seconds"], 8.0)
        self.assertTrue(music[0]["duck"])
        self.assertNotIn("_open", music[0])

    def test_music_run_restarts_when_track_changes(self) -> None:
        story = self._ready_story()
        scenes = list(story.scenes)
        scenes[1] = scenes[1].model_copy(
            update={"asset_ids": {**scenes[1].asset_ids, "music": "asset_other"}}
        )
        timeline = build_timeline(story.model_copy(update={"scenes": scenes}))
        self.assertEqual(len(timeline["tracks"]["music"]), 2)
        self.assertEqual(timeline["tracks"]["music"][1]["start_seconds"], 5.0)

    def test_subtitles_split_japanese_narration(self) -> None:
        timeline = build_timeline(self._ready_story())
        subtitles = timeline["tracks"]["subtitles"]
        self.assertGreaterEqual(len(subtitles), 2)
        self.assertTrue(all(entry["end_seconds"] > entry["start_seconds"] for entry in subtitles))
        scene_one = [entry for entry in subtitles if entry["scene_id"] == "scene_01"]
        self.assertAlmostEqual(scene_one[-1]["end_seconds"], 5.0)

    def test_subtitles_can_be_disabled(self) -> None:
        timeline = build_timeline(self._ready_story(), include_subtitles=False)
        self.assertEqual(timeline["tracks"]["subtitles"], [])

    def test_asset_path_lookup_is_applied(self) -> None:
        timeline = build_timeline(
            self._ready_story(),
            asset_path_lookup=lambda asset_id: f"/outputs/{asset_id}.png",
        )
        self.assertEqual(
            timeline["tracks"]["visual"][0]["path"], "/outputs/asset_v0.png"
        )

    def test_missing_visual_raises_with_scene_ids(self) -> None:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        with self.assertRaises(ValueError) as context:
            build_timeline(story)
        self.assertIn("scene_01", str(context.exception))
        self.assertIn("scene_02", str(context.exception))

    def test_story_without_scenes_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_timeline(_story())

    def test_missing_scene_assets_reports_roles(self) -> None:
        story = apply_text_result(_story(), "scene_list", _SCENE_PAYLOAD)
        missing = missing_scene_assets(story)
        self.assertIn({"scene_id": "scene_01", "role": "visual"}, missing)
        self.assertIn({"scene_id": "scene_01", "role": "narration"}, missing)

        ready = self._ready_story()
        self.assertEqual(missing_scene_assets(ready), [])


class TextUtilsTests(unittest.TestCase):
    def test_count_words_handles_japanese_and_english(self) -> None:
        self.assertEqual(count_words("hello world"), 2)
        self.assertEqual(count_words("# 第一章\n吾輩は猫である。"), 10)
        self.assertEqual(count_words("---"), 0)

    def test_split_subtitle_lines_respects_cjk_budget(self) -> None:
        text = "朝の光が街をゆっくりと照らしていた。彼女は静かに息を吸ってから歩き出した。"
        lines = split_subtitle_lines(text)
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 21 for line in lines))

    def test_split_subtitle_lines_wraps_latin_text(self) -> None:
        lines = split_subtitle_lines("word " * 30)
        self.assertTrue(all(len(line) <= 42 for line in lines))

    def test_split_subtitle_lines_on_empty_text(self) -> None:
        self.assertEqual(split_subtitle_lines("   "), [])


if __name__ == "__main__":
    unittest.main()
