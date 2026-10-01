"""Video runtime adapters for procedural and learned text-to-video execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
import inspect
from pathlib import Path
import random
import textwrap
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from core.jobs.context import GenerationCancelled
from core.models import ModelManifest
from core.schemas import GenerationRequest
from generators.common import safe_is_cancelled

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext

PROCEDURAL_VIDEO_OUTPUT_FORMATS = frozenset({"gif"})
LEARNED_VIDEO_OUTPUT_FORMATS = frozenset({"mp4"})
SUPPORTED_VIDEO_OUTPUT_FORMATS = PROCEDURAL_VIDEO_OUTPUT_FORMATS | LEARNED_VIDEO_OUTPUT_FORMATS


class BaseVideoRuntime(ABC):
    """A runtime adapter that turns a generation request into a saved video asset."""

    @abstractmethod
    def render(
        self,
        *,
        request: GenerationRequest,
        manifest: ModelManifest,
        runtime_obj: dict[str, Any],
        output_dir: Path,
        effective_params: dict[str, Any],
        context: "GenerationContext | None" = None,
    ) -> dict[str, Any]:
        """Render a video asset and return its file paths and metadata."""


def coerce_procedural_render_params(effective_params: dict[str, Any]) -> dict[str, Any]:
    """Coerce and pop the procedural runtime's own request-owned numeric params.

    Codex P2 finding "Video: restrict deferred procedural parameter
    failures": `width`/`height`/`fps`/`duration_seconds`/`num_frames` are
    the only inputs to `ProceduralStoryboardRuntime.render()` that come
    from the request/manifest defaults rather than the loaded runtime
    itself (contrast `runtime_obj.get("palette")`, coerced by
    `_hex_to_rgb()` inside `render()` -- a malformed value there is a
    genuine runtime defect, not a request error). Extracted to a
    standalone function so `VideoGenerator.generate()` can wrap *only*
    this conversion in a narrow `try/except`, immediately before calling
    `render()`, instead of a broad `except (ValueError, TypeError)` around
    the entire render call -- which used to also catch a bad palette and
    incorrectly treat it as a harmless input error instead of invalidating
    the runtime that produced it. Raises `ValueError`/`TypeError` exactly
    as the original inline coercion did.
    """

    width = max(256, int(effective_params.pop("width", 576)))
    height = max(256, int(effective_params.pop("height", 320)))
    fps = max(4, int(effective_params.pop("fps", 8)))
    duration_seconds = max(2, int(effective_params.pop("duration_seconds", 4)))
    num_frames = max(12, int(effective_params.pop("num_frames", duration_seconds * fps)))
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration_seconds": duration_seconds,
        "num_frames": num_frames,
    }


class ProceduralStoryboardRuntime(BaseVideoRuntime):
    """Generate lightweight animated storyboard previews as gif assets."""

    def render(
        self,
        *,
        request: GenerationRequest,
        manifest: ModelManifest,
        runtime_obj: dict[str, Any],
        output_dir: Path,
        effective_params: dict[str, Any],
        context: "GenerationContext | None" = None,
    ) -> dict[str, Any]:
        # Already coerced by `VideoGenerator.generate()` via
        # `coerce_procedural_render_params()` before this call, on any
        # path reached through the real router -- re-coercing here is a
        # harmless no-op on already-valid ints, and keeps this method
        # self-sufficient for any caller that skips that step.
        coerced = coerce_procedural_render_params(effective_params)
        width = coerced["width"]
        height = coerced["height"]
        fps = coerced["fps"]
        duration_seconds = coerced["duration_seconds"]
        num_frames = coerced["num_frames"]
        camera_motion = (
            str(effective_params.pop("camera_motion", "push-in")).strip() or "push-in"
        )
        visual_style = (
            str(effective_params.pop("visual_style", "storyboard")).strip()
            or "storyboard"
        )
        negative_prompt = request.negative_prompt.strip() if request.negative_prompt else None
        raw_palette = runtime_obj.get("palette")
        palette = raw_palette if isinstance(raw_palette, list) else []
        rng = random.Random(request.seed if request.seed is not None else hash(request.prompt))

        frames = []
        progress_error: Exception | None = None
        for frame_index in range(num_frames):
            if context is not None:
                # PR4b Lane B: `context.is_cancelled()` itself can raise
                # (JobRepository I/O -- see
                # `generators/common/cancellation.py`), the same fault mode
                # `VideoGenerator.generate()`'s own pre-render probe already
                # guards against, one call frame up. That is external
                # bookkeeping, not a rendering fault, so it must not
                # propagate through this `render()` call and invalidate a
                # runtime that is rendering frames correctly -- captured and
                # handed back via the same `probe_error` sentinel as a
                # `report_progress()` failure below, so `VideoGenerator`
                # raises it only once its lease has already released.
                # A genuine cancellation request (`cancelled` true, no probe
                # exception) still raises `GenerationCancelled` here exactly
                # as `context.raise_if_cancelled()` used to -- mid-render
                # cancellation keeps its existing conservative invalidation
                # behavior; only the probe's own external exception is
                # isolated.
                cancelled, frame_probe_error = safe_is_cancelled(context)
                if frame_probe_error is not None:
                    return {"probe_error": frame_probe_error}
                if cancelled:
                    raise GenerationCancelled()
            frames.append(
                self._render_frame(
                    index=frame_index,
                    total_frames=num_frames,
                    width=width,
                    height=height,
                    prompt=request.prompt,
                    negative_prompt=negative_prompt,
                    palette=[str(color) for color in palette],
                    camera_motion=camera_motion,
                    visual_style=visual_style,
                    rng=rng,
                )
            )
            if context is not None:
                # Codex P2 finding "Video: progress publication must not
                # invalidate procedural runtime": `report_progress()`
                # writes through JobRepository/event publication in
                # production -- external bookkeeping, not a rendering
                # fault (see this module's fault-boundary note, mirroring
                # Image). A transient DB/event failure here must not
                # escape through this render() call and invalidate a
                # runtime that is rendering frames correctly. Captured
                # (the first one; never swallowed) and frame rendering
                # keeps going -- retaining progress updates for any later
                # frame whose report succeeds -- instead of aborting an
                # otherwise-healthy render over a boundary hiccup.
                # `VideoGenerator.generate()` re-raises it only once this
                # lease has already exited cleanly.
                try:
                    context.report_progress((frame_index + 1) / num_frames)
                except Exception as exc:
                    if progress_error is None:
                        progress_error = exc

        # Deliberately no GIF encoding (filesystem I/O) here: frame
        # generation above is the actual runtime-use interval this
        # method is responsible for. `pending_frames` hands the raw,
        # already-rendered frames back to `VideoGenerator.generate()`,
        # which encodes them via `encode_frames_as_gif()` only *after* its
        # runtime lease has released -- an output-filesystem failure
        # (disk-full, permission denial) during encoding is then no longer
        # able to invalidate a runtime that finished rendering correctly.
        frame_duration_ms = max(50, int(1000 / fps))
        return {
            "pending_frames": frames,
            "pending_frame_duration_ms": frame_duration_ms,
            "output_format": "gif",
            # `None` when every `report_progress()` call succeeded (or no
            # `context` was supplied). `VideoGenerator.generate()` raises
            # this itself, only once this lease has already released.
            "progress_error": progress_error,
            "params": {
                "width": width,
                "height": height,
                "fps": fps,
                "duration_seconds": duration_seconds,
                "num_frames": num_frames,
                "camera_motion": camera_motion,
                "visual_style": visual_style,
                **effective_params,
            },
            "runtime_metadata": {
                "runtime_adapter": "procedural_storyboard",
                "frame_count": num_frames,
                "frame_duration_ms": frame_duration_ms,
                "negative_prompt": negative_prompt,
                "manifest_runtime": manifest.runtime,
            },
        }

    def _render_frame(
        self,
        *,
        index: int,
        total_frames: int,
        width: int,
        height: int,
        prompt: str,
        negative_prompt: str | None,
        palette: list[str],
        camera_motion: str,
        visual_style: str,
        rng: random.Random,
    ) -> Image.Image:
        progress = index / max(1, total_frames - 1)
        base_color = self._hex_to_rgb(palette[0] if palette else "#111827")
        accent_a = self._hex_to_rgb(palette[1] if len(palette) > 1 else "#1d4ed8")
        accent_b = self._hex_to_rgb(palette[2] if len(palette) > 2 else "#f59e0b")
        glow = self._hex_to_rgb(palette[4] if len(palette) > 4 else "#0f766e")

        frame = Image.new("RGB", (width, height), color=base_color)
        draw = ImageDraw.Draw(frame)

        horizon_y = int(height * (0.54 + 0.06 * (progress - 0.5)))
        for band_index in range(10):
            ratio = band_index / 9
            color = self._blend_rgb(accent_a, accent_b, ratio * (0.55 + progress * 0.45))
            top = int(horizon_y * ratio)
            bottom = int(horizon_y + (height - horizon_y) * ratio)
            draw.rectangle((0, top, width, bottom), fill=color)

        orb_radius = int(width * (0.12 + progress * 0.05))
        orb_x = int(width * (0.2 + progress * 0.58))
        orb_y = int(height * (0.18 + (0.08 if camera_motion == "tilt-up" else 0.0)))
        draw.ellipse(
            (orb_x - orb_radius, orb_y - orb_radius, orb_x + orb_radius, orb_y + orb_radius),
            fill=self._blend_rgb(accent_b, glow, 0.5),
        )

        parallax = int((progress - 0.5) * width * (0.08 if camera_motion == "push-in" else 0.04))
        for column in range(5):
            base_x = int(width * (0.12 + column * 0.18)) - parallax
            rect_width = int(width * (0.08 + (column % 2) * 0.03))
            rect_height = int(height * (0.25 + 0.08 * rng.random()))
            draw.rounded_rectangle(
                (base_x, height - rect_height - 24, base_x + rect_width, height - 24),
                radius=18,
                outline=self._blend_rgb(glow, (255, 255, 255), 0.25),
                width=3,
                fill=self._blend_rgb((17, 24, 39), accent_a, 0.25),
            )

        prompt_lines = textwrap.wrap(prompt.strip(), width=34)[:3]
        font = ImageFont.load_default()
        panel_x0 = 26
        panel_y0 = 24
        panel_x1 = width - 26
        panel_y1 = min(height - 28, 120 + 18 * len(prompt_lines))
        draw.rounded_rectangle(
            (panel_x0, panel_y0, panel_x1, panel_y1),
            radius=20,
            fill=(9, 12, 18),
            outline=self._blend_rgb((255, 255, 255), accent_b, 0.35),
            width=2,
        )
        draw.text((44, 42), f"SHOT {index + 1:02d}", fill=(250, 245, 230), font=font)
        draw.text((138, 42), visual_style.upper(), fill=(245, 197, 104), font=font)
        text_y = 70
        for line in prompt_lines:
            draw.text((44, text_y), line, fill=(243, 244, 246), font=font)
            text_y += 18
        if negative_prompt:
            negative_line = textwrap.shorten(
                f"avoid: {negative_prompt}",
                width=54,
                placeholder="...",
            )
            draw.text((44, panel_y1 - 26), negative_line, fill=(252, 165, 165), font=font)

        footer = f"{camera_motion} | {total_frames}f | local storyboard"
        draw.text((44, height - 42), footer, fill=(209, 213, 219), font=font)
        return frame

    def _blend_rgb(
        self,
        left: tuple[int, int, int],
        right: tuple[int, int, int],
        amount: float,
    ) -> tuple[int, int, int]:
        clamped = max(0.0, min(1.0, amount))
        return (
            int(left[0] + (right[0] - left[0]) * clamped),
            int(left[1] + (right[1] - left[1]) * clamped),
            int(left[2] + (right[2] - left[2]) * clamped),
        )

    def _hex_to_rgb(self, value: str) -> tuple[int, int, int]:
        normalized = value.lstrip("#")
        if len(normalized) != 6:
            return (17, 24, 39)
        return (
            int(normalized[0:2], 16),
            int(normalized[2:4], 16),
            int(normalized[4:6], 16),
        )


class LearnedVideoRuntime(BaseVideoRuntime):
    """Adapter for learned text-to-video runtimes provided by local entrypoints."""

    def render(
        self,
        *,
        request: GenerationRequest,
        manifest: ModelManifest,
        runtime_obj: dict[str, Any],
        output_dir: Path,
        effective_params: dict[str, Any],
        context: "GenerationContext | None" = None,
    ) -> dict[str, Any]:
        renderer = runtime_obj.get("renderer")
        pipeline = runtime_obj.get("pipeline")
        load_error = runtime_obj.get("load_error")
        if load_error:
            raise RuntimeError(f"Learned video runtime is unavailable: {load_error}")

        callable_runtime = (
            renderer if callable(renderer) else pipeline if callable(pipeline) else None
        )
        if callable_runtime is None:
            raise RuntimeError(
                "Learned video runtime requires a callable renderer or pipeline. "
                "Provide it from the local runtime entrypoint."
            )

        generation_kwargs = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seed": request.seed,
            "output_dir": output_dir,
            "output_format": request.output_format or effective_params.get("output_format", "mp4"),
            **effective_params,
        }
        if context is not None:
            # The loaded model's own callable is opaque third-party code (see
            # LearnedVideoLoader), so step-level cancellation only happens if
            # the adapter itself opts in: it can pop this kwarg and call it
            # from a diffusers-style callback_on_step_end (see the CogVideoX
            # adapter under models/video/learned-runtime/runtime.py).
            # `_callable_accepts_kwarg` guards against a fixed-signature
            # renderer (no **kwargs) raising TypeError on this extra
            # argument.
            if _callable_accepts_kwarg(callable_runtime, "raise_if_cancelled"):
                generation_kwargs["raise_if_cancelled"] = context.raise_if_cancelled
            # Codex P2 finding "learned-video pre-call cancellation":
            # VideoGenerator.generate() samples cancellation once, before
            # calling `render()` at all (`cancelled_before_render`), but
            # resolving the runtime router and building `generation_kwargs`
            # above is enough intervening work for cancellation to land in
            # that window. A *second* boundary check belongs immediately
            # before this actually-expensive call so an already-cancelled
            # job never starts inference. It must not raise
            # `GenerationCancelled` here, though: that would unwind through
            # `VideoGenerator.generate()`'s `with` block and conservatively
            # invalidate a runtime this call never touched. A boolean check
            # plus a sentinel return lets `VideoGenerator` observe the
            # cancellation, let the lease exit cleanly, and raise only once
            # the runtime is no longer under active use.
            #
            # PR4b Lane B: `context.is_cancelled()` itself can raise
            # (JobRepository I/O). Observed here, before the loaded model
            # (potentially GPU-weight-resident) is ever invoked, that is a
            # bookkeeping failure, not a runtime fault -- `safe_is_cancelled()`
            # keeps it from propagating and hands it back the same way as
            # the `cancelled` sentinel, so `VideoGenerator` raises it only
            # once this lease has already released.
            cancelled, probe_error = safe_is_cancelled(context)
            if probe_error is not None:
                return {"probe_error": probe_error}
            if cancelled:
                return {"cancelled_before_invocation": True}
        generated = callable_runtime(**generation_kwargs)
        return self._normalize_generated_output(
            generated=generated,
            output_dir=output_dir,
            manifest=manifest,
            runtime_obj=runtime_obj,
            effective_params=effective_params,
        )

    def _normalize_generated_output(
        self,
        *,
        generated: Any,
        output_dir: Path,
        manifest: ModelManifest,
        runtime_obj: dict[str, Any],
        effective_params: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(generated, dict):
            normalized = dict(generated)
            output_path = normalized.get("output_path")
            preview_paths = normalized.get("preview_paths") or []
            output_id = normalized.get("output_id") or f"vid_{uuid4().hex}"
            if isinstance(output_path, str) and output_path:
                return {
                    "output_id": output_id,
                    "output_path": output_path,
                    "preview_paths": (
                        list(preview_paths) if isinstance(preview_paths, list) else [output_path]
                    ),
                    "output_format": normalized.get(
                        "output_format", Path(output_path).suffix.lstrip(".") or "mp4"
                    ),
                    "params": dict(effective_params),
                    "runtime_metadata": {
                        "runtime_adapter": "learned_text_to_video",
                        "manifest_runtime": manifest.runtime,
                        **dict(normalized.get("metadata", {})),
                    },
                }
            generated = normalized.get("frames", generated)

        if isinstance(generated, (str, Path)):
            output_path = str(generated)
            output_id = f"vid_{uuid4().hex}"
            return {
                "output_id": output_id,
                "output_path": output_path,
                "preview_paths": [output_path],
                "output_format": Path(output_path).suffix.lstrip(".") or "mp4",
                "params": dict(effective_params),
                "runtime_metadata": {
                    "runtime_adapter": "learned_text_to_video",
                    "manifest_runtime": manifest.runtime,
                },
            }

        if (
            isinstance(generated, list)
            and generated
            and all(isinstance(frame, Image.Image) for frame in generated)
        ):
            # Same reasoning as ProceduralStoryboardRuntime.render(): no GIF
            # encoding (filesystem I/O) here -- the adapter already
            # finished its own runtime-use interval by returning these
            # frames. `pending_frames` defers the actual encode to
            # `VideoGenerator.generate()`, after its lease has released.
            #
            # Codex P2 finding "learned-video request parameter parsing":
            # unlike ProceduralStoryboardRuntime (which coerces its own
            # `fps` before rendering a single frame), `fps` here is a
            # request-owned value this adapter never validates itself.
            # Converting it to a duration eagerly, right here, would still
            # be inside the active lease -- a malformed value (e.g.
            # `fps="bad"`) would raise `ValueError` after inference already
            # completed successfully and incorrectly invalidate a healthy
            # runtime. The raw value is instead passed through unconverted;
            # `VideoGenerator.generate()` parses it only once its lease has
            # released, via `frame_duration_ms_from_fps()`.
            return {
                "pending_frames": generated,
                "pending_frame_fps": effective_params.get("fps", 8),
                "output_format": "gif",
                "params": dict(effective_params),
                "runtime_metadata": {
                    "runtime_adapter": "learned_text_to_video",
                    "manifest_runtime": manifest.runtime,
                    "frame_count": len(generated),
                },
            }

        raise RuntimeError(
            "Learned video runtime returned an unsupported payload. "
            "Use output_path, frames, or a saved file path."
        )


def encode_frames_as_gif(
    frames: list[Image.Image], output_dir: Path, frame_duration_ms: int
) -> tuple[str, Path]:
    """Encode already-rendered `frames` to a GIF file. Pure filesystem I/O.

    Deliberately standalone (not a method on either runtime class) so
    `VideoGenerator.generate()` can call it itself, *after* its runtime
    lease has released, for a `render()` result carrying `pending_frames`
    -- see `ProceduralStoryboardRuntime.render()` and
    `LearnedVideoRuntime._normalize_generated_output()`. A disk-full or
    permission failure here is an output-filesystem fault, never a
    runtime fault.
    """

    output_id = f"vid_{uuid4().hex}"
    output_path = output_dir / f"{output_id}.gif"
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration_ms,
        loop=0,
        disposal=2,
    )
    return output_id, output_path


def frame_duration_ms_from_fps(fps: Any) -> int:
    """Convert a request-owned, possibly-unvalidated `fps` value to a GIF frame duration.

    Kept standalone so `VideoGenerator.generate()` can parse `fps` itself,
    after a `pending_frames` runtime lease has already released -- a
    malformed value (e.g. `fps="bad"`) then raises `ValueError` with no
    runtime involved at all, instead of raising from inside
    `LearnedVideoRuntime.render()`'s active lease. See
    `LearnedVideoRuntime._normalize_generated_output()`.
    """

    return max(50, int(1000 / max(1, int(fps))))


def _callable_accepts_kwarg(callable_obj: Any, name: str) -> bool:
    """Whether calling `callable_obj(**{name: ...})` would not raise TypeError.

    True when the callable declares `name` explicitly, or accepts arbitrary
    keyword arguments via `**kwargs`. Used to avoid handing an opt-in
    parameter (e.g. `raise_if_cancelled`) to a fixed-signature renderer that
    was never updated to accept it.
    """

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if name in parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class VideoRuntimeRouter:
    """Select the appropriate video runtime implementation for a manifest/runtime pair."""

    def __init__(self) -> None:
        self.procedural_runtime = ProceduralStoryboardRuntime()
        self.learned_runtime = LearnedVideoRuntime()

    def resolve(self, runtime_obj: dict[str, Any]) -> BaseVideoRuntime:
        runtime_adapter = str(runtime_obj.get("runtime_adapter", "procedural_storyboard"))
        if runtime_adapter == "learned_text_to_video":
            return self.learned_runtime
        return self.procedural_runtime


__all__ = [
    "BaseVideoRuntime",
    "LearnedVideoRuntime",
    "ProceduralStoryboardRuntime",
    "SUPPORTED_VIDEO_OUTPUT_FORMATS",
    "VideoRuntimeRouter",
    "coerce_procedural_render_params",
    "encode_frames_as_gif",
    "frame_duration_ms_from_fps",
]
