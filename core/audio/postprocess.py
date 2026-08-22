"""Shared audio post-processing for music and narration, numpy only.

Every function is pure: it takes a mono float32 array in [-1, 1] and returns a
new array plus a small JSON-safe dict describing what it did. The dict matters as
much as the audio — job metadata records the chain so a listener who asks "why is
this quieter than the last render?" can be answered from the job record instead
of by ear.

The whole module deliberately avoids scipy/librosa: an envelope follower and a
few ramps are a handful of numpy operations, and adding a DSP dependency for them
would be the largest install in the project.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

# Below this amplitude a buffer counts as silence. Normalizing against a peak or
# RMS this small either divides by ~0 or multiplies dither noise into an audible
# hiss, so the normalizers skip instead.
_SILENCE_FLOOR = 1e-6

# Window used to follow the narration level when ducking. Shorter windows react
# to individual glottal pulses and make the gain chatter; ~25 ms tracks syllables.
_ENVELOPE_WINDOW_SECONDS = 0.025

MUSIC_PRESET = "music"
SPEECH_PRESET = "speech"
_PRESETS = (MUSIC_PRESET, SPEECH_PRESET)

# Speech is normalized quieter than music leaves headroom for, because narration
# is mixed on top of a bed and needs room above it, and then peak-limited last.
_SPEECH_TARGET_RMS_DB = -20.0
_SPEECH_TARGET_PEAK_DB = -1.0
_MUSIC_TARGET_RMS_DB = -18.0
_MUSIC_TARGET_PEAK_DB = -1.0
_MUSIC_FADE_IN_SECONDS = 0.05
_MUSIC_FADE_OUT_SECONDS = 0.5


def normalize_peak(
    samples: Any,
    target_peak_db: float = -1.0,
    *,
    attenuate_only: bool = False,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Scale the buffer so its loudest sample sits at ``target_peak_db``.

    ``attenuate_only`` skips the scale-up when the buffer is already quieter
    than the target. The chain uses this for a buffer whose RMS stage gain
    was capped: that cap exists specifically to leave anomalously quiet
    content (model noise, not real signal) too quiet rather than amplifying
    it, and an uncapped peak boost right after would silently undo it.
    """

    array = _as_mono_float32(samples)
    peak_before = _peak(array)
    info: dict[str, Any] = {
        "step": "normalize_peak",
        "applied": False,
        "target_peak_db": float(target_peak_db),
        "gain_db": 0.0,
        "peak_db_before": _amplitude_to_db(peak_before),
        "peak_db_after": _amplitude_to_db(peak_before),
        "skipped_reason": None,
    }

    skipped_reason = _silence_reason(array, peak_before)
    if skipped_reason is not None:
        info["skipped_reason"] = skipped_reason
        return array, info

    gain = _db_to_amplitude(target_peak_db) / peak_before
    if attenuate_only and gain > 1.0:
        info["skipped_reason"] = "attenuate_only: buffer is already below target peak"
        return array, info

    processed = _clip(array * gain)
    info["applied"] = True
    info["gain_db"] = round(_gain_to_db(gain), 3)
    info["peak_db_after"] = _amplitude_to_db(_peak(processed))
    return processed, info


