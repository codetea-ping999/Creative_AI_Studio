"""Image generator backed by a local diffusers runtime."""

from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import secrets
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from PIL import Image

from core.models import ModelService
from core.prompting import PromptComposer
from core.quality import (
    enrich_quality_report,
    evaluate_image_output,
    evaluate_image_semantics,
)
from core.reference_capabilities import MissingReferenceAssetError, validate_reference_inputs
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator
from generators.common import resolve_generation_prompt
from generators.image.providers import (
    ImageGenerationSpec,
    LocalDiffusersImageProvider,
    UnsupportedImageParameterError,
    local_diffusers_capabilities,
    validate_capabilities,
)

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext
    from core.models import ModelManifest
    from core.reference_capabilities import ReferenceImageInput
    from generators.common.prompting import ResolvedPrompt

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
        (
            reference_image_path,
            reference_strength,
            considered_references,
        ) = self._resolve_references_for_conditioning(request, resolved_prompt, manifest)
        # A reference is only actually honored when a dedicated img2img-shaped
        # runtime exists and its own call signature takes image/strength
        # (#201: one supported conditioning path, not every image model
        # family -- StableDiffusionXLPipeline itself never accepts these, see
        # core/models/loader.py's separate img2img_pipeline). When a
        # reference was requested but this can't be honored,
        # `reference_capable` stays False and `capabilities.supports_reference_image`
        # below stays at its default False -- `validate_capabilities` then
        # rejects the request before any pipeline call, rather than silently
        # dropping the reference or passing it into a call that doesn't
        # accept it.
        reference_pipeline = runtime_obj.get("img2img_pipeline")
        reference_capable = (
            reference_image_path is not None
            and reference_pipeline is not None
            and self._pipeline_accepts_reference_image(reference_pipeline)
        )
        if reference_capable and "denoising_start" in effective_params:
            # Diffusers img2img's get_timesteps() ignores `strength` entirely
            # whenever `denoising_start` is also set -- so the lock strength
            # computed just below (from the public 0=no-effect/1=follow-
            # closely contract) would be silently discarded, and the
            # requested reference conditioning would not actually be
            # honored the way the caller asked for it.
            raise UnsupportedImageParameterError(
                f"Model {manifest.public_model_id!r}: 'denoising_start' "
                "cannot be combined with reference-image conditioning -- "
                "diffusers img2img ignores the computed 'strength' whenever "
                "denoising_start is set, so the reference's lock strength "
                "would not be honored. Remove denoising_start from params "
                "or drop the reference."
            )
        # LoRA is configured on `pipeline` above, but `img2img_pipeline` (see
        # core/models/loader.py) wraps the *same* unet/text-encoder objects
        # rather than copies, so a loaded adapter is visible to both --
        # nothing extra is needed here for "LoRA + reference" to compose.
        active_pipeline = reference_pipeline if reference_capable else pipeline
        # Route the pipeline call through the provider-neutral contract
        # (generators/image/providers.py) so this local diffusers path and a
        # future cloud provider are invoked and validated the same way; the
        # kwargs passed to `pipeline` below are unchanged from before this
        # contract existed, so behavior is identical to a direct call.
        provider = LocalDiffusersImageProvider(
            model_id=manifest.public_model_id,
            pipeline=active_pipeline,
            capabilities=(
                local_diffusers_capabilities(supports_reference_image=True)
                if reference_capable
                else None
            ),
        )
        spec_lora_path = lora_metadata["path"]
        spec_lora_scale = lora_metadata["scale"]
        request_spec = ImageGenerationSpec(
            prompt=resolved_prompt.prompt,
            negative_prompt=resolved_prompt.negative_prompt,
            width=width,
            height=height,
            seed=base_seed,
            # One call below produces exactly one image (`replace(request_spec,
            # seed=...)` is called once per variation, inside the loop) --
            # batch_size describes that single call, not the job-wide
            # variation_count. A provider with an honestly small max_batch
            # would otherwise reject every multi-variation request.
            batch_size=1,
            lora_path=str(spec_lora_path) if spec_lora_path is not None else None,
            lora_scale=(
                float(cast(float, spec_lora_scale)) if spec_lora_scale is not None else 1.0
            ),
            # Set whenever a reference resolved, regardless of pipeline
            # support -- this is what makes validate_capabilities() actually
            # reject an unsupported request instead of validating nothing.
            reference_image_path=reference_image_path,
        )
        # Checked here -- before the reference image is even opened, let
        # alone resized -- rather than only inside generate_image() inside
        # the variation loop below: an absurd width/height (e.g. 100000)
        # must be rejected before Pillow attempts a matching allocation, not
        # after.
        if provider.capabilities is not None:
            validate_capabilities(provider.capabilities, request_spec)
        reference_conditioning_kwargs: dict[str, Any] = {}
        # Diffusers img2img only ever runs int(num_inference_steps *
        # strength) denoising steps internally (see the strength comment
        # below), not the full requested count -- a step callback driven by
        # the requested count would then top out well under 100% and jump
        # straight to whatever the *next* variation (or job completion)
        # reports, never itself reaching a completed fraction. Defaults to
        # the plain requested count for the non-reference (text2img) path,
        # where every requested step actually runs.
        effective_inference_steps = num_inference_steps
        if reference_capable:
            # A real img2img call derives its output size from `image`
            # itself rather than accepting width/height (unlike the text2img
            # call below), so the reference is resized to the requested
            # output dimensions instead of forwarding width/height.
            # ReferenceImageInput.strength is 0=no effect, 1=follow the
            # reference most closely (core/reference_capabilities.py).
            # diffusers img2img `strength` is the opposite: the fraction
            # of denoising applied to the *source* image, so 0=closest to
            # the reference and 1=ignores it almost entirely. Inverting
            # here keeps the public contract's meaning intact for callers
            # regardless of which pipeline convention ends up serving it.
            # Diffusers computes init_timestep = int(num_inference_steps
            # * strength) and needs at least one surviving step, so the
            # floor is derived from num_inference_steps rather than a
            # fixed constant: with the default 30 steps, a fixed 0.01
            # floor rounds down to zero steps and diffusers has nothing
            # left to denoise.
            img2img_strength = min(
                1.0,
                max(
                    # `+ 1e-6` covers float rounding in the division
                    # itself (e.g. 30 * (1.0 / 30) landing a hair under
                    # 1.0), which would otherwise still truncate to zero
                    # steps. Capped at 1.0 by the outer min(): with very
                    # few num_inference_steps (e.g. 1), the floor alone
                    # can exceed 1.0 -- diffusers has no way to honor a
                    # gentle lock with that few steps regardless of the
                    # requested strength, so it is forced to the
                    # strongest (least reference-preserving) setting.
                    (1.0 / max(1, num_inference_steps)) + 1e-6,
                    1.0 - cast(float, reference_strength),
                ),
            )
            reference_conditioning_kwargs = {
                "image": (
                    Image.open(cast(str, reference_image_path))
                    .convert("RGB")
                    .resize((width, height))
                ),
                "strength": img2img_strength,
            }
            # Mirrors diffusers' own init_timestep computation so the step
            # callback's denominator matches how many steps will actually
            # fire; floored at 1 for the same reason the strength floor
            # above exists -- there is always at least one real step to
            # report progress against.
            effective_inference_steps = max(
                1, int(num_inference_steps * img2img_strength)
            )
        common_generation_kwargs = {
            "prompt": resolved_prompt.prompt,
            "negative_prompt": resolved_prompt.negative_prompt,
            "guidance_scale": guidance_scale,
            "num_inference_steps": num_inference_steps,
            **({} if reference_capable else {"width": width, "height": height}),
            **effective_params,
            **reference_conditioning_kwargs,
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
                    active_pipeline,
                    effective_inference_steps,
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
                # `considered_references` is whichever source
                # (request.references or Bible-derived resolved_references,
                # see _resolve_references_for_conditioning) actually fed
                # conditioning -- distinct from `resolved_prompt
                # .resolved_references` above, which is Bible-only audit
                # trail regardless of which source won. #201:
                # reference_conditioning_applied is true only when exactly
                # one reference was considered and reached the pipeline as
                # image/strength conditioning; more than one considered
                # reference, or one this pipeline can't honor, already
                # failed generation before reaching here (see
                # request_spec.reference_image_path / validate_capabilities
                # and _resolve_references_for_conditioning's own checks).
                "resolved_references": [
                    reference.model_dump(mode="json")
                    for reference in resolved_prompt.resolved_references
                ],
                "considered_references": [
                    reference.model_dump(mode="json") for reference in considered_references
                ],
                "reference_conditioning_applied": reference_capable,
                "reference_applied_asset_id": (
                    considered_references[0].asset_id if reference_capable else None
                ),
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "image_provider_id": provider.provider_id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": type(runtime_obj).__name__,
                "pipeline_class": type(active_pipeline).__name__,
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

    def _resolve_references_for_conditioning(
        self,
        request: GenerationRequest,
        resolved_prompt: "ResolvedPrompt",
        manifest: "ModelManifest",
    ) -> tuple[str | None, float | None, list["ReferenceImageInput"]]:
        """Pick the references to condition on and validate them against the manifest.

        `request.references` -- the documented top-level field `JobService`
        already validates against `manifest.reference_capability` before a
        job is even created -- is combined with Bible-derived
        `resolved_prompt.resolved_references` rather than one silently
        replacing the other (#201 follow-up). Both sources already applied
        to prompt composition and both appear under `resolved_references`
        in job metadata, so a Bible reference dropped here would receive no
        pixel conditioning while still looking "considered" everywhere else,
        and would bypass the one-reference-at-a-time check below entirely.
        Bible references never go through `JobService`'s check (there is no
        `request.references` for it to see), so they are validated here
        against the same `manifest.reference_capability` contract instead of
        bypassing it entirely.

        This conditioning path honors exactly one reference image at a time:
        a request considering more than one -- whether >1 from a single
        source, or one from each source combined -- raises rather than
        silently applying only the first and reporting every one of them as
        honored.
        """

        references = list(request.references or []) + list(
            resolved_prompt.resolved_references
        )
        if not references:
            return None, None, []

        validate_reference_inputs(
            references,
            capability=manifest.reference_capability,
            model_id=manifest.public_model_id,
        )
        # validate_reference_inputs only checks that *some* mode is declared
        # (via capability.enabled) plus role/strength/preprocessing/count --
        # it does not require "img2img" specifically. This conditioning path
        # only ever performs an img2img-style call (see the img2img_pipeline
        # probe in generate()), so a manifest that advertises e.g. only
        # ip_adapter must not be routed through it.
        capability = manifest.reference_capability
        if capability is None or "img2img" not in capability.supported_modes:
            raise UnsupportedImageParameterError(
                f"Model {manifest.public_model_id!r} does not advertise "
                "img2img in reference_capability.supported_modes, which is "
                "the only conditioning mode this path implements."
            )
        if len(references) > 1:
            raise UnsupportedImageParameterError(
                f"Model {manifest.public_model_id!r} was asked to honor "
                f"{len(references)} reference images at once, but this "
                "conditioning path applies exactly one; remove all but one "
                "reference from the request or Bible entries in play."
            )
        primary = references[0]
        # Likewise, validate_reference_inputs only checks the requested
        # preprocessing is one the manifest declares support for -- it does
        # not know this path never actually applies face_crop/canny/depth
        # transforms, only a plain resize. Silently ignoring a declared
        # preprocessing request would report conditioning as applied while
        # quietly skipping part of what was asked for.
        if primary.preprocessing not in ("none", "auto"):
            raise UnsupportedImageParameterError(
                f"Model {manifest.public_model_id!r}: reference preprocessing "
                f"{primary.preprocessing!r} is not implemented by this "
                "conditioning path (only 'none'/'auto', a plain resize, are)."
            )

        asset_repository = (
            self.prompt_composer.asset_repository
            if self.prompt_composer is not None
            else None
        )
        if asset_repository is None:
            raise MissingReferenceAssetError(
                f"Reference asset {primary.asset_id!r} was requested but no "
                "asset repository is configured to resolve it."
            )
        asset = asset_repository.get(primary.asset_id)
        if asset is None:
            raise MissingReferenceAssetError(
                f"Reference asset {primary.asset_id!r} could not be resolved "
                "(missing or deleted); it may have existed when the prompt "
                "was composed but is no longer available."
            )
        if asset.media_type != "image":
            # PromptComposer._resolve_reference_asset already enforces this
            # for Bible-derived references; request.references skips the
            # composer entirely; enforced here too so it is not the only
            # source that can point conditioning at a non-image asset.
            raise MissingReferenceAssetError(
                f"Reference asset {primary.asset_id!r} is {asset.media_type!r}, "
                "not image; reference-image conditioning requires an image asset."
            )
        if primary.strength <= 0.0:
            # ReferenceImageInput.strength=0 means "no effect" -- but img2img
            # still VAE-encodes the reference and consumes the seeded
            # generator's random draws to do it, so even diffusers
            # strength=1.0 (the value this would otherwise invert to) is not
            # guaranteed to reproduce what a plain text2img call would have
            # produced. An explicit zero-strength reference is treated as
            # unconditioned generation -- reported in `considered_references`
            # for audit purposes, but never routed through img2img -- rather
            # than as "closest to the reference" at diffusers strength 1.0.
            return None, None, references
        return asset.path, primary.strength, references

    def _pipeline_accepts_reference_image(self, pipeline: object) -> bool:
        call = getattr(pipeline, "__call__", None)
        if call is None:
            return False
        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):
            return False
        return "image" in signature.parameters and "strength" in signature.parameters

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
