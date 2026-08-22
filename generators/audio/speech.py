"""Narration generator: text to speech plus the shared speech post-processing."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from uuid import uuid4
import wave

import numpy as np

from core.audio import SPEECH_PRESET, process_audio, skipped_processing_report
from core.models import ModelService
from core.quality import evaluate_audio_output
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

# Reused rather than copied so the lineage keys recorded by the music generator and
# the narration generator cannot drift apart.
from .generator import _coerce_postprocess_flag, _extract_lineage_metadata

_MAX_INT16 = 32_767

# Characters per synthesis call. Chosen to stay inside the context of small local
# TTS models while still holding two or three full sentences.
_DEFAULT_MAX_CHUNK_CHARACTERS = 280
_DEFAULT_CHUNK_GAP_SECONDS = 0.25

# Request limits are intentionally enforced here rather than left to individual
# runtimes. Every backend receives the same bounded workload, including a custom
# loader installed by a local studio deployment.
_MAX_PROMPT_CHARACTERS = 20_000
_MAX_CHUNKS = 128
_MAX_CHUNK_CHARACTERS = 2_000
_MAX_CHUNK_GAP_SECONDS = 2.0
_MIN_SPEED = 0.5
_MAX_SPEED = 2.0
_MIN_PITCH = -0.15
_MAX_PITCH = 0.15
_MAX_OUTPUT_SECONDS = 600.0
_MAX_SAMPLE_RATE = 96_000

# A conservative Japanese narration estimate. It is a resource guard rather than
# a promised duration; actual samples are checked separately as every chunk
# returns.
_ESTIMATED_CHARACTERS_PER_SECOND = 6.0

_SENTENCE_TERMINATORS = "。！？!?."
# Latin punctuation is ambiguous: a period also appears in "3.5" and "e.g.".
_LATIN_TERMINATORS = ".!?"
# Punctuation that belongs to the sentence that just ended, not the next one.
_CLOSING_CHARACTERS = "」』）】〕》”’\")']>"
# Mid-sentence breathing points, used only when a single sentence blows the budget.
_CLAUSE_SEPARATORS = "、，,；;：:…—"


class SpeechGenerator(BaseGenerator):
    """Synthesize narration with the resolved text-to-speech runtime."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/audio",
        *,
        task_type: str = "text-to-speech",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.task_type = task_type

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "audio":
            raise ValueError("SpeechGenerator only supports audio requests.")
        if not request.prompt.strip():
            raise ValueError("Narration text must not be empty.")
        if len(request.prompt) > _MAX_PROMPT_CHARACTERS:
            raise ValueError(
                "Narration text is too long: "
                f"{len(request.prompt)} characters exceeds the "
                f"{_MAX_PROMPT_CHARACTERS} character limit."
            )
        if request.output_format and request.output_format.lower() != "wav":
            raise ValueError(
                "SpeechGenerator currently supports wav output only, got "
                f"{request.output_format!r}."
            )

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # Intentionally omits `context`: BaseGenerator.run() introspects generate()'s
    # signature (see generators/base.py) and calls context-free generators without
    # it, so cancellation is only honored at the job-boundary for this generator.
    def generate(self, request: GenerationRequest) -> GenerationResult:  # type: ignore[override]
        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="audio",
            task_type=self.task_type,
        )
        synthesize = runtime_obj.get("synthesize")
        if not callable(synthesize):
            raise RuntimeError(
                f"Model {manifest.public_model_id!r} exposes no synthesize() runtime; "
                f"loader {manifest.loader!r} must return one for text-to-speech."
            )

        effective_params = {**manifest.default_params, **request.params}
        voice = effective_params.pop("voice", None) or runtime_obj.get("default_voice")
        speed = _coerce_float_control(
            "speed",
            effective_params.pop("speed", runtime_obj.get("default_speed", 1.0)),
        )
        pitch = _coerce_float_control(
            "pitch",
            effective_params.pop("pitch", 0.0),
        )
        max_chunk_characters = _coerce_int_control(
            "max_chunk_characters",
            effective_params.pop(
                "max_chunk_characters",
                _DEFAULT_MAX_CHUNK_CHARACTERS,
            ),
        )
        chunk_gap_seconds = _coerce_float_control(
            "chunk_gap_seconds",
            effective_params.pop(
                "chunk_gap_seconds",
                _DEFAULT_CHUNK_GAP_SECONDS,
            ),
        )
        postprocess_enabled = _coerce_postprocess_flag(
            effective_params.pop("postprocess", True)
        )
        _validate_generation_controls(
            speed=speed,
            pitch=pitch,
            max_chunk_characters=max_chunk_characters,
            chunk_gap_seconds=chunk_gap_seconds,
        )
        # Consumed by the loader, not by synthesis; dropped so they do not look like
        # per-request generation parameters in the job record.
        for loader_key in (
            "language",
            "voices",
            "speaker_id",
            "timeout_seconds",
            "max_output_seconds",
            "max_chunks",
        ):
            effective_params.pop(loader_key, None)

        chunks = split_into_chunks(request.prompt, max_characters=max_chunk_characters)
        if not chunks:
            raise ValueError("Narration text contains no speakable characters.")
        if len(chunks) > _MAX_CHUNKS:
            raise ValueError(
                f"Narration would require {len(chunks)} chunks, exceeding the "
                f"{_MAX_CHUNKS} chunk limit. Increase max_chunk_characters or "
                "split the narration into separate jobs."
            )
        _validate_estimated_duration_budget(
            chunks,
            speed=speed,
            chunk_gap_seconds=chunk_gap_seconds,
        )

        advertised_sample_rate = runtime_obj.get("sample_rate")
        if advertised_sample_rate is not None:
            _validate_estimated_sample_budget(
                chunks,
                _coerce_sample_rate(advertised_sample_rate, manifest.public_model_id),
                speed=speed,
                chunk_gap_seconds=chunk_gap_seconds,
            )

        segments, sample_rate = self._synthesize_chunks(
            synthesize,
            chunks,
            voice=str(voice) if voice is not None else None,
            speed=speed,
            pitch=pitch,
            chunk_gap_seconds=chunk_gap_seconds,
            model_label=manifest.public_model_id,
        )
        estimated_samples = _estimate_joined_samples(
            chunks,
            sample_rate,
            speed=speed,
            chunk_gap_seconds=chunk_gap_seconds,
        )
        joined = _concatenate_with_gaps(
            segments,
            sample_rate,
            chunk_gap_seconds,
            max_output_seconds=_MAX_OUTPUT_SECONDS,
        )
        if postprocess_enabled:
            processed, postprocess_applied = process_audio(
                joined,
                sample_rate,
                preset=SPEECH_PRESET,
            )
        else:
            processed = np.clip(joined, -1.0, 1.0).astype(np.float32)
            postprocess_applied = skipped_processing_report(
                sample_rate,
                preset=SPEECH_PRESET,
                sample_count=int(joined.size),
            )

        output_id = f"spk_{uuid4().hex}"
        output_path = self.output_dir / f"{output_id}.wav"
        _write_wave_file(output_path, processed, sample_rate=sample_rate)

        quality_report = evaluate_audio_output(output_path)

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
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": type(runtime_obj).__name__,
                "runtime_status": runtime_obj.get("runtime_status", "ready"),
                "device": runtime_obj.get("device"),
                # Recorded so a run against an HTTP engine always shows where the
                # narration text was sent.
                "endpoint_base_url": runtime_obj.get("endpoint_base_url"),
                "available_voices": [
                    str(item)
                    for item in runtime_obj.get("voices", [])
                    if str(item).strip()
                ],
                "supports_pitch": bool(runtime_obj.get("supports_pitch", False)),
                "language_code": runtime_obj.get("language_code"),
                "seed": request.seed,
                "output_format": "wav",
                "sample_rate": sample_rate,
                "sampling_rate": sample_rate,
                "channels": 1,
                "voice": voice,
                "speed": speed,
                "pitch": pitch,
                "chunk_count": len(chunks),
                "chunk_characters": [len(chunk) for chunk in chunks],
                "text_characters": len(request.prompt),
                "estimated_samples": estimated_samples,
                "max_output_seconds": _MAX_OUTPUT_SECONDS,
                "audio_postprocess": postprocess_applied,
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                **_extract_lineage_metadata(request.params),
                "params": {
                    "voice": voice,
                    "speed": speed,
                    "pitch": pitch,
                    "max_chunk_characters": max_chunk_characters,
                    "chunk_gap_seconds": chunk_gap_seconds,
                    "postprocess": postprocess_enabled,
                    **effective_params,
                },
                "duration_seconds_generated": float(processed.size / sample_rate),
            },
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

    def _synthesize_chunks(
        self,
        synthesize: Any,
        chunks: list[str],
        *,
        voice: str | None,
        speed: float,
        pitch: float,
        chunk_gap_seconds: float,
        model_label: str,
    ) -> tuple[list[np.ndarray], int]:
        segments: list[np.ndarray] = []
        sample_rate: int | None = None
        received_samples = 0

        for position, chunk in enumerate(chunks, start=1):
            audio, chunk_rate = synthesize(chunk, voice=voice, speed=speed, pitch=pitch)
            rate = _coerce_sample_rate(chunk_rate, model_label, position=position)
            raw_samples = np.asarray(audio)
            max_samples = int(rate * _MAX_OUTPUT_SECONDS)
            if raw_samples.size > max_samples:
                raise RuntimeError(
                    f"Model {model_label!r} returned {raw_samples.size} samples for "
                    f"narration chunk {position}, exceeding the {_MAX_OUTPUT_SECONDS:g} "
                    "second output limit."
                )
            samples = np.asarray(raw_samples, dtype=np.float32)
            if samples.size == 0:
                raise RuntimeError(
                    f"Model {model_label!r} returned no audio for narration chunk "
                    f"{position}: {chunk[:48]!r}"
                )
            if samples.ndim != 1:
                raise RuntimeError(
                    f"Model {model_label!r} returned {samples.ndim}D audio for "
                    f"narration chunk {position}; the speech runtime contract "
                    "requires mono samples."
                )
            if not np.all(np.isfinite(samples)):
                raise RuntimeError(
                    f"Model {model_label!r} returned non-finite audio samples for "
                    f"narration chunk {position}."
                )
            if sample_rate is None:
                sample_rate = rate
                _validate_estimated_sample_budget(
                    chunks,
                    rate,
                    speed=speed,
                    chunk_gap_seconds=chunk_gap_seconds,
                )
            elif rate != sample_rate:
                raise RuntimeError(
                    f"Model {model_label!r} returned {rate} Hz for narration chunk "
                    f"{position} after {sample_rate} Hz; one narration asset needs a "
                    "single sample rate."
                )
            gap_samples = int(round(chunk_gap_seconds * rate)) * max(
                len(chunks) - 1,
                0,
            )
            received_samples += samples.size
            if received_samples + gap_samples > max_samples:
                raise RuntimeError(
                    f"Model {model_label!r} produced too much narration audio: "
                    f"{received_samples + gap_samples} samples including gaps exceeds "
                    f"the {_MAX_OUTPUT_SECONDS:g} second limit at {rate} Hz."
                )
            segments.append(samples)

        if sample_rate is None:
            raise RuntimeError(f"Model {model_label!r} produced no narration audio.")
        return segments, sample_rate


