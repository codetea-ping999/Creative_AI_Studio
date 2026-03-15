"""Heuristic quality scoring for locally generated image and audio outputs."""

from __future__ import annotations

from array import array
import math
from pathlib import Path
import wave

from PIL import Image, ImageChops, ImageFilter, ImageSequence, ImageStat


def evaluate_image_output(output_path: str | Path) -> dict[str, object]:
    """Return a lightweight technical quality report for an image asset."""

    path = Path(output_path)
    with Image.open(path) as image:
        rgb_image = image.convert("RGB")
        width, height = rgb_image.size
        rgb_stat = ImageStat.Stat(rgb_image)
        hsv_stat = ImageStat.Stat(rgb_image.convert("HSV"))
        edge_stat = ImageStat.Stat(
            rgb_image.convert("L").filter(ImageFilter.FIND_EDGES)
        )
        histogram = rgb_image.histogram()

    pixel_count = max(1, width * height)
    channel_count = 3
    brightness_mean = sum(rgb_stat.mean) / channel_count
    contrast_stddev = sum(rgb_stat.stddev) / channel_count
    saturation_mean = float(hsv_stat.mean[1])
    edge_energy = float(edge_stat.mean[0])
    clipped_pixels = 0
    for channel_index in range(channel_count):
        offset = channel_index * 256
        clipped_pixels += histogram[offset] + histogram[offset + 255]
    clipped_ratio = clipped_pixels / max(1, pixel_count * channel_count)
    megapixels = (width * height) / 1_000_000
    file_size_bytes = path.stat().st_size

    resolution_score = _score_band(megapixels, floor=0.35, target=1.0, ceiling=2.2)
    brightness_score = _centered_score(brightness_mean, center=132, tolerance=62)
    contrast_score = _centered_score(contrast_stddev, center=58, tolerance=34)
    saturation_score = _centered_score(saturation_mean, center=110, tolerance=72)
    edge_score = _score_band(edge_energy, floor=9, target=22, ceiling=70)
    clipping_penalty = min(20.0, clipped_ratio * 1000)

    technical_quality_score = round(
        max(
            0.0,
            min(
                100.0,
                resolution_score * 24
                + brightness_score * 18
                + contrast_score * 18
                + saturation_score * 14
                + edge_score * 26
                - clipping_penalty,
            ),
        ),
        1,
    )
    business_readiness_score = round(
        max(
            0.0,
            min(
                100.0,
                technical_quality_score * 0.7
                + _score_band(file_size_bytes / 1024, floor=60, target=180, ceiling=900)
                * 30,
            ),
        ),
        1,
    )

    checks: list[str] = []
    if technical_quality_score >= 80:
        checks.append("technical quality is strong for local review")
    if megapixels < 0.7:
        checks.append("resolution is low for production reuse")
    if clipped_ratio > 0.01:
        checks.append("highlights or shadows appear clipped")
    if edge_energy < 10:
        checks.append("image detail appears soft")
    if contrast_stddev < 28:
        checks.append("global contrast is muted")
    if not checks:
        checks.append("no major technical warning detected")

    return {
        "method": "heuristic_local_v1",
        "quality_score": technical_quality_score,
        "quality_level": _quality_level(technical_quality_score),
        "business_readiness_score": business_readiness_score,
        "business_readiness_level": _quality_level(business_readiness_score),
        "checks": checks,
        "metrics": {
            "width": width,
            "height": height,
            "megapixels": round(megapixels, 3),
            "file_size_bytes": file_size_bytes,
            "brightness_mean": round(brightness_mean, 2),
            "contrast_stddev": round(contrast_stddev, 2),
            "saturation_mean": round(saturation_mean, 2),
            "edge_energy": round(edge_energy, 2),
            "clipped_ratio": round(clipped_ratio, 4),
        },
        "notes": [
            "semantic prompt fidelity is not measured here",
            "score reflects technical proxy quality only",
        ],
    }


