"""The seam between build_timeline() and AssemblyGenerator.

`tests/test_story.py` checks the timeline's shape and throws it away;
`tests/test_assembly.py` renders from hand-written dicts. Neither runs a story's
own timeline through the renderer, which is how a scene's camera direction could
be silently discarded (issue #101) without a single test going red.

Everything here is offline: small PNGs and short WAVs at a tiny resolution.
"""

from __future__ import annotations

import math
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from core.schemas import GenerationRequest  # noqa: E402
from core.story import (  # noqa: E402
    StoryDocument,
    apply_text_result,
    build_timeline,
)
from core.storage.json_files import utc_now  # noqa: E402
from generators.video import AssemblyGenerator  # noqa: E402

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as exc:  # pragma: no cover - environment guard
    FFMPEG = None
    FFMPEG_ERROR = exc


_SCENES = {
    "scenes": [
        {
            "heading": "屋上の朝",
            "narration": "朝の光が街を照らしていた。",
            "image_prompt": "rooftop at dawn",
            "bgm_mood": "hopeful",
            "duration_seconds": 2,
            # Free text a language model would plausibly write. The renderer has
            # no such motion, so this is the value issue #101 is about.
            "camera": "slow push in",
        },
        {
            "heading": "路地の追跡",
            "narration": "",
            "image_prompt": "narrow alley at night",
            "bgm_mood": "hopeful",
            "duration_seconds": 2,
            "camera": "pan_left",
        },
    ]
}


def _story() -> StoryDocument:
    now = utc_now()
    return StoryDocument(id="story_seam", created_at=now, updated_at=now)


def _write_png(path: Path, colour: tuple[int, int, int]) -> Path:
    Image.new("RGB", (320, 180), colour).save(path)
    return path


def _write_wav(path: Path, seconds: float, freq: int) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(11000 * math.sin(2 * math.pi * freq * t / 44100)))
                for t in range(int(44100 * seconds))
            )
        )
    return path


class StoryToVideoSeamTests(unittest.TestCase):
    """A story's own timeline must be renderable by the assembly generator."""

    def setUp(self) -> None:
        if FFMPEG is None:  # pragma: no cover - environment guard
            self.skipTest(f"bundled ffmpeg unavailable: {FFMPEG_ERROR}")
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

        story = apply_text_result(_story(), "scene_list", _SCENES)
        self.assets: dict[str, str] = {}
        scenes = []
        for index, scene in enumerate(story.scenes):
            visual = _write_png(
                self.root / f"{scene.id}.png", (40 + index * 60, 90, 160)
            )
            asset_ids = {"visual": f"asset_v{index}"}
            self.assets[f"asset_v{index}"] = str(visual)
            if scene.narration.strip():
                narration = _write_wav(self.root / f"{scene.id}_n.wav", 1.5, 220)
                asset_ids["narration"] = f"asset_n{index}"
                self.assets[f"asset_n{index}"] = str(narration)
            music = _write_wav(self.root / "music.wav", 4.0, 440)
            asset_ids["music"] = "asset_music"
            self.assets["asset_music"] = str(music)
            scenes.append(scene.model_copy(update={"asset_ids": asset_ids}))
        self.story = story.model_copy(update={"scenes": scenes})

    def _render(self) -> tuple[Path, dict]:
        timeline = build_timeline(
            self.story,
            resolution=(320, 180),
            fps=8,
            asset_path_lookup=self.assets.get,
        )
        generator = AssemblyGenerator(output_dir=self.root / "out")
        result = generator.run(
            GenerationRequest(
                media_type="video",
                task_type="assembly",
                prompt="seam",
                model_id="",
                output_format="mp4",
                params={"timeline": timeline},
            )
        )
        return Path(result.outputs[0]), timeline

    def test_story_timeline_renders_to_a_playable_mp4(self) -> None:
        output, timeline = self._render()

        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 0)

        probe = subprocess.run(
            [FFMPEG, "-i", str(output), "-hide_banner"],
            capture_output=True,
            text=True,
        )
        self.assertIn("Video:", probe.stderr)
        self.assertIn("Audio:", probe.stderr, "narration and music must reach the mux")
        self.assertIn(
            "Duration: 00:00:04.0",
            probe.stderr,
            f"expected the scene durations to sum to 4s, got:\n{probe.stderr[:400]}",
        )
        self.assertAlmostEqual(timeline["total_duration_seconds"], 4.0)

    def test_free_text_camera_still_produces_a_moving_shot(self) -> None:
        """Regression for #101.

        The first scene asks for "slow push in", which the renderer does not
        support. Before the fix that value reached assembly untouched and was
        degraded to ``none``, so the shot silently went static. The timeline must
        resolve it to a real motion instead.
        """

        _, timeline = self._render()
        first, second = timeline["tracks"]["visual"]

        self.assertNotEqual(
            first["motion"],
            "none",
            "a free-text camera direction must not collapse into a static shot",
        )
        self.assertIn(first["motion"], {"ken_burns_in", "ken_burns_out", "pan_left", "pan_right"})
        # The writer's original wording survives for review.
        self.assertEqual(first["requested_camera"], "slow push in")
        # A value the renderer already understands passes through untouched.
        self.assertEqual(second["motion"], "pan_left")
        self.assertNotIn("requested_camera", second)

    def test_narration_only_covers_the_scene_that_has_it(self) -> None:
        _, timeline = self._render()

        narration = timeline["tracks"]["narration"]
        self.assertEqual(len(narration), 1)
        self.assertEqual(narration[0]["scene_id"], "scene_01")
        self.assertEqual(narration[0]["start_seconds"], 0.0)

    def test_shared_music_spans_both_scenes_rather_than_restarting(self) -> None:
        _, timeline = self._render()

        music = timeline["tracks"]["music"]
        self.assertEqual(len(music), 1)
        self.assertAlmostEqual(music[0]["duration_seconds"], 4.0)


if __name__ == "__main__":
    unittest.main()