def split_into_sentences(text: str) -> list[str]:
    """Split narration into sentences on Japanese 。！？ and Latin .!? plus newlines."""

    sentences: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        character = text[index]
        buffer.append(character)
        index += 1

        if character == "\n":
            # A line break is a boundary the author declared explicitly.
            _flush(buffer, sentences)
            continue
        if character not in _SENTENCE_TERMINATORS:
            continue

        # Keep runs of terminators ("!?") and the closing quotes or brackets that
        # belong to the sentence just ended.
        while index < length and text[index] in _SENTENCE_TERMINATORS + _CLOSING_CHARACTERS:
            buffer.append(text[index])
            index += 1

        if character in _LATIN_TERMINATORS and not _ends_latin_sentence(text, index):
            # "3.5" and "v1.2" are not two sentences.
            continue
        _flush(buffer, sentences)

    _flush(buffer, sentences)
    return sentences


def split_into_chunks(
    text: str,
    *,
    max_characters: int = _DEFAULT_MAX_CHUNK_CHARACTERS,
) -> list[str]:
    """Group sentences into synthesis chunks no longer than ``max_characters``.

    Splitting happens at sentence boundaries rather than every N characters because
    a TTS model plans prosody over the whole utterance it is given. Cut mid-sentence
    and the first half ends on a rising, unfinished intonation while the second half
    starts as if it were a new statement — the seam is audible even with a perfect
    concatenation, and a cut inside a word produces a clipped syllable outright. The
    character budget then only decides how many whole sentences travel together.
    """

    if max_characters < 1:
        raise ValueError(f"max_characters must be at least 1, got {max_characters!r}.")

    chunks: list[str] = []
    current = ""
    for sentence in split_into_sentences(text):
        for piece in _split_long_sentence(sentence, max_characters):
            if not current:
                current = piece
                continue
            candidate = current + _join_separator(current, piece) + piece
            if len(candidate) <= max_characters:
                current = candidate
                continue
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _flush(buffer: list[str], sentences: list[str]) -> None:
    sentence = "".join(buffer).strip()
    buffer.clear()
    if sentence:
        sentences.append(sentence)