def normalize_rms(
    samples: Any,
    target_rms_db: float = -20.0,
    max_gain_db: float = 12.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Scale the buffer toward ``target_rms_db`` with a cap on the boost.

    The cap is the point of this function. RMS normalization on a barely audible
    take asks for 30-40 dB of gain, which lifts room tone, encoder noise, and the
    TTS backend's own artefacts to the same level as the words. Capping the boost
    means a too-quiet input stays too quiet — an obvious, fixable symptom — rather
    than becoming loud noise that sounds like a broken model. Attenuation is not
    capped: pulling a hot render down never adds noise.
    """

    array = _as_mono_float32(samples)
    rms_before = _rms(array)
    info: dict[str, Any] = {
        "step": "normalize_rms",
        "applied": False,
        "target_rms_db": float(target_rms_db),
        "max_gain_db": float(max_gain_db),
        "gain_db": 0.0,
        "gain_capped": False,
        "rms_db_before": _amplitude_to_db(rms_before),
        "rms_db_after": _amplitude_to_db(rms_before),
        "skipped_reason": None,
    }

    skipped_reason = _silence_reason(array, rms_before)
    if skipped_reason is not None:
        info["skipped_reason"] = skipped_reason
        return array, info

    requested_gain_db = float(target_rms_db) - _to_db(rms_before)
    gain_db = min(requested_gain_db, float(max_gain_db))
    processed = _clip(array * _db_to_amplitude(gain_db))
    info["applied"] = True
    info["gain_db"] = round(gain_db, 3)
    info["requested_gain_db"] = round(requested_gain_db, 3)
    info["gain_capped"] = bool(gain_db < requested_gain_db - 1e-9)
    info["rms_db_after"] = _amplitude_to_db(_rms(processed))
    return processed, info


def trim_silence(
    samples: Any,
    sample_rate: int,
    threshold_db: float = -45.0,
    keep_padding_seconds: float = 0.05,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Drop leading and trailing silence, keeping a short breath of padding.

    The padding is kept because a cut placed exactly on the first sample above the
    threshold clips the attack of the first consonant and sounds like a dropout.
    """

    array = _as_mono_float32(samples)
    rate = _require_sample_rate(sample_rate)
    duration_before = _duration_seconds(array, rate)
    info: dict[str, Any] = {
        "step": "trim_silence",
        "applied": False,
        "threshold_db": float(threshold_db),
        "keep_padding_seconds": float(keep_padding_seconds),
        "trimmed_leading_seconds": 0.0,
        "trimmed_trailing_seconds": 0.0,
        "duration_seconds_before": duration_before,
        "duration_seconds_after": duration_before,
        "skipped_reason": None,
    }

    if array.size == 0:
        info["skipped_reason"] = "empty buffer"
        return array, info

    above_threshold = np.abs(array) > _db_to_amplitude(threshold_db)
    if not bool(above_threshold.any()):
        # Trimming to nothing would hand an empty asset to the writer, so keep the
        # buffer and let the quality report flag how quiet it is.
        info["skipped_reason"] = "no sample above threshold"
        return array, info

    first = int(np.argmax(above_threshold))
    last = int(array.size - 1 - np.argmax(above_threshold[::-1]))
    padding = max(0, int(round(float(keep_padding_seconds) * rate)))
    start = max(0, first - padding)
    end = min(array.size, last + 1 + padding)

    processed = np.array(array[start:end], dtype=np.float32, copy=True)
    info["applied"] = bool(start > 0 or end < array.size)
    info["trimmed_leading_seconds"] = round(start / rate, 6)
    info["trimmed_trailing_seconds"] = round((array.size - end) / rate, 6)
    info["duration_seconds_after"] = _duration_seconds(processed, rate)
    return processed, info


def apply_fades(
    samples: Any,
    sample_rate: int,
    fade_in_seconds: float = 0.02,
    fade_out_seconds: float = 0.08,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Ramp the first and last samples so joins and stops do not click."""

    array = _as_mono_float32(samples)
    rate = _require_sample_rate(sample_rate)
    info: dict[str, Any] = {
        "step": "apply_fades",
        "applied": False,
        "fade_in_seconds": float(fade_in_seconds),
        "fade_out_seconds": float(fade_out_seconds),
        "fade_in_samples": 0,
        "fade_out_samples": 0,
        "skipped_reason": None,
    }

    if array.size == 0:
        info["skipped_reason"] = "empty buffer"
        return array, info

    fade_in = max(0, int(round(max(0.0, float(fade_in_seconds)) * rate)))
    fade_out = max(0, int(round(max(0.0, float(fade_out_seconds)) * rate)))
    if fade_in + fade_out > array.size:
        # Shrink both ramps proportionally rather than letting them overlap, which
        # would multiply the two curves and punch a hole in a very short clip.
        scale = array.size / float(fade_in + fade_out)
        fade_in = int(fade_in * scale)
        fade_out = int(fade_out * scale)

    processed = np.array(array, dtype=np.float32, copy=True)
    if fade_in > 0:
        processed[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out > 0:
        processed[processed.size - fade_out :] *= np.linspace(
            1.0, 0.0, fade_out, dtype=np.float32
        )

    info["applied"] = bool(fade_in > 0 or fade_out > 0)
    info["fade_in_samples"] = int(fade_in)
    info["fade_out_samples"] = int(fade_out)
    return processed, info


def duck_envelope(
    spans: Sequence[tuple[int, int]],
    length: int,
    sample_rate: int,
    *,
    reduction_db: float = -12.0,
    attack_seconds: float = 0.15,
    release_seconds: float = 0.4,
) -> np.ndarray:
    """Build the music gain curve that dips to ``reduction_db`` across ``spans``.

    This is the entire ducking DSP, and it lives here alone on purpose. Two
    callers need the same curve from different evidence: :func:`duck` derives the
    spans by following the level of a narration buffer, while the assembly
    generator derives them from where narration clips were *placed* on a
    timeline. When each owned its own ramp math, a change to the depth or the
    ramps moved one mix and not the other, which is indistinguishable from a bug
    to whoever is listening.

    The gain moves along ramps rather than jumping between the two levels. A step
    change in gain is a discontinuity in the waveform: it clicks, and even when it
    does not, the music appears to "pump" in and out on every pause, which is far
    more distracting than the narration it was meant to make room for. The attack
    ramp also starts *before* the span so the bed is already down on the first
    syllable, the way a broadcast ducker behaves.

    ``spans`` are ``[start, end)`` sample indices; anything outside
    ``[0, length)`` is clipped rather than rejected, because a narration clip that
    runs past the end of the bed is a normal timeline, not an error.
    """

    total = max(0, int(length))
    rate = _require_sample_rate(sample_rate)
    gain = np.ones(total, dtype=np.float32)
    if total == 0:
        return gain

    reduction = _db_to_amplitude(reduction_db)
    attack = max(1, int(round(max(0.0, float(attack_seconds)) * rate)))
    release = max(1, int(round(max(0.0, float(release_seconds)) * rate)))
    attack_ramp = np.linspace(1.0, reduction, attack + 1, dtype=np.float32)[:-1]
    release_ramp = np.linspace(reduction, 1.0, release + 1, dtype=np.float32)[1:]

    for raw_start, raw_end in spans:
        start = max(0, int(raw_start))
        end = min(total, int(raw_end))
        if start >= total or end <= start:
            continue

        # np.minimum everywhere: when spans are close enough that a release ramp
        # runs into the next attack, the lower (more ducked) value has to win or
        # the gain would bounce back up between two words.
        ramp_start = max(0, start - attack)
        if start > ramp_start:
            # Take the tail of the ramp, never a compressed copy of it: a span
            # near the head of the buffer gets a shorter dip, at the same slope.
            segment = attack_ramp[attack - (start - ramp_start) :]
            gain[ramp_start:start] = np.minimum(gain[ramp_start:start], segment)

        gain[start:end] = np.minimum(gain[start:end], reduction)

        release_end = min(total, end + release)
        if release_end > end:
            segment = release_ramp[: release_end - end]
            gain[end:release_end] = np.minimum(gain[end:release_end], segment)

    return gain


def duck(
    music: Any,
    narration: Any,
    sample_rate: int,
    reduction_db: float = -12.0,
    attack_seconds: float = 0.15,
    release_seconds: float = 0.4,
    threshold_db: float = -40.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Attenuate ``music`` wherever ``narration`` is speaking.

    Span detection lives here; the curve itself comes from
    :func:`duck_envelope`, which the assembly generator also uses.

    The returned array always has the length of ``music``; a narration longer or
    shorter than the bed simply covers less or more of it.
    """

    music_array = _as_mono_float32(music, name="music")
    narration_array = _as_mono_float32(narration, name="narration")
    rate = _require_sample_rate(sample_rate)
    info: dict[str, Any] = {
        "step": "duck",
        "applied": False,
        "reduction_db": float(reduction_db),
        "attack_seconds": float(attack_seconds),
        "release_seconds": float(release_seconds),
        "threshold_db": float(threshold_db),
        "music_seconds": _duration_seconds(music_array, rate),
        "narration_seconds": _duration_seconds(narration_array, rate),
        "ducked_spans": 0,
        "ducked_seconds": 0.0,
        "min_gain_db": 0.0,
        "skipped_reason": None,
    }

    if music_array.size == 0:
        info["skipped_reason"] = "empty music buffer"
        return music_array, info
    if narration_array.size == 0:
        info["skipped_reason"] = "empty narration buffer"
        return music_array, info

    envelope = _smoothed_envelope(narration_array, rate)
    spans = _contiguous_spans(envelope > _db_to_amplitude(threshold_db))
    if not spans:
        info["skipped_reason"] = "no narration above threshold"
        return music_array, info

    gain = duck_envelope(
        spans,
        music_array.size,
        rate,
        reduction_db=reduction_db,
        attack_seconds=attack_seconds,
        release_seconds=release_seconds,
    )

    ducked_samples = int(np.count_nonzero(gain < 1.0 - 1e-6))
    info["applied"] = ducked_samples > 0
    info["ducked_spans"] = len(spans)
    info["ducked_seconds"] = round(ducked_samples / rate, 6)
    info["min_gain_db"] = _amplitude_to_db(float(gain.min()))
    return _clip(music_array * gain), info


def process_audio(
    samples: Any,
    sample_rate: int,
    *,
    preset: str,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Run the standard chain for ``preset`` and report every step.

    Order matters. Silence is trimmed before the level is measured so leading dead
    air cannot drag the RMS down and provoke a boost, and the peak normalizer runs
    last so it acts as the safety limiter for whatever the RMS stage did. When the
    RMS stage's own boost was capped (an explicit signal that the buffer is
    anomalously quiet, likely model noise rather than real signal), the peak
    stage only attenuates instead of boosting further — otherwise it would
    silently amplify exactly the content the RMS cap was protecting.
    """

    if preset not in _PRESETS:
        raise ValueError(
            f"Unknown audio post-processing preset {preset!r}; "
            f"expected one of {', '.join(_PRESETS)}."
        )

    rate = _require_sample_rate(sample_rate)
    array = _as_mono_float32(samples)
    duration_before = _duration_seconds(array, rate)
    steps: list[dict[str, Any]] = []

    if preset == SPEECH_PRESET:
        array, info = trim_silence(array, rate)
        steps.append(info)
        array, rms_info = normalize_rms(array, target_rms_db=_SPEECH_TARGET_RMS_DB)
        steps.append(rms_info)
        array, info = apply_fades(array, rate)
        steps.append(info)
        array, info = normalize_peak(
            array,
            target_peak_db=_SPEECH_TARGET_PEAK_DB,
            attenuate_only=bool(rms_info.get("gain_capped")),
        )
        steps.append(info)
    else:
        # Music keeps its leading and trailing silence: a slow intro or a decaying
        # tail is part of the arrangement, not dead air to cut.
        array, rms_info = normalize_rms(array, target_rms_db=_MUSIC_TARGET_RMS_DB)
        steps.append(rms_info)
        array, info = apply_fades(
            array,
            rate,
            fade_in_seconds=_MUSIC_FADE_IN_SECONDS,
            fade_out_seconds=_MUSIC_FADE_OUT_SECONDS,
        )
        steps.append(info)
        array, info = normalize_peak(
            array,
            target_peak_db=_MUSIC_TARGET_PEAK_DB,
            attenuate_only=bool(rms_info.get("gain_capped")),
        )
        steps.append(info)

    applied: dict[str, Any] = {
        "preset": preset,
        "sample_rate": rate,
        "enabled": True,
        "chain": [str(step["step"]) for step in steps],
        "steps": steps,
        "duration_seconds_before": duration_before,
        "duration_seconds_after": _duration_seconds(array, rate),
    }
    return array, applied


def process_music_channels(
    channels: Any,
    sample_rate: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Run the music preset across one or more channels with linked gain.

    ``channels`` is (channel_count, samples), or a plain mono buffer. Gain
    steps (RMS and peak normalization) are measured once against every
    channel's samples pooled together and the same gain applied to every
    channel; measuring gain independently per channel would erase a stereo
    signal's intentional channel-level difference (pan, balance), since each
    channel would be pulled toward the target level on its own. Fades are a
    fixed time-based ramp that does not depend on channel content, so each
    channel keeps its own (identical) fade call. As in ``process_audio()``,
    the peak stage only attenuates when the RMS stage's own boost was
    capped, so it cannot un-cap an anomalously quiet (likely noise) buffer.
    """

    array = np.asarray(channels, dtype=np.float32)
    single_channel = array.ndim == 1
    if single_channel:
        array = array[np.newaxis, :]
    rate = _require_sample_rate(sample_rate)
    true_duration = round(array.shape[-1] / rate, 6)

    combined = array.reshape(-1)
    _, rms_info = normalize_rms(combined, target_rms_db=_MUSIC_TARGET_RMS_DB)
    array = _clip(array * _db_to_amplitude(rms_info["gain_db"]))

    fade_results = [
        apply_fades(
            channel,
            rate,
            fade_in_seconds=_MUSIC_FADE_IN_SECONDS,
            fade_out_seconds=_MUSIC_FADE_OUT_SECONDS,
        )
        for channel in array
    ]
    array = np.stack([faded for faded, _ in fade_results], axis=0)
    fade_info = fade_results[0][1]

    combined = array.reshape(-1)
    _, peak_info = normalize_peak(
        combined,
        target_peak_db=_MUSIC_TARGET_PEAK_DB,
        attenuate_only=bool(rms_info.get("gain_capped")),
    )
    array = _clip(array * _db_to_amplitude(peak_info["gain_db"]))

    applied: dict[str, Any] = {
        "preset": MUSIC_PRESET,
        "sample_rate": rate,
        "enabled": True,
        "chain": ["normalize_rms", "apply_fades", "normalize_peak"],
        "steps": [rms_info, fade_info, peak_info],
        "duration_seconds_before": true_duration,
        "duration_seconds_after": true_duration,
    }
    if single_channel:
        array = array[0]
    return array, applied


def skipped_processing_report(
    sample_rate: int,
    *,
    preset: str,
    sample_count: int,
) -> dict[str, Any]:
    """Report the same shape as ``process_audio`` for a caller that skipped it.

    Job metadata always carries an ``audio_postprocess`` entry, whether or not
    the chain actually ran, so a listener does not need a separate null-check
    to find out why a render sounds different from the usual chain.
    """

    if preset not in _PRESETS:
        raise ValueError(
            f"Unknown audio post-processing preset {preset!r}; "
            f"expected one of {', '.join(_PRESETS)}."
        )
    rate = _require_sample_rate(sample_rate)
    duration = round(max(0, int(sample_count)) / rate, 6)
    return {
        "preset": preset,
        "sample_rate": rate,
        "enabled": False,
        "chain": [],
        "steps": [],
        "duration_seconds_before": duration,
        "duration_seconds_after": duration,
    }


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _as_mono_float32(samples: Any, *, name: str = "samples") -> np.ndarray:
    """Copy the input into a contiguous mono float32 array.

    Always copying is what makes these functions safe to chain: a caller can hand
    in a buffer it still needs and get an independent result back.
    """

    array = np.array(samples, dtype=np.float32, copy=True)
    if array.ndim > 1:
        if 1 not in array.shape:
            raise ValueError(
                f"{name} must be mono, got shape {array.shape}. "
                "Mix down to a single channel before post-processing."
            )
        array = array.reshape(-1)
    return np.ascontiguousarray(array, dtype=np.float32)


def _require_sample_rate(sample_rate: int) -> int:
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError(f"sample_rate must be positive, got {sample_rate!r}.")
    return rate


def _peak(array: np.ndarray) -> float:
    if array.size == 0:
        return 0.0
    return float(np.max(np.abs(array)))


def _rms(array: np.ndarray) -> float:
    if array.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(array, dtype=np.float64))))


