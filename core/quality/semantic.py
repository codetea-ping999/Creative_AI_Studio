"""Optional semantic judges for prompt-to-asset alignment."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import wave
from typing import Any

from PIL import Image, ImageSequence

from .evaluators import decode_pcm_samples


@dataclass(slots=True)
class SemanticJudgeConfig:
    """Runtime configuration for the local semantic judge stack."""

    enabled: bool
    local_files_only: bool
    cache_dir: Path
    video_backend: str
    video_sample_frames: int
    image_model_id: str
    audio_model_id: str
    video_model_id: str
    image_model_path: str | None
    audio_model_path: str | None
    video_model_path: str | None
    image_enabled: bool
    audio_enabled: bool
    video_enabled: bool


class ScoreCache:
    """Simple on-disk cache for semantic score reuse."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> dict[str, Any] | None:
        cache_path = self.cache_dir / f"{key}.json"
        if not cache_path.exists():
            return None
        return json.loads(cache_path.read_text(encoding="utf-8"))

    def put(self, key: str, payload: dict[str, Any]) -> None:
        cache_path = self.cache_dir / f"{key}.json"
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )


class _ClipImageBackend:
    def __init__(self, config: SemanticJudgeConfig, cache: ScoreCache) -> None:
        self.config = config
        self.cache = cache
        self._runtime: tuple[object, object] | None = None
        self._error: str | None = None

    def evaluate(
        self,
        output_path: str | Path,
        prompt: str,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled or not self.config.image_enabled:
            return _disabled_report("image")

        resolved_output_path = Path(output_path)
        cache_key = _build_cache_key(
            media_type="image",
            output_path=resolved_output_path,
            prompt=prompt,
            negative_prompt=negative_prompt,
            model_ref=self._model_ref(),
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

        runtime = self._load_runtime()
        if runtime is None:
            return {
                "status": "unavailable",
                "media_type": "image",
                "mode": "local_transformers",
                "backend": "clip",
                "model_id": self._model_ref(),
                "reason": self._error or "image semantic runtime unavailable",
                "cache_hit": False,
            }

        processor, model = runtime
        with Image.open(resolved_output_path) as image:
            result = self._score_image(
                image.convert("RGB"),
                prompt=prompt,
                negative_prompt=negative_prompt,
                processor=processor,
                model=model,
            )
        result["cache_hit"] = False
        self.cache.put(cache_key, result)
        return result

    def score_image_object(
        self,
        image: Image.Image,
        *,
        prompt: str,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled or not self.config.image_enabled:
            return _disabled_report("image")

        runtime = self._load_runtime()
        if runtime is None:
            return {
                "status": "unavailable",
                "media_type": "image",
                "mode": "local_transformers",
                "backend": "clip",
                "model_id": self._model_ref(),
                "reason": self._error or "image semantic runtime unavailable",
                "cache_hit": False,
            }

        processor, model = runtime
        result = self._score_image(
            image.convert("RGB"),
            prompt=prompt,
            negative_prompt=negative_prompt,
            processor=processor,
            model=model,
        )
        result["cache_hit"] = False
        return result

    def _score_image(
        self,
        image: Image.Image,
        *,
        prompt: str,
        negative_prompt: str | None,
        processor: object,
        model: object,
    ) -> dict[str, Any]:
        import torch

        texts = [prompt.strip() or "untitled image prompt"]
        if negative_prompt and negative_prompt.strip():
            texts.append(negative_prompt.strip())
        inputs = processor(
            text=texts,
            images=image,
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
        semantic_score = _compose_semantic_score(positive_cosine, negative_cosine)
        return {
            "status": "scored",
            "media_type": "image",
            "mode": "local_transformers",
            "backend": "clip",
            "model_id": self._model_ref(),
            "semantic_alignment_score": semantic_score,
            "semantic_alignment_level": _score_level(semantic_score),
            "details": {
                "positive_cosine": round(positive_cosine, 4),
                "negative_cosine": round(negative_cosine, 4)
                if negative_cosine is not None
                else None,
            },
        }

    def _load_runtime(self) -> tuple[object, object] | None:
        if self._runtime is not None:
            return self._runtime
        if self._error is not None:
            return None

        try:
            from transformers import AutoProcessor, CLIPModel

            model_ref = self._model_ref()
            processor = AutoProcessor.from_pretrained(
                model_ref,
                local_files_only=self.config.local_files_only,
            )
            model = CLIPModel.from_pretrained(
                model_ref,
                local_files_only=self.config.local_files_only,
            )
            self._runtime = (processor, model)
            return self._runtime
        except Exception as exc:  # pragma: no cover - depends on optional local assets
            self._error = str(exc)
            return None

    def _model_ref(self) -> str:
        return self.config.image_model_path or self.config.image_model_id


class _ClapAudioBackend:
    def __init__(self, config: SemanticJudgeConfig, cache: ScoreCache) -> None:
        self.config = config
        self.cache = cache
        self._runtime: tuple[object, object] | None = None
        self._error: str | None = None

    def evaluate(
        self,
        output_path: str | Path,
        prompt: str,
    ) -> dict[str, Any]:
        if not self.config.enabled or not self.config.audio_enabled:
            return _disabled_report("audio")

        resolved_output_path = Path(output_path)
        cache_key = _build_cache_key(
            media_type="audio",
            output_path=resolved_output_path,
            prompt=prompt,
            negative_prompt=None,
            model_ref=self._model_ref(),
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

        runtime = self._load_runtime()
        if runtime is None:
            return {
                "status": "unavailable",
                "media_type": "audio",
                "mode": "local_transformers",
                "backend": "clap",
                "model_id": self._model_ref(),
                "reason": self._error or "audio semantic runtime unavailable",
                "cache_hit": False,
            }

        processor, model = runtime
        import torch

        samples, source_sample_rate = _read_wav_samples(resolved_output_path)
        feature_extractor = getattr(processor, "feature_extractor", None)
        processor_sample_rate = getattr(feature_extractor, "sampling_rate", None)
        sample_rate = (
            int(processor_sample_rate)
            if isinstance(processor_sample_rate, (int, float))
            else source_sample_rate
        )
        samples = _resample_audio_samples(
            samples,
            source_sample_rate=source_sample_rate,
            target_sample_rate=sample_rate,
        )
        inputs = processor(
            text=[prompt.strip() or "untitled audio prompt"],
            audio=[samples],
            sampling_rate=sample_rate,
            return_tensors="pt",
            padding=True,
        )

        with torch.inference_mode():
            outputs = model(**inputs)

        audio_embeds = outputs.audio_embeds / outputs.audio_embeds.norm(dim=-1, keepdim=True)
        text_embeds = outputs.text_embeds / outputs.text_embeds.norm(dim=-1, keepdim=True)
        positive_cosine = float((audio_embeds[0] * text_embeds[0]).sum().detach().cpu())
        semantic_score = _compose_semantic_score(positive_cosine, None)

        result = {
            "status": "scored",
            "media_type": "audio",
            "mode": "local_transformers",
            "backend": "clap",
            "model_id": self._model_ref(),
            "semantic_alignment_score": semantic_score,
            "semantic_alignment_level": _score_level(semantic_score),
            "details": {
                "positive_cosine": round(positive_cosine, 4),
                "source_sample_rate": source_sample_rate,
                "sample_rate": sample_rate,
            },
            "cache_hit": False,
        }
        self.cache.put(cache_key, result)
        return result

    def _load_runtime(self) -> tuple[object, object] | None:
        if self._runtime is not None:
            return self._runtime
        if self._error is not None:
            return None

        try:
            from transformers import ClapModel, ClapProcessor

            model_ref = self._model_ref()
            processor = ClapProcessor.from_pretrained(
                model_ref,
                local_files_only=self.config.local_files_only,
            )
            model = ClapModel.from_pretrained(
                model_ref,
                local_files_only=self.config.local_files_only,
            )
            self._runtime = (processor, model)
            return self._runtime
        except Exception as exc:  # pragma: no cover - depends on optional local assets
            self._error = str(exc)
            return None

    def _model_ref(self) -> str:
        return self.config.audio_model_path or self.config.audio_model_id


class _VideoFrameBackend:
    """Video semantic judge built on frame sampling plus the image backend."""

    def __init__(
        self,
        config: SemanticJudgeConfig,
        cache: ScoreCache,
        image_backend: _ClipImageBackend,
    ) -> None:
        self.config = config
        self.cache = cache
        self.image_backend = image_backend

    def evaluate(
        self,
        output_path: str | Path,
        prompt: str,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled or not self.config.video_enabled:
            return _disabled_report("video")
        if self.config.video_backend == "disabled":
            return {
                "status": "disabled",
                "media_type": "video",
                "mode": "off",
                "reason": "QUALITY_SEMANTIC_VIDEO_BACKEND is disabled",
            }
        if self.config.video_backend != "image_frames":
            return {
                "status": "unavailable",
                "media_type": "video",
                "mode": self.config.video_backend,
                "backend": "video",
                "model_id": self.config.video_model_path or self.config.video_model_id,
                "reason": f"Unsupported video semantic backend: {self.config.video_backend}",
                "cache_hit": False,
            }

        resolved_output_path = Path(output_path)
        cache_key = _build_cache_key(
            media_type="video",
            output_path=resolved_output_path,
            prompt=prompt,
            negative_prompt=negative_prompt,
            model_ref=self.config.video_model_path or self.config.video_model_id,
            extra={"sample_frames": self.config.video_sample_frames},
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

        try:
            sampled_frames = _sample_video_frames(
                resolved_output_path,
                sample_frames=self.config.video_sample_frames,
            )
        except OSError:
            return {
                "status": "unavailable",
                "media_type": "video",
                "mode": "image_frames",
                "backend": "video_frame_fallback",
                "model_id": self.config.image_model_path or self.config.image_model_id,
                "reason": "Video frames are not readable by the image frame backend",
                "cache_hit": False,
            }
        if not sampled_frames:
            return {
                "status": "unavailable",
                "media_type": "video",
                "mode": "image_frames",
                "backend": "video_frame_fallback",
                "model_id": self.config.image_model_path or self.config.image_model_id,
                "reason": "No frames available for video semantic scoring",
                "cache_hit": False,
            }

        frame_scores: list[float] = []
        frame_cosines: list[float] = []
        for frame in sampled_frames:
            frame_result = self.image_backend.score_image_object(
                frame,
                prompt=prompt,
                negative_prompt=negative_prompt,
            )
            score = frame_result.get("semantic_alignment_score")
            details = frame_result.get("details", {})
            if isinstance(score, (int, float)):
                frame_scores.append(float(score))
            if isinstance(details, dict):
                cosine = details.get("positive_cosine")
                if isinstance(cosine, (int, float)):
                    frame_cosines.append(float(cosine))

        if not frame_scores:
            return {
                "status": "unavailable",
                "media_type": "video",
                "mode": "image_frames",
                "backend": "video_frame_fallback",
                "model_id": self.config.image_model_path or self.config.image_model_id,
                "reason": "Frame semantic judge did not return usable scores",
                "cache_hit": False,
            }

        semantic_score = round(sum(frame_scores) / len(frame_scores), 1)
        result = {
            "status": "scored",
            "media_type": "video",
            "mode": "image_frames",
            "backend": "video_frame_fallback",
            "model_id": self.config.image_model_path or self.config.image_model_id,
            "semantic_alignment_score": semantic_score,
            "semantic_alignment_level": _score_level(semantic_score),
            "details": {
                "sampled_frames": len(frame_scores),
                "average_positive_cosine": round(sum(frame_cosines) / len(frame_cosines), 4)
                if frame_cosines
                else None,
            },
            "cache_hit": False,
        }
        self.cache.put(cache_key, result)
        return result


class SemanticJudge:
    """Lazy semantic judge backed by optional local transformer models."""

    def __init__(self, config: SemanticJudgeConfig) -> None:
        self.config = config
        self.cache = ScoreCache(config.cache_dir)
        self.image_backend = _ClipImageBackend(config, self.cache)
        self.audio_backend = _ClapAudioBackend(config, self.cache)
        self.video_backend = _VideoFrameBackend(config, self.cache, self.image_backend)

    def evaluate_image(
        self,
        output_path: str | Path,
        prompt: str,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        return self.image_backend.evaluate(output_path, prompt, negative_prompt)

    def evaluate_audio(
        self,
        output_path: str | Path,
        prompt: str,
    ) -> dict[str, Any]:
        return self.audio_backend.evaluate(output_path, prompt)

    def evaluate_video(
        self,
        output_path: str | Path,
        prompt: str,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        return self.video_backend.evaluate(output_path, prompt, negative_prompt)


def evaluate_image_semantics(
    output_path: str | Path,
    prompt: str,
    negative_prompt: str | None = None,
) -> dict[str, Any]:
    return get_semantic_judge().evaluate_image(output_path, prompt, negative_prompt)


def evaluate_audio_semantics(
    output_path: str | Path,
    prompt: str,
) -> dict[str, Any]:
    return get_semantic_judge().evaluate_audio(output_path, prompt)


def evaluate_video_semantics(
    output_path: str | Path,
    prompt: str,
    negative_prompt: str | None = None,
) -> dict[str, Any]:
    return get_semantic_judge().evaluate_video(output_path, prompt, negative_prompt)


def enrich_quality_report(
    quality_report: dict[str, Any],
    semantic_report: dict[str, Any],
) -> dict[str, Any]:
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
    return SemanticJudge(_load_config())


def _build_cache_key(
    *,
    media_type: str,
    output_path: Path,
    prompt: str,
    negative_prompt: str | None,
    model_ref: str,
    extra: dict[str, Any] | None = None,
) -> str:
    stat = output_path.stat()
    payload = {
        "media_type": media_type,
        "output_path": str(output_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "model_ref": model_ref,
        "extra": extra or {},
    }
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest


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


def _resample_audio_samples(
    samples: list[float],
    *,
    source_sample_rate: int,
    target_sample_rate: int,
) -> list[float]:
    if source_sample_rate == target_sample_rate:
        return samples
    if source_sample_rate <= 0 or target_sample_rate <= 0:
        raise ValueError("Audio sample rates must be positive.")

    import torch
    from torchaudio.functional import resample

    waveform = torch.tensor(samples, dtype=torch.float32).unsqueeze(0)
    converted = resample(
        waveform,
        orig_freq=source_sample_rate,
        new_freq=target_sample_rate,
    )
    return converted.squeeze(0).tolist()


def _sample_video_frames(output_path: Path, *, sample_frames: int) -> list[Image.Image]:
    with Image.open(output_path) as video:
        frames = [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(video)]

    if not frames:
        return []
    if len(frames) <= sample_frames:
        return frames

    sampled: list[Image.Image] = []
    last_index = len(frames) - 1
    for index in range(sample_frames):
        position = round((index / max(1, sample_frames - 1)) * last_index)
        sampled.append(frames[position])
    return sampled


def _load_config() -> SemanticJudgeConfig:
    return SemanticJudgeConfig(
        enabled=_env_flag("QUALITY_ENABLE_SEMANTIC_JUDGE", default=False),
        local_files_only=_env_flag("QUALITY_SEMANTIC_LOCAL_ONLY", default=True),
        cache_dir=Path(os.getenv("QUALITY_SEMANTIC_CACHE_DIR", "data/semantic-cache")),
        video_backend=os.getenv("QUALITY_SEMANTIC_VIDEO_BACKEND", "image_frames"),
        video_sample_frames=_env_int("QUALITY_SEMANTIC_VIDEO_SAMPLE_FRAMES", default=3, minimum=1),
        image_model_id=os.getenv(
            "QUALITY_SEMANTIC_IMAGE_MODEL",
            "openai/clip-vit-base-patch32",
        ),
        audio_model_id=os.getenv(
            "QUALITY_SEMANTIC_AUDIO_MODEL",
            "laion/clap-htsat-unfused",
        ),
        video_model_id=os.getenv(
            "QUALITY_SEMANTIC_VIDEO_MODEL",
            "openai/clip-vit-base-patch32",
        ),
        image_model_path=_env_optional("QUALITY_SEMANTIC_IMAGE_MODEL_PATH"),
        audio_model_path=_env_optional("QUALITY_SEMANTIC_AUDIO_MODEL_PATH"),
        video_model_path=_env_optional("QUALITY_SEMANTIC_VIDEO_MODEL_PATH"),
        image_enabled=_env_flag("QUALITY_SEMANTIC_ENABLE_IMAGE", default=True),
        audio_enabled=_env_flag("QUALITY_SEMANTIC_ENABLE_AUDIO", default=True),
        video_enabled=_env_flag("QUALITY_SEMANTIC_ENABLE_VIDEO", default=True),
    )


def _compose_semantic_score(
    positive_cosine: float,
    negative_cosine: float | None,
) -> float:
    positive_score = ((positive_cosine + 1.0) / 2.0) * 100.0
    if negative_cosine is None:
        return round(max(0.0, min(100.0, positive_score)), 1)

    negative_score = ((negative_cosine + 1.0) / 2.0) * 100.0
    penalty = max(0.0, negative_score - 45.0) * 0.35
    return round(max(0.0, min(100.0, positive_score - penalty)), 1)


def _disabled_report(media_type: str) -> dict[str, Any]:
    return {
        "status": "disabled",
        "media_type": media_type,
        "mode": "off",
        "reason": "QUALITY_ENABLE_SEMANTIC_JUDGE is false",
    }


def _env_flag(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _env_int(name: str, *, default: int, minimum: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(minimum, int(value))
    except ValueError:
        return default


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


__all__ = [
    "SemanticJudge",
    "SemanticJudgeConfig",
    "enrich_quality_report",
    "evaluate_audio_semantics",
    "evaluate_image_semantics",
    "evaluate_video_semantics",
    "get_semantic_judge",
]
