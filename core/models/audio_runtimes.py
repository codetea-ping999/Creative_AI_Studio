"""Text-to-speech runtimes normalized behind one calling convention.

Every TTS backend exposes the same ``synthesize`` callable so ``SpeechGenerator``
never branches on which engine is loaded:

    synthesize(text, *, voice=None, speed=1.0, pitch=0.0) -> tuple[ndarray, int]

The array is mono ``float32`` in [-1, 1] and the int is its sample rate. Beside it
each runtime publishes ``voices`` (list[str]), ``default_voice``, and ``device`` so
the API can describe a model without loading a second code path per backend.

Two backends are provided: the pip-installed local ``kokoro`` package (no server,
JA and EN), and a local VOICEVOX-style HTTP engine behind the same loopback egress
guard the text endpoints use.
"""

from __future__ import annotations

import io
import math
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
import wave

import numpy as np

# The definition of "this machine" is shared with the text endpoints on purpose:
# there must be exactly one answer to which hosts count as local. The *opt-in* is
# deliberately separate (see resolve_audio_endpoint) because allowing prompts to
# reach a remote LLM is a different decision from allowing narration text to reach
# a remote speech engine.
from .text_runtimes import _LOOPBACK_HOSTS

SynthesizeCallable = Callable[..., tuple[np.ndarray, int]]

# kokoro renders at a fixed 24 kHz.
_KOKORO_SAMPLE_RATE = 24_000

# kokoro selects its G2P front end with a one-letter language code.
_KOKORO_LANGUAGE_ALIASES = {
    "a": "a",
    "b": "b",
    "en": "a",
    "en-gb": "b",
    "en-us": "a",
    "english": "a",
    "j": "j",
    "ja": "j",
    "japanese": "j",
    "jp": "j",
}
_KOKORO_DEFAULT_VOICES = {
    "a": ["af_heart", "af_bella", "am_michael"],
    "b": ["bf_emma", "bm_george"],
    "j": ["jf_alpha", "jf_gongitsune", "jm_kumo"],
}

# VOICEVOX engines render at 24 kHz by default; the authoritative rate is read
# back from the wav the engine returns, this is only what we advertise up front.
_VOICEVOX_SAMPLE_RATE = 24_000
_VOICEVOX_TIMEOUT_SECONDS = 60.0
_MIN_SPEECH_SPEED = 0.5
_MAX_SPEECH_SPEED = 2.0
_MIN_SPEECH_PITCH = -0.15
_MAX_SPEECH_PITCH = 0.15


# --------------------------------------------------------------------------
# Endpoint guard
# --------------------------------------------------------------------------


def resolve_audio_endpoint(base_url: str) -> str:
    """Validate an audio endpoint against the local-only default and return it.

    Raises ``ValueError`` for a non-loopback host unless
    ``ALLOW_REMOTE_AUDIO_ENDPOINTS=true`` is set, so a manifest cannot quietly
    start shipping narration text off the machine.
    """

    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("Audio endpoint base URL must not be empty.")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Audio endpoint must use http or https: {base_url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "Audio endpoint must not include userinfo; configure credentials "
            "outside the endpoint URL."
        )
    if "?" in normalized:
        raise ValueError("Audio endpoint must not include a query string.")
    if "#" in normalized:
        raise ValueError("Audio endpoint must not include a fragment.")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"Audio endpoint must include a host: {base_url!r}")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"Audio endpoint has an invalid port: {base_url!r}") from exc
    if host in _LOOPBACK_HOSTS:
        return normalized

    if os.getenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", "").strip().lower() == "true":
        return normalized

    raise ValueError(
        f"Refusing to use non-loopback audio endpoint {host!r}. "
        "This studio defaults to local-only generation; set "
        "ALLOW_REMOTE_AUDIO_ENDPOINTS=true to allow it explicitly."
    )


def audio_endpoint_origin(base_url: str) -> str:
    """Return a credential-free origin suitable for job metadata."""

    parsed = urlparse(resolve_audio_endpoint(base_url))
    host = (parsed.hostname or "").lower()
    rendered_host = f"[{host}]" if ":" in host else host
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme.lower()}://{rendered_host}{port}"


# --------------------------------------------------------------------------
# kokoro (local pip package)
# --------------------------------------------------------------------------


