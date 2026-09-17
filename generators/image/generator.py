"""Image generator backed by a local diffusers runtime."""

from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import secrets
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from PIL import Image

from core.jobs.context import GenerationCancelled
from core.models import ModelService
from core.models.service import RuntimeHandle
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
    from collections.abc import Callable

    from core.jobs.context import GenerationContext
    from core.models import ModelManifest
    from core.reference_capabilities import ReferenceImageInput
    from generators.common.prompting import ResolvedPrompt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_VARIATION_COUNT = 4
_SEED_MODULUS = 1 << 63
_CANCELLATION_POLL_SECONDS = 0.1


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

        # ------------------------------------------------------------- preflight
        # PR4b / FP-001 + FP-002: everything in this section is pure request
        # parsing, parameter normalization, prompt/reference resolution, and
        # LoRA path existence checking -- none of it touches a runtime, so
        # none of it may run inside the lease below. A bad width/height, a
        # missing LoRA file, or an unresolvable reference asset must fail
        # before a healthy runtime is ever acquired, let alone invalidated.
        manifest = self.model_service.get_manifest(
            requested_model_id, media_type="image", task_type=self.task_type
        )
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
        # Pure input error: a nonexistent/invalid LoRA path is rejected here,
        # before any runtime is acquired, so it can never invalidate a
        # healthy one. `_apply_lora()` inside the lease receives the already
        # -resolved path and never re-touches the filesystem.
        resolved_lora_path = self._resolve_optional_path(lora_path)
        base_seed = (
            resolved_prompt.seed
            if resolved_prompt.seed is not None
            else secrets.randbits(63)
        )
        (
            reference_image_path,
            reference_strength,
            reference_applied_asset_id,
            considered_references,
        ) = self._resolve_references_for_conditioning(
            request,
            resolved_prompt,
            manifest,
            project_id=context.project_id if context is not None else None,
        )
        # Pure input error: a corrupt, unreadable, or since-deleted
        # reference image is decoded here, before any runtime is acquired,
        # so it can never invalidate a healthy one. Decoded/resized
        # unconditionally whenever a reference resolved; only actually used
        # inside the lease if `reference_capable` (determined by runtime
        # inspection) turns out true.
        reference_image: Image.Image | None = None
        if reference_image_path is not None:
            # Reject an oversized/misaligned width or height against the
            # local provider's fixed, runtime-independent size bounds
            # before decoding the reference image at all -- avoids
            # resizing to an absurd resolution for a request that would be
            # rejected either way. These bounds do not depend on
            # `reference_capable` (only known once the runtime is
            # inspected inside the lease below), so this preliminary check
            # is safe here; the authoritative, reference-support-aware
            # validate_capabilities() call still runs again inside the
            # lease once `reference_capable` is known.
            validate_capabilities(
                local_diffusers_capabilities(),
                ImageGenerationSpec(
                    prompt=resolved_prompt.prompt,
                    negative_prompt=resolved_prompt.negative_prompt,
                    width=width,
                    height=height,
                    seed=base_seed,
                    batch_size=1,
                ),
            )
            reference_image = (
                Image.open(reference_image_path).convert("RGB").resize((width, height))
            )
        batch_id = f"img_{uuid4().hex}"

        # ----------------------------------------------------------------- lease
        # PR4b / FP-001 + FP-002: acquire the runtime once and hold it for
        # every variation -- pipeline/img2img-pipeline inspection, LoRA
        # mutation, and every variation's actual inference all happen inside
        # this one lease. It is released *before* file writes and
        # quality/semantic scoring, which do not touch the runtime, so a
        # future local semantic judge sharing the same process-wide admission
        # domain (CLIP/CLAP) never faces a same-thread nested acquisition.
        #
        # `validation_error` covers the one case that needs runtime
        # *inspection* (does this pipeline accept a reference image? does the
        # declared capability/size contract accept this request?) but not
        # runtime *mutation* -- reference-capability discovery only reads
        # pipeline call signatures, it never calls into diffusers. When set,
        # the lease is left to exit normally (no exception raised inside the
        # `with`), so `RuntimeHandle.__exit__` releases a still-healthy entry
        # instead of marking it INVALID; the error is raised only after the
        # lease is gone. Once LoRA mutation actually begins, every failure
        # past that point is a genuine runtime-use failure and is left to
        # propagate, which conservatively marks the entry INVALID.
        cancelled_before_mutation = False
        late_cancellation = False
        progress_error: Exception | None = None
        validation_error: Exception | None = None

        def _record_progress_error(exc: Exception) -> None:
            # Retain only the first progress-publication failure: later
            # step callbacks in the same (or a later) variation may also
            # fail once report_progress starts erroring, but only the
            # first is diagnostically useful and only one gets re-raised
            # once the lease releases.
            nonlocal progress_error
            if progress_error is None:
                progress_error = exc

        reference_capable = False
        lora_metadata: dict[str, object | None] = {"path": None, "scale": None}
        runtime_type = ""
        pipeline_class_name = ""
        device: object = None
        load_dtype: object = None
        torch_dtype: object = None
        image_provider_id = ""
        collected_variations: list[dict[str, Any]] = []

        with self._acquire_runtime(requested_model_id, context) as handle:
            manifest = handle.manifest
            runtime_obj = handle.runtime
            pipeline = runtime_obj["pipeline"]

            cancelled_before_mutation = context is not None and context.is_cancelled()
            if not cancelled_before_mutation:
                # A reference is only actually honored when a dedicated
                # img2img-shaped runtime exists and its own call signature
                # takes image/strength (#201: one supported conditioning
                # path, not every image model family -- StableDiffusionXLPipeline
                # itself never accepts these, see core/models/loader.py's
                # separate img2img_pipeline). This is inspection only (a
                # signature check), never a pipeline call.
                reference_pipeline = runtime_obj.get("img2img_pipeline")
                reference_capable = (
                    reference_image_path is not None
                    and reference_pipeline is not None
                    and self._pipeline_accepts_reference_image(reference_pipeline)
                )
                if reference_capable:
                    # Diffusers img2img's get_timesteps() ignores the computed
                    # `strength` whenever `denoising_start` is also set --
                    # and `denoising_end`/`timesteps`/`sigmas` each silently
                    # break the reference's lock strength or progress
                    # reporting the same way. Rejected outright for a
                    # reference job rather than forwarded alongside strength.
                    for incompatible_param in (
                        "denoising_start",
                        "denoising_end",
                        "timesteps",
                        "sigmas",
                    ):
                        if incompatible_param in effective_params:
                            validation_error = UnsupportedImageParameterError(
                                f"Model {manifest.public_model_id!r}: "
                                f"{incompatible_param!r} cannot be combined with "
                                "reference-image conditioning -- diffusers img2img's "
                                "timestep selection from denoising_start/denoising_end/"
                                "timesteps/sigmas does not compose with the computed "
                                "'strength', so the reference's lock strength would "
                                f"not be honored. Remove {incompatible_param!r} from "
                                "params or drop the reference."
                            )
                            break

                # LoRA is applied to `pipeline` below, but `img2img_pipeline`
                # wraps the *same* unet/text-encoder objects rather than
                # copies, so a loaded adapter is visible to both -- nothing
                # extra is needed for "LoRA + reference" to compose.
                active_pipeline = reference_pipeline if reference_capable else pipeline
                # Route the pipeline call through the provider-neutral
                # contract (generators/image/providers.py) so this local
                # diffusers path and a future cloud provider are invoked and
                # validated the same way.
                provider = LocalDiffusersImageProvider(
                    model_id=manifest.public_model_id,
                    pipeline=active_pipeline,
                    capabilities=(
                        local_diffusers_capabilities(supports_reference_image=True)
                        if reference_capable
                        else None
                    ),
                )
                spec_lora_path = (
                    str(resolved_lora_path) if resolved_lora_path is not None else None
                )
                request_spec = ImageGenerationSpec(
                    prompt=resolved_prompt.prompt,
                    negative_prompt=resolved_prompt.negative_prompt,
                    width=width,
                    height=height,
                    seed=base_seed,
                    # One call below produces exactly one image -- batch_size
                    # describes that single call, not the job-wide
                    # variation_count.
                    batch_size=1,
                    lora_path=spec_lora_path,
                    lora_scale=float(lora_scale) if spec_lora_path is not None else 1.0,
                    # Set whenever a reference resolved, regardless of
                    # pipeline support -- this is what makes
                    # validate_capabilities() actually reject an unsupported
                    # request instead of validating nothing.
                    reference_image_path=reference_image_path,
                )
                # Checked here -- before the reference image is even opened,
                # let alone resized -- rather than only inside
                # generate_image() inside the variation loop below: an
                # absurd width/height (e.g. 100000) must be rejected before
                # Pillow attempts a matching allocation, not after. This is
                # still inspection-only (reference_capable came from a
                # signature check, not a pipeline call), so a rejection here
                # still exits the lease without invalidating it.
                if validation_error is None and provider.capabilities is not None:
                    try:
                        validate_capabilities(provider.capabilities, request_spec)
                    except UnsupportedImageParameterError as exc:
                        validation_error = exc

                reference_conditioning_kwargs: dict[str, Any] = {}
                # Diffusers img2img only ever runs int(num_inference_steps *
                # strength) denoising steps internally -- defaults to the
                # plain requested count for the non-reference (text2img)
                # path, where every requested step actually runs.
                effective_inference_steps = num_inference_steps
                if validation_error is None and reference_capable:
                    # ReferenceImageInput.strength is 0=no effect, 1=follow
                    # the reference most closely; diffusers img2img
                    # `strength` is the opposite (0=closest to the
                    # reference, 1=ignores it), so it is inverted here.
                    # Diffusers computes init_timestep = int(num_inference_steps
                    # * strength) and needs at least one surviving step;
                    # rejecting a combination that would leave zero is more
                    # honest than silently flooring it to a materially
                    # different, weaker lock.
                    img2img_strength = 1.0 - cast(float, reference_strength)
                    if int(num_inference_steps * img2img_strength) < 1:
                        validation_error = UnsupportedImageParameterError(
                            f"Model {manifest.public_model_id!r}: the requested "
                            f"reference lock strength {reference_strength} combined "
                            f"with {num_inference_steps} inference step(s) would "
                            "leave diffusers with zero denoising steps to actually "
                            "run. Increase num_inference_steps or reduce the "
                            "reference strength."
                        )
                    else:
                        reference_conditioning_kwargs = {
                            # Already decoded/resized during preflight above
                            # -- `reference_capable` being true guarantees
                            # `reference_image_path` (and therefore
                            # `reference_image`) was not None.
                            "image": reference_image,
                            "strength": img2img_strength,
                        }
                        # Mirrors diffusers' own init_timestep computation --
                        # the check above already guarantees this is >= 1.
                        effective_inference_steps = int(
                            num_inference_steps * img2img_strength
                        )

                if validation_error is None:
                    # Runtime mutation begins here. Every failure from this
                    # point on (a bad LoRA file diffusers itself rejects, a
                    # provider/inference error, mid-render cancellation) is a
                    # genuine runtime-use failure and is left to propagate,
                    # which conservatively marks this entry INVALID via
                    # RuntimeHandle.__exit__.
                    lora_metadata = self._apply_lora(
                        runtime_obj, pipeline, resolved_lora_path, lora_scale
                    )
                    runtime_type = type(runtime_obj).__name__
                    pipeline_class_name = type(active_pipeline).__name__
                    device = runtime_obj["device"]
                    load_dtype = runtime_obj.get("load_dtype")
                    torch_dtype = runtime_obj["torch_dtype"]
                    image_provider_id = provider.provider_id

                    common_generation_kwargs = {
                        "prompt": resolved_prompt.prompt,
                        "negative_prompt": resolved_prompt.negative_prompt,
                        "guidance_scale": guidance_scale,
                        "num_inference_steps": num_inference_steps,
                        **(
                            {}
                            if reference_capable
                            else {"width": width, "height": height}
                        ),
                        **effective_params,
                        **reference_conditioning_kwargs,
                    }

                    for variation_index in range(variation_count):
                        # Snapshot, not raise: by this point LoRA mutation
                        # has already happened (once, above the loop), so a
                        # cancellation observed here -- whether before
                        # starting a fresh variation or (below) right after
                        # a variation's provider call returns -- is a
                        # request to stop after already-successful runtime
                        # use, not a runtime fault. Raising inside the lease
                        # would conservatively invalidate a runtime that is
                        # otherwise perfectly healthy; stopping the loop and
                        # exiting cleanly instead lets the caller retry
                        # without an unnecessary reload. Only a
                        # `GenerationCancelled` raised from *inside* the
                        # provider call itself (the step callback below,
                        # invoked while inference is genuinely in progress)
                        # is a real mid-use interruption and is left to
                        # propagate and conservatively invalidate.
                        if context is not None and context.is_cancelled():
                            late_cancellation = True
                            break
                        variation_seed = self._derive_variation_seed(
                            base_seed, variation_index
                        )
                        generation_kwargs = dict(common_generation_kwargs)
                        generation_kwargs["generator"] = self._create_generator(
                            variation_seed, runtime_obj["device"], torch
                        )
                        step_callback = self._build_step_callback(
                            active_pipeline,
                            effective_inference_steps,
                            context,
                            variation_index=variation_index,
                            variation_count=variation_count,
                            record_progress_error=_record_progress_error,
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

                        if context is not None and context.is_cancelled():
                            late_cancellation = True
                            break
                        # Each provider_result.image is a CPU-side PIL image,
                        # never a device/runtime-bound tensor -- holding up
                        # to _MAX_VARIATION_COUNT (4) of them until after the
                        # lease releases is bounded, ordinary memory, not a
                        # reason to re-acquire the runtime per variation.
                        collected_variations.append(
                            {
                                "variation_index": variation_index,
                                "seed": variation_seed,
                                "image": provider_result.image,
                                "provider_id": provider_result.identity.provider_id,
                                "provider_request_id": provider_result.identity.request_id,
                            }
                        )
                        if context is not None and step_callback is None:
                            # The provider call already returned
                            # successfully above -- this variation's
                            # runtime use is done. In production this hook
                            # writes through JobRepository, so a transient
                            # DB/event-publication failure here must not be
                            # allowed to escape the lease and invalidate a
                            # runtime that rendered correctly; capture it
                            # and stop starting further variations instead,
                            # exactly like `late_cancellation` above.
                            try:
                                context.report_progress(
                                    (variation_index + 1) / variation_count
                                )
                            except Exception as exc:
                                progress_error = exc
                                break
                        elif progress_error is not None:
                            # The step callback above already captured a
                            # progress-publication failure mid-inference
                            # (deferred past the lease, not raised there).
                            # The provider call still completed successfully,
                            # so stop starting further variations instead of
                            # masking or repeating the failure, exactly like
                            # `late_cancellation` above.
                            break

        # No mutation or inference took place, or every started variation's
        # provider call completed successfully before cancellation was
        # observed: exit cleanly instead of marking the cache INVALID. A
        # `GenerationCancelled` raised from *inside* a provider call (a
        # genuine mid-use interruption) is not caught above and still
        # unwinds as unsafe.
        if cancelled_before_mutation or late_cancellation:
            raise GenerationCancelled()
        # A boundary progress-publication failure observed only after a
        # variation's runtime use already completed successfully -- the
        # lease already exited normally above, so raising here never
        # touches a still-healthy runtime's INVALID state.
        if progress_error is not None:
            raise progress_error
        # A validation-only rejection discovered via runtime inspection
        # (unsupported reference/size, or an unreachable reference lock
        # strength) -- the lease already exited normally above, so raising
        # here never touches a still-healthy runtime's INVALID state.
        if validation_error is not None:
            raise validation_error

        # -------------------------------------------------------- post-lease
        # File writes and quality/semantic scoring happen with the runtime
        # lease already released -- semantic scoring in particular must
        # observe the lease inactive so a future CLIP/CLAP admission
        # participant sharing this process-wide gate never nests inside this
        # generator's own runtime-execution ownership.
        output_paths: list[str] = []
        variation_metadata: list[dict[str, Any]] = []
        quality_reports: list[dict[str, Any]] = []
        try:
            for entry in collected_variations:
                # Cancellation is still observed here, even though the
                # runtime is no longer involved: without this check, every
                # remaining PNG would be written and scored before
                # JobRunner notices cancellation at its own outer boundary,
                # leaving orphaned output files behind. `GenerationCancelled`
                # is an `Exception`, so it is caught by the `except` below
                # like any other post-lease failure, reusing the exact same
                # cleanup that removes whatever this loop already saved.
                if context is not None:
                    context.raise_if_cancelled()
                variation_index = cast(int, entry["variation_index"])
                output_path = self.output_dir / (
                    f"{batch_id}.png"
                    if variation_count == 1
                    else f"{batch_id}_v{variation_index + 1}.png"
                )
                image = cast(Image.Image, entry["image"])
                image.save(output_path)
                output_paths.append(str(output_path))

                quality_report = evaluate_image_output(output_path)
                semantic_report = evaluate_image_semantics(
                    output_path,
                    resolved_prompt.prompt,
                    resolved_prompt.negative_prompt,
                )
                enrich_quality_report(quality_report, semantic_report)
                # Codex P2 finding "Image cancellation during
                # quality/semantic scoring": the checkpoint above only
                # catches cancellation requested *between* variations -- a
                # stop requested while evaluate_image_output()/
                # evaluate_image_semantics() ran on the current variation,
                # particularly the final one, was never observed (no
                # further loop iteration to catch it), leaving the PNG it
                # just saved behind once JobRunner discards the cancelled
                # result. Checking again here, right after scoring, closes
                # that gap for every variation -- still inside this same
                # `try`, so the `except` below removes every PNG this loop
                # has saved so far, and the runtime lease released above is
                # never touched.
                if context is not None:
                    context.raise_if_cancelled()
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
                        "seed": entry["seed"],
                        "output_path": str(output_path),
                        "preview_path": str(output_path),
                        "params": variation_params,
                        "quality_report": quality_report,
                        "provider_id": entry["provider_id"],
                        "provider_request_id": entry["provider_request_id"],
                    }
                )
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
                "resolved_references": [
                    reference.model_dump(mode="json")
                    for reference in resolved_prompt.resolved_references
                ],
                "considered_references": [
                    reference.model_dump(mode="json") for reference in considered_references
                ],
                "reference_conditioning_applied": reference_capable,
                "reference_applied_asset_id": (
                    reference_applied_asset_id if reference_capable else None
                ),
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "image_provider_id": image_provider_id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": runtime_type,
                "pipeline_class": pipeline_class_name,
                "device": device,
                "load_dtype": load_dtype,
                "torch_dtype": torch_dtype,
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

    def _acquire_runtime(
        self, model_id: str | None, context: "GenerationContext | None"
    ) -> RuntimeHandle:
        if context is None:
            return self.model_service.acquire_runtime(
                model_id, media_type="image", task_type=self.task_type
            )
        # One logical, cancellation-aware wait: ModelService polls in bounded
        # slices and calls the checkpoint between them with nothing held.
        # Only synchronization waits are waited out; cache capacity, invalid
        # entries and loader errors still surface. This does not preempt a
        # synchronous loader that already started.
        return self.model_service.acquire_runtime(
            model_id, media_type="image", task_type=self.task_type,
            wait_checkpoint=context.raise_if_cancelled,
            poll_interval=_CANCELLATION_POLL_SECONDS,
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
        record_progress_error: "Callable[[Exception], None]",
    ):
        if context is None or num_inference_steps <= 0:
            return None
        if not self._pipeline_accepts_step_callback(pipeline):
            return None

        def _on_step_end(pipe: object, step_index: int, timestep: object, callback_kwargs: dict):
            step_fraction = (step_index + 1) / num_inference_steps
            # Only a progress-publication failure (DB/event-publication, in
            # production) is caught here -- it is bookkeeping outside the
            # runtime fault boundary, not a provider/runtime failure, so it
            # must not escape this callback and abort inference or cause
            # RuntimeHandle.__exit__ to invalidate an otherwise-healthy
            # runtime. It is deferred and re-raised once the lease has
            # released cleanly. Cancellation is a genuine runtime-fault-
            # boundary concern and is left to propagate un-caught, exactly
            # as before.
            try:
                context.report_progress(
                    (variation_index + step_fraction) / variation_count
                )
            except Exception as exc:
                record_progress_error(exc)
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
        *,
        project_id: str | None = None,
    ) -> tuple[str | None, float | None, str | None, list["ReferenceImageInput"]]:
        """Pick the references to condition on and validate them against the manifest.

        Returns ``(reference_image_path, reference_strength,
        applied_asset_id, considered_references)``. ``considered_references``
        is every reference actually requested (zero-strength ones included,
        for audit); ``applied_asset_id`` is the one reference among those
        that was actually selected for conditioning, distinct from
        ``considered_references[0]`` -- a zero-strength reference can
        legitimately be requested first and a later, non-zero-strength one
        still be the one applied.

        This performs no runtime access at all -- only manifest lookups and
        asset-repository resolution -- so it belongs in `generate()`'s
        preflight section, entirely before any runtime lease is acquired.

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

        This conditioning path honors exactly one *effective* (strength > 0)
        reference image at a time: a request whose effective references
        number more than one -- whether >1 from a single source, or one from
        each source combined -- raises rather than silently applying only
        the first and reporting every one of them as honored. A zero-strength
        reference (strength=0 means "no effect" in the public contract)
        never occupies that one-image slot and is never selected as the
        primary conditioning reference, since it never reaches img2img
        either way (#201 follow-up, confirmed product decision on Codex's
        fourteenth review round on PR #376) -- it is still validated above
        and still reported in the returned list for audit/provenance
        metadata (see the effective-vs-requested split below).

        `project_id` re-checks the same project-membership invariant
        `JobService.create_job()` already enforced at job-creation time
        (#201 follow-up, seventh Codex round on PR #376): that check
        resolves a *mutable* Bible entry once at creation, but this method
        resolves it again here when the job actually executes -- if
        `PATCH /bible/{entry_id}` changed `reference_asset_ids` to a
        different project's asset in between, the creation-time check saw
        only the old asset. Re-checking against the resolved asset itself,
        right where it is actually used, closes that gap regardless of
        which upstream check (if any) already ran.
        """

        # #201 follow-up (Codex P2, eleventh round): request.references and
        # resolved_prompt.resolved_references are independent sources -- a
        # caller can supply the same (asset, role, strength, preprocessing)
        # explicitly and also have it resolve through a Bible entry. That is
        # one semantic lock, not two; counting it twice below would reject a
        # request this path can actually honor. Bible-internal duplicates are
        # already collapsed by PromptComposer.compose(), so only the
        # cross-source case needs handling here.
        seen_references: set[tuple[str, str, float, str]] = set()
        references: list["ReferenceImageInput"] = []
        for reference in list(request.references or []) + list(
            resolved_prompt.resolved_references
        ):
            dedupe_key = (
                reference.asset_id,
                reference.role,
                reference.strength,
                reference.preprocessing,
            )
            if dedupe_key in seen_references:
                continue
            seen_references.add(dedupe_key)
            references.append(reference)
        if not references:
            return None, None, None, []

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
        # #201 follow-up (Codex P2, fourteenth round, confirmed product
        # decision): strength=0 means "no effect", so a zero-strength
        # reference must not consume the single applied-image slot below or
        # be selected as `primary` -- it is excluded from the count and
        # selection here, but stays in `references` (returned unfiltered)
        # so it is still reported in `considered_references` metadata.
        effective_references = [
            reference for reference in references if reference.strength > 0.0
        ]
        if not effective_references:
            # ReferenceImageInput.strength=0 means "no effect" -- but img2img
            # still VAE-encodes the reference and consumes the seeded
            # generator's random draws to do it, so even diffusers
            # strength=1.0 (the value zero would otherwise invert to) is not
            # guaranteed to reproduce what a plain text2img call would have
            # produced. Every requested reference here is zero-strength (or
            # there were none), so generation stays unconditioned, with no
            # primary asset to resolve or apply.
            return None, None, None, references
        if len(effective_references) > 1:
            raise UnsupportedImageParameterError(
                f"Model {manifest.public_model_id!r} was asked to honor "
                f"{len(effective_references)} reference images at once, but "
                "this conditioning path applies exactly one; remove all but "
                "one non-zero-strength reference from the request or Bible "
                "entries in play."
            )
        primary = effective_references[0]
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
        if asset.project_id != project_id:
            # Re-check at the point of actual use, not just at job creation
            # (see the docstring above) -- covers both request.references
            # (already checked once by JobService) and Bible-derived
            # references (only ever checked at job-creation time for
            # character/location entries; this is authoritative for both).
            raise MissingReferenceAssetError(
                f"Reference asset {primary.asset_id!r} belongs to project "
                f"{asset.project_id or 'no project'!r}, not "
                f"{project_id or 'no project'!r}; a reference must belong "
                "to the same project as the job it conditions."
            )
        # `primary` is drawn from `effective_references` above, so it is
        # always strength > 0 here -- the strength=0 case (and img2img's own
        # VAE-encode/seeded-generator side effects that make even diffusers
        # strength=1.0 an unsafe stand-in for "unconditioned") is handled
        # above, before any asset is resolved. `primary.asset_id` is
        # returned explicitly rather than left for the caller to infer as
        # `considered_references[0]` -- a zero-strength reference can
        # legitimately be requested first while a later, effective one is
        # what actually gets applied.
        return asset.path, primary.strength, primary.asset_id, references

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

    def _apply_lora(
        self,
        runtime_obj: dict[str, object],
        pipeline: Any,
        resolved_path: Path | None,
        lora_scale: float,
    ) -> dict[str, object | None]:
        """Runtime-mutating half of LoRA configuration.

        `resolved_path` is already-validated (existence-checked in
        `generate()`'s preflight section via `_resolve_optional_path()`)
        before this is ever called -- this method only touches
        `runtime_obj`/`pipeline` state and never re-touches the filesystem,
        so it belongs entirely inside the runtime lease.
        """

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
            load_kwargs: dict[str, object] = {"adapter_name": adapter_name}
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
