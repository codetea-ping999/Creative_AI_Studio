"""Assemble a story timeline into a delivered mp4.

This generator owns the last mile of the pipeline: it turns the timeline that
``core.story.timeline.build_timeline`` emits into a single muxed mp4 with motion,
burned-in subtitles, and a narration/music mix. It deliberately needs no model
service — assembly is deterministic composition, not inference — so it stays
usable even when no weights are installed.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from itertools import chain
import math
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.quality import evaluate_video_output
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

ASSEMBLY_TASK_TYPE = "assembly"
ASSEMBLY_OUTPUT_FORMATS = frozenset({"mp4"})

# Sources that carry their own timebase are sampled across their frames instead
# of being treated as a still.
_MOVING_SOURCE_SUFFIXES = frozenset({".mp4", ".gif", ".webm", ".mov", ".m4v", ".mkv"})

_TRANSITION_CROSSFADE = "crossfade"
_KEN_BURNS_ZOOM = 1.12
_PAN_ZOOM = 1.12
_SUPPORTED_MOTIONS = frozenset(
    {"none", "ken_burns_in", "ken_burns_out", "pan_left", "pan_right"}
)

_SUBTITLE_HEIGHT_RATIO = 0.055
_SUBTITLE_MIN_FONT_PIXELS = 10
_SUBTITLE_BOTTOM_MARGIN_RATIO = 0.08
_SUBTITLE_SIDE_MARGIN_RATIO = 0.06
_SUBTITLE_OUTLINE_PIXELS = 2
_SUBTITLE_FILL = (255, 255, 255)
_SUBTITLE_OUTLINE_FILL = (0, 0, 0)
# Prefer fonts with Japanese glyph coverage. DejaVu is deliberately after the
# CJK candidates: it loads successfully on most Linux hosts but renders Japanese
# as tofu boxes, preventing later candidates from ever being tried.
_FONT_CANDIDATES = (
    "NotoSansCJK-Regular.ttc",
    "NotoSansJP-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "Arial.ttf",
    "Helvetica.ttc",
    "DejaVuSans.ttf",
)

_DUCK_GAIN_DB = -9.0
_DUCK_RAMP_SECONDS = 0.12
_MAX_INT16 = 32_767
_FFMPEG_STDERR_TAIL_LINES = 12
_DEFAULT_FFMPEG_TIMEOUT_SECONDS = 180.0

# Assembly is intentionally allowed to accept user-authored timelines, but every
# dimension that drives CPU, memory, or disk use must be bounded before any file
# is decoded. These limits admit DCI 4K and a ten-minute 60-fps delivery while
# keeping hostile inputs finite for this short-form production pipeline.
_MAX_OUTPUT_DIMENSION = 4_096
_MAX_OUTPUT_PIXELS = 4_096 * 2_160
_MAX_FPS = 60
_MAX_SCENES = 300
_MAX_TOTAL_DURATION_SECONDS = 600.0
_MAX_FRAME_COUNT = 36_000
_MAX_SUBTITLE_ENTRIES = 5_000
_MAX_SUBTITLE_CHARACTERS = 2_000
_MAX_TOTAL_SUBTITLE_CHARACTERS = 100_000
_MAX_AUDIO_ENTRIES = 1_000
_MAX_AUDIO_SAMPLE_RATE = 48_000
_MAX_AUDIO_MIX_SAMPLE_FRAMES = int(
    _MAX_TOTAL_DURATION_SECONDS * _MAX_AUDIO_SAMPLE_RATE
)

# A decoded DCI-4K RGB frame is around 27 MiB. Keeping only a handful makes the
# cache useful for repeated frame requests without recreating the original
# unbounded "entire movie in RAM" behaviour.
_MAX_DECODED_SOURCE_FRAMES = 4

# Pillow returns either font class depending on whether truetype support and a
# usable font file are available on the host.
_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


@dataclass(frozen=True)
class _Clip:
    """One resolved visual entry with everything the renderer needs."""

    scene_id: str
    source: Path
    frame_count: int
    transition: str
    motion: str


@dataclass(frozen=True)
class _AudioMix:
    """A finished float mix plus the facts the result metadata reports."""

    samples: np.ndarray
    has_narration: bool
    has_music: bool
    ducked: bool
    unresolved: list[str]

    @property
    def is_silent(self) -> bool:
        """True when nothing was actually placed, so no track is worth muxing."""

        return not (self.has_narration or self.has_music)


class AssemblyGenerator(BaseGenerator):
    """Render a timeline into a delivered mp4 with audio and subtitles."""

    def __init__(
        self,
        output_dir: str | Path = "outputs/videos",
        *,
        asset_path_lookup: Callable[[str], str | None] | None = None,
        sample_rate: int = 44_100,
        allow_direct_paths: bool = True,
        ffmpeg_timeout_seconds: float = _DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.asset_path_lookup = asset_path_lookup
        self.sample_rate = int(sample_rate)
        if self.sample_rate <= 0 or self.sample_rate > _MAX_AUDIO_SAMPLE_RATE:
            raise ValueError(
                "Assembly audio sample_rate must be between 1 and "
                f"{_MAX_AUDIO_SAMPLE_RATE}; got {self.sample_rate}."
            )
        self.allow_direct_paths = bool(allow_direct_paths)
        self.ffmpeg_timeout_seconds = float(ffmpeg_timeout_seconds)
        if (
            not math.isfinite(self.ffmpeg_timeout_seconds)
            or self.ffmpeg_timeout_seconds <= 0
        ):
            raise ValueError("ffmpeg_timeout_seconds must be a positive number.")
        self.task_type = ASSEMBLY_TASK_TYPE
        self._font_cache: dict[int, _Font] = {}

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "video":
            raise ValueError(
                "AssemblyGenerator only supports video requests; got "
                f"{request.media_type!r}."
            )
        if (
            request.output_format
            and request.output_format.lower() not in ASSEMBLY_OUTPUT_FORMATS
        ):
            raise ValueError(
                "AssemblyGenerator delivers mp4 output only; got "
                f"{request.output_format!r}."
            )

        timeline = request.params.get("timeline")
        if not isinstance(timeline, dict) or not timeline:
            raise ValueError(
                "AssemblyGenerator requires params['timeline'] built by "
                "core.story.timeline.build_timeline."
            )

        tracks = timeline.get("tracks")
        if not isinstance(tracks, dict):
            raise ValueError(
                "Timeline is missing its 'tracks' mapping; expected keys visual, "
                "narration, music, subtitles."
            )

        visual = tracks.get("visual")
        if not isinstance(visual, list) or not visual:
            raise ValueError(
                "Timeline visual track is empty: there is nothing to render."
            )

        fps_value = _as_float(timeline.get("fps"))
        if fps_value <= 0:
            raise ValueError(
                f"Timeline fps must be a positive number; got {timeline.get('fps')!r}."
            )
        if fps_value > _MAX_FPS:
            raise ValueError(
                f"Timeline fps exceeds the maximum of {_MAX_FPS}; got {fps_value:g}."
            )

        size = self._resolve_resolution(timeline)
        if len(visual) > _MAX_SCENES:
            raise ValueError(
                "Timeline visual scene count exceeds the maximum of "
                f"{_MAX_SCENES}; got {len(visual)}."
            )

        total_duration = sum(
            _as_float(entry.get("duration_seconds"))
            for entry in visual
            if isinstance(entry, dict)
        )
        if total_duration <= 0:
            raise ValueError(
                "Timeline total duration is zero: every visual entry needs a "
                "positive duration_seconds."
            )
        if total_duration > _MAX_TOTAL_DURATION_SECONDS:
            raise ValueError(
                "Timeline total duration exceeds the maximum of "
                f"{_MAX_TOTAL_DURATION_SECONDS:g} seconds; got {total_duration:g}."
            )

        fps = max(1, int(fps_value))
        frame_count = sum(
            max(1, round(_as_float(entry.get("duration_seconds")) * fps))
            for entry in visual
            if isinstance(entry, dict)
            and _as_float(entry.get("duration_seconds")) > 0
        )
        if frame_count > _MAX_FRAME_COUNT:
            raise ValueError(
                "Timeline frame count exceeds the maximum of "
                f"{_MAX_FRAME_COUNT}; got {frame_count}."
            )

        subtitle_entries = tracks.get("subtitles") or []
        if not isinstance(subtitle_entries, list):
            raise ValueError("Timeline subtitles track must be a list.")
        if len(subtitle_entries) > _MAX_SUBTITLE_ENTRIES:
            raise ValueError(
                "Timeline subtitle entry count exceeds the maximum of "
                f"{_MAX_SUBTITLE_ENTRIES}; got {len(subtitle_entries)}."
            )
        subtitle_lengths = [
            len(str(entry.get("text") or ""))
            for entry in subtitle_entries
            if isinstance(entry, dict)
        ]
        oversized_subtitle = next(
            (length for length in subtitle_lengths if length > _MAX_SUBTITLE_CHARACTERS),
            None,
        )
        if oversized_subtitle is not None:
            raise ValueError(
                "Timeline subtitle length exceeds the per-entry maximum of "
                f"{_MAX_SUBTITLE_CHARACTERS} characters; got {oversized_subtitle}."
            )
        total_subtitle_characters = sum(subtitle_lengths)
        if total_subtitle_characters > _MAX_TOTAL_SUBTITLE_CHARACTERS:
            raise ValueError(
                "Timeline total subtitle length exceeds the maximum of "
                f"{_MAX_TOTAL_SUBTITLE_CHARACTERS} characters; got "
                f"{total_subtitle_characters}."
            )

        audio_entry_count = 0
        for track_name in ("narration", "music"):
            audio_entries = tracks.get(track_name) or []
            if not isinstance(audio_entries, list):
                raise ValueError(f"Timeline {track_name} track must be a list.")
            audio_entry_count += len(audio_entries)
        if audio_entry_count > _MAX_AUDIO_ENTRIES:
            raise ValueError(
                "Timeline audio entry count exceeds the maximum of "
                f"{_MAX_AUDIO_ENTRIES}; got {audio_entry_count}."
            )

        # Keep the resolved value live so resolution validation above cannot be
        # accidentally removed as an "unused" call.
        _ = size

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        timeline: dict[str, Any] = request.params["timeline"]
        tracks: dict[str, Any] = timeline.get("tracks") or {}
        size = self._resolve_resolution(timeline)
        fps = max(1, int(_as_float(timeline.get("fps"))))

        clips = self._build_clips(tracks.get("visual") or [], fps=fps)
        subtitles = self._normalize_subtitles(tracks.get("subtitles") or [])
        frame_total = sum(clip.frame_count for clip in clips)
        duration_rendered = frame_total / fps

        job_id = f"vid_{uuid4().hex}"
        output_path = self.output_dir / f"{job_id}.mp4"
        preview_path = self.output_dir / f"{job_id}_preview.png"

        try:
            # Everything except the delivered mp4 and its preview is scratch, so
            # it lives in a temporary directory that cleans itself on every exit.
            with tempfile.TemporaryDirectory(prefix="assembly_") as staging_root:
                staging = Path(staging_root)
                silent_video = staging / "video.mp4"
                self._render_video(
                    silent_video,
                    clips=clips,
                    subtitles=subtitles,
                    size=size,
                    fps=fps,
                    preview_path=preview_path,
                )

                mix = self._build_audio_mix(tracks, total_seconds=duration_rendered)
                audio_path: Path | None = None
                if mix is not None and not mix.is_silent:
                    audio_path = staging / "audio.wav"
                    self._write_wave_file(audio_path, mix.samples)

                self._mux(silent_video, audio_path, output_path)

            quality_report = evaluate_video_output(output_path)
        except Exception:
            # A failed job must not leave a plausible-looking preview or a partial
            # delivery behind for callers to mistake for a completed render.
            preview_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            raise

        return GenerationResult(
            job_id=job_id,
            status="succeeded",
            outputs=[str(output_path)],
            previews=[str(preview_path)],
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                # Assembly composes existing assets, so there is no model behind
                # it; the key stays for metadata parity with the other generators.
                "model_id": request.model_id.strip(),
                "requested_model_id": request.model_id.strip() or None,
                "output_format": "mp4",
                "quality_report": quality_report,
                "scene_count": len(clips),
                "resolution": [size[0], size[1]],
                "fps": fps,
                "duration_seconds_rendered": round(duration_rendered, 3),
                "frame_count": frame_total,
                "has_narration": bool(mix and mix.has_narration),
                "has_music": bool(mix and mix.has_music),
                "audio_ducked": bool(mix and mix.ducked),
                "audio_sample_rate": self.sample_rate if audio_path is not None else None,
                "unresolved_audio_assets": list(mix.unresolved) if mix else [],
                "subtitle_count": len(subtitles),
                "transitions": [clip.transition for clip in clips],
                "motions": [clip.motion for clip in clips],
                **_extract_lineage_metadata(request.params),
                "params": dict(request.params),
            },
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

    # ------------------------------------------------------------------ inputs

    def _resolve_resolution(self, timeline: dict[str, Any]) -> tuple[int, int]:
        raw = timeline.get("resolution") or [1920, 1080]
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError(
                f"Timeline resolution must be a [width, height] pair; got {raw!r}."
            )

        width = int(_as_float(raw[0]))
        height = int(_as_float(raw[1]))
        if width < 2 or height < 2:
            raise ValueError(
                f"Timeline resolution must be at least 2x2 pixels; got {width}x{height}."
            )
        if width > _MAX_OUTPUT_DIMENSION or height > _MAX_OUTPUT_DIMENSION:
            raise ValueError(
                "Timeline resolution exceeds the maximum dimension of "
                f"{_MAX_OUTPUT_DIMENSION} pixels; got {width}x{height}."
            )

        # h264 with yuv420p needs even dimensions. Rounding up here keeps what we
        # compose identical to what ffmpeg encodes, instead of letting the writer
        # silently rescale a frame at mux time.
        resolved = width + width % 2, height + height % 2
        if resolved[0] * resolved[1] > _MAX_OUTPUT_PIXELS:
            raise ValueError(
                "Timeline resolution exceeds the maximum pixel count of "
                f"{_MAX_OUTPUT_PIXELS}; got {resolved[0]}x{resolved[1]}."
            )
        return resolved

    def _build_clips(self, entries: Sequence[Any], *, fps: int) -> list[_Clip]:
        clips: list[_Clip] = []
        unresolved: list[str] = []
        invalid_duration: list[str] = []

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Timeline visual entry #{index} must be a mapping; got {entry!r}."
                )

            scene_id = str(entry.get("scene_id") or entry.get("asset_id") or f"#{index}")
            duration = _as_float(entry.get("duration_seconds"))
            if duration <= 0:
                invalid_duration.append(scene_id)
                continue

            source = self._resolve_source(entry)
            if source is None:
                unresolved.append(f"{scene_id} (asset_id={entry.get('asset_id')!r})")
                continue

            clips.append(
                _Clip(
                    scene_id=scene_id,
                    source=source,
                    frame_count=max(1, round(duration * fps)),
                    transition=str(entry.get("transition") or "cut").strip().lower(),
                    motion=_normalize_motion(entry.get("motion")),
                )
            )

        # A half-empty video is worse than a refusal: name every offending scene
        # so the caller can regenerate exactly the missing assets.
        if unresolved:
            raise ValueError(
                "Cannot assemble the timeline: no readable visual file for scenes: "
                + ", ".join(unresolved)
            )
        if invalid_duration:
            raise ValueError(
                "Cannot assemble the timeline: duration_seconds must be positive for "
                "scenes: " + ", ".join(invalid_duration)
            )
        return clips

    def _resolve_source(self, entry: dict[str, Any]) -> Path | None:
        asset_id = entry.get("asset_id")
        if self.asset_path_lookup is not None and asset_id:
            resolved = self.asset_path_lookup(str(asset_id))
            if resolved:
                candidate = Path(str(resolved)).expanduser()
                if candidate.is_file():
                    return candidate

        # Direct paths remain enabled by default for generator-level callers and
        # offline tests. Production construction disables this seam so a request
        # can only name assets authorized by the repository lookup.
        raw_path = entry.get("path")
        if self.allow_direct_paths and raw_path:
            candidate = Path(str(raw_path)).expanduser()
            if candidate.is_file():
                return candidate
        return None

    def _normalize_subtitles(
        self, entries: Sequence[Any]
    ) -> list[tuple[float, float, str]]:
        normalized: list[tuple[float, float, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            start = _as_float(entry.get("start_seconds"))
            end = _as_float(entry.get("end_seconds"))
            # A degenerate caption window is not worth refusing a whole render.
            if end <= start:
                continue
            normalized.append((start, end, text))
        return sorted(normalized)

    # ------------------------------------------------------------------- video

    def _render_video(
        self,
        target: Path,
        *,
        clips: list[_Clip],
        subtitles: list[tuple[float, float, str]],
        size: tuple[int, int],
        fps: int,
        preview_path: Path,
    ) -> None:
        # Imported lazily so importing this module never probes for the ffmpeg
        # binary, matching how the other generators defer heavy runtime imports.
        from imageio import v2 as imageio_v2

        writer = imageio_v2.get_writer(
            str(target),
            fps=fps,
            codec="libx264",
            quality=8,
            # Dimensions are already even, so this only guards against a future
            # odd-sized frame slipping through rather than triggering a rescale.
            macro_block_size=2,
            pixelformat="yuv420p",
            ffmpeg_log_level="error",
        )
        sources = _SourceFrames()
        try:
            frames = self._iter_frames(clips, size=size, fps=fps, sources=sources)
            for index, frame in enumerate(frames):
                finished = self._burn_subtitles(
                    frame, subtitles, seconds=index / fps, size=size
                )
                if index == 0:
                    finished.save(preview_path)
                writer.append_data(np.asarray(finished))
        finally:
            try:
                sources.close()
            finally:
                writer.close()

    def _iter_frames(
        self,
        clips: list[_Clip],
        *,
        size: tuple[int, int],
        fps: int,
        sources: _SourceFrames,
    ) -> Iterator[Image.Image]:
        """Yield every finished frame in order, buffering only the fade window.

        A crossfade blends a clip's tail with the *next* clip's head in place and
        the next clip still plays in full. Overlapping the two clips instead would
        shorten the delivery by one fade per cut, so the rendered duration would
        no longer match the timeline the caller approved.
        """

        fades = [self._fade_frames(clips, index, fps) for index in range(len(clips))]
        carried: list[Image.Image] = []

        for index, clip in enumerate(clips):
            # Frames already rendered while fading out of the previous clip.
            head, carried = carried, []
            fade = fades[index]
            tail_start = clip.frame_count - fade
            tail: list[Image.Image] = []

            rendered = self._iter_clip_frames(
                clip, size=size, sources=sources, start=len(head)
            )
            for position, frame in enumerate(chain(head, rendered)):
                if position < tail_start:
                    yield frame
                else:
                    tail.append(frame)

            if not fade:
                yield from tail
                continue

            next_head = list(
                self._iter_clip_frames(
                    clips[index + 1], size=size, sources=sources, limit=fade
                )
            )
            for offset, (outgoing, incoming) in enumerate(zip(tail, next_head)):
                yield Image.blend(outgoing, incoming, (offset + 1) / (fade + 1))
            carried = next_head

    def _fade_frames(self, clips: list[_Clip], index: int, fps: int) -> int:
        if index + 1 >= len(clips):
            return 0
        if clips[index].transition != _TRANSITION_CROSSFADE:
            return 0
        return max(
            0,
            min(fps // 2, clips[index].frame_count, clips[index + 1].frame_count),
        )

    def _iter_clip_frames(
        self,
        clip: _Clip,
        *,
        size: tuple[int, int],
        sources: _SourceFrames,
        start: int = 0,
        limit: int | None = None,
    ) -> Iterator[Image.Image]:
        source_frame_count = sources.frame_count(clip.source)
        last_source_index = source_frame_count - 1
        end = clip.frame_count if limit is None else min(clip.frame_count, start + limit)

        for index in range(start, end):
            progress = index / (clip.frame_count - 1) if clip.frame_count > 1 else 0.0
            source_index = round(progress * last_source_index) if last_source_index else 0
            yield _compose_frame(
                sources.get(clip.source, source_index),
                size=size,
                motion=clip.motion,
                progress=progress,
            )

    # --------------------------------------------------------------- subtitles

    def _burn_subtitles(
        self,
        frame: Image.Image,
        subtitles: list[tuple[float, float, str]],
        *,
        seconds: float,
        size: tuple[int, int],
    ) -> Image.Image:
        active = [text for start, end, text in subtitles if start <= seconds < end]
        if not active:
            return frame

        width, height = size
        draw = ImageDraw.Draw(frame)
        font = self._font(height)
        max_width = max(1, width - 2 * round(width * _SUBTITLE_SIDE_MARGIN_RATIO))

        def measure(text: str) -> float:
            return float(draw.textlength(text, font=font))

        lines: list[str] = []
        for text in active:
            lines.extend(_wrap_text(text, max_width=max_width, measure=measure))
        if not lines:
            return frame

        line_height = _line_height(draw, font)
        baseline = height - round(height * _SUBTITLE_BOTTOM_MARGIN_RATIO)
        # Clamp so an unexpectedly tall block stays on screen instead of
        # being cropped off the top of the frame.
        top = max(0, baseline - line_height * len(lines))

        for offset, line in enumerate(lines):
            left = (width - measure(line)) / 2
            y = top + offset * line_height
            # A contrasting outline keeps text legible over any underlying image.
            for shadow_x, shadow_y in _OUTLINE_OFFSETS:
                draw.text(
                    (left + shadow_x * _SUBTITLE_OUTLINE_PIXELS,
                     y + shadow_y * _SUBTITLE_OUTLINE_PIXELS),
                    line,
                    font=font,
                    fill=_SUBTITLE_OUTLINE_FILL,
                )
            draw.text((left, y), line, font=font, fill=_SUBTITLE_FILL)
        return frame

    def _font(self, frame_height: int) -> _Font:
        size = max(
            _SUBTITLE_MIN_FONT_PIXELS, round(frame_height * _SUBTITLE_HEIGHT_RATIO)
        )
        cached = self._font_cache.get(size)
        if cached is not None:
            return cached

        font = _load_font(size)
        self._font_cache[size] = font
        return font

    # ------------------------------------------------------------------- audio

    def _build_audio_mix(
        self,
        tracks: dict[str, Any],
        *,
        total_seconds: float,
    ) -> _AudioMix | None:
        total_samples = max(1, round(total_seconds * self.sample_rate))
        if (
            total_seconds > _MAX_TOTAL_DURATION_SECONDS
            or total_samples > _MAX_AUDIO_MIX_SAMPLE_FRAMES
        ):
            raise ValueError(
                "Audio mix exceeds the bounded sample budget of "
                f"{_MAX_AUDIO_MIX_SAMPLE_FRAMES} stereo frames."
            )

        narration_entries = [
            entry for entry in (tracks.get("narration") or []) if isinstance(entry, dict)
        ]
        music_entries = [
            entry for entry in (tracks.get("music") or []) if isinstance(entry, dict)
        ]
        if not narration_entries and not music_entries:
            return None

        mix = np.zeros((total_samples, 2), dtype=np.float32)
        unresolved: list[str] = []
        narration_spans: list[tuple[int, int]] = []

        for entry in narration_entries:
            start = max(0, round(_as_float(entry.get("start_seconds")) * self.sample_rate))
            remaining = total_samples - start
            if remaining <= 0:
                continue
            source = self._resolve_source(entry)
            if source is None:
                # Losing narration degrades the delivery but still produces a
                # watchable video, so it is reported instead of aborting.
                unresolved.append(str(entry.get("asset_id") or entry.get("scene_id")))
                continue
            samples = self._read_audio(source, max_output_samples=remaining)
            span = _place_samples(mix, samples, start)
            if span is not None:
                narration_spans.append(span)

        # The envelope spans the whole mix, so it is only built when some music
        # actually asks to duck under narration that landed in the timeline.
        wants_ducking = narration_spans and any(
            entry.get("duck") for entry in music_entries
        )
        envelope = (
            self._duck_envelope(total_samples, narration_spans) if wants_ducking else None
        )
        ducked = False
        has_music = False

        for entry in music_entries:
            start = max(0, round(_as_float(entry.get("start_seconds")) * self.sample_rate))
            remaining = total_samples - start
            if remaining <= 0:
                continue
            source = self._resolve_source(entry)
            if source is None:
                unresolved.append(str(entry.get("asset_id")))
                continue

            wanted = round(_as_float(entry.get("duration_seconds")) * self.sample_rate)
            if wanted > 0:
                # Clamp before reading or looping: fitting an attacker-controlled
                # requested duration first could allocate far beyond the mix.
                wanted = min(wanted, remaining)
            read_limit = wanted if wanted > 0 else remaining
            samples = self._read_audio(source, max_output_samples=read_limit)
            if wanted > 0:
                samples = _fit_length(samples, wanted, loop=bool(entry.get("loop")))
            else:
                samples = samples[:remaining]

            gain_db = _as_float(entry.get("gain_db"))
            samples = samples * float(10.0 ** (gain_db / 20.0))

            duck = bool(entry.get("duck")) and envelope is not None
            span = _place_samples(mix, samples, start, envelope=envelope if duck else None)
            if span is not None:
                has_music = True
                ducked = ducked or duck

        np.clip(mix, -1.0, 1.0, out=mix)
        return _AudioMix(
            samples=mix,
            has_narration=bool(narration_spans),
            has_music=has_music,
            ducked=ducked,
            unresolved=unresolved,
        )

    def _duck_envelope(
        self, total_samples: int, spans: list[tuple[int, int]]
    ) -> np.ndarray:
        """Build the music gain curve, ramping in and out of narration spans.

        An instant jump is audible as a click and reads as a mistake, so the dip
        starts just before the voice and recovers just after it.
        """

        envelope = np.ones(total_samples, dtype=np.float32)
        if not spans:
            return envelope

        duck_gain = float(10.0 ** (_DUCK_GAIN_DB / 20.0))
        ramp = max(1, round(_DUCK_RAMP_SECONDS * self.sample_rate))
        for begin, end in spans:
            attack_start = max(0, begin - ramp)
            if begin > attack_start:
                attack = np.linspace(
                    1.0, duck_gain, begin - attack_start, dtype=np.float32
                )
                envelope[attack_start:begin] = np.minimum(
                    envelope[attack_start:begin], attack
                )
            envelope[begin:end] = np.minimum(envelope[begin:end], duck_gain)

            release_end = min(total_samples, end + ramp)
            if release_end > end:
                release = np.linspace(
                    duck_gain, 1.0, release_end - end, dtype=np.float32
                )
                envelope[end:release_end] = np.minimum(
                    envelope[end:release_end], release
                )
        return envelope

    def _read_audio(
        self, path: Path, *, max_output_samples: int | None = None
    ) -> np.ndarray:
        """Decode a PCM wav into float32 stereo samples at the mix rate."""

        try:
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                frame_rate = wav_file.getframerate()
                frame_limit = wav_file.getnframes()
                if max_output_samples is not None:
                    # Include two source frames for interpolation at the boundary,
                    # then truncate exactly after resampling.
                    frame_limit = min(
                        frame_limit,
                        max(
                            1,
                            math.ceil(
                                max(0, max_output_samples)
                                * frame_rate
                                / self.sample_rate
                            )
                            + 2,
                        ),
                    )
                raw_frames = wav_file.readframes(frame_limit)
        except wave.Error as exc:
            raise ValueError(
                f"Timeline audio must be a PCM wav file; {path} could not be read: {exc}"
            ) from exc

        samples = _decode_pcm_array(raw_frames, sample_width)
        usable = (samples.size // max(1, channels)) * max(1, channels)
        frames = samples[:usable].reshape(-1, max(1, channels))
        result = _resample(_to_stereo(frames), frame_rate, self.sample_rate)
        if max_output_samples is not None:
            result = result[: max(0, max_output_samples)]
        return result

    def _write_wave_file(self, path: Path, samples: np.ndarray) -> None:
        pcm = (np.clip(samples, -1.0, 1.0) * _MAX_INT16).astype("<i2")
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(int(samples.shape[1]))
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm.tobytes())

    # --------------------------------------------------------------------- mux

    def _mux(self, video_path: Path, audio_path: Path | None, output_path: Path) -> None:
        from imageio_ffmpeg import get_ffmpeg_exe

        # There is no system ffmpeg on the supported machines; the bundled binary
        # that ships with imageio-ffmpeg is the only one we may rely on.
        ffmpeg_exe = get_ffmpeg_exe()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = output_path.with_name(
            f".{output_path.stem}.{uuid4().hex}.partial{output_path.suffix}"
        )
        command = [ffmpeg_exe, "-y", "-hide_banner", "-nostdin", "-i", str(video_path)]
        if audio_path is not None:
            command += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"]
        # The staged video is already h264/yuv420p, so the stream copies verbatim
        # and the mux stays cheap and lossless.
        command += ["-c:v", "copy"]
        if audio_path is not None:
            command += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        command += ["-movflags", "+faststart", str(partial_path)]

        try:
            completed = subprocess.run(  # noqa: S603 - argv only, never a shell
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.ffmpeg_timeout_seconds,
            )
            if completed.returncode != 0 or not partial_path.is_file():
                tail = "\n".join(
                    (completed.stderr or "").strip().splitlines()[
                        -_FFMPEG_STDERR_TAIL_LINES:
                    ]
                )
                raise RuntimeError(
                    f"ffmpeg failed to mux {output_path.name} "
                    f"(exit {completed.returncode}). Last ffmpeg output:\n{tail}"
                )
            # os.replace semantics through Path.replace give readers either the
            # previous complete delivery or this complete delivery, never bytes
            # from a still-running ffmpeg process.
            partial_path.replace(output_path)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ffmpeg timed out while muxing {output_path.name} after "
                f"{self.ffmpeg_timeout_seconds:g} seconds."
            ) from exc
        finally:
            partial_path.unlink(missing_ok=True)


class _SourceFrames:
    """Read stills directly and moving sources through one bounded decoder."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._still: Image.Image | None = None
        self._moving: _MovingSourceFrames | None = None

    def frame_count(self, path: Path) -> int:
        self._select(path)
        return self._moving.frame_count if self._moving is not None else 1

    def get(self, path: Path, index: int) -> Image.Image:
        self._select(path)
        if self._moving is not None:
            return self._moving.get(index)
        if self._still is None:
            raise ValueError(f"Visual source contains no frames: {path}")
        return self._still

    def close(self) -> None:
        if self._moving is not None:
            self._moving.close()
        self._moving = None
        self._still = None
        self._path = None

    def _select(self, path: Path) -> None:
        if self._path == path:
            return
        self.close()
        self._path = path
        if path.suffix.lower() in _MOVING_SOURCE_SUFFIXES:
            self._moving = _MovingSourceFrames(path)
        else:
            self._still = _read_still(path)