def evaluate_audio_output(output_path: str | Path) -> dict[str, object]:
    """Return a lightweight technical quality report for a PCM wav asset."""

    path = Path(output_path)
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw_frames = wav_file.readframes(frame_count)

    normalized_samples = decode_pcm_samples(raw_frames, sample_width)
    if not normalized_samples:
        raise ValueError(f"Audio output is empty: {path}")

    abs_samples = [abs(sample) for sample in normalized_samples]
    peak = max(abs_samples)
    rms = math.sqrt(sum(sample * sample for sample in normalized_samples) / len(normalized_samples))
    silence_ratio = sum(1 for sample in abs_samples if sample < 0.01) / len(abs_samples)
    clipping_ratio = sum(1 for sample in abs_samples if sample >= 0.995) / len(abs_samples)
    zero_crossings = 0
    for previous, current in zip(normalized_samples, normalized_samples[1:]):
        if (previous < 0 <= current) or (previous > 0 >= current):
            zero_crossings += 1
    zero_crossing_rate = zero_crossings / max(1, len(normalized_samples) - 1)
    duration_seconds = frame_count / max(1, frame_rate)
    dynamic_span = max(0.0, peak - rms)
    file_size_bytes = path.stat().st_size

    duration_score = _score_band(duration_seconds, floor=2.0, target=8.0, ceiling=45.0)
    loudness_score = _centered_score(rms, center=0.18, tolerance=0.13)
    clipping_score = 1.0 - min(1.0, clipping_ratio * 50)
    silence_score = 1.0 - min(1.0, max(0.0, silence_ratio - 0.08) / 0.42)
    dynamics_score = _score_band(dynamic_span, floor=0.05, target=0.2, ceiling=0.7)
    motion_score = _centered_score(zero_crossing_rate, center=0.16, tolerance=0.13)

    technical_quality_score = round(
        max(
            0.0,
            min(
                100.0,
                duration_score * 12
                + loudness_score * 22
                + clipping_score * 22
                + silence_score * 18
                + dynamics_score * 16
                + motion_score * 10,
            ),
        ),
        1,
    )
    business_readiness_score = round(
        max(
            0.0,
            min(
                100.0,
                technical_quality_score * 0.75
                + _score_band(file_size_bytes / 1024, floor=80, target=240, ceiling=2400)
                * 25,
            ),
        ),
        1,
    )

    checks: list[str] = []
    if technical_quality_score >= 80:
        checks.append("technical playback quality is strong for local review")
    if clipping_ratio > 0.01:
        checks.append("audio contains clipping")
    if silence_ratio > 0.32:
        checks.append("audio includes a high silent portion")
    if rms < 0.035:
        checks.append("audio level is very quiet")
    if duration_seconds < 3:
        checks.append("clip is short for production reuse")
    if not checks:
        checks.append("no major technical warning detected")

    return {
        "method": "heuristic_local_v1",
        "quality_score": technical_quality_score,
        "quality_level": _quality_level(technical_quality_score),
        "business_readiness_score": business_readiness_score,
        "business_readiness_level": _quality_level(business_readiness_score),
        "checks": checks,
        "metrics": {
            "duration_seconds": round(duration_seconds, 3),
            "channels": channels,
            "sample_width_bytes": sample_width,
            "sample_rate": frame_rate,
            "file_size_bytes": file_size_bytes,
            "peak_level": round(peak, 4),
            "rms_level": round(rms, 4),
            "silence_ratio": round(silence_ratio, 4),
            "clipping_ratio": round(clipping_ratio, 4),
            "zero_crossing_rate": round(zero_crossing_rate, 4),
            "dynamic_span": round(dynamic_span, 4),
        },
        "notes": [
            "musicality and style alignment are not measured here",
            "score reflects playback-oriented proxy quality only",
        ],
    }


