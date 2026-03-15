"""Procedural video generator for local storyboard-style previews."""

from __future__ import annotations

from pathlib import Path
import random
import textwrap
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from core.models import ModelService
from core.quality import evaluate_video_output
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator


class VideoGenerator(BaseGenerator):
    """Generate lightweight animated storyboard previews as gif assets."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/videos",
        *,
        task_type: str = "text-to-video",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.task_type = task_type

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "video":
            raise ValueError("VideoGenerator only supports video requests.")
        if not request.prompt.strip():
            raise ValueError("Video prompt must not be empty.")
        if request.output_format and request.output_format.lower() != "gif":
            raise ValueError("VideoGenerator currently supports gif output only.")

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="video",
            task_type=self.task_type,
        )

        effective_params = {**manifest.default_params, **request.params}
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
        output_path = self.output_dir / f"{output_id}.gif"
        frame_duration_ms = max(50, int(1000 / fps))
        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration_ms,
            loop=0,
            disposal=2,
        )

        quality_report = evaluate_video_output(output_path)
        quality_report["semantic_report"] = {
            "status": "not_supported",
            "reason": "semantic judge is currently implemented for image/audio only",
        }

        return GenerationResult(
            job_id=output_id,
            status="succeeded",
            outputs=[str(output_path)],
            previews=[str(output_path)],
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": request.prompt,
                "negative_prompt": negative_prompt,
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": type(runtime_obj).__name__,
                "output_format": "gif",
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
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
            },
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

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


__all__ = ["VideoGenerator"]
