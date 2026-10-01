"""Audio generator backed by a local transformers MusicGen runtime."""

from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
import wave

from core.assets import AssetRepository
from core.audio import MUSIC_PRESET, process_music_channels, skipped_processing_report
from core.audio_conditioning import prepare_wav_reference
from core.jobs.context import GenerationCancelled
from core.models import ModelService
from core.models.service import RuntimeHandle
from core.quality import (
    enrich_quality_report,
    evaluate_audio_output,
    evaluate_audio_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

from .providers import (
    AudioProviderCapability,
    ensure_capabilities_declared,
    manifest_declared_capabilities,
)

_MAX_INT16 = 32_767
_AUDIO_PARAM_RANGES = {
    "guidance_scale": (1.0, 10.0),
    "temperature": (0.1, 2.0),
    "top_k": (0.0, 1000.0),
    "top_p": (0.0, 1.0),
    "bpm": (40.0, 240.0),
}
_SHORT_DURATION_RANGE = (2.0, 30.0)
_LONG_DURATION_RANGE = (31.0, 120.0)
_LONG_STRIDE_RANGE = (5.0, 29.0)
_DEFAULT_LONG_STRIDE_SECONDS = 18.0
_CANCELLATION_POLL_SECONDS = 0.1

ProgressCallback = Callable[[float, int, int], None]
CancelRequested = Callable[[], bool]


def _safe_cancel_requested(
    cancel_requested: "CancelRequested | None",
) -> tuple[bool, Exception | None]:
    """Probe `cancel_requested()`, isolating a bookkeeping failure.

    Audio-local counterpart to Text/Image/Video's `GenerationContext`-based
    `safe_is_cancelled()` (`generators/common/cancellation.py`, frozen for
    this lane): `AudioGenerator.run_with_control()` receives cancellation as
    a plain `Callable[[], bool]` -- `lambda: self._is_cancelled(job_id)`,
    wired by `JobRunner.process_job()` -- rather than a `GenerationContext`,
    so there is no `.is_cancelled()` method to share that helper's call
    site. In production this reads `JobRepository.get(job_id)`, real I/O
    that can raise (a transient DB/connection failure, for example).

    Returns `(cancelled, probe_error)`. Exactly one call is made to
    `cancel_requested()` (none at all if it is `None`, in which case
    `(False, None)` is returned): if it raises, that exception is captured
    and returned as `probe_error` with `cancelled` left `False`; otherwise
    its bool result is returned as `cancelled` with `probe_error` `None`.
    Never raises itself. Kept private and Audio-local rather than added to
    `generators/common/cancellation.py`, which is frozen for this lane.
    """

    if cancel_requested is None:
        return False, None
    try:
        return cancel_requested(), None
    except Exception as exc:  # noqa: BLE001 -- deliberately narrow to this one call
        return False, exc


class LongFormGenerationCancelled(RuntimeError):
    """Raised after a completed AudioCraft segment when cancellation was requested."""


class AudioGenerator(BaseGenerator):
    """Generate music with the resolved model runtime."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/audio",
        *,
        asset_repository: AssetRepository | None = None,
        task_type: str = "text-to-music",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.asset_repository = asset_repository
        self.task_type = task_type

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "audio":
            raise ValueError("AudioGenerator only supports audio requests.")
        if not request.prompt.strip():
            raise ValueError("Audio prompt must not be empty.")
        if request.output_format and request.output_format.lower() != "wav":
            raise ValueError("AudioGenerator currently supports wav output only.")
        manifest = self.model_service.get_manifest(
            request.model_id.strip() or None,
            media_type="audio",
            task_type=self.task_type,
        )
        is_long_form = "long-form" in manifest.tags
        if manifest.provider == "cloud":
            self._validate_cloud_capabilities(manifest, request, is_long_form=is_long_form)
        duration_minimum, duration_maximum = (
            _LONG_DURATION_RANGE if is_long_form else _SHORT_DURATION_RANGE
        )
        duration_value = request.params.get(
            "duration_seconds",
            manifest.default_params.get("duration_seconds"),
        )
        self._validate_numeric_range(
            "duration_seconds",
            duration_value,
            duration_minimum,
            duration_maximum,
        )

        stride_value = request.params.get(
            "extend_stride_seconds",
            manifest.default_params.get("extend_stride_seconds"),
        )
        if is_long_form:
            self._validate_numeric_range(
                "extend_stride_seconds",
                stride_value,
                *_LONG_STRIDE_RANGE,
            )
        elif "extend_stride_seconds" in request.params:
            raise ValueError(
                "Audio parameter 'extend_stride_seconds' is only supported by "
                "models tagged 'long-form'."
            )

        for name, (minimum, maximum) in _AUDIO_PARAM_RANGES.items():
            value = request.params.get(name)
            if value is None:
                continue
            self._validate_numeric_range(name, value, minimum, maximum)

        if "postprocess" in request.params:
            _coerce_postprocess_flag(request.params["postprocess"])
        elif "postprocess" in manifest.default_params:
            _coerce_postprocess_flag(manifest.default_params["postprocess"])

    def _validate_numeric_range(
        self,
        name: str,
        value: Any,
        minimum: float,
        maximum: float,
    ) -> None:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Audio parameter '{name}' must be numeric.") from exc
        if not math.isfinite(numeric_value) or not minimum <= numeric_value <= maximum:
            raise ValueError(
                f"Audio parameter '{name}' must be between {minimum:g} and {maximum:g}."
            )

    def _validate_cloud_capabilities(
        self,
        manifest: Any,
        request: GenerationRequest,
        *,
        is_long_form: bool,
    ) -> None:
        """Reject a `provider: cloud` request the manifest never advertised.

        Runs from validate_request(), strictly before generate() would
        resolve any runtime/adapter, so an unsupported capability never
        reaches a network call (see generators/audio/providers.py, #234).
        Local (non-cloud) manifests never call this, so their behavior is
        unchanged.
        """

        required = {AudioProviderCapability.TEXT_TO_MUSIC}
        if is_long_form:
            required.add(AudioProviderCapability.LONG_FORM)
        if str(request.params.get("reuse_action") or "") == "melody":
            required.add(AudioProviderCapability.MELODY_CONDITIONING)

        manifest_label = f"Model {manifest.public_model_id!r}"
        declared = manifest_declared_capabilities(
            manifest.default_params,
            manifest_label=manifest_label,
        )
        ensure_capabilities_declared(
            declared,
            frozenset(required),
            manifest_label=manifest_label,
        )

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_with_control(
        self,
        request: GenerationRequest,
        *,
        progress_callback: ProgressCallback,
        cancel_requested: CancelRequested,
    ) -> GenerationResult:
        """Run with cooperative segment progress/cancellation for long-form models."""

        self.validate_request(request)
        self.prepare(request)
        try:
            return self.generate(
                request,
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
            )
        finally:
            self.cleanup(request)

    # Intentionally not context-shaped: this generator opts into segment-level
    # progress/cancel via run_with_control() (see JobRunner.process_job in
    # core/jobs/runner.py), a separate duck-typed dispatch path BaseGenerator.run()
    # checks for before falling back to the generic `context` signature.
    def generate(  # type: ignore[override]
        self,
        request: GenerationRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_requested: CancelRequested | None = None,
    ) -> GenerationResult:
        import torch

        requested_model_id = request.model_id.strip() or None
        # PR4b Music lane / FP-001 + FP-002: manifest resolution reads the
        # registry only -- it never touches the runtime cache/loader, so it
        # is safe (and required) to do before any lease is acquired, exactly
        # like every other generator's preflight `get_manifest()` call.
        manifest = self.model_service.get_manifest(
            requested_model_id, media_type="audio", task_type=self.task_type
        )
        if "long-form" in manifest.tags:
            return self._generate_long_form(
                request,
                manifest=manifest,
                requested_model_id=requested_model_id,
                torch=torch,
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
            )
        return self._generate_short_form(
            request,
            manifest=manifest,
            requested_model_id=requested_model_id,
            torch=torch,
            cancel_requested=cancel_requested,
        )

    def _generate_short_form(
        self,
        request: GenerationRequest,
        *,
        manifest: Any,
        requested_model_id: str | None,
        torch: Any,
        cancel_requested: CancelRequested | None,
    ) -> GenerationResult:
        effective_params = {**manifest.default_params, **request.params}
        lineage_metadata = _extract_lineage_metadata(effective_params)
        for lineage_key in lineage_metadata:
            effective_params.pop(lineage_key, None)
        reuse_action = str(lineage_metadata.get("reuse_action") or "")
        min_reference_duration_value = effective_params.pop(
            "min_reference_duration_seconds",
            None,
        )
        max_reference_duration_value = effective_params.pop(
            "max_reference_duration_seconds",
            None,
        )
        duration_seconds = max(1, int(effective_params.pop("duration_seconds", 8)))
        guidance_scale = float(effective_params.pop("guidance_scale", 3.0))
        temperature = float(effective_params.pop("temperature", 1.0))
        top_k = int(effective_params.pop("top_k", 250))
        top_p = float(effective_params.pop("top_p", 0.0))
        bpm_value = effective_params.pop("bpm", None)
        bpm = int(bpm_value) if bpm_value is not None else None
        mood = str(effective_params.pop("mood", "")).strip().lower() or None
        genre = str(effective_params.pop("genre", "")).strip().lower() or None
        instruments = str(effective_params.pop("instruments", "")).strip() or None
        structure = str(effective_params.pop("structure", "")).strip().lower() or None
        postprocess_enabled = _coerce_postprocess_flag(
            effective_params.pop("postprocess", True)
        )
        conditioning_prompt = self._build_conditioning_prompt(
            request.prompt,
            mood=mood,
            genre=genre,
            instruments=instruments,
            structure=structure,
            bpm=bpm,
        )

        # PR4b Music lane / FP-001 + FP-002: melody conditioning's Gallery
        # lookup and reference-duration parsing touch no runtime at all --
        # a missing asset ID, an asset that doesn't exist, the wrong media
        # type, or an invalid duration bound must all fail here, before any
        # lease is even acquired, let alone held. Only `prepare_wav_reference()`
        # itself (below, inside the lease) genuinely needs the runtime: the
        # target sampling rate comes from the loaded model's own config
        # (`runtime_obj["sampling_rate"]`), not from the manifest or
        # request, so it cannot be resolved any earlier than this.
        melody_source_asset = None
        min_reference_duration_seconds: float | None = None
        max_reference_duration_seconds: float | None = None
        if reuse_action == "melody":
            if "melody-conditioning" not in manifest.tags:
                raise ValueError(
                    f"Model {manifest.public_model_id!r} does not support melody conditioning."
                )
            source_asset_id = lineage_metadata.get("source_asset_id")
            if not isinstance(source_asset_id, str) or not source_asset_id:
                raise ValueError("Melody generation requires a Gallery reference asset ID.")
            if self.asset_repository is None:
                raise RuntimeError("Melody generation requires an asset registry.")
            melody_source_asset = self.asset_repository.get(source_asset_id)
            if melody_source_asset is None:
                raise ValueError("Melody reference asset is not present in the Gallery registry.")
            if melody_source_asset.media_type != "audio":
                raise ValueError("Melody reference must be an audio Gallery asset.")
            try:
                min_reference_duration_seconds = float(min_reference_duration_value)
                max_reference_duration_seconds = float(max_reference_duration_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Melody model does not define a valid reference duration limit."
                ) from exc

        # PR4b Music lane: everything from processor/runtime access through
        # `model.generate()` -- including melody's `prepare_wav_reference()`
        # and processor call, since both need the loaded runtime -- is the
        # runtime-use interval, protected by one `acquire_runtime()` lease.
        # `cancellation_probe_error`/`late_cancellation_probe_error` isolate
        # `cancel_requested()` itself raising (JobRepository I/O, see
        # `_safe_cancel_requested()`'s own docstring above) from a genuine
        # runtime fault, exactly like every other generator's pre-/post-use probe: raised
        # only after the lease has already exited cleanly. Once
        # `model.generate()` is reached, every other failure (a malformed
        # reference file, a processor error, the generation call itself) is
        # left to propagate and conservatively invalidates, matching this
        # generator's previous behavior (which had no lease -- and therefore
        # no invalidation -- at all) as closely as introducing one allows.
        cancelled_before_generation = False
        cancellation_probe_error: Exception | None = None
        late_cancellation = False
        late_cancellation_probe_error: Exception | None = None
        conditioning_metadata: dict[str, Any] = {}

        with self._acquire_runtime(requested_model_id, cancel_requested) as handle:
            runtime_obj = handle.runtime
            model = runtime_obj["model"]
            processor = runtime_obj["processor"]
            device = runtime_obj["device"]
            sampling_rate = int(runtime_obj["sampling_rate"])
            frame_rate = int(runtime_obj["frame_rate"])

            cancelled_before_generation, cancellation_probe_error = _safe_cancel_requested(
                cancel_requested
            )
            if not cancelled_before_generation and cancellation_probe_error is None:
                if reuse_action == "melody":
                    # Narrowed to non-`None` by the preflight block above,
                    # which always runs first and raises otherwise -- these
                    # asserts are for the type checker, not a runtime check.
                    assert melody_source_asset is not None
                    assert min_reference_duration_seconds is not None
                    assert max_reference_duration_seconds is not None
                    reference_audio, reference_info = prepare_wav_reference(
                        melody_source_asset.path,
                        target_sampling_rate=sampling_rate,
                        min_duration_seconds=min_reference_duration_seconds,
                        max_duration_seconds=max_reference_duration_seconds,
                        torch=torch,
                    )
                    processor_inputs = processor(
                        text=[conditioning_prompt],
                        audio=[reference_audio.numpy()],
                        sampling_rate=sampling_rate,
                        padding=True,
                        return_tensors="pt",
                    )
                    if "input_features" not in processor_inputs:
                        raise RuntimeError(
                            "MusicGen Melody processor did not produce input_features."
                        )
                    conditioning_metadata = {
                        "conditioning": {
                            "type": "melody",
                            "reference_asset_id": melody_source_asset.id,
                            "original_channels": reference_info.channels,
                            "original_sampling_rate": reference_info.sampling_rate,
                            "original_duration_seconds": reference_info.duration_seconds,
                            "prepared_channels": 1,
                            "prepared_sampling_rate": sampling_rate,
                            "prepared_sample_count": int(reference_audio.shape[-1]),
                            "min_reference_duration_seconds": min_reference_duration_seconds,
                            "max_reference_duration_seconds": max_reference_duration_seconds,
                        }
                    }
                else:
                    processor_inputs = processor(
                        text=[conditioning_prompt],
                        padding=True,
                        return_tensors="pt",
                    )
                model_inputs = {
                    key: value.to(device)
                    for key, value in processor_inputs.items()
                    if hasattr(value, "to")
                }

                max_new_tokens = max(32, int(duration_seconds * frame_rate))

                generation_kwargs = {
                    **model_inputs,
                    "do_sample": True,
                    "guidance_scale": guidance_scale,
                    "max_new_tokens": max_new_tokens,
                    "temperature": temperature,
                }
                if top_k > 0:
                    generation_kwargs["top_k"] = top_k
                if 0.0 < top_p < 1.0:
                    generation_kwargs["top_p"] = top_p
                generation_kwargs.update(effective_params)

                with self._seeded_generation(request.seed, device, torch):
                    with torch.inference_mode():
                        audio_values = model.generate(**generation_kwargs)

                # Detach to a plain CPU tensor while the lease is still
                # held -- the accelerator/runtime state it protects is no
                # longer needed once this line completes, so everything
                # after it is safe to run post-lease.
                audio_tensor = audio_values[0].detach().cpu()

                late_cancellation, late_cancellation_probe_error = _safe_cancel_requested(
                    cancel_requested
                )

            model_class_name = type(model).__name__
            processor_class_name = type(processor).__name__
            runtime_type_name = type(runtime_obj).__name__
            load_dtype = runtime_obj.get("load_dtype")
            torch_dtype = runtime_obj["torch_dtype"]

        # No inference took place, or it completed successfully: exit
        # cleanly instead of marking the cache INVALID.
        if cancellation_probe_error is not None:
            raise cancellation_probe_error
        if late_cancellation_probe_error is not None:
            raise late_cancellation_probe_error
        if cancelled_before_generation or late_cancellation:
            raise GenerationCancelled()

        self._require_finite_audio(
            audio_tensor, model_label=manifest.public_model_id, torch=torch
        )
        processed_tensor, postprocess_applied = self._postprocess_music(
            audio_tensor,
            sampling_rate,
            enabled=postprocess_enabled,
            torch=torch,
        )

        output_id = f"aud_{uuid4().hex}"
        output_path = self.output_dir / f"{output_id}.wav"
        self._write_wave_file(
            output_path, processed_tensor, sampling_rate=sampling_rate, torch=torch
        )

        output_duration = float(processed_tensor.shape[-1] / sampling_rate)
        quality_report = evaluate_audio_output(output_path)
        semantic_report = evaluate_audio_semantics(output_path, conditioning_prompt)
        enrich_quality_report(quality_report, semantic_report)

        return GenerationResult(
            job_id=output_id,
            status="succeeded",
            outputs=[str(output_path)],
            previews=[],
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": request.prompt,
                "conditioning_prompt": conditioning_prompt,
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": runtime_type_name,
                "model_class": model_class_name,
                "processor_class": processor_class_name,
                "device": device,
                "load_dtype": load_dtype,
                "torch_dtype": torch_dtype,
                "seed": request.seed,
                "output_format": "wav",
                "sampling_rate": sampling_rate,
                "channels": int(audio_tensor.shape[0] if audio_tensor.ndim > 1 else 1),
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                "audio_postprocess": postprocess_applied,
                **lineage_metadata,
                **conditioning_metadata,
                "params": {
                    "duration_seconds": duration_seconds,
                    "max_new_tokens": max_new_tokens,
                    "guidance_scale": guidance_scale,
                    "temperature": temperature,
                    "top_k": top_k,
                    "top_p": top_p,
                    "mood": mood,
                    "bpm": bpm,
                    "genre": genre,
                    "instruments": instruments,
                    "structure": structure,
                    "postprocess": postprocess_enabled,
                    **effective_params,
                },
                "duration_seconds_generated": output_duration,
            },
            error_message=None,
        )

    def _generate_long_form(
        self,
        request: GenerationRequest,
        *,
        manifest: Any,
        requested_model_id: str | None,
        torch: Any,
        progress_callback: ProgressCallback | None,
        cancel_requested: CancelRequested | None,
    ) -> GenerationResult:
        """Generate 31-120 seconds through AudioCraft's extended MusicGen path."""

        effective_params = {**manifest.default_params, **request.params}
        lineage_metadata = _extract_lineage_metadata(effective_params)
        for lineage_key in lineage_metadata:
            effective_params.pop(lineage_key, None)

        duration_seconds = int(effective_params.pop("duration_seconds"))
        extend_stride_seconds = float(
            effective_params.pop(
                "extend_stride_seconds",
                _DEFAULT_LONG_STRIDE_SECONDS,
            )
        )
        guidance_scale = float(effective_params.pop("guidance_scale", 3.0))
        temperature = float(effective_params.pop("temperature", 1.0))
        top_k = int(effective_params.pop("top_k", 250))
        top_p = float(effective_params.pop("top_p", 0.0))
        bpm_value = effective_params.pop("bpm", None)
        bpm = int(bpm_value) if bpm_value is not None else None
        mood = str(effective_params.pop("mood", "")).strip().lower() or None
        genre = str(effective_params.pop("genre", "")).strip().lower() or None
        instruments = str(effective_params.pop("instruments", "")).strip() or None
        structure = str(effective_params.pop("structure", "")).strip().lower() or None
        postprocess_enabled = _coerce_postprocess_flag(
            effective_params.pop("postprocess", True)
        )
        conditioning_prompt = self._build_conditioning_prompt(
            request.prompt,
            mood=mood,
            genre=genre,
            instruments=instruments,
            structure=structure,
            bpm=bpm,
        )

        # PR4b Music lane / FP-001 + FP-002: `set_generation_params()`,
        # `set_custom_progress_callback()`, `model.generate()`, and the
        # callback teardown all sit inside this one `acquire_runtime()`
        # lease -- the callback teardown (`finally` below) runs strictly
        # before the lease releases. `cancellation_probe_error` covers the
        # pre-use probe (before `set_generation_params()`, the first
        # mutation); `callback_probe_error` covers `cancel_requested()`
        # itself raising from *inside* `report_token_progress()`, which is
        # recorded (first failure wins) and never raised through the running
        # `model.generate()`; `late_cancellation_probe_error` covers the
        # post-generation recheck. All three are raised only after the lease
        # has already exited cleanly, and none of them interrupts inference.
        #
        # A genuine `LongFormGenerationCancelled` -- `cancel_requested()`
        # returning `True` mid-generation -- is deliberately different: it is
        # not caught anywhere in this method, so it interrupts generation and
        # keeps its conservative-invalidation behavior, because a cancellation
        # request is not proof inference stopped cleanly. That asymmetry is
        # the point: only an interruption we cannot avoid invalidates, and a
        # broken bookkeeping read is never allowed to become one.
        cancelled_before_generation = False
        cancellation_probe_error: Exception | None = None
        callback_probe_error: Exception | None = None
        progress_publication_error: Exception | None = None
        late_cancellation = False
        late_cancellation_probe_error: Exception | None = None
        segment_count = 0
        completed_segments = 0

        with self._acquire_runtime(requested_model_id, cancel_requested) as handle:
            runtime_obj = handle.runtime
            model = runtime_obj["model"]
            device = str(runtime_obj["device"])
            frame_rate = int(runtime_obj["frame_rate"])
            max_duration = float(runtime_obj.get("max_duration", model.max_duration))

            cancelled_before_generation, cancellation_probe_error = _safe_cancel_requested(
                cancel_requested
            )
            if not cancelled_before_generation and cancellation_probe_error is None:
                # Runtime mutation begins here.
                model.set_generation_params(
                    use_sampling=True,
                    top_k=top_k,
                    top_p=top_p,
                    temperature=temperature,
                    duration=float(duration_seconds),
                    cfg_coef=guidance_scale,
                    extend_stride=extend_stride_seconds,
                    **effective_params,
                )
                segment_count = 1 + math.ceil(
                    max(0.0, duration_seconds - max_duration) / extend_stride_seconds
                )
                segment_boundaries = [
                    min(
                        duration_seconds,
                        max_duration + index * extend_stride_seconds,
                    )
                    for index in range(segment_count)
                ]

                def report_token_progress(
                    generated_tokens: int, _tokens_to_generate: int
                ) -> None:
                    nonlocal completed_segments, progress_publication_error
                    nonlocal callback_probe_error
                    generated_seconds = generated_tokens / max(1, frame_rate)
                    while (
                        completed_segments < segment_count
                        and generated_seconds + 1 / max(1, frame_rate)
                        >= segment_boundaries[completed_segments]
                    ):
                        completed_segments += 1
                        if progress_callback is not None:
                            # External bookkeeping (writes through
                            # JobRepository/event publication in
                            # production) -- captured, not raised, so a
                            # transient failure here never aborts a
                            # generation that is otherwise proceeding
                            # correctly. Only the first failure is kept;
                            # deferred and re-raised once the lease has
                            # already released cleanly.
                            try:
                                progress_callback(
                                    completed_segments / segment_count,
                                    completed_segments,
                                    segment_count,
                                )
                            except Exception as exc:
                                if progress_publication_error is None:
                                    progress_publication_error = exc
                        cancelled, probe_error = _safe_cancel_requested(cancel_requested)
                        if probe_error is not None:
                            # External bookkeeping, exactly like the
                            # `progress_callback()` failure above: the
                            # JobRepository read behind `cancel_requested()`
                            # broke, which says nothing about the model. Do
                            # NOT raise through the running
                            # `model.generate()`. Aborting an in-flight
                            # AudioCraft generation is indistinguishable, from
                            # the model's point of view, from the genuine
                            # mid-execution cancellation below -- and that one
                            # deliberately invalidates the runtime, because a
                            # half-unwound generation is not proof the model
                            # is still clean. Interrupting here and then
                            # declaring the runtime healthy would be the one
                            # combination this generator must not produce.
                            # Record the first failure, let generation run to
                            # completion, and re-raise verbatim after the
                            # lease has exited cleanly.
                            if callback_probe_error is None:
                                callback_probe_error = probe_error
                        elif cancelled:
                            raise LongFormGenerationCancelled(
                                "Long-form generation cancelled at segment boundary "
                                f"{completed_segments}/{segment_count}."
                            )

                model.set_custom_progress_callback(report_token_progress)
                try:
                    with self._seeded_generation(request.seed, device, torch):
                        audio_values = model.generate([conditioning_prompt], progress=True)
                finally:
                    # Callback teardown, still inside the lease -- before it
                    # is released below, whether generation completed, was
                    # interrupted by a genuine mid-execution cancellation, or
                    # failed inside the model itself.
                    model.set_custom_progress_callback(None)

                # Generation ran to completion: a recorded bookkeeping probe
                # failure never interrupts it, so the result is always a real
                # one here. Detach to a plain CPU tensor while the lease is
                # still held, exactly as the short-form path does.
                audio_tensor = audio_values[0].detach().cpu()
                late_cancellation, late_cancellation_probe_error = _safe_cancel_requested(
                    cancel_requested
                )

            sampling_rate = int(runtime_obj["sampling_rate"])
            model_class_name = type(model).__name__
            runtime_type_name = type(runtime_obj).__name__

        # No inference took place, or it ran to completion: exit cleanly
        # instead of marking the cache INVALID. Nothing above ever aborts a
        # running generation for a bookkeeping reason, so reaching this point
        # never means the model was left half-unwound. A
        # `LongFormGenerationCancelled` raised from *inside*
        # `model.generate()` because `cancel_requested()` genuinely returned
        # `True` (a real mid-use interruption) is not caught above and still
        # unwinds as unsafe, as does any failure from the model itself.
        if cancellation_probe_error is not None:
            raise cancellation_probe_error
        if callback_probe_error is not None:
            raise callback_probe_error
        if late_cancellation_probe_error is not None:
            raise late_cancellation_probe_error
        if cancelled_before_generation:
            raise GenerationCancelled()
        if late_cancellation:
            raise LongFormGenerationCancelled(
                "Long-form generation cancelled before WAV publication."
            )
        if progress_publication_error is not None:
            raise progress_publication_error
        if completed_segments < segment_count and progress_callback is not None:
            progress_callback(1.0, segment_count, segment_count)

        self._require_finite_audio(
            audio_tensor, model_label=manifest.public_model_id, torch=torch
        )
        processed_tensor, postprocess_applied = self._postprocess_music(
            audio_tensor,
            sampling_rate,
            enabled=postprocess_enabled,
            torch=torch,
        )
        output_id = f"aud_{uuid4().hex}"
        output_path = self.output_dir / f"{output_id}.wav"
        self._write_wave_file(
            output_path,
            processed_tensor,
            sampling_rate=sampling_rate,
            torch=torch,
        )

        output_duration = float(processed_tensor.shape[-1] / sampling_rate)
        quality_report = evaluate_audio_output(output_path)
        semantic_report = evaluate_audio_semantics(output_path, conditioning_prompt)
        enrich_quality_report(quality_report, semantic_report)

        return GenerationResult(
            job_id=output_id,
            status="succeeded",
            outputs=[str(output_path)],
            previews=[],
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": request.prompt,
                "conditioning_prompt": conditioning_prompt,
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": runtime_type_name,
                "model_class": model_class_name,
                "device": device,
                "seed": request.seed,
                "output_format": "wav",
                "sampling_rate": sampling_rate,
                "channels": int(audio_tensor.shape[0] if audio_tensor.ndim > 1 else 1),
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                "audio_postprocess": postprocess_applied,
                **lineage_metadata,
                "params": {
                    "duration_seconds": duration_seconds,
                    "extend_stride_seconds": extend_stride_seconds,
                    "guidance_scale": guidance_scale,
                    "temperature": temperature,
                    "top_k": top_k,
                    "top_p": top_p,
                    "mood": mood,
                    "bpm": bpm,
                    "genre": genre,
                    "instruments": instruments,
                    "structure": structure,
                    "postprocess": postprocess_enabled,
                    **effective_params,
                },
                "duration_seconds_generated": output_duration,
                "final_duration_seconds": output_duration,
                "segment_count": segment_count,
                "extend_stride_seconds": extend_stride_seconds,
            },
            error_message=None,
        )

    def _acquire_runtime(
        self, model_id: str | None, cancel_requested: CancelRequested | None
    ) -> RuntimeHandle:
        if cancel_requested is None:
            return self.model_service.acquire_runtime(
                model_id, media_type="audio", task_type=self.task_type
            )

        def _wait_checkpoint() -> None:
            if cancel_requested():
                raise GenerationCancelled()

        # One logical, cancellation-aware wait: ModelService polls in bounded
        # slices and calls the checkpoint between them with nothing held.
        # Only synchronization waits are waited out; cache capacity, invalid
        # entries and loader errors still surface. This does not preempt a
        # synchronous loader that already started.
        return self.model_service.acquire_runtime(
            model_id, media_type="audio", task_type=self.task_type,
            wait_checkpoint=_wait_checkpoint,
            poll_interval=_CANCELLATION_POLL_SECONDS,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

    def _build_conditioning_prompt(
        self,
        prompt: str,
        *,
        mood: str | None,
        genre: str | None,
        instruments: str | None,
        structure: str | None,
        bpm: int | None,
    ) -> str:
        parts: list[str] = []
        if genre:
            parts.append(f"{genre} music")
        if mood:
            parts.append(f"{mood} mood")
        if bpm is not None:
            parts.append(f"{bpm} BPM")
        if instruments:
            parts.append(f"featuring {instruments}")
        if structure:
            parts.append(f"{structure} structure")
        parts.append(prompt.strip())
        return ", ".join(part for part in parts if part)

    @contextmanager
    def _seeded_generation(
        self,
        seed: int | None,
        device: str,
        torch: Any,
    ):
        if seed is None:
            yield
            return

        device_name = str(device)
        if device_name.startswith("cuda"):
            cuda_device = torch.device(device_name)
            device_index = (
                cuda_device.index
                if cuda_device.index is not None
                else torch.cuda.current_device()
            )
            with torch.random.fork_rng(devices=[device_index], device_type="cuda"):
                torch.manual_seed(seed)
                torch.cuda.manual_seed(seed)
                yield
            return

        mps_module = getattr(torch, "mps", None)
        mps_get_state = getattr(mps_module, "get_rng_state", None)
        mps_set_state = getattr(mps_module, "set_rng_state", None)
        mps_state = None
        if device_name == "mps" and callable(mps_get_state):
            mps_state = mps_get_state()

        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(seed)
            if device_name == "mps":
                mps_manual_seed = getattr(mps_module, "manual_seed", None)
                if callable(mps_manual_seed):
                    mps_manual_seed(seed)
            try:
                yield
            finally:
                if mps_state is not None and callable(mps_set_state):
                    mps_set_state(mps_state)

    def _require_finite_audio(
        self,
        audio_tensor: Any,
        *,
        model_label: str,
        torch: Any,
    ) -> None:
        """Reject NaN/Inf model output before it reaches numpy or a WAV file.

        A non-finite sample contaminates the whole channel through the
        gain-based postprocessing steps (NaN propagates through any
        multiplication), writes corrupt PCM, and lands NaN/Infinity in the
        JSON job metadata even though this module's report contract is meant
        to stay JSON-safe. Speech already rejects this per chunk right after
        synthesis (generators/audio/speech.py); music needs the same check
        right after generation, before any further processing.
        """

        if not bool(torch.isfinite(audio_tensor).all()):
            raise RuntimeError(
                f"Model {model_label!r} returned non-finite (NaN/Inf) audio samples."
            )

    def _postprocess_music(
        self,
        audio_tensor: Any,
        sampling_rate: int,
        *,
        enabled: bool,
        torch: Any,
    ) -> tuple[Any, dict[str, Any]]:
        """Apply the shared music post-processing chain to a generated clip.

        ``audio_tensor`` is (channels, samples). ``process_music_channels()``
        links normalization gain across channels so a stereo checkpoint (e.g.
        a musicgen-stereo-* variant) keeps its channel-level balance instead
        of every channel being pulled independently to the same target
        level. Channels are cast to float32 before ``.numpy()``: a manifest
        may run the model in bfloat16 or float16 on an accelerator, and
        NumPy has no bfloat16 type.
        """

        if enabled:
            processed_array, report = process_music_channels(
                audio_tensor.to(torch.float32).contiguous().numpy(),
                sampling_rate,
            )
            return torch.from_numpy(processed_array), report
        report = skipped_processing_report(
            sampling_rate,
            preset=MUSIC_PRESET,
            sample_count=int(audio_tensor.shape[-1]),
        )
        return audio_tensor.clamp(-1.0, 1.0), report

    def _write_wave_file(
        self,
        output_path: Path,
        audio_tensor,
        *,
        sampling_rate: int,
        torch: Any,
    ) -> None:
        if audio_tensor.ndim == 1:
            audio_tensor = audio_tensor.unsqueeze(0)
        elif audio_tensor.ndim == 3:
            audio_tensor = audio_tensor[0]

        normalized = audio_tensor.clamp(-1.0, 1.0)
        pcm = (
            normalized.transpose(0, 1)
            .mul(_MAX_INT16)
            .to(torch.int16)
            .contiguous()
            .numpy()
            .tobytes()
        )

        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(int(audio_tensor.shape[0]))
            wav_file.setsampwidth(2)
            wav_file.setframerate(sampling_rate)
            wav_file.writeframes(pcm)


def _coerce_postprocess_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"Audio parameter 'postprocess' must be a boolean, got {value!r}.")


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


__all__ = ["AudioGenerator", "LongFormGenerationCancelled"]