def build_kokoro_runtime(
    *,
    model_path: Path | None = None,
    language: str = "ja",
    default_voice: str | None = None,
    default_speed: float = 1.0,
    voices: list[str] | None = None,
    device: str = "auto",
) -> dict[str, Any]:
    """Build the runtime fragment for the local kokoro TTS package."""

    try:
        from kokoro import KPipeline
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Local narration requires the kokoro TTS package. "
            "Install it with: pip install kokoro\n"
            'Japanese text also needs the misaki Japanese extra: pip install "misaki[ja]"'
        ) from exc

    default_language_code = resolve_kokoro_language_code(language)
    resolved_default_speed, _ = _validate_synthesis_controls(default_speed, 0.0)
    resolved_device = _resolve_kokoro_device(device)
    model = (
        _load_kokoro_model(model_path, device=resolved_device)
        if model_path is not None
        else None
    )
    available_voices = list(voices or _KOKORO_DEFAULT_VOICES.get(default_language_code, []))
    resolved_default_voice = default_voice or (
        available_voices[0] if available_voices else None
    )
    pipelines: dict[str, Any] = {}

    def pipeline_for(language_code: str) -> Any:
        # One pipeline per language: kokoro's grapheme-to-phoneme front end is
        # language specific, so a JA and an EN voice cannot share an instance.
        pipeline = pipelines.get(language_code)
        if pipeline is None:
            pipeline_kwargs: dict[str, Any] = {
                "lang_code": language_code,
                "device": resolved_device,
            }
            if model is not None:
                pipeline_kwargs["model"] = model
            pipeline = KPipeline(**pipeline_kwargs)
            pipelines[language_code] = pipeline
        return pipeline

    def synthesize(
        text: str,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        pitch: float = 0.0,
    ) -> tuple[np.ndarray, int]:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("kokoro synthesize() was called with empty text.")
        resolved_speed, resolved_pitch = _validate_synthesis_controls(speed, pitch)
        if abs(resolved_pitch) > 1e-6:
            raise ValueError(
                f"kokoro TTS has no pitch control but pitch={pitch} was requested. "
                "Drop the pitch parameter or use a VOICEVOX model, which supports it."
            )

        selected_voice = voice or resolved_default_voice
        if not selected_voice:
            raise ValueError(
                "No kokoro voice selected: set default_params.voice in the manifest "
                "or pass params.voice on the request."
            )

        language_code = _kokoro_language_for_voice(
            str(selected_voice), default_language_code
        )
        voice_argument = (
            str(_resolve_local_kokoro_voice(model_path, str(selected_voice)))
            if model_path is not None
            else str(selected_voice)
        )
        segments: list[np.ndarray] = []
        for result in pipeline_for(language_code)(
            cleaned,
            voice=voice_argument,
            speed=resolved_speed,
        ):
            segment = _extract_kokoro_audio(result)
            if segment is not None and segment.size:
                segments.append(segment)

        if not segments:
            raise RuntimeError(
                f"kokoro returned no audio for voice {selected_voice!r}; "
                "the text may contain no pronounceable characters."
            )
        return np.concatenate(segments).astype(np.float32), _KOKORO_SAMPLE_RATE

    return {
        "synthesize": synthesize,
        "voices": available_voices,
        "default_voice": resolved_default_voice,
        "default_speed": resolved_default_speed,
        "sample_rate": _KOKORO_SAMPLE_RATE,
        "language_code": default_language_code,
        "supports_pitch": False,
        "device": resolved_device,
    }


def resolve_kokoro_language_code(language: str) -> str:
    """Map a manifest language such as ``ja`` onto a kokoro language code."""

    normalized = str(language).strip().lower()
    code = _KOKORO_LANGUAGE_ALIASES.get(normalized)
    if code is None:
        raise ValueError(
            f"Unsupported kokoro language {language!r}; "
            f"expected one of {', '.join(sorted(_KOKORO_LANGUAGE_ALIASES))}."
        )
    return code


def _kokoro_language_for_voice(voice: str, default_language_code: str) -> str:
    """Derive the language from a kokoro voice name such as ``jf_alpha``.

    Voice names are prefixed with their language letter, so honouring the prefix
    lets one manifest serve both Japanese and English narration instead of forcing
    a separate model entry per language.
    """

    prefix = voice[:1].lower()
    if prefix in _KOKORO_DEFAULT_VOICES:
        return prefix
    return default_language_code


def _extract_kokoro_audio(result: Any) -> np.ndarray | None:
    """Pull the audio out of one kokoro pipeline result."""

    audio = getattr(result, "audio", None)
    if audio is None and isinstance(result, (tuple, list)) and result:
        # Older kokoro releases yield ``(graphemes, phonemes, audio)`` tuples.
        audio = result[-1]
    if audio is None:
        return None
    return to_mono_float32(audio)