def _ends_latin_sentence(text: str, index: int) -> bool:
    return index >= len(text) or text[index].isspace()


def _join_separator(left: str, right: str) -> str:
    # Latin scripts need the word space back after stripping; Japanese does not use
    # one, and inserting it would change how the engine phrases the line.
    if left[-1:].isascii() and right[:1].isascii():
        return " "
    return ""


def _split_long_sentence(sentence: str, max_characters: int) -> list[str]:
    """Break one over-budget sentence at clauses, words, then hard-cut."""

    if len(sentence) <= max_characters:
        return [sentence]

    pieces: list[str] = []
    current = ""
    for clause in _split_clauses(sentence):
        if len(current) + len(clause) <= max_characters:
            current += clause
            continue
        if current.strip():
            pieces.append(current.strip())
        current = ""
        while len(clause) > max_characters:
            break_at = _preferred_break_position(clause, max_characters)
            piece = clause[:break_at].strip()
            if piece:
                pieces.append(piece)
            clause = clause[break_at:].strip()
        current = clause
    if current.strip():
        pieces.append(current.strip())
    return [piece for piece in pieces if piece]


def _split_clauses(sentence: str) -> list[str]:
    clauses: list[str] = []
    buffer: list[str] = []
    for character in sentence:
        buffer.append(character)
        if character in _CLAUSE_SEPARATORS:
            clauses.append("".join(buffer))
            buffer.clear()
    if buffer:
        clauses.append("".join(buffer))
    return clauses