class _MovingSourceFrames:
    """Sequential moving-image decoder with a small LRU of decoded frames.

    Timeline sampling asks for monotonically increasing indexes in the common
    case. Intervening source frames are decoded and immediately discarded. A
    backwards seek restarts the iterator instead of retaining the whole source.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.frame_count = _moving_source_frame_count(path)
        self._iterator: Iterator[np.ndarray] | None = None
        self._next_index = 0
        self._last_frame: Image.Image | None = None
        self._cache: OrderedDict[int, Image.Image] = OrderedDict()

    def get(self, index: int) -> Image.Image:
        target = max(0, min(int(index), self.frame_count - 1))
        cached = self._cache.get(target)
        if cached is not None:
            self._cache.move_to_end(target)
            return cached

        if self._iterator is None or target < self._next_index:
            self._restart()

        try:
            while self._next_index <= target:
                assert self._iterator is not None
                raw = next(self._iterator)
                frame = Image.fromarray(np.asarray(raw)).convert("RGB")
                decoded_index = self._next_index
                self._next_index += 1
                self._last_frame = frame
                self._cache[decoded_index] = frame
                self._cache.move_to_end(decoded_index)
                while len(self._cache) > _MAX_DECODED_SOURCE_FRAMES:
                    self._cache.popitem(last=False)
        except StopIteration:
            # Container duration/fps metadata can round one frame high. Reusing
            # the actual last frame gives the clip a stable final frame.
            self.frame_count = max(1, self._next_index)
            if self._last_frame is not None:
                return self._last_frame
            raise ValueError(f"Visual source contains no frames: {self.path}") from None
        except Exception as exc:  # noqa: BLE001 - every decode failure is fatal
            raise ValueError(
                f"Could not read frames from visual source {self.path}: {exc}"
            ) from exc

        frame = self._cache.get(target)
        if frame is None:
            # The target was decoded in the loop and cannot be evicted unless the
            # cache bound is configured to zero.
            raise ValueError(f"Could not decode frame {target} from {self.path}.")
        return frame

    def close(self) -> None:
        iterator = self._iterator
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
        self._iterator = None

    def _restart(self) -> None:
        self.close()
        from imageio import v3 as imageio_v3

        try:
            self._iterator = iter(imageio_v3.imiter(self.path))
        except Exception as exc:  # noqa: BLE001 - every decode failure is fatal
            raise ValueError(
                f"Could not open moving visual source {self.path}: {exc}"
            ) from exc
        self._next_index = 0
        self._last_frame = None


_OUTLINE_OFFSETS = ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0))


def _read_still(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _moving_source_frame_count(path: Path) -> int:
    from imageio import v3 as imageio_v3

    try:
        properties = imageio_v3.improps(path)
        count = properties.n_images
        if count is not None and math.isfinite(float(count)) and int(count) > 0:
            return int(count)
        metadata = imageio_v3.immeta(path)
        duration = _as_float(metadata.get("duration"))
        fps = _as_float(metadata.get("fps"))
        if duration > 0 and fps > 0:
            return max(1, round(duration * fps))
    except Exception as exc:  # noqa: BLE001 - metadata failures need context
        raise ValueError(
            f"Could not inspect moving visual source {path}: {exc}"
        ) from exc

    # Some plugins do not expose a count. The first frame still provides a useful
    # deterministic visual without scanning an unbounded source just to count it.
    return 1


def _compose_frame(
    source: Image.Image,
    *,
    size: tuple[int, int],
    motion: str,
    progress: float,
) -> Image.Image:
    """Crop a target-aspect window out of the source and resize it to the frame.

    Cropping first is what keeps a portrait still from being stretched into a
    landscape frame: the aspect ratio is preserved and the overflow is cut.
    """

    target_width, target_height = size
    source_width, source_height = source.size

    # Largest window inside the source that already has the target aspect ratio.
    if source_width * target_height >= source_height * target_width:
        base_height = float(source_height)
        base_width = source_height * target_width / target_height
    else:
        base_width = float(source_width)
        base_height = source_width * target_height / target_width

    zoom = _zoom_for(motion, progress)
    crop_width = base_width / zoom
    crop_height = base_height / zoom
    left = (source_width - crop_width) * _pan_ratio(motion, progress)
    top = (source_height - crop_height) / 2

    return source.resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
        box=(left, top, left + crop_width, top + crop_height),
    )


def _zoom_for(motion: str, progress: float) -> float:
    if motion == "ken_burns_in":
        return 1.0 + (_KEN_BURNS_ZOOM - 1.0) * progress
    if motion == "ken_burns_out":
        return _KEN_BURNS_ZOOM - (_KEN_BURNS_ZOOM - 1.0) * progress
    if motion in {"pan_left", "pan_right"}:
        # A pan needs headroom to travel through, so the window stays zoomed in.
        return _PAN_ZOOM
    return 1.0


def _pan_ratio(motion: str, progress: float) -> float:
    if motion == "pan_left":
        return 1.0 - progress
    if motion == "pan_right":
        return progress
    return 0.5


def _normalize_motion(raw_motion: Any) -> str:
    # ``Scene.camera`` is free text upstream, so an unknown value must degrade to
    # a static shot rather than fail a whole render.
    motion = str(raw_motion or "none").strip().lower()
    return motion if motion in _SUPPORTED_MOTIONS else "none"


def _load_font(size: int) -> _Font:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow before 10.1 cannot size the bitmap fallback font.
        return ImageFont.load_default()


def _line_height(draw: ImageDraw.ImageDraw, font: _Font) -> int:
    # "Ag" covers both an ascender and a descender, which bitmap fonts do not
    # report through getmetrics().
    box = draw.textbbox((0, 0), "Ag", font=font)
    return max(1, int((box[3] - box[1]) * 1.45))


def _wrap_text(
    text: str,
    *,
    max_width: float,
    measure: Callable[[str], float],
) -> list[str]:
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and measure(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    # Scripts without spaces (and very long words) survive the pass above intact,
    # so anything still too wide is broken on character boundaries.
    wrapped: list[str] = []
    for line in lines:
        if measure(line) <= max_width:
            wrapped.append(line)
            continue
        wrapped.extend(_break_characters(line, max_width=max_width, measure=measure))
    return wrapped


def _break_characters(
    text: str,
    *,
    max_width: float,
    measure: Callable[[str], float],
) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and measure(candidate) > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _decode_pcm_array(frames: bytes, sample_width: int) -> np.ndarray:
    """Normalize interleaved PCM to float32 in [-1, 1].

    This mirrors ``core.quality.evaluators.decode_pcm_samples`` but stays in
    numpy: a full timeline mix would otherwise become tens of millions of Python
    floats.
    """

    if sample_width == 1:
        return (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32_768.0
    if sample_width == 4:
        return np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2_147_483_648.0
    raise ValueError(f"Unsupported PCM sample width: {sample_width}")


def _to_stereo(frames: np.ndarray) -> np.ndarray:
    if frames.shape[1] == 2:
        return frames.astype(np.float32, copy=False)
    if frames.shape[1] == 1:
        return np.repeat(frames.astype(np.float32, copy=False), 2, axis=1)
    mono = frames.mean(axis=1, dtype=np.float32, keepdims=True)
    return np.repeat(mono, 2, axis=1)


def _resample(frames: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or frames.shape[0] == 0:
        return frames
    if source_rate <= 0:
        raise ValueError(f"Audio sample rate must be positive; got {source_rate}.")

    target_length = max(1, round(frames.shape[0] * target_rate / source_rate))
    source_positions = np.arange(frames.shape[0], dtype=np.float64)
    target_positions = np.arange(target_length, dtype=np.float64) * (
        source_rate / target_rate
    )
    resampled = np.stack(
        [
            np.interp(target_positions, source_positions, frames[:, channel])
            for channel in range(frames.shape[1])
        ],
        axis=1,
    )
    return resampled.astype(np.float32)


def _fit_length(frames: np.ndarray, wanted: int, *, loop: bool) -> np.ndarray:
    if frames.shape[0] >= wanted:
        return frames[:wanted]
    if not loop:
        return frames
    repeats = math.ceil(wanted / max(1, frames.shape[0]))
    return np.tile(frames, (repeats, 1))[:wanted]


def _place_samples(
    mix: np.ndarray,
    samples: np.ndarray,
    start: int,
    *,
    envelope: np.ndarray | None = None,
) -> tuple[int, int] | None:
    """Add samples into the mix at a sample offset, returning the placed span."""

    if samples.shape[0] == 0 or start >= mix.shape[0]:
        return None

    end = min(mix.shape[0], start + samples.shape[0])
    if end <= start:
        return None

    segment = samples[: end - start]
    if envelope is not None:
        segment = segment * envelope[start:end, None]
    mix[start:end] += segment
    return start, end


def _as_float(value: Any) -> float:
    """Coerce timeline numbers, treating junk as zero so validation reports it."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _extract_lineage_metadata(params: dict[str, Any]) -> dict[str, Any]:
    lineage_keys = (
        "source_asset_id",
        "source_job_id",
        "reference_asset_path",
        "reuse_action",
        "story_id",
    )
    lineage_payload: dict[str, Any] = {}
    for key in lineage_keys:
        value = params.get(key)
        if value is not None:
            lineage_payload[key] = value
    return lineage_payload


__all__ = ["ASSEMBLY_OUTPUT_FORMATS", "ASSEMBLY_TASK_TYPE", "AssemblyGenerator"]