def _resolve_kokoro_device(requested_device: str) -> str:
    """Resolve ``auto`` using the same accelerator policy as Kokoro itself."""

    normalized = str(requested_device).strip().lower()
    if normalized and normalized != "auto":
        return normalized

    import torch

    if torch.cuda.is_available():
        return "cuda"
    if (
        os.getenv("PYTORCH_ENABLE_MPS_FALLBACK") == "1"
        and torch.backends.mps.is_available()
    ):
        return "mps"
    return "cpu"


def _resolve_local_kokoro_voice(model_path: Path, voice: str) -> Path:
    """Resolve a voice pack without allowing Kokoro to download it on demand."""

    voice_name = voice.strip()
    if not voice_name:
        raise ValueError("Kokoro voice must not be empty.")
    if Path(voice_name).name != voice_name:
        raise ValueError(
            f"Kokoro voice must be a local voice id, not a path: {voice!r}"
        )

    filename = voice_name if voice_name.endswith(".pt") else f"{voice_name}.pt"
    candidates = (model_path / "voices" / filename, model_path / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Kokoro voice pack {filename!r} was not found under "
        f"{model_path / 'voices'} or {model_path}. Download the voice pack with "
        "the model files before enabling this local-only manifest."
    )


def _load_kokoro_model(model_path: Path, *, device: str) -> Any:
    """Build a kokoro model pinned to local files so synthesis stays offline."""

    from kokoro import KModel

    config_file = model_path / "config.json"
    if not config_file.exists():
        raise FileNotFoundError(
            f"kokoro config.json was not found under {model_path}. "
            "Place the kokoro model files there (config.json plus the .pth weights)."
        )

    weight_files = sorted(model_path.glob("*.pth"))
    if not weight_files:
        raise FileNotFoundError(
            f"No .pth weight file found under {model_path} for the kokoro runtime."
        )
    if len(weight_files) > 1:
        raise ValueError(
            f"Multiple .pth weight files found under {model_path}; keep only the one "
            f"kokoro should use: {', '.join(path.name for path in weight_files)}"
        )
    model = KModel(
        repo_id="hexgrad/Kokoro-82M",
        config=str(config_file),
        model=str(weight_files[0]),
    )
    return model.to(device).eval()


# --------------------------------------------------------------------------
# VOICEVOX-style local HTTP engine
# --------------------------------------------------------------------------


