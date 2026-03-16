"""Image generator backed by a local diffusers runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from core.models import ModelService
from core.quality import (
    enrich_quality_report,
    evaluate_image_output,
    evaluate_image_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ImageGenerator(BaseGenerator):
    """Generate images with the resolved model runtime."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/images",
        *,
        task_type: str = "text-to-image",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.task_type = task_type

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "image":
            raise ValueError("ImageGenerator only supports image requests.")
        if not request.prompt.strip():
            raise ValueError("Image prompt must not be empty.")
        if request.output_format and request.output_format.lower() != "png":
            raise ValueError("ImageGenerator currently supports png output only.")

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        import torch

        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="image",
            task_type=self.task_type,
        )
        pipeline = runtime_obj["pipeline"]
        effective_params = {**manifest.default_params, **request.params}
        width = int(effective_params.pop("width", 1024))
        height = int(effective_params.pop("height", 1024))
        num_inference_steps = int(
            effective_params.pop(
                "num_inference_steps",
                effective_params.pop("steps", 30),
            )
        )
        guidance_scale = float(effective_params.pop("guidance_scale", 7.5))
        lora_path = effective_params.pop("lora_path", None)
        lora_scale = float(effective_params.pop("lora_scale", 1.0))
        lora_metadata = self._configure_lora(runtime_obj, pipeline, lora_path, lora_scale)
        generator = self._create_generator(request.seed, runtime_obj["device"], torch)

        generation_kwargs = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "width": width,
            "height": height,
            "guidance_scale": guidance_scale,
            "num_inference_steps": num_inference_steps,
            **effective_params,
        }
        if generator is not None:
            generation_kwargs["generator"] = generator

        with torch.inference_mode():
            pipeline_output = pipeline(**generation_kwargs)

        job_id = f"img_{uuid4().hex}"
        output_path = self.output_dir / f"{job_id}.png"
        image = pipeline_output.images[0]
        image.save(output_path)
        quality_report = evaluate_image_output(output_path)
        semantic_report = evaluate_image_semantics(
            output_path,
            request.prompt,
            request.negative_prompt,
        )
        enrich_quality_report(quality_report, semantic_report)

        return GenerationResult(
            job_id=job_id,
            status="succeeded",
            outputs=[str(output_path)],
            previews=[str(output_path)],
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": type(runtime_obj).__name__,
                "pipeline_class": type(pipeline).__name__,
                "device": runtime_obj["device"],
                "load_dtype": runtime_obj.get("load_dtype"),
                "torch_dtype": runtime_obj["torch_dtype"],
                "lora_path": lora_metadata["path"],
                "lora_scale": lora_metadata["scale"],
                "seed": request.seed,
                "output_format": "png",
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                **_extract_lineage_metadata(request.params),
                "params": {
                    "width": width,
                    "height": height,
                    "num_inference_steps": num_inference_steps,
                    "guidance_scale": guidance_scale,
                    "lora_path": lora_metadata["path"],
                    "lora_scale": lora_metadata["scale"],
                    **effective_params,
                },
            },
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

    def _create_generator(
        self,
        seed: int | None,
        device: str,
        torch: object,
    ):
        if seed is None:
            return None

        generator_device = device if str(device).startswith("cuda") else "cpu"
        return torch.Generator(device=generator_device).manual_seed(seed)

    def _configure_lora(
        self,
        runtime_obj: dict[str, object],
        pipeline: object,
        lora_path: object,
        lora_scale: float,
    ) -> dict[str, object | None]:
        resolved_path = self._resolve_optional_path(lora_path)
        active_path = runtime_obj.get("active_lora_path")
        active_adapter = runtime_obj.get("active_lora_adapter")

        if resolved_path is None:
            if active_path:
                self._reset_lora(pipeline, runtime_obj)
            return {"path": None, "scale": None}

        normalized_path = str(resolved_path)
        if normalized_path != active_path:
            if active_path:
                self._reset_lora(pipeline, runtime_obj)

            adapter_name = f"lora_{uuid4().hex[:8]}"
            load_path, weight_name = self._resolve_lora_source(resolved_path)
            load_kwargs = {"adapter_name": adapter_name}
            if weight_name is not None:
                load_kwargs["weight_name"] = weight_name
            pipeline.load_lora_weights(load_path, **load_kwargs)
            runtime_obj["active_lora_path"] = normalized_path
            runtime_obj["active_lora_adapter"] = adapter_name
            active_adapter = adapter_name

        if active_adapter is None:
            raise RuntimeError("LoRA adapter state is missing after load.")

        pipeline.set_adapters(active_adapter, adapter_weights=lora_scale)
        runtime_obj["active_lora_scale"] = lora_scale
        return {"path": normalized_path, "scale": lora_scale}

    def _resolve_optional_path(self, raw_path: object) -> Path | None:
        if raw_path is None:
            return None
        text = str(raw_path).strip()
        if not text:
            return None

        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = (_REPO_ROOT / candidate).resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"LoRA path does not exist: {candidate}")
        return candidate

    def _resolve_lora_source(self, path: Path) -> tuple[str, str | None]:
        if path.is_dir():
            return str(path), None
        return str(path.parent), path.name

    def _reset_lora(self, pipeline: object, runtime_obj: dict[str, object]) -> None:
        adapter_name = runtime_obj.get("active_lora_adapter")
        if adapter_name and hasattr(pipeline, "delete_adapters"):
            pipeline.delete_adapters(adapter_name)
        if hasattr(pipeline, "unload_lora_weights"):
            pipeline.unload_lora_weights()
        runtime_obj.pop("active_lora_path", None)
        runtime_obj.pop("active_lora_adapter", None)
        runtime_obj.pop("active_lora_scale", None)


def _extract_lineage_metadata(params: dict[str, Any]) -> dict[str, Any]:
    lineage_keys = (
        "source_asset_id",
        "source_job_id",
        "reference_asset_path",
        "reuse_action",
    )
    lineage_payload: dict[str, Any] = {}
    for key in lineage_keys:
        value = params.get(key)
        if value is not None:
            lineage_payload[key] = value
    return lineage_payload


__all__ = ["ImageGenerator"]