def _preferred_break_position(text: str, max_characters: int) -> int:
    """Prefer the last whitespace inside the budget, otherwise hard-cut."""

    for index in range(max_characters, 0, -1):
        if text[index - 1].isspace():
            return index - 1 or max_characters
    return max_characters


def _validate_generation_controls(
    *,
    speed: float,
    pitch: float,
    max_chunk_characters: int,
    chunk_gap_seconds: float,
) -> None:
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError(f"Speech speed must be positive and finite, got {speed!r}.")
    if not _MIN_SPEED <= speed <= _MAX_SPEED:
        raise ValueError(
            f"Speech speed must be between {_MIN_SPEED} and {_MAX_SPEED}, "
            f"got {speed!r}."
        )
    if not math.isfinite(pitch):
        raise ValueError(f"Speech pitch must be finite, got {pitch!r}.")
    if not _MIN_PITCH <= pitch <= _MAX_PITCH:
        raise ValueError(
            f"Speech pitch must be between {_MIN_PITCH} and {_MAX_PITCH}, "
            f"got {pitch!r}."
        )
    if max_chunk_characters < 1:
        raise ValueError(
            "max_chunk_characters must be at least 1, got "
            f"{max_chunk_characters!r}."
        )
    if max_chunk_characters > _MAX_CHUNK_CHARACTERS:
        raise ValueError(
            "max_chunk_characters must not exceed "
            f"{_MAX_CHUNK_CHARACTERS}, got "
            f"{max_chunk_characters!r}."
        )
    if not math.isfinite(chunk_gap_seconds) or chunk_gap_seconds < 0:
        raise ValueError(
            "chunk_gap_seconds must be non-negative and finite, got "
            f"{chunk_gap_seconds!r}."
        )
    if chunk_gap_seconds > _MAX_CHUNK_GAP_SECONDS:
        raise ValueError(
            "chunk_gap_seconds must not exceed "
            f"{_MAX_CHUNK_GAP_SECONDS}, got "
            f"{chunk_gap_seconds!r}."
        )


def _coerce_float_control(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}.")
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}.") from exc


def _coerce_int_control(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {value!r}.")
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}.") from exc
    if isinstance(value, float) and value != integer:
        raise ValueError(f"{name} must be an integer, got {value!r}.")
    return integer


