"""Audio generator backed by a local transformers MusicGen runtime."""

from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
from typing import Any
from uuid import uuid4
import wave

from core.assets import AssetRepository
from core.audio_conditioning import prepare_wav_reference
from core.models import ModelService
from core.quality import (
    enrich_quality_report,
    evaluate_audio_output,
    evaluate_audio_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

_MAX_INT16 = 32_767
_AUDIO_PARAM_RANGES = {
    "duration_seconds": (2.0, 30.0),
    "guidance_scale": (1.0, 10.0),
    "temperature": (0.1, 2.0),
    "top_k": (0.0, 1000.0),
    "top_p": (0.0, 1.0),
    "bpm": (40.0, 240.0),
}


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
        for name, (minimum, maximum) in _AUDIO_PARAM_RANGES.items():
            value = request.params.get(name)
            if value is None:
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Audio parameter '{name}' must be numeric.") from exc
            if not math.isfinite(numeric_value) or not minimum <= numeric_value <= maximum:
                raise ValueError(
                    f"Audio parameter '{name}' must be between {minimum:g} and {maximum:g}."
                )

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        import torch

        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="audio",
            task_type=self.task_type,
        )
        model = runtime_obj["model"]
        processor = runtime_obj["processor"]
        device = runtime_obj["device"]

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
        conditioning_prompt = self._build_conditioning_prompt(
            request.prompt,
            mood=mood,
            genre=genre,
            instruments=instruments,
            structure=structure,
            bpm=bpm,
        )

        conditioning_metadata: dict[str, Any] = {}
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
            source_asset = self.asset_repository.get(source_asset_id)
            if source_asset is None:
                raise ValueError("Melody reference asset is not present in the Gallery registry.")
            if source_asset.media_type != "audio":
                raise ValueError("Melody reference must be an audio Gallery asset.")
            try:
                min_reference_duration_seconds = float(min_reference_duration_value)
                max_reference_duration_seconds = float(max_reference_duration_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Melody model does not define a valid reference duration limit."
                ) from exc
            sampling_rate = int(runtime_obj["sampling_rate"])
            reference_audio, reference_info = prepare_wav_reference(
                source_asset.path,
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
                    "reference_asset_id": source_asset.id,
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

        frame_rate = int(runtime_obj["frame_rate"])
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

        audio_tensor = audio_values[0].detach().cpu()
        sampling_rate = int(runtime_obj["sampling_rate"])

        output_id = f"aud_{uuid4().hex}"
        output_path = self.output_dir / f"{output_id}.wav"
        self._write_wave_file(output_path, audio_tensor, sampling_rate=sampling_rate, torch=torch)

        output_duration = float(audio_tensor.shape[-1] / sampling_rate)
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
                "runtime_type": type(runtime_obj).__name__,
                "model_class": type(model).__name__,
                "processor_class": type(processor).__name__,
                "device": device,
                "load_dtype": runtime_obj.get("load_dtype"),
                "torch_dtype": runtime_obj["torch_dtype"],
                "seed": request.seed,
                "output_format": "wav",
                "sampling_rate": sampling_rate,
                "channels": int(audio_tensor.shape[0] if audio_tensor.ndim > 1 else 1),
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
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
                    **effective_params,
                },
                "duration_seconds_generated": output_duration,
            },
            error_message=None,
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
        torch: object,
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

    def _write_wave_file(
        self,
        output_path: Path,
        audio_tensor,
        *,
        sampling_rate: int,
        torch: object,
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


__all__ = ["AudioGenerator"]
