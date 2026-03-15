"""Optional semantic judges for prompt-to-asset alignment."""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import wave

from PIL import Image

from .evaluators import decode_pcm_samples


class SemanticJudge:
    """Lazy semantic judge backed by optional local transformer models."""

    def __init__(
        self,
        *,
        enabled: bool,
        local_files_only: bool,
        image_model_id: str,
        audio_model_id: str,
    ) -> None:
        self.enabled = enabled
        self.local_files_only = local_files_only
        self.image_model_id = image_model_id
        self.audio_model_id = audio_model_id
        self._image_runtime: tuple[object, object] | None = None
        self._audio_runtime: tuple[object, object] | None = None
        self._image_error: str | None = None
        self._audio_error: str | None = None

    def evaluate_image(
        self,
        output_path: str | Path,
        prompt: str,
        negative_prompt: str | None = None,
    ) -> dict[str, object]:
        if not self.enabled:
            return {
                "status": "disabled",
                "mode": "off",
                "reason": "QUALITY_ENABLE_SEMANTIC_JUDGE is false",
            }

        runtime = self._load_image_runtime()
        if runtime is None:
            return {
                "status": "unavailable",
                "mode": "local_transformers",
                "model_id": self.image_model_id,
                "reason": self._image_error or "image semantic runtime unavailable",
            }

        processor, model = runtime
        import torch

        with Image.open(output_path) as image:
            rgb_image = image.convert("RGB")
            texts = [prompt.strip() or "untitled image prompt"]
            if negative_prompt and negative_prompt.strip():
                texts.append(negative_prompt.strip())
            inputs = processor(
                text=texts,
                images=rgb_image,
                return_tensors="pt",
                padding=True,
            )

        with torch.inference_mode():
            outputs = model(**inputs)

        image_embeds = outputs.image_embeds / outputs.image_embeds.norm(dim=-1, keepdim=True)
        text_embeds = outputs.text_embeds / outputs.text_embeds.norm(dim=-1, keepdim=True)

        positive_cosine = float((image_embeds[0] * text_embeds[0]).sum().detach().cpu())
        negative_cosine = (
            float((image_embeds[0] * text_embeds[1]).sum().detach().cpu())
            if text_embeds.shape[0] > 1
            else None
        )
        semantic_score = self._compose_semantic_score(positive_cosine, negative_cosine)

        return {
            "status": "scored",
            "mode": "local_transformers",
            "model_id": self.image_model_id,
            "semantic_alignment_score": semantic_score,
            "semantic_alignment_level": _score_level(semantic_score),
            "details": {
                "positive_cosine": round(positive_cosine, 4),
                "negative_cosine": round(negative_cosine, 4)
                if negative_cosine is not None
                else None,
            },
        }

    def evaluate_audio(
        self,
        output_path: str | Path,
        prompt: str,
    ) -> dict[str, object]:
        if not self.enabled:
            return {
                "status": "disabled",
                "mode": "off",
                "reason": "QUALITY_ENABLE_SEMANTIC_JUDGE is false",
            }

        runtime = self._load_audio_runtime()
        if runtime is None:
            return {
                "status": "unavailable",
                "mode": "local_transformers",
                "model_id": self.audio_model_id,
                "reason": self._audio_error or "audio semantic runtime unavailable",
            }

        processor, model = runtime
        import torch

        samples, sample_rate = _read_wav_samples(output_path)
        inputs = processor(
            text=[prompt.strip() or "untitled audio prompt"],
            audios=[samples],
            sampling_rate=sample_rate,
            return_tensors="pt",
            padding=True,
        )

        with torch.inference_mode():
            outputs = model(**inputs)

        audio_embeds = outputs.audio_embeds / outputs.audio_embeds.norm(dim=-1, keepdim=True)
        text_embeds = outputs.text_embeds / outputs.text_embeds.norm(dim=-1, keepdim=True)
        positive_cosine = float((audio_embeds[0] * text_embeds[0]).sum().detach().cpu())
        semantic_score = self._compose_semantic_score(positive_cosine, None)

        return {
            "status": "scored",
            "mode": "local_transformers",
            "model_id": self.audio_model_id,
            "semantic_alignment_score": semantic_score,
            "semantic_alignment_level": _score_level(semantic_score),
            "details": {
                "positive_cosine": round(positive_cosine, 4),
                "sample_rate": sample_rate,
            },
        }

    def _load_image_runtime(self) -> tuple[object, object] | None:
        if self._image_runtime is not None:
            return self._image_runtime
        if self._image_error is not None:
            return None

        try:
            from transformers import AutoProcessor, CLIPModel

            processor = AutoProcessor.from_pretrained(
                self.image_model_id,
                local_files_only=self.local_files_only,
            )
            model = CLIPModel.from_pretrained(
                self.image_model_id,
                local_files_only=self.local_files_only,
            )
            self._image_runtime = (processor, model)
            return self._image_runtime
        except Exception as exc:  # pragma: no cover - depends on optional local assets
            self._image_error = str(exc)
            return None

    def _load_audio_runtime(self) -> tuple[object, object] | None:
        if self._audio_runtime is not None:
            return self._audio_runtime
        if self._audio_error is not None:
            return None

        try:
            from transformers import ClapModel, ClapProcessor

            processor = ClapProcessor.from_pretrained(
                self.audio_model_id,
                local_files_only=self.local_files_only,
            )
            model = ClapModel.from_pretrained(
                self.audio_model_id,
                local_files_only=self.local_files_only,
            )
            self._audio_runtime = (processor, model)
            return self._audio_runtime
        except Exception as exc:  # pragma: no cover - depends on optional local assets
            self._audio_error = str(exc)
            return None

    def _compose_semantic_score(
        self,
        positive_cosine: float,
        negative_cosine: float | None,
    ) -> float:
        positive_score = ((positive_cosine + 1.0) / 2.0) * 100.0
        if negative_cosine is None:
            return round(max(0.0, min(100.0, positive_score)), 1)

        negative_score = ((negative_cosine + 1.0) / 2.0) * 100.0
        penalty = max(0.0, negative_score - 45.0) * 0.35
        return round(max(0.0, min(100.0, positive_score - penalty)), 1)


