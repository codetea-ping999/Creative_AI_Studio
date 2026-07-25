"""Local CogVideoX-2B adapter for the learned video runtime contract."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
from uuid import uuid4


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.models.readiness import missing_diffusers_files


def load_runtime(manifest: dict[str, Any]) -> dict[str, Any]:
    """Load a local Diffusers CogVideoX pipeline without downloading weights."""

    defaults = dict(manifest.get("default_params", {}))
    pipeline_path = _resolve_pipeline_path(defaults.get("pipeline_path"))
    # Same requirement set /models reports, so the adapter never fails on a
    # file the API just called ready.
    missing = missing_diffusers_files(pipeline_path)
    if missing:
        raise FileNotFoundError(
            f"CogVideoX model files are missing under {pipeline_path}: "
            + ", ".join(missing)
            + ". Place THUDM/CogVideoX-2b Diffusers weights at that path."
        )

    try:
        import torch
        from diffusers import CogVideoXPipeline
        from diffusers.utils import export_to_video
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "CogVideoX requires torch, diffusers, transformers, accelerate, and imageio-ffmpeg."
        ) from exc

    requested_device = str(defaults.get("device", "auto"))
    device = _resolve_device(torch, requested_device)
    dtype = _resolve_dtype(torch, str(defaults.get("dtype", "float16")), device)
    pipeline = CogVideoXPipeline.from_pretrained(
        str(pipeline_path),
        torch_dtype=dtype,
        local_files_only=True,
    )
    if hasattr(pipeline, "vae"):
        pipeline.vae.enable_tiling()
        pipeline.vae.enable_slicing()

    try:
        pipeline.to(device)
    except (RuntimeError, NotImplementedError):
        device = "cpu"
        pipeline.to(device)

    def renderer(**kwargs: Any) -> dict[str, Any]:
        output_dir = Path(kwargs.pop("output_dir"))
        output_format = str(kwargs.pop("output_format", "mp4")).lower()
        if output_format != "mp4":
            raise ValueError("CogVideoX pilot supports mp4 output only.")
        seed = kwargs.pop("seed", None)
        generation_kwargs = _normalize_generation_kwargs(kwargs)
        generator_device = "cpu" if device == "mps" else device
        if seed is not None:
            generation_kwargs["generator"] = torch.Generator(device=generator_device).manual_seed(
                int(seed)
            )

        try:
            result = pipeline(**generation_kwargs)
        except (RuntimeError, NotImplementedError) as exc:
            if device != "mps":
                raise
            pipeline.to("cpu")
            generation_kwargs.pop("generator", None)
            if seed is not None:
                generation_kwargs["generator"] = torch.Generator(device="cpu").manual_seed(
                    int(seed)
                )
            result = pipeline(**generation_kwargs)
            runtime_device = "cpu"
            fallback_reason = str(exc)
        else:
            runtime_device = device
            fallback_reason = None

        frames = result.frames[0]
        output_dir.mkdir(parents=True, exist_ok=True)
        output_id = f"vid_{uuid4().hex}"
        output_path = output_dir / f"{output_id}.mp4"
        fps = max(1, int(kwargs.get("fps", defaults.get("fps", 8))))
        export_to_video(frames, str(output_path), fps=fps)
        metadata = {
            "pipeline_id": defaults.get("pipeline_id", "THUDM/CogVideoX-2b"),
            "pipeline_path": str(pipeline_path),
            "pipeline_class": type(pipeline).__name__,
            "device": runtime_device,
            "dtype": str(dtype).removeprefix("torch."),
            "frame_count": len(frames),
            "fps": fps,
        }
        if fallback_reason is not None:
            metadata["cpu_fallback_reason"] = fallback_reason
        return {
            "output_id": output_id,
            "output_path": str(output_path),
            "preview_paths": [str(output_path)],
            "output_format": "mp4",
            "metadata": metadata,
        }

    return {
        "runtime_adapter": "learned_text_to_video",
        "pipeline": pipeline,
        "renderer": renderer,
        "device": device,
        "dtype": str(dtype).removeprefix("torch."),
    }


def _resolve_pipeline_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("default_params.pipeline_path is required for CogVideoX.")
    candidate = Path(value).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (_REPO_ROOT / candidate).resolve()


def _resolve_device(torch: Any, requested: str) -> str:
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _resolve_dtype(torch: Any, requested: str, device: str) -> Any:
    if device == "cpu":
        return torch.float32
    return {
        "float16": torch.float16,
        "fp16": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float32": torch.float32,
        "fp32": torch.float32,
    }.get(requested.lower(), torch.float16)


def _normalize_generation_kwargs(values: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "prompt",
        "negative_prompt",
        "height",
        "width",
        "num_frames",
        "num_inference_steps",
        "guidance_scale",
        "num_videos_per_prompt",
    }
    return {key: value for key, value in values.items() if key in allowed and value is not None}


__all__ = ["load_runtime"]
