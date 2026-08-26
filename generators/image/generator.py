"""Image generator backed by a local diffusers runtime."""

from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import secrets
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.models import ModelService
from core.prompting import PromptComposer
from core.quality import (
    enrich_quality_report,
    evaluate_image_output,
    evaluate_image_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator
from generators.common import resolve_generation_prompt
from generators.image.providers import ImageGenerationSpec, LocalDiffusersImageProvider

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_VARIATION_COUNT = 4
_SEED_MODULUS = 1 << 63


class ImageGenerator(BaseGenerator):
    """Generate images with the resolved model runtime."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/images",
        *,
        task_type: str = "text-to-image",
        prompt_composer: PromptComposer | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.task_type = task_type
        self.prompt_composer = prompt_composer

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "image":
            raise ValueError("ImageGenerator only supports image requests.")
        if not request.prompt.strip():
            raise ValueError("Image prompt must not be empty.")
        if request.output_format and request.output_format.lower() != "png":
            raise ValueError("ImageGenerator currently supports png output only.")
        self._resolve_variation_count(request.params)

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        request: GenerationRequest,
        context: "GenerationContext | None" = None,
    ) -> GenerationResult:
        import torch

        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="image",
            task_type=self.task_type,
        )
        pipeline = runtime_obj["pipeline"]
        effective_params = {**manifest.default_params, **request.params}
        resolved_prompt = resolve_generation_prompt(
            request,
            effective_params,
            composer=self.prompt_composer,
            template=str(effective_params.get("prompt_template", "image")),
        )
        variation_count = self._resolve_variation_count(effective_params)
        effective_params.pop("variation_count", None)
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
        lineage_metadata = _extract_lineage_metadata(effective_params)
        for key in lineage_metadata:
            effective_params.pop(key, None)
        # An explicit lora_path always wins; a bible-supplied LoRA fills in when
        # the request did not name one.
        if not lora_path and resolved_prompt.lora:
            lora_path = resolved_prompt.lora.get("path")
            lora_scale = float(resolved_prompt.lora.get("scale", lora_scale))
        lora_metadata = self._configure_lora(runtime_obj, pipeline, lora_path, lora_scale)
        base_seed = (
            resolved_prompt.seed
            if resolved_prompt.seed is not None
            else secrets.randbits(63)
        )
        # Route the pipeline call through the provider-neutral contract
        # (generators/image/providers.py) so this local diffusers path and a
        # future cloud provider are invoked and validated the same way; the
        # kwargs passed to `pipeline` below are unchanged from before this
        # contract existed, so behavior is identical to a direct call.
        provider = LocalDiffusersImageProvider(
            model_id=manifest.public_model_id,
            pipeline=pipeline,
        )
        spec_lora_path = lora_metadata["path"]
        spec_lora_scale = lora_metadata["scale"]
        request_spec = ImageGenerationSpec(
            prompt=resolved_prompt.prompt,
            negative_prompt=resolved_prompt.negative_prompt,
            width=width,
            height=height,
            seed=base_seed,
            batch_size=variation_count,
            lora_path=str(spec_lora_path) if spec_lora_path is not None else None,
            lora_scale=(
                float(cast(float, spec_lora_scale)) if spec_lora_scale is not None else 1.0
            ),
            reference_image_path=None,
        )
        common_generation_kwargs = {
            "prompt": resolved_prompt.prompt,
            "negative_prompt": resolved_prompt.negative_prompt,
            "width": width,
            "height": height,
            "guidance_scale": guidance_scale,
            "num_inference_steps": num_inference_steps,
            **effective_params,
        }
        batch_id = f"img_{uuid4().hex}"
        output_paths: list[str] = []
        variation_metadata: list[dict[str, Any]] = []
        quality_reports: list[dict[str, Any]] = []

        try:
            for variation_index in range(variation_count):
                if context is not None:
                    context.raise_if_cancelled()
                variation_seed = self._derive_variation_seed(
                    base_seed,
                    variation_index,
                )
                generation_kwargs = dict(common_generation_kwargs)
                generation_kwargs["generator"] = self._create_generator(
                    variation_seed,
                    runtime_obj["device"],
                    torch,
                )
                step_callback = self._build_step_callback(
                    pipeline,
                    num_inference_steps,
                    context,
                    variation_index=variation_index,
                    variation_count=variation_count,
                )
                if step_callback is not None:
                    generation_kwargs["callback_on_step_end"] = step_callback

                variation_request_id = f"{batch_id}_v{variation_index + 1}"
                with torch.inference_mode():
                    provider_result = provider.generate_image(
                        replace(request_spec, seed=variation_seed),
                        request_id=variation_request_id,
                        pipeline_kwargs=generation_kwargs,
                    )

                if context is not None:
                    context.raise_if_cancelled()
                output_path = self.output_dir / (
                    f"{batch_id}.png"
                    if variation_count == 1
                    else f"{batch_id}_v{variation_index + 1}.png"
                )
                image = provider_result.image
                image.save(output_path)
                output_paths.append(str(output_path))

                quality_report = evaluate_image_output(output_path)
                semantic_report = evaluate_image_semantics(
                    output_path,
                    resolved_prompt.prompt,
                    resolved_prompt.negative_prompt,
                )
                enrich_quality_report(quality_report, semantic_report)
                quality_reports.append(quality_report)
                variation_params = {
                    "width": width,
                    "height": height,
                    "num_inference_steps": num_inference_steps,
                    "guidance_scale": guidance_scale,
                    "lora_path": lora_metadata["path"],
                    "lora_scale": lora_metadata["scale"],
                    "variation_count": 1,
                    **effective_params,
                }
                variation_metadata.append(
                    {
                        "variation_index": variation_index,
                        "seed": variation_seed,
                        "output_path": str(output_path),
                        "preview_path": str(output_path),
                        "params": variation_params,
                        "quality_report": quality_report,
                        "provider_id": provider_result.identity.provider_id,
                        "provider_request_id": provider_result.identity.request_id,
                    }
                )
                if context is not None and step_callback is None:
                    context.report_progress((variation_index + 1) / variation_count)
                if context is not None:
                    context.raise_if_cancelled()
        except Exception:
            for saved_path in output_paths:
                Path(saved_path).unlink(missing_ok=True)
            raise

        job_params = {
            "width": width,
            "height": height,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "lora_path": lora_metadata["path"],
            "lora_scale": lora_metadata["scale"],
            "variation_count": variation_count,
            **effective_params,
        }

        return GenerationResult(
            job_id=batch_id,
            status="succeeded",
            outputs=output_paths,
            previews=list(output_paths),
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": resolved_prompt.prompt,
                "negative_prompt": resolved_prompt.negative_prompt,
                "requested_prompt": request.prompt,
                "prompt_composition": resolved_prompt.composition,
                "reference_asset_ids": resolved_prompt.reference_asset_ids,
                # #199: which asset, role (character/location), and strength a
                # Bible reference resolved to -- recorded even though nothing
                # downstream conditions on it yet (#201), so a job's metadata
                # is a complete audit trail of what was asked for.
                "resolved_references": [
                    reference.model_dump(mode="json")
                    for reference in resolved_prompt.resolved_references
                ],
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "image_provider_id": provider.provider_id,
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
                "seed": base_seed,
                "base_seed": base_seed,
                "requested_seed": request.seed,
                "variation_count": variation_count,
                "variations": variation_metadata,
                "output_format": "png",
                "default_params": dict(manifest.default_params),
                "quality_report": quality_reports[0],
                **lineage_metadata,
                "params": job_params,
            },
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

    def _build_step_callback(
        self,
        pipeline: object,
        num_inference_steps: int,
        context: "GenerationContext | None",
        *,
        variation_index: int = 0,
        variation_count: int = 1,
    ):
        if context is None or num_inference_steps <= 0:
            return None
        if not self._pipeline_accepts_step_callback(pipeline):
            return None

        def _on_step_end(pipe: object, step_index: int, timestep: object, callback_kwargs: dict):
            step_fraction = (step_index + 1) / num_inference_steps
            context.report_progress(
                (variation_index + step_fraction) / variation_count
            )
            context.raise_if_cancelled()
            return callback_kwargs

        return _on_step_end

    def _pipeline_accepts_step_callback(self, pipeline: object) -> bool:
        call = getattr(pipeline, "__call__", None)
        if call is None:
            return False
        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):
            return False
        return "callback_on_step_end" in signature.parameters

    def _resolve_variation_count(self, params: dict[str, Any]) -> int:
        value = params.get("variation_count", 1)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                "Image parameter 'variation_count' must be an integer between 1 and 4."
            )
        if value < 1 or value > _MAX_VARIATION_COUNT:
            raise ValueError(
                "Image parameter 'variation_count' must be between 1 and 4."
            )
        return value

    def _derive_variation_seed(self, base_seed: int, variation_index: int) -> int:
        if variation_index == 0:
            return base_seed
        return (base_seed + variation_index) % _SEED_MODULUS

    def _create_generator(
        self,
        seed: int | None,
        device: str,
        torch: Any,
    ):
        if seed is None:
            return None

        generator_device = device if str(device).startswith("cuda") else "cpu"
        return torch.Generator(device=generator_device).manual_seed(seed)

    def _configure_lora(
        self,
        runtime_obj: dict[str, object],
        pipeline: Any,
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
        "review_issue_tags",
        "review_source",
    )
    lineage_payload: dict[str, Any] = {}
    for key in lineage_keys:
        value = params.get(key)
        if value is not None:
            lineage_payload[key] = value
    return lineage_payload


__all__ = ["ImageGenerator"]