def evaluate_image_semantics(
    output_path: str | Path,
    prompt: str,
    negative_prompt: str | None = None,
) -> dict[str, object]:
    return get_semantic_judge().evaluate_image(output_path, prompt, negative_prompt)


def evaluate_audio_semantics(
    output_path: str | Path,
    prompt: str,
) -> dict[str, object]:
    return get_semantic_judge().evaluate_audio(output_path, prompt)


def enrich_quality_report(
    quality_report: dict[str, object],
    semantic_report: dict[str, object],
) -> dict[str, object]:
    quality_report["semantic_report"] = semantic_report
    if semantic_report.get("status") != "scored":
        return quality_report

    semantic_score = semantic_report.get("semantic_alignment_score")
    if not isinstance(semantic_score, (int, float)):
        return quality_report

    quality_score = quality_report.get("quality_score", 0.0)
    base_quality_score = float(quality_score) if isinstance(quality_score, (int, float)) else 0.0
    creative_alignment_score = round(base_quality_score * 0.65 + float(semantic_score) * 0.35, 1)

    quality_report["semantic_alignment_score"] = round(float(semantic_score), 1)
    quality_report["semantic_alignment_level"] = semantic_report.get(
        "semantic_alignment_level",
        _score_level(float(semantic_score)),
    )
    quality_report["creative_alignment_score"] = creative_alignment_score
    quality_report["creative_alignment_level"] = _score_level(creative_alignment_score)

    checks = quality_report.get("checks")
    if isinstance(checks, list):
        if float(semantic_score) >= 80:
            checks.append("semantic alignment is strong against the prompt")
        elif float(semantic_score) < 55:
            checks.append("semantic alignment is weak against the prompt")

    notes = quality_report.get("notes")
    if isinstance(notes, list):
        notes.append("semantic alignment is optional and depends on configured local judge models")

    return quality_report


@lru_cache(maxsize=1)
def get_semantic_judge() -> SemanticJudge:
    return SemanticJudge(
        enabled=_env_flag("QUALITY_ENABLE_SEMANTIC_JUDGE", default=False),
        local_files_only=_env_flag("QUALITY_SEMANTIC_LOCAL_ONLY", default=True),
        image_model_id=os.getenv(
            "QUALITY_SEMANTIC_IMAGE_MODEL",
            "openai/clip-vit-base-patch32",
        ),
        audio_model_id=os.getenv(
            "QUALITY_SEMANTIC_AUDIO_MODEL",
            "laion/clap-htsat-unfused",
        ),
    )


def _read_wav_samples(output_path: str | Path) -> tuple[list[float], int]:
    with wave.open(str(output_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw_frames = wav_file.readframes(frame_count)

    decoded = decode_pcm_samples(raw_frames, sample_width)
    if channels <= 1:
        return decoded, sample_rate

    mono_samples: list[float] = []
    for index in range(0, len(decoded), channels):
        frame = decoded[index:index + channels]
        mono_samples.append(sum(frame) / len(frame))
    return mono_samples, sample_rate


def _env_flag(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _score_level(score: float) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "strong"
    if score >= 55:
        return "usable"
    if score >= 40:
        return "weak"
    return "poor"