def evaluate_video_output(output_path: str | Path) -> dict[str, object]:
    """Return a lightweight technical quality report for a local gif asset."""

    path = Path(output_path)
    with Image.open(path) as gif:
        frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(gif)]
        width, height = gif.size
        duration_ms = int(gif.info.get("duration", 100))

    if not frames:
        raise ValueError(f"Video output is empty: {path}")

    frame_count = len(frames)
    first_frame = frames[0]
    last_frame = frames[-1]
    first_stat = ImageStat.Stat(first_frame)
    edge_stat = ImageStat.Stat(first_frame.convert("L").filter(ImageFilter.FIND_EDGES))

    motion_delta = 0.0
    if frame_count > 1:
        diff = ImageChops.difference(first_frame, last_frame)
        motion_delta = float(sum(ImageStat.Stat(diff).mean) / 3)

    brightness_mean = float(sum(first_stat.mean) / 3)
    edge_energy = float(edge_stat.mean[0])
    duration_seconds = (frame_count * duration_ms) / 1000
    file_size_bytes = path.stat().st_size

    resolution_score = _score_band(
        (width * height) / 1_000_000,
        floor=0.15,
        target=0.48,
        ceiling=1.2,
    )
    duration_score = _score_band(duration_seconds, floor=1.2, target=3.0, ceiling=10.0)
    brightness_score = _centered_score(brightness_mean, center=128, tolerance=70)
    detail_score = _score_band(edge_energy, floor=7, target=18, ceiling=60)
    motion_score = _score_band(motion_delta, floor=4, target=18, ceiling=95)

    technical_quality_score = round(
        max(
            0.0,
            min(
                100.0,
                resolution_score * 24
                + duration_score * 16
                + brightness_score * 16
                + detail_score * 22
                + motion_score * 22,
            ),
        ),
        1,
    )
    business_readiness_score = round(
        max(
            0.0,
            min(
                100.0,
                technical_quality_score * 0.74
                + _score_band(file_size_bytes / 1024, floor=120, target=450, ceiling=2400)
                * 26,
            ),
        ),
        1,
    )

    checks: list[str] = []
    if technical_quality_score >= 80:
        checks.append("storyboard preview quality is strong for local review")
    if frame_count < 12:
        checks.append("video uses a short frame sequence")
    if motion_delta < 6:
        checks.append("motion variation is limited")
    if edge_energy < 10:
        checks.append("frame detail appears soft")
    if not checks:
        checks.append("no major technical warning detected")

    return {
        "method": "heuristic_local_v1",
        "quality_score": technical_quality_score,
        "quality_level": _quality_level(technical_quality_score),
        "business_readiness_score": business_readiness_score,
        "business_readiness_level": _quality_level(business_readiness_score),
        "checks": checks,
        "metrics": {
            "width": width,
            "height": height,
            "frame_count": frame_count,
            "frame_duration_ms": duration_ms,
            "duration_seconds": round(duration_seconds, 3),
            "file_size_bytes": file_size_bytes,
            "brightness_mean": round(brightness_mean, 2),
            "edge_energy": round(edge_energy, 2),
            "motion_delta": round(motion_delta, 2),
        },
        "notes": [
            "motion smoothness is estimated from frame deltas",
            "score reflects local storyboard preview quality only",
        ],
    }


def decode_pcm_samples(frames: bytes, sample_width: int) -> list[float]:
    if sample_width == 1:
        values = [(value - 128) / 128 for value in frames]
        return [float(value) for value in values]
    if sample_width == 2:
        buffer = array("h")
        buffer.frombytes(frames)
        return [sample / 32768 for sample in buffer]
    if sample_width == 4:
        buffer = array("i")
        buffer.frombytes(frames)
        return [sample / 2147483648 for sample in buffer]
    raise ValueError(f"Unsupported PCM sample width: {sample_width}")


def _score_band(value: float, *, floor: float, target: float, ceiling: float) -> float:
    if value <= floor:
        return 0.0
    if value >= ceiling:
        return 0.85
    if value == target:
        return 1.0
    if value < target:
        return (value - floor) / max(target - floor, 1e-6)
    return 1.0 - ((value - target) / max(ceiling - target, 1e-6)) * 0.15


def _centered_score(value: float, *, center: float, tolerance: float) -> float:
    distance = abs(value - center)
    if distance >= tolerance:
        return 0.0
    return 1.0 - (distance / max(tolerance, 1e-6))


def _quality_level(score: float) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "strong"
    if score >= 55:
        return "usable"
    if score >= 40:
        return "weak"
    return "poor"