def _silence_reason(array: np.ndarray, level: float) -> str | None:
    if array.size == 0:
        return "empty buffer"
    if level <= _SILENCE_FLOOR:
        return "buffer is silent"
    return None


def _clip(array: np.ndarray) -> np.ndarray:
    return np.clip(array, -1.0, 1.0).astype(np.float32)


def _db_to_amplitude(decibels: float) -> float:
    return float(10.0 ** (float(decibels) / 20.0))


def _to_db(amplitude: float) -> float:
    return 20.0 * math.log10(max(float(amplitude), _SILENCE_FLOOR))


def _gain_to_db(gain: float) -> float:
    return _to_db(gain)


def _amplitude_to_db(amplitude: float) -> float | None:
    """Return the level in dB, or ``None`` for silence.

    ``-inf`` would be the honest answer, but these dicts land in JSON job
    metadata, and ``Infinity`` is not valid JSON.
    """

    if amplitude <= _SILENCE_FLOOR:
        return None
    return round(_to_db(amplitude), 3)


def _duration_seconds(array: np.ndarray, rate: int) -> float:
    return round(array.size / rate, 6)


def _smoothed_envelope(array: np.ndarray, rate: int) -> np.ndarray:
    """Follow the absolute level with a centered moving average.

    Implemented with a cumulative sum rather than ``np.convolve`` so the cost is
    linear in samples instead of samples times window; a minute of 48 kHz audio
    with a 25 ms window is the difference between milliseconds and seconds.
    """

    window = max(1, int(round(_ENVELOPE_WINDOW_SECONDS * rate)))
    magnitude = np.abs(array)
    if window <= 1 or array.size == 0:
        return magnitude

    left = window // 2
    right = window - left - 1
    padded = np.concatenate(
        [
            np.zeros(left, dtype=np.float64),
            magnitude.astype(np.float64),
            np.zeros(right, dtype=np.float64),
        ]
    )
    cumulative = np.concatenate([[0.0], np.cumsum(padded)])
    window_sums = cumulative[window:] - cumulative[:-window]
    return (window_sums / window).astype(np.float32)


def _contiguous_spans(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return ``[start, end)`` index pairs for each run of True in ``mask``."""

    if not bool(mask.any()):
        return []
    padded = np.concatenate(([False], mask.astype(bool), [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist()))


__all__ = [
    "MUSIC_PRESET",
    "SPEECH_PRESET",
    "apply_fades",
    "duck",
    "duck_envelope",
    "normalize_peak",
    "normalize_rms",
    "process_audio",
    "process_music_channels",
    "skipped_processing_report",
    "trim_silence",
]
