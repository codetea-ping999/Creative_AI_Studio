"""Tests for the timeline assembly generator.

Everything here runs offline against tiny synthesized assets: a few 1-second
clips at 256x144 and 8 fps keep the full encode-and-mux path honest while still
finishing in seconds.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
import wave

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import duck_envelope  # noqa: E402
from core.schemas import GenerationRequest  # noqa: E402
from generators.video import assembly as assembly_module  # noqa: E402
from generators.video.assembly import AssemblyGenerator  # noqa: E402

FFMPEG_ERROR: Exception | None = None
FFMPEG_EXE: str | None = None
try:
    import imageio.v2 as imageio_v2
    import imageio.v3 as imageio_v3
    import imageio_ffmpeg

    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as exc:  # noqa: BLE001 - any failure means we cannot encode at all
    FFMPEG_ERROR = exc

_SAMPLE_RATE = 44_100


def _write_still(
    path: Path,
    *,
    size: tuple[int, int] = (320, 180),
    tint: tuple[int, int, int] = (0, 0, 0),
) -> None:
    """Write a gradient png so cropping and panning produce visible change."""

    width, height = size
    grid_x, grid_y = np.meshgrid(np.arange(width), np.arange(height))
    data = np.stack(
        [
            (grid_x * 3 + tint[0]) % 256,
            (grid_y * 5 + tint[1]) % 256,
            ((grid_x + grid_y) * 2 + tint[2]) % 256,
        ],
        axis=2,
    ).astype(np.uint8)
    Image.fromarray(data).save(path)


def _write_solid(path: Path, color: tuple[int, int, int], size=(320, 180)) -> None:
    Image.new("RGB", size, color).save(path)


def _write_wav(
    path: Path,
    *,
    seconds: float,
    frequency: float = 440.0,
    amplitude: float = 0.5,
    sample_rate: int = 22_050,
    channels: int = 1,
) -> None:
    frame_count = max(1, int(seconds * sample_rate))
    times = np.arange(frame_count) / sample_rate
    tone = (amplitude * np.sin(2 * np.pi * frequency * times)).astype(np.float32)
    data = np.stack([tone] * channels, axis=1)
    pcm = (np.clip(data, -1.0, 1.0) * 32_767).astype("<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def _write_constant_wav(
    path: Path,
    *,
    seconds: float,
    value: float,
    sample_rate: int = _SAMPLE_RATE,
) -> None:
    """Write a DC wav, so anything multiplied into it is readable off the mix."""

    frame_count = max(1, int(round(seconds * sample_rate)))
    # 32_768 rather than 32_767: a power of two round-trips 0.5 exactly.
    pcm = np.full((frame_count, 1), round(value * 32_768), dtype="<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def _write_video_source(
    path: Path,
    *,
    frames: int = 12,
    size: tuple[int, int] = (320, 180),
    fps: int = 12,
) -> None:
    """Write a grey ramp video so sampling across its frames is measurable."""

    writer = imageio_v2.get_writer(
        str(path),
        fps=fps,
        codec="libx264",
        quality=9,
        macro_block_size=2,
        pixelformat="yuv420p",
        ffmpeg_log_level="error",
    )
    try:
        for index in range(frames):
            level = int(round(255 * index / max(1, frames - 1)))
            writer.append_data(
                np.full((size[1], size[0], 3), level, dtype=np.uint8)
            )
    finally:
        writer.close()


def _timeline(
    visual: list[dict],
    *,
    resolution: tuple[int, int] = (256, 144),
    fps: int = 8,
    narration: list[dict] | None = None,
    music: list[dict] | None = None,
    subtitles: list[dict] | None = None,
) -> dict:
    total = sum(float(entry.get("duration_seconds", 0.0)) for entry in visual)
    return {
        "resolution": [resolution[0], resolution[1]],
        "fps": fps,
        "total_duration_seconds": total,
        "tracks": {
            "visual": visual,
            "narration": list(narration or []),
            "music": list(music or []),
            "subtitles": list(subtitles or []),
        },
    }


def _visual(
    scene_id: str,
    path: Path,
    *,
    duration: float = 1.0,
    transition: str = "cut",
    motion: str = "none",
) -> dict:
    return {
        "scene_id": scene_id,
        "asset_id": f"asset_{scene_id}",
        "path": str(path),
        "duration_seconds": duration,
        "transition": transition,
        "motion": motion,
    }


def _request(timeline: dict, **overrides) -> GenerationRequest:
    payload = {
        "media_type": "video",
        "task_type": "assembly",
        "prompt": "assemble the story",
        "model_id": "",
        "output_format": "mp4",
        "params": {"timeline": timeline},
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


def _probe(path: Path) -> str:
    """Return the ffmpeg stream report for a file (ffmpeg exits 1 by design)."""

    assert FFMPEG_EXE is not None
    completed = subprocess.run(
        [FFMPEG_EXE, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stderr


def _has_audio_stream(path: Path) -> bool:
    return "Audio:" in _probe(path)


def _extract_audio(mp4_path: Path, wav_path: Path) -> np.ndarray:
    assert FFMPEG_EXE is not None
    subprocess.run(
        [
            FFMPEG_EXE, "-y", "-hide_banner", "-nostdin", "-i", str(mp4_path),
            "-vn", "-ac", "2", "-ar", str(_SAMPLE_RATE), str(wav_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    with wave.open(str(wav_path), "rb") as wav_file:
        raw = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, 2) / 32_768.0


def _window_rms(samples: np.ndarray, start_seconds: float, end_seconds: float) -> float:
    begin = int(start_seconds * _SAMPLE_RATE)
    end = min(samples.shape[0], int(end_seconds * _SAMPLE_RATE))
    window = samples[begin:end]
    if window.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(window))))


def _read_frames(path: Path) -> list[np.ndarray]:
    return [np.asarray(frame) for frame in imageio_v3.imiter(path)]


@unittest.skipUnless(
    FFMPEG_EXE is not None,
    f"bundled ffmpeg is unavailable, so assembly cannot run: {FFMPEG_ERROR}",
)
class AssemblyGeneratorTests(unittest.TestCase):
    def test_renders_three_cut_mp4_with_audio_matching_timeline_duration(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            stills = []
            for index in range(3):
                still = workspace / f"scene_{index}.png"
                _write_still(still, tint=(index * 60, index * 30, index * 90))
                stills.append(still)

            narration = workspace / "narration.wav"
            music = workspace / "music.wav"
            _write_wav(narration, seconds=1.0, frequency=220.0, amplitude=0.08)
            _write_wav(music, seconds=0.75, frequency=110.0, amplitude=0.4)

            timeline = _timeline(
                [
                    _visual(
                        f"sc_{index}",
                        still,
                        transition="crossfade" if index < 2 else "cut",
                        motion="ken_burns_in",
                    )
                    for index, still in enumerate(stills)
                ],
                narration=[
                    {
                        "scene_id": "sc_1",
                        "asset_id": "aud_narration",
                        "path": str(narration),
                        "start_seconds": 1.0,
                    }
                ],
                music=[
                    {
                        "asset_id": "aud_music",
                        "path": str(music),
                        "start_seconds": 0.0,
                        "duration_seconds": 3.0,
                        "gain_db": -6.0,
                        "loop": True,
                        "duck": True,
                    }
                ],
            )
            generator = AssemblyGenerator(workspace / "outputs")
            result = generator.run(_request(timeline))

            output_path = Path(result.outputs[0])
            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)
            self.assertEqual(output_path.suffix, ".mp4")
            self.assertIn("Video:", _probe(output_path))
            self.assertTrue(_has_audio_stream(output_path))

            frames = _read_frames(output_path)
            self.assertEqual(len(frames), 24)
            self.assertEqual(frames[0].shape[:2], (144, 256))

            rendered_seconds = len(frames) / timeline["fps"]
            self.assertAlmostEqual(
                rendered_seconds,
                timeline["total_duration_seconds"],
                delta=1 / timeline["fps"],
            )

            metadata = result.metadata
            self.assertEqual(metadata["generator"], "AssemblyGenerator")
            self.assertEqual(metadata["media_type"], "video")
            self.assertEqual(metadata["task_type"], "assembly")
            self.assertEqual(metadata["output_format"], "mp4")
            self.assertEqual(metadata["model_id"], "")
            self.assertEqual(metadata["scene_count"], 3)
            self.assertEqual(metadata["resolution"], [256, 144])
            self.assertEqual(metadata["fps"], 8)
            self.assertEqual(metadata["frame_count"], 24)
            self.assertAlmostEqual(metadata["duration_seconds_rendered"], 3.0, places=3)
            self.assertTrue(metadata["has_narration"])
            self.assertTrue(metadata["has_music"])
            self.assertTrue(metadata["audio_ducked"])
            self.assertEqual(metadata["subtitle_count"], 0)
            self.assertIn("quality_score", metadata["quality_report"])
            self.assertIn("timeline", metadata["params"])

            preview_path = Path(result.previews[0])
            self.assertTrue(preview_path.is_file())
            with Image.open(preview_path) as preview:
                self.assertEqual(preview.size, (256, 144))

    def test_ken_burns_moves_within_a_single_clip(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_still(still)

            timeline = _timeline(
                [_visual("sc_0", still, duration=1.0, motion="ken_burns_in")]
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            frames = _read_frames(Path(result.outputs[0]))
            difference = np.mean(
                np.abs(frames[0].astype(np.int16) - frames[-1].astype(np.int16))
            )
            self.assertGreater(difference, 1.0)

    def test_crossfade_frame_differs_from_both_neighbours(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            first = workspace / "red.png"
            second = workspace / "blue.png"
            _write_solid(first, (230, 20, 20))
            _write_solid(second, (20, 20, 230))

            timeline = _timeline(
                [
                    _visual("sc_0", first, transition="crossfade"),
                    _visual("sc_1", second),
                ]
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            frames = _read_frames(Path(result.outputs[0]))
            self.assertEqual(len(frames), 16)

            # fps // 2 == 4, so frames 4..7 blend the tail of the first clip into
            # the head of the second while both clips still play in full.
            outgoing = frames[0].astype(np.int16)
            incoming = frames[8].astype(np.int16)
            blended = frames[6].astype(np.int16)

            self.assertGreater(np.mean(np.abs(blended - outgoing)), 10.0)
            self.assertGreater(np.mean(np.abs(blended - incoming)), 10.0)
            # The blend sits between the two solid colours instead of jumping.
            self.assertLess(
                float(np.mean(blended[..., 0])), float(np.mean(outgoing[..., 0]))
            )
            self.assertGreater(
                float(np.mean(blended[..., 2])), float(np.mean(outgoing[..., 2]))
            )

    def test_subtitles_change_only_the_lower_region(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_still(still)
            visual = [_visual("sc_0", still, duration=1.0)]

            plain = AssemblyGenerator(workspace / "plain").run(
                _request(_timeline(list(visual)))
            )
            captioned = AssemblyGenerator(workspace / "captioned").run(
                _request(
                    _timeline(
                        list(visual),
                        subtitles=[
                            {
                                "scene_id": "sc_0",
                                "text": "a narrated line that must wrap across the frame",
                                "start_seconds": 0.0,
                                "end_seconds": 1.0,
                            }
                        ],
                    )
                )
            )

            self.assertEqual(captioned.metadata["subtitle_count"], 1)

            # The previews are lossless pngs of the same rendered first frame, so
            # only the burned-in text may differ.
            with Image.open(plain.previews[0]) as image:
                plain_pixels = np.asarray(image.convert("RGB")).astype(np.int16)
            with Image.open(captioned.previews[0]) as image:
                captioned_pixels = np.asarray(image.convert("RGB")).astype(np.int16)

            height = plain_pixels.shape[0]
            upper_delta = np.mean(
                np.abs(captioned_pixels[: height // 2] - plain_pixels[: height // 2])
            )
            lower_delta = np.mean(
                np.abs(
                    captioned_pixels[height * 3 // 4 :] - plain_pixels[height * 3 // 4 :]
                )
            )
            self.assertEqual(upper_delta, 0.0)
            self.assertGreater(lower_delta, 1.0)

    def test_narration_only_timeline_has_an_audio_stream(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            narration = workspace / "narration.wav"
            _write_still(still)
            _write_wav(narration, seconds=1.5, frequency=180.0)

            timeline = _timeline(
                [_visual("sc_0", still, duration=2.0)],
                narration=[
                    {
                        "scene_id": "sc_0",
                        "asset_id": "aud_narration",
                        "path": str(narration),
                        "start_seconds": 0.25,
                    }
                ],
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            self.assertTrue(result.metadata["has_narration"])
            self.assertFalse(result.metadata["has_music"])
            self.assertTrue(_has_audio_stream(Path(result.outputs[0])))

    def test_music_only_timeline_loops_and_has_an_audio_stream(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            music = workspace / "music.wav"
            _write_still(still)
            # Shorter than the timeline, so it only fills the video when looped.
            _write_wav(music, seconds=0.75, frequency=110.0, channels=2)

            timeline = _timeline(
                [_visual("sc_0", still, duration=2.0)],
                music=[
                    {
                        "asset_id": "aud_music",
                        "path": str(music),
                        "start_seconds": 0.0,
                        "duration_seconds": 2.0,
                        "gain_db": -6.0,
                        "loop": True,
                        "duck": True,
                    }
                ],
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            self.assertTrue(result.metadata["has_music"])
            self.assertFalse(result.metadata["has_narration"])
            # Ducking needs narration to duck under; there is none here.
            self.assertFalse(result.metadata["audio_ducked"])
            self.assertTrue(_has_audio_stream(Path(result.outputs[0])))

    def test_timeline_without_audio_has_no_audio_stream(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_still(still)

            result = AssemblyGenerator(workspace / "outputs").run(
                _request(_timeline([_visual("sc_0", still)]))
            )

            self.assertEqual(result.metadata["audio_sample_rate"], None)
            self.assertFalse(_has_audio_stream(Path(result.outputs[0])))

    def test_ducking_lowers_music_level_during_narration(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            music = workspace / "music.wav"
            narration = workspace / "narration.wav"
            _write_still(still)
            _write_wav(music, seconds=3.0, frequency=120.0, amplitude=0.5)
            # A quiet narration keeps the measurement about the music dipping
            # rather than about the voice adding level.
            _write_wav(narration, seconds=1.0, frequency=600.0, amplitude=0.04)

            timeline = _timeline(
                [_visual("sc_0", still, duration=3.0)],
                narration=[
                    {
                        "scene_id": "sc_0",
                        "asset_id": "aud_narration",
                        "path": str(narration),
                        "start_seconds": 1.0,
                    }
                ],
                music=[
                    {
                        "asset_id": "aud_music",
                        "path": str(music),
                        "start_seconds": 0.0,
                        "duration_seconds": 3.0,
                        "gain_db": 0.0,
                        "loop": False,
                        "duck": True,
                    }
                ],
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))
            self.assertTrue(result.metadata["audio_ducked"])

            samples = _extract_audio(
                Path(result.outputs[0]), workspace / "extracted.wav"
            )
            inside = _window_rms(samples, 1.3, 1.9)
            outside = _window_rms(samples, 2.4, 2.9)

            self.assertGreater(outside, 0.0)
            self.assertLess(inside, outside * 0.75)

    def test_ducking_uses_the_shared_core_audio_envelope(self) -> None:
        """The mix must match core.audio's curve, not a second copy of the math.

        Two ducking implementations drift: this pins assembly's music gain to the
        shared builder so a change to the ramp or the depth moves both.
        """

        with TemporaryDirectory() as root:
            workspace = Path(root)
            music = workspace / "music.wav"
            narration = workspace / "narration.wav"
            # Constant music and silent narration make the mix *be* the gain
            # curve, so the comparison is exact instead of statistical.
            _write_constant_wav(music, seconds=3.0, value=0.5)
            _write_constant_wav(narration, seconds=1.0, value=0.0)

            generator = AssemblyGenerator(
                workspace / "outputs", sample_rate=_SAMPLE_RATE
            )
            # 0.05 s in is closer to the head than the 0.12 s attack, so this also
            # covers the truncated ramp.
            narration_start = round(0.05 * _SAMPLE_RATE)
            tracks = {
                "narration": [
                    {
                        "scene_id": "sc_0",
                        "asset_id": "aud_narration",
                        "path": str(narration),
                        "start_seconds": 0.05,
                    }
                ],
                "music": [
                    {
                        "asset_id": "aud_music",
                        "path": str(music),
                        "start_seconds": 0.0,
                        "duration_seconds": 3.0,
                        "gain_db": 0.0,
                        "loop": False,
                        "duck": True,
                    }
                ],
            }

            mix = generator._build_audio_mix(tracks, total_seconds=3.0)

            self.assertIsNotNone(mix)
            self.assertTrue(mix.ducked)
            gain = mix.samples[:, 0] / 0.5
            expected = duck_envelope(
                [(narration_start, narration_start + _SAMPLE_RATE)],
                mix.samples.shape[0],
                _SAMPLE_RATE,
                reduction_db=assembly_module._DUCK_GAIN_DB,
                attack_seconds=assembly_module._DUCK_RAMP_SECONDS,
                release_seconds=assembly_module._DUCK_RAMP_SECONDS,
            )

            self.assertTrue(
                np.allclose(gain, expected, atol=1e-6),
                f"gain diverges from core.audio.duck_envelope by "
                f"{float(np.max(np.abs(gain - expected)))}",
            )
            # The clipped attack keeps the shared slope, so the mix opens part-way
            # down rather than at unity.
            self.assertLess(float(gain[0]), 1.0)

    def test_video_source_is_sampled_across_its_frames(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            source = workspace / "source.mp4"
            _write_video_source(source, frames=12)

            timeline = _timeline([_visual("sc_0", source, duration=1.0)])
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            frames = _read_frames(Path(result.outputs[0]))
            self.assertEqual(len(frames), 8)

            levels = [round(float(np.mean(frame))) for frame in frames]
            self.assertGreater(len(set(levels)), 5, f"frames look frozen: {levels}")
            self.assertGreater(levels[-1] - levels[0], 100)

    def test_moving_source_decoder_retains_only_a_bounded_cache(self) -> None:
        with TemporaryDirectory() as root:
            source = Path(root) / "source.mp4"
            _write_video_source(source, frames=20)
            frames = assembly_module._MovingSourceFrames(source)
            try:
                for index in range(frames.frame_count):
                    frames.get(index)
                self.assertLessEqual(
                    len(frames._cache),
                    assembly_module._MAX_DECODED_SOURCE_FRAMES,
                )
                self.assertLess(len(frames._cache), frames.frame_count)
            finally:
                frames.close()

    def test_asset_path_lookup_resolves_entries_without_a_path(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            narration = workspace / "narration.wav"
            _write_still(still)
            _write_wav(narration, seconds=0.5)
            lookup = {"asset_visual": str(still), "asset_narration": str(narration)}

            timeline = _timeline(
                [
                    {
                        "scene_id": "sc_0",
                        "asset_id": "asset_visual",
                        "duration_seconds": 1.0,
                        "transition": "cut",
                        "motion": "pan_right",
                    }
                ],
                narration=[
                    {
                        "scene_id": "sc_0",
                        "asset_id": "asset_narration",
                        "start_seconds": 0.0,
                    }
                ],
            )
            generator = AssemblyGenerator(
                workspace / "outputs",
                asset_path_lookup=lookup.get,
            )
            result = generator.run(_request(timeline))

            self.assertTrue(Path(result.outputs[0]).is_file())
            self.assertTrue(result.metadata["has_narration"])

    def test_asset_lookup_is_preferred_over_a_direct_path(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            direct = workspace / "direct.png"
            registered = workspace / "registered.png"
            _write_solid(direct, (255, 0, 0))
            _write_solid(registered, (0, 0, 255))
            generator = AssemblyGenerator(
                workspace / "outputs",
                asset_path_lookup=lambda asset_id: (
                    str(registered) if asset_id == "asset_visual" else None
                ),
            )

            resolved = generator._resolve_source(
                {"asset_id": "asset_visual", "path": str(direct)}
            )

            self.assertEqual(resolved, registered)

    def test_direct_paths_can_be_disabled_for_production(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            direct = workspace / "direct.png"
            _write_still(direct)
            generator = AssemblyGenerator(
                workspace / "outputs",
                allow_direct_paths=False,
            )

            self.assertIsNone(
                generator._resolve_source(
                    {"asset_id": "unregistered", "path": str(direct)}
                )
            )

    def test_missing_visual_raises_naming_every_scene(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_still(still)

            timeline = _timeline(
                [
                    _visual("sc_ok", still),
                    {
                        "scene_id": "sc_gone",
                        "asset_id": "asset_gone",
                        "path": str(workspace / "missing.png"),
                        "duration_seconds": 1.0,
                    },
                    {
                        "scene_id": "sc_unknown",
                        "asset_id": "asset_unknown",
                        "duration_seconds": 1.0,
                    },
                ]
            )
            generator = AssemblyGenerator(workspace / "outputs")

            with self.assertRaises(ValueError) as raised:
                generator.run(_request(timeline))

            message = str(raised.exception)
            self.assertIn("sc_gone", message)
            self.assertIn("sc_unknown", message)
            self.assertNotIn("sc_ok", message)

    def test_unresolved_narration_is_reported_instead_of_aborting(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_still(still)

            timeline = _timeline(
                [_visual("sc_0", still)],
                narration=[
                    {
                        "scene_id": "sc_0",
                        "asset_id": "aud_gone",
                        "path": str(workspace / "missing.wav"),
                        "start_seconds": 0.0,
                    }
                ],
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            # A missing narration still leaves a watchable video, unlike a missing
            # visual, so it is reported rather than raised.
            self.assertFalse(result.metadata["has_narration"])
            self.assertEqual(result.metadata["unresolved_audio_assets"], ["aud_gone"])
            self.assertFalse(_has_audio_stream(Path(result.outputs[0])))

    def test_mux_failure_reports_the_ffmpeg_output(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            broken = workspace / "broken.mp4"
            broken.write_bytes(b"not a video at all")
            generator = AssemblyGenerator(workspace / "outputs")

            with self.assertRaises(RuntimeError) as raised:
                generator._mux(broken, None, workspace / "final.mp4")

            message = str(raised.exception)
            self.assertIn("ffmpeg failed to mux final.mp4", message)
            self.assertIn("Invalid data found when processing input", message)

    def test_mux_timeout_removes_partial_output(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            video = workspace / "video.mp4"
            video.write_bytes(b"placeholder")
            output = workspace / "final.mp4"
            generator = AssemblyGenerator(
                workspace / "outputs",
                ffmpeg_timeout_seconds=0.25,
            )

            def time_out(command, **kwargs):
                Path(command[-1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])

            with mock.patch.object(
                assembly_module.subprocess, "run", side_effect=time_out
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    generator._mux(video, None, output)

            self.assertFalse(output.exists())
            self.assertEqual(list(workspace.glob(".*.partial.mp4")), [])

    def test_mux_replaces_the_final_file_only_after_ffmpeg_succeeds(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            video = workspace / "video.mp4"
            video.write_bytes(b"placeholder")
            output = workspace / "final.mp4"
            output.write_bytes(b"previous-complete-delivery")
            generator = AssemblyGenerator(workspace / "outputs")

            def succeed(command, **kwargs):
                self.assertEqual(output.read_bytes(), b"previous-complete-delivery")
                Path(command[-1]).write_bytes(b"new-complete-delivery")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(
                assembly_module.subprocess, "run", side_effect=succeed
            ):
                generator._mux(video, None, output)

            self.assertEqual(output.read_bytes(), b"new-complete-delivery")
            self.assertEqual(list(workspace.glob(".*.partial.mp4")), [])

    def test_music_is_clamped_to_remaining_mix_before_length_fitting(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            music = workspace / "music.wav"
            _write_wav(music, seconds=0.1)
            generator = AssemblyGenerator(workspace / "outputs", sample_rate=10)
            tracks = {
                "narration": [],
                "music": [
                    {
                        "asset_id": "music",
                        "path": str(music),
                        "start_seconds": 9.5,
                        "duration_seconds": 10**12,
                        "loop": True,
                    }
                ],
            }
            bounded_samples = np.ones((5, 2), dtype=np.float32)

            with mock.patch.object(
                generator, "_read_audio", return_value=bounded_samples
            ) as read_audio:
                mix = generator._build_audio_mix(tracks, total_seconds=10.0)

            self.assertIsNotNone(mix)
            self.assertEqual(mix.samples.shape, (100, 2))
            read_audio.assert_called_once_with(music, max_output_samples=5)
            self.assertTrue(np.all(mix.samples[:95] == 0.0))

    def test_audio_mix_rejects_a_sample_budget_over_the_memory_cap(self) -> None:
        generator = AssemblyGenerator("outputs/videos")
        tracks = {
            "narration": [],
            "music": [{"asset_id": "never-resolved"}],
        }

        with self.assertRaisesRegex(ValueError, "bounded sample budget"):
            generator._build_audio_mix(
                tracks,
                total_seconds=assembly_module._MAX_TOTAL_DURATION_SECONDS + 0.1,
            )

    def test_odd_resolution_still_encodes(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_still(still)

            timeline = _timeline(
                [_visual("sc_0", still, duration=0.5)], resolution=(255, 143)
            )
            result = AssemblyGenerator(workspace / "outputs").run(_request(timeline))

            self.assertEqual(result.metadata["resolution"], [256, 144])
            self.assertEqual(_read_frames(Path(result.outputs[0]))[0].shape[:2], (144, 256))

    def test_japanese_subtitle_is_burned_and_wraps_without_spaces(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            still = workspace / "scene.png"
            _write_solid(still, (32, 32, 32))
            japanese_text = (
                "これは日本語の長い字幕です。空白がなくても画面の幅に合わせて"
                "安全に折り返され、映像の下部へ描画されます。"
            )
            result = AssemblyGenerator(workspace / "outputs").run(
                _request(
                    _timeline(
                        [_visual("sc_0", still)],
                        subtitles=[
                            {
                                "text": japanese_text,
                                "start_seconds": 0.0,
                                "end_seconds": 1.0,
                            }
                        ],
                    )
                )
            )

            with Image.open(result.previews[0]) as preview:
                pixels = np.asarray(preview.convert("RGB"))
            self.assertEqual(result.metadata["subtitle_count"], 1)
            self.assertTrue(np.all(pixels[: pixels.shape[0] // 2] == 32))
            self.assertGreater(
                np.count_nonzero(pixels[pixels.shape[0] // 2 :] != 32),
                100,
            )

    def test_cjk_font_candidates_are_tried_before_dejavu(self) -> None:
        sentinel = object()

        with mock.patch.object(
            assembly_module.ImageFont,
            "truetype",
            side_effect=lambda name, size: (
                sentinel
                if name == "NotoSansCJK-Regular.ttc"
                else (_ for _ in ()).throw(OSError())
            ),
        ) as truetype:
            loaded = assembly_module._load_font(24)

        self.assertIs(loaded, sentinel)
        self.assertEqual(truetype.call_args_list[0].args[0], "NotoSansCJK-Regular.ttc")


class AssemblyValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = AssemblyGenerator("outputs/videos")
        self.timeline = _timeline(
            [_visual("sc_0", Path("scene.png"), duration=1.0)]
        )

    def test_rejects_non_video_media_type(self) -> None:
        request = _request(self.timeline, media_type="image", output_format=None)
        with self.assertRaisesRegex(ValueError, "only supports video requests"):
            self.generator.validate_request(request)

    def test_rejects_sample_rate_over_the_audio_memory_cap(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_rate must be between"):
            AssemblyGenerator(
                "outputs/videos",
                sample_rate=assembly_module._MAX_AUDIO_SAMPLE_RATE + 1,
            )

    def test_rejects_non_mp4_output_format(self) -> None:
        request = _request(self.timeline, output_format="gif")
        with self.assertRaisesRegex(ValueError, "mp4 output only"):
            self.generator.validate_request(request)

    def test_rejects_missing_timeline(self) -> None:
        request = _request(self.timeline)
        request.params["timeline"] = {}
        with self.assertRaisesRegex(ValueError, r"params\['timeline'\]"):
            self.generator.validate_request(request)

    def test_rejects_timeline_without_tracks(self) -> None:
        request = _request(self.timeline)
        request.params["timeline"] = {"resolution": [256, 144], "fps": 8}
        with self.assertRaisesRegex(ValueError, "missing its 'tracks' mapping"):
            self.generator.validate_request(request)

    def test_rejects_empty_visual_track(self) -> None:
        timeline = _timeline([])
        timeline["tracks"]["visual"] = []
        with self.assertRaisesRegex(ValueError, "visual track is empty"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_zero_fps(self) -> None:
        timeline = _timeline([_visual("sc_0", Path("scene.png"))], fps=0)
        with self.assertRaisesRegex(ValueError, "fps must be a positive number"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_zero_total_duration(self) -> None:
        timeline = _timeline([_visual("sc_0", Path("scene.png"), duration=0.0)])
        with self.assertRaisesRegex(ValueError, "total duration is zero"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_resolution_dimension_over_the_hard_cap(self) -> None:
        timeline = _timeline(
            [_visual("sc_0", Path("scene.png"))],
            resolution=(assembly_module._MAX_OUTPUT_DIMENSION + 1, 2),
        )
        with self.assertRaisesRegex(ValueError, "maximum dimension"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_resolution_pixel_count_over_the_hard_cap(self) -> None:
        timeline = _timeline(
            [_visual("sc_0", Path("scene.png"))],
            resolution=(4_096, 2_162),
        )
        with self.assertRaisesRegex(ValueError, "maximum pixel count"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_fps_over_the_hard_cap(self) -> None:
        timeline = _timeline(
            [_visual("sc_0", Path("scene.png"))],
            fps=assembly_module._MAX_FPS + 1,
        )
        with self.assertRaisesRegex(ValueError, "fps exceeds the maximum"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_scene_count_over_the_hard_cap(self) -> None:
        visual = [
            _visual(f"sc_{index}", Path("scene.png"), duration=0.1)
            for index in range(assembly_module._MAX_SCENES + 1)
        ]
        with self.assertRaisesRegex(ValueError, "scene count exceeds"):
            self.generator.validate_request(_request(_timeline(visual)))

    def test_rejects_total_duration_over_the_hard_cap(self) -> None:
        timeline = _timeline(
            [
                _visual(
                    "sc_0",
                    Path("scene.png"),
                    duration=assembly_module._MAX_TOTAL_DURATION_SECONDS + 0.1,
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "total duration exceeds"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_frame_count_over_the_hard_cap(self) -> None:
        timeline = _timeline(
            [_visual("sc_0", Path("scene.png"), duration=1.0)],
            fps=8,
        )
        with mock.patch.object(assembly_module, "_MAX_FRAME_COUNT", 7):
            with self.assertRaisesRegex(ValueError, "frame count exceeds"):
                self.generator.validate_request(_request(timeline))

    def test_rejects_subtitle_over_the_per_entry_length_cap(self) -> None:
        timeline = _timeline(
            [_visual("sc_0", Path("scene.png"))],
            subtitles=[
                {
                    "text": "字" * (assembly_module._MAX_SUBTITLE_CHARACTERS + 1),
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                }
            ],
        )
        with self.assertRaisesRegex(ValueError, "subtitle length exceeds"):
            self.generator.validate_request(_request(timeline))

    def test_rejects_audio_entry_count_over_the_hard_cap(self) -> None:
        timeline = _timeline(
            [_visual("sc_0", Path("scene.png"))],
            narration=[
                {"asset_id": f"aud_{index}", "start_seconds": 0.0}
                for index in range(assembly_module._MAX_AUDIO_ENTRIES + 1)
            ],
        )
        with self.assertRaisesRegex(ValueError, "audio entry count exceeds"):
            self.generator.validate_request(_request(timeline))

    def test_accepts_a_well_formed_timeline(self) -> None:
        self.assertIsNone(self.generator.validate_request(_request(self.timeline)))


if __name__ == "__main__":
    unittest.main()