def _coerce_sample_rate(
    value: Any,
    model_label: str,
    *,
    position: int | None = None,
) -> int:
    try:
        rate = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(
            f"Model {model_label!r} reported invalid sample rate {value!r}."
        ) from exc
    if rate <= 0:
        location = f" for narration chunk {position}" if position is not None else ""
        raise RuntimeError(
            f"Model {model_label!r} reported sample rate {value!r}{location}."
        )
    if rate > _MAX_SAMPLE_RATE:
        raise RuntimeError(
            f"Model {model_label!r} reported sample rate {rate} Hz, exceeding the "
            f"{_MAX_SAMPLE_RATE} Hz speech limit."
        )
    return rate


def _estimate_joined_samples(
    chunks: list[str],
    sample_rate: int,
    *,
    speed: float,
    chunk_gap_seconds: float,
) -> int:
    speakable_characters = sum(
        1 for character in "".join(chunks) if not character.isspace()
    )
    estimated_speech_seconds = (
        speakable_characters / (_ESTIMATED_CHARACTERS_PER_SECOND * speed)
    )
    estimated_speech_samples = int(math.ceil(estimated_speech_seconds * sample_rate))
    gap_samples = int(round(chunk_gap_seconds * sample_rate)) * max(len(chunks) - 1, 0)
    return estimated_speech_samples + gap_samples


def _validate_estimated_sample_budget(
    chunks: list[str],
    sample_rate: int,
    *,
    speed: float,
    chunk_gap_seconds: float,
) -> None:
    _validate_estimated_duration_budget(
        chunks,
        speed=speed,
        chunk_gap_seconds=chunk_gap_seconds,
    )
    estimated_samples = _estimate_joined_samples(
        chunks,
        sample_rate,
        speed=speed,
        chunk_gap_seconds=chunk_gap_seconds,
    )
    maximum_samples = int(sample_rate * _MAX_OUTPUT_SECONDS)
    if estimated_samples > maximum_samples:
        estimated_seconds = estimated_samples / sample_rate
        raise ValueError(
            "Narration is estimated at "
            f"{estimated_seconds:.1f} seconds including chunk gaps, exceeding the "
            f"{_MAX_OUTPUT_SECONDS:g} second output limit. Split it into separate jobs."
        )


def _validate_estimated_duration_budget(
    chunks: list[str],
    *,
    speed: float,
    chunk_gap_seconds: float,
) -> None:
    speakable_characters = sum(
        1 for character in "".join(chunks) if not character.isspace()
    )
    estimated_seconds = (
        speakable_characters / (_ESTIMATED_CHARACTERS_PER_SECOND * speed)
        + chunk_gap_seconds * max(len(chunks) - 1, 0)
    )
    if estimated_seconds > _MAX_OUTPUT_SECONDS:
        raise ValueError(
            "Narration is estimated at "
            f"{estimated_seconds:.1f} seconds including chunk gaps, exceeding the "
            f"{_MAX_OUTPUT_SECONDS:g} second output limit. Split it into separate jobs."
        )


def _concatenate_with_gaps(
    segments: list[np.ndarray],
    sample_rate: int,
    gap_seconds: float,
    *,
    max_output_seconds: float = _MAX_OUTPUT_SECONDS,
) -> np.ndarray:
    """Join chunk audio with a short silence, which reads as a sentence pause."""

    gap_samples = int(round(gap_seconds * sample_rate))
    total_samples = sum(segment.size for segment in segments) + gap_samples * max(
        len(segments) - 1,
        0,
    )
    maximum_samples = int(sample_rate * max_output_seconds)
    if total_samples > maximum_samples:
        raise RuntimeError(
            f"Narration requires {total_samples} samples including gaps, exceeding "
            f"the {max_output_seconds:g} second output limit at {sample_rate} Hz."
        )
    if len(segments) == 1 or gap_samples <= 0:
        return np.concatenate(segments).astype(np.float32)

    gap = np.zeros(gap_samples, dtype=np.float32)
    joined: list[np.ndarray] = []
    for position, segment in enumerate(segments):
        if position:
            joined.append(gap)
        joined.append(segment)
    return np.concatenate(joined).astype(np.float32)


def _write_wave_file(
    output_path: Path,
    samples: np.ndarray,
    *,
    sample_rate: int,
) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * _MAX_INT16).astype("<i2").tobytes()
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)


__all__ = ["SpeechGenerator", "split_into_chunks", "split_into_sentences"]
