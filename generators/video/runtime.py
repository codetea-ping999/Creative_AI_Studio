"""Video runtime adapters for procedural and learned text-to-video execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
import random
import textwrap
from typing import Any
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from core.models import ModelManifest
from core.schemas import GenerationRequest


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
    ) -> dict[str, Any]:
        """Render a video asset and return its file paths and metadata."""


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
    ) -> dict[str, Any]:
        width = max(256, int(effective_params.pop("width", 576)))
        height = max(256, int(effective_params.pop("height", 320)))
        fps = max(4, int(effective_params.pop("fps", 8)))
        duration_seconds = max(2, int(effective_params.pop("duration_seconds", 4)))
        num_frames = max(12, int(effective_params.pop("num_frames", duration_seconds * fps)))
        camera_motion = (
            str(effective_params.pop("camera_motion", "push-in")).strip() or "push-in"
        )
        visual_style = (
            str(effective_params.pop("visual_style", "storyboard")).strip()
            or "storyboard"
        )
        negative_prompt = request.negative_prompt.strip() if request.negative_prompt else None
        palette = runtime_obj.get("palette") if isinstance(runtime_obj.get("palette"), list) else []
        rng = random.Random(request.seed if request.seed is not None else hash(request.prompt))

        frames = [
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
            for frame_index in range(num_frames)
        ]

        output_id = f"vid_{uuid4().hex}"
        output_path = output_dir / f"{output_id}.gif"
        frame_duration_ms = max(50, int(1000 / fps))
        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration_ms,
            loop=0,
            disposal=2,
        )
        return {
            "output_id": output_id,
            "output_path": str(output_path),
            "preview_paths": [str(output_path)],
            "output_format": "gif",
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
        return tuple(
            int(left[channel] + (right[channel] - left[channel]) * clamped)
            for channel in range(3)
        )

    def _hex_to_rgb(self, value: str) -> tuple[int, int, int]:
        normalized = value.lstrip("#")
        if len(normalized) != 6:
            return (17, 24, 39)
        return tuple(int(normalized[index:index + 2], 16) for index in (0, 2, 4))


class LearnedVideoRuntime(BaseVideoRuntime):
    """Adapter for learned text-to-video runtimes provided by local entrypoints."""

    def __init__(self, fallback_runtime: BaseVideoRuntime) -> None:
        self.fallback_runtime = fallback_runtime

    def render(
        self,
        *,
        request: GenerationRequest,
        manifest: ModelManifest,
        runtime_obj: dict[str, Any],
        output_dir: Path,
        effective_params: dict[str, Any],
    ) -> dict[str, Any]:
        renderer = runtime_obj.get("renderer")
        pipeline = runtime_obj.get("pipeline")
        load_error = runtime_obj.get("load_error")
        fallback_runtime = str(runtime_obj.get("fallback_runtime", "procedural_storyboard"))

        if load_error and fallback_runtime == "procedural_storyboard":
            fallback_result = self.fallback_runtime.render(
                request=request,
                manifest=manifest,
                runtime_obj=runtime_obj,
                output_dir=output_dir,
                effective_params=effective_params,
            )
            fallback_result["runtime_metadata"]["runtime_fallback"] = {
                "from": "learned_text_to_video",
                "to": "procedural_storyboard",
                "reason": str(load_error),
            }
            return fallback_result

        callable_runtime = renderer if callable(renderer) else pipeline if callable(pipeline) else None
        if callable_runtime is None:
            raise RuntimeError(
                "Learned video runtime requires a callable renderer or pipeline. "
                "Provide it from the local runtime entrypoint."
            )

        generation_kwargs = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seed": request.seed,
            **effective_params,
        }
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
                    "preview_paths": list(preview_paths) if isinstance(preview_paths, list) else [output_path],
                    "output_format": normalized.get("output_format", Path(output_path).suffix.lstrip(".") or "mp4"),
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

        if isinstance(generated, list) and generated and all(isinstance(frame, Image.Image) for frame in generated):
            output_id = f"vid_{uuid4().hex}"
            output_path = output_dir / f"{output_id}.gif"
            generated[0].save(
                output_path,
                save_all=True,
                append_images=generated[1:],
                duration=max(50, int(1000 / max(1, int(effective_params.get("fps", 8))))),
                loop=0,
                disposal=2,
            )
            return {
                "output_id": output_id,
                "output_path": str(output_path),
                "preview_paths": [str(output_path)],
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


class VideoRuntimeRouter:
    """Select the appropriate video runtime implementation for a manifest/runtime pair."""

    def __init__(self) -> None:
        self.procedural_runtime = ProceduralStoryboardRuntime()
        self.learned_runtime = LearnedVideoRuntime(self.procedural_runtime)

    def resolve(self, runtime_obj: dict[str, Any]) -> BaseVideoRuntime:
        runtime_adapter = str(runtime_obj.get("runtime_adapter", "procedural_storyboard"))
        if runtime_adapter == "learned_text_to_video":
            return self.learned_runtime
        return self.procedural_runtime


__all__ = [
    "BaseVideoRuntime",
    "LearnedVideoRuntime",
    "ProceduralStoryboardRuntime",
    "VideoRuntimeRouter",
]