def build_voicevox_runtime(
    base_url: str,
    *,
    default_speaker_id: int = 1,
    voices: list[str] | None = None,
    timeout_seconds: float = _VOICEVOX_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build the runtime fragment for a local VOICEVOX-style HTTP engine."""

    try:
        import httpx
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "VOICEVOX narration requires httpx. Install it with: pip install httpx"
        ) from exc

    resolved_base_url = resolve_audio_endpoint(base_url)
    endpoint_origin = audio_endpoint_origin(resolved_base_url)
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            f"VOICEVOX timeout_seconds must be a positive finite number, got "
            f"{timeout_seconds!r}."
        )
    if int(default_speaker_id) < 0:
        raise ValueError(
            f"VOICEVOX default_speaker_id must be non-negative, got "
            f"{default_speaker_id!r}."
        )
    speaker_labels, speaker_lookup, speakers_error = _fetch_voicevox_speakers(
        httpx,
        resolved_base_url,
        timeout,
    )
    if not speaker_labels and voices:
        # The engine is not running yet; fall back to what the manifest declares so
        # the model can still be described in GET /models.
        speaker_labels = [str(voice) for voice in voices]

    def synthesize(
        text: str,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        pitch: float = 0.0,
    ) -> tuple[np.ndarray, int]:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("VOICEVOX synthesize() was called with empty text.")
        resolved_speed, resolved_pitch = _validate_synthesis_controls(speed, pitch)

        speaker_id = _resolve_voicevox_speaker(
            voice,
            speaker_lookup,
            default_speaker_id=int(default_speaker_id),
            labels=speaker_labels,
        )

        query_response = httpx.post(
            f"{resolved_base_url}/audio_query",
            params={"text": cleaned, "speaker": speaker_id},
            timeout=timeout,
        )
        query_response.raise_for_status()
        query = query_response.json()
        query["speedScale"] = resolved_speed
        query["pitchScale"] = resolved_pitch

        synthesis_response = httpx.post(
            f"{resolved_base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query,
            timeout=timeout,
        )
        synthesis_response.raise_for_status()
        return decode_wav_bytes(synthesis_response.content)

    return {
        "synthesize": synthesize,
        "voices": speaker_labels,
        "default_voice": _voicevox_default_label(
            int(default_speaker_id),
            speaker_labels,
        ),
        "default_speaker_id": int(default_speaker_id),
        "sample_rate": _VOICEVOX_SAMPLE_RATE,
        # Only the origin is safe to persist. A deployment may use a path prefix
        # containing tenant or routing information that does not belong in jobs.
        "endpoint_base_url": endpoint_origin,
        "speakers_error": speakers_error,
        "runtime_status": (
            "ready" if speakers_error is None else "configured_unreachable"
        ),
        "supports_pitch": True,
    }


def _fetch_voicevox_speakers(
    httpx: Any,
    base_url: str,
    timeout_seconds: float,
) -> tuple[list[str], dict[str, int], str | None]:
    """Read the engine's speaker table, tolerating an engine that is not up.

    A loader that hard-failed here would make ``GET /models`` depend on the engine
    being running, so the failure is recorded and surfaced instead: synthesis will
    raise the real connection error when it is actually attempted.
    """

    try:
        response = httpx.get(f"{base_url}/speakers", timeout=timeout_seconds)
        response.raise_for_status()
        speakers = response.json()
    except Exception as exc:  # pragma: no cover - depends on a running engine
        return [], {}, f"{type(exc).__name__}: {exc}"

    labels: list[str] = []
    lookup: dict[str, int] = {}
    for speaker in speakers if isinstance(speakers, list) else []:
        if not isinstance(speaker, dict):
            continue
        name = str(speaker.get("name", "")).strip()
        for style in speaker.get("styles", []) or []:
            if not isinstance(style, dict) or "id" not in style:
                continue
            style_id = int(style["id"])
            style_name = str(style.get("name", "")).strip()
            label = f"{style_id} {name} ({style_name})" if style_name else f"{style_id} {name}"
            labels.append(label)
            for key in (label, name, f"{name} ({style_name})", str(style_id)):
                if key:
                    lookup.setdefault(key.lower(), style_id)
    return labels, lookup, None


def _resolve_voicevox_speaker(
    voice: str | None,
    lookup: dict[str, int],
    *,
    default_speaker_id: int,
    labels: list[str],
) -> int:
    if voice is None or not str(voice).strip():
        return default_speaker_id

    requested = str(voice).strip()
    if requested.isdigit():
        return int(requested)

    speaker_id = lookup.get(requested.lower())
    if speaker_id is not None:
        return speaker_id

    known = ", ".join(labels[:8]) if labels else "none reported by the engine"
    raise ValueError(
        f"Unknown VOICEVOX voice {voice!r}. Pass a numeric speaker id or one of: {known}"
    )


def _voicevox_default_label(speaker_id: int, labels: list[str]) -> str:
    prefix = f"{speaker_id} "
    for label in labels:
        if label.startswith(prefix):
            return label
    return str(speaker_id)


# --------------------------------------------------------------------------
# Cloud provider seam (see core/models/cloud_guard.py)
# --------------------------------------------------------------------------


def resolve_cloud_endpoint(base_url: str) -> str:
    """Validate a cloud speech endpoint URL and return it, origin-only in shape.

    Cloud providers are never loopback, so this is a narrower check than
    ``resolve_audio_endpoint``: TLS is required and no userinfo, query, or
    fragment may ride along in the URL, since those are exactly the kind of
    routing or tenant detail that must not end up in job metadata.
    """

    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("Cloud endpoint base URL must not be empty.")

    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise ValueError(f"Cloud endpoint must use https: {base_url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "Cloud endpoint must not include userinfo; the API key travels in a "
            "header, resolved from an environment variable."
        )
    if "?" in normalized:
        raise ValueError("Cloud endpoint must not include a query string.")
    if "#" in normalized:
        raise ValueError("Cloud endpoint must not include a fragment.")
    if not parsed.hostname:
        raise ValueError(f"Cloud endpoint must include a host: {base_url!r}")

    return normalized


def cloud_endpoint_origin(base_url: str) -> str:
    """Return a credential- and path-free origin suitable for job metadata."""

    parsed = urlparse(resolve_cloud_endpoint(base_url))
    return f"{parsed.scheme}://{parsed.hostname}"


def build_cloud_http_speech_runtime(
    base_url: str,
    *,
    api_key_env: str,
    default_voice: str | None = None,
    voices: list[str] | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Build the runtime fragment for one example cloud TTS HTTP provider.

    This is the seam the issue asked for, not a vendor integration: a generic
    JSON-in/wav-out contract (``POST {text, voice, speed, pitch}`` -> wav
    bytes) that a real provider adapter can replace once one is picked. The
    API key is resolved from ``api_key_env`` only -- never from the manifest
    -- and no request is built until ``synthesize`` is actually called, so
    loading this runtime never sends anything by itself.
    """

    try:
        import httpx
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Cloud speech generation requires httpx. Install it with: pip install httpx"
        ) from exc

    resolved_base_url = resolve_cloud_endpoint(base_url)
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            f"Cloud speech timeout_seconds must be a positive finite number, got "
            f"{timeout_seconds!r}."
        )

    api_key_env_name = str(api_key_env).strip()
    if not api_key_env_name:
        raise ValueError("Cloud speech provider manifest must set api_key_env.")
    api_key = os.getenv(api_key_env_name, "").strip()
    if not api_key:
        raise ValueError(
            f"Cloud speech provider requires the {api_key_env_name} environment "
            "variable to hold the API key. It is never read from the manifest."
        )

    def synthesize(
        text: str,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        pitch: float = 0.0,
    ) -> tuple[np.ndarray, int]:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("Cloud speech synthesize() was called with empty text.")
        resolved_speed, resolved_pitch = _validate_synthesis_controls(speed, pitch)

        response = httpx.post(
            resolved_base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "text": cleaned,
                "voice": voice or default_voice,
                "speed": resolved_speed,
                "pitch": resolved_pitch,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return decode_wav_bytes(response.content)

    return {
        "synthesize": synthesize,
        "voices": list(voices) if voices else [],
        "default_voice": default_voice,
        # Only the origin is safe to persist; job metadata must never carry the
        # bearer token or a path that could encode a tenant or routing secret.
        "endpoint_base_url": cloud_endpoint_origin(resolved_base_url),
        "runtime_status": "ready",
        "supports_pitch": True,
    }


# --------------------------------------------------------------------------
# Shared conversion helpers
# --------------------------------------------------------------------------


def _validate_synthesis_controls(speed: float, pitch: float) -> tuple[float, float]:
    """Normalize controls shared by local and HTTP speech runtimes."""

    resolved_speed = float(speed)
    resolved_pitch = float(pitch)
    if not math.isfinite(resolved_speed) or resolved_speed <= 0:
        raise ValueError(
            f"Speech speed must be a positive finite number, got {speed!r}."
        )
    if not _MIN_SPEECH_SPEED <= resolved_speed <= _MAX_SPEECH_SPEED:
        raise ValueError(
            f"Speech speed must be between {_MIN_SPEECH_SPEED} and "
            f"{_MAX_SPEECH_SPEED}, got {speed!r}."
        )
    if not math.isfinite(resolved_pitch):
        raise ValueError(f"Speech pitch must be finite, got {pitch!r}.")
    if not _MIN_SPEECH_PITCH <= resolved_pitch <= _MAX_SPEECH_PITCH:
        raise ValueError(
            f"Speech pitch must be between {_MIN_SPEECH_PITCH} and "
            f"{_MAX_SPEECH_PITCH}, got {pitch!r}."
        )
    return resolved_speed, resolved_pitch


def decode_wav_bytes(payload: bytes) -> tuple[np.ndarray, int]:
    """Decode PCM wav bytes into mono float32 samples and their sample rate.

    Uses the standard library rather than soundfile/librosa so an HTTP speech
    engine needs no audio-decoding dependency of its own.
    """

    with wave.open(io.BytesIO(payload), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width == 1:
        # 8-bit wav is unsigned with 128 as the zero point.
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(
            f"Unsupported PCM sample width in speech engine response: {sample_width} bytes"
        )

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), int(sample_rate)


def to_mono_float32(audio: Any) -> np.ndarray:
    """Coerce a backend's audio object into contiguous mono float32 samples."""

    if hasattr(audio, "detach"):  # torch tensor, without importing torch here
        audio = audio.detach().cpu().numpy()
    array = np.asarray(audio, dtype=np.float32)
    if array.ndim > 1:
        # Average the channels: the runtime contract is mono and averaging keeps
        # the perceived level where summing would clip.
        array = array.mean(axis=int(np.argmin(array.shape)), dtype=np.float32)
    return np.ascontiguousarray(array.reshape(-1), dtype=np.float32)


__all__ = [
    "SynthesizeCallable",
    "audio_endpoint_origin",
    "build_cloud_http_speech_runtime",
    "build_kokoro_runtime",
    "build_voicevox_runtime",
    "cloud_endpoint_origin",
    "decode_wav_bytes",
    "resolve_audio_endpoint",
    "resolve_cloud_endpoint",
    "resolve_kokoro_language_code",
    "to_mono_float32",
]
