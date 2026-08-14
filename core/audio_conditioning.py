"""Validation and preprocessing for Gallery-backed WAV conditioning audio."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import wave


@dataclass(frozen=True, slots=True)
class WavReferenceInfo:
    """Header metadata for a validated PCM WAV reference."""

    path: Path
    channels: int
    sampling_rate: int
    sample_width: int
    frame_count: int
    duration_seconds: float


def inspect_wav_reference(
    path: str | Path,
    *,
    min_duration_seconds: float = 1.0,
    max_duration_seconds: float,
) -> WavReferenceInfo:
    """Validate the reference contract without decoding the full waveform."""

    reference_path = Path(path)
    if reference_path.suffix.lower() != ".wav":
        raise ValueError("Melody reference must be a Gallery WAV asset.")
    if not reference_path.is_file():
        raise ValueError("Melody reference WAV is missing from local storage.")
    if min_duration_seconds <= 0 or max_duration_seconds < min_duration_seconds:
        raise ValueError("Melody model does not define a valid reference duration limit.")

    try:
        with wave.open(str(reference_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sampling_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("Melody reference is not a readable WAV file.") from exc

    if compression != "NONE":
        raise ValueError("Melody reference WAV must use uncompressed PCM audio.")
    if channels not in (1, 2):
        raise ValueError("Melody reference WAV must contain one or two channels.")
    if sampling_rate <= 0:
        raise ValueError("Melody reference WAV has an invalid sampling rate.")
    if sample_width not in (1, 2, 3, 4):
        raise ValueError("Melody reference WAV uses an unsupported PCM sample width.")

    duration_seconds = frame_count / sampling_rate
    if frame_count <= 0 or duration_seconds < min_duration_seconds:
        raise ValueError(
            "Melody reference WAV is too short: "
            f"{duration_seconds:.3f}s is below the {min_duration_seconds:g}s minimum."
        )
    if duration_seconds > max_duration_seconds:
        raise ValueError(
            "Melody reference WAV is too long: "
            f"{duration_seconds:.3f}s exceeds the configured {max_duration_seconds:g}s limit."
        )

    return WavReferenceInfo(
        path=reference_path,
        channels=channels,
        sampling_rate=sampling_rate,
        sample_width=sample_width,
        frame_count=frame_count,
        duration_seconds=duration_seconds,
    )


def prepare_wav_reference(
    path: str | Path,
    *,
    target_sampling_rate: int,
    min_duration_seconds: float = 1.0,
    max_duration_seconds: float,
    torch: Any,
) -> tuple[Any, WavReferenceInfo]:
    """Decode PCM, convert to mono, and resample before processor invocation."""

    info = inspect_wav_reference(
        path,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
    )
    if target_sampling_rate <= 0:
        raise ValueError("Melody model sampling rate must be positive.")

    with wave.open(str(info.path), "rb") as wav_file:
        raw_frames = bytearray(wav_file.readframes(info.frame_count))

    samples = _decode_pcm(raw_frames, sample_width=info.sample_width, torch=torch)
    samples = samples.reshape(info.frame_count, info.channels)
    mono = samples.mean(dim=1)

    if info.sampling_rate != target_sampling_rate:
        target_length = max(
            1,
            round(info.frame_count * target_sampling_rate / info.sampling_rate),
        )
        mono = torch.nn.functional.interpolate(
            mono.reshape(1, 1, -1),
            size=target_length,
            mode="linear",
            align_corners=False,
        ).reshape(-1)

    return mono.contiguous(), info


def _decode_pcm(raw_frames: bytearray, *, sample_width: int, torch: Any):
    if sample_width == 1:
        values = torch.frombuffer(raw_frames, dtype=torch.uint8).clone()
        return (values.to(torch.float32) - 128.0) / 128.0
    if sample_width == 2:
        values = torch.frombuffer(raw_frames, dtype=torch.int16).clone()
        return values.to(torch.float32) / 32_768.0
    if sample_width == 3:
        octets = torch.frombuffer(raw_frames, dtype=torch.uint8).clone().reshape(-1, 3)
        values = (
            octets[:, 0].to(torch.int32)
            | (octets[:, 1].to(torch.int32) << 8)
            | (octets[:, 2].to(torch.int32) << 16)
        )
        values = torch.where(values >= (1 << 23), values - (1 << 24), values)
        return values.to(torch.float32) / 8_388_608.0
    values = torch.frombuffer(raw_frames, dtype=torch.int32).clone()
    return values.to(torch.float32) / 2_147_483_648.0


__all__ = [
    "WavReferenceInfo",
    "inspect_wav_reference",
    "prepare_wav_reference",
]
