"""Contract tests for shared audio post-processing configuration and metadata.

``core/audio/postprocess.py`` is the one place music and speech both go through
for normalize/trim/fade/duck. These tests pin the parts of that contract that
individual-function tests (``tests/test_speech_audio.py``) do not: that music and
speech metadata share one shape, that the disabled path reports the same shape
as the enabled one, that processing order is explicit and repeatable, and that
every producer's metadata survives a JSON round trip (job/result metadata is
persisted as JSON, so a NaN/Infinity leaking through would corrupt the record).

See issue #56 (parent) and #229 (this contract) for the acceptance criteria.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from core.audio import (
    MUSIC_PRESET,
    SPEECH_PRESET,
    process_audio,
    process_music_channels,
    skipped_processing_report,
)

_RATE = 8_000

# The full set of keys every post-processing metadata dict must carry, whether
# it came from an enabled chain or a skipped one. A caller reads this shape
# without a preset-specific branch, so `enabled` alone must be enough to tell
# an applied chain from a skipped one.
_APPLIED_METADATA_KEYS = {
    "preset",
    "sample_rate",
    "enabled",
    "chain",
    "steps",
    "duration_seconds_before",
    "duration_seconds_after",
}


def _sine(seconds: float, *, amplitude: float = 0.3, frequency: float = 220.0) -> np.ndarray:
    times = np.arange(int(seconds * _RATE), dtype=np.float32) / _RATE
    return (amplitude * np.sin(2 * np.pi * frequency * times)).astype(np.float32)


# --------------------------------------------------------------------------
# Music and speech share one configuration shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("preset", [MUSIC_PRESET, SPEECH_PRESET])
def test_process_audio_metadata_has_the_shared_shape(preset: str) -> None:
    _, applied = process_audio(_sine(0.5), _RATE, preset=preset)

    assert set(applied.keys()) == _APPLIED_METADATA_KEYS
    assert applied["preset"] == preset
    assert applied["enabled"] is True
    assert applied["sample_rate"] == _RATE
    assert isinstance(applied["chain"], list) and applied["chain"]
    assert len(applied["chain"]) == len(applied["steps"])


@pytest.mark.parametrize("preset", [MUSIC_PRESET, SPEECH_PRESET])
def test_skipped_report_has_the_same_shape_as_an_applied_one(preset: str) -> None:
    """`skipped_processing_report` is documented to mirror `process_audio`'s shape.

    A caller (job metadata reader, UI) should be able to read `audio_postprocess`
    without knowing ahead of time whether the chain ran.
    """

    _, applied = process_audio(_sine(0.5), _RATE, preset=preset)
    skipped = skipped_processing_report(_RATE, preset=preset, sample_count=4_000)

    assert set(skipped.keys()) == set(applied.keys()) == _APPLIED_METADATA_KEYS
    assert skipped["enabled"] is False
    assert skipped["chain"] == []
    assert skipped["steps"] == []
    assert skipped["preset"] == preset
    assert skipped["sample_rate"] == _RATE


def test_process_music_channels_metadata_has_the_shared_shape() -> None:
    _, applied = process_music_channels(_sine(0.5), _RATE)

    assert set(applied.keys()) == _APPLIED_METADATA_KEYS
    assert applied["preset"] == MUSIC_PRESET
    assert applied["enabled"] is True


# --------------------------------------------------------------------------
# Processing order is explicit and deterministic
# --------------------------------------------------------------------------


def test_speech_and_music_chains_are_explicit_and_related() -> None:
    _, speech_applied = process_audio(_sine(0.5), _RATE, preset=SPEECH_PRESET)
    _, music_applied = process_audio(_sine(0.5), _RATE, preset=MUSIC_PRESET)

    assert speech_applied["chain"] == [
        "trim_silence",
        "normalize_rms",
        "apply_fades",
        "normalize_peak",
    ]
    assert music_applied["chain"] == ["normalize_rms", "apply_fades", "normalize_peak"]
    # Music skips only the leading/trailing silence trim (a slow intro or a
    # decaying tail is part of the arrangement); every other step, and their
    # relative order, is identical for both presets. A change that reorders
    # gain-then-fade for one preset but not the other would break this.
    assert music_applied["chain"] == speech_applied["chain"][1:]


@pytest.mark.parametrize("preset", [MUSIC_PRESET, SPEECH_PRESET])
def test_process_audio_is_deterministic(preset: str) -> None:
    """Same input, same preset, same output — metadata included.

    Job metadata records this dict as "what actually happened"; if two runs on
    identical input produced different chains or step values, that record would
    not be trustworthy.
    """

    source = _sine(0.5)
    first_audio, first_applied = process_audio(source.copy(), _RATE, preset=preset)
    second_audio, second_applied = process_audio(source.copy(), _RATE, preset=preset)

    assert np.array_equal(first_audio, second_audio)
    assert first_applied == second_applied


# --------------------------------------------------------------------------
# Disabling processing preserves the original audio path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("preset", [MUSIC_PRESET, SPEECH_PRESET])
def test_skipped_report_never_claims_a_step_ran(preset: str) -> None:
    report = skipped_processing_report(_RATE, preset=preset, sample_count=1_234)

    assert report["enabled"] is False
    assert report["chain"] == []
    assert report["steps"] == []


def test_skipped_report_duration_matches_what_the_same_buffer_would_report_enabled() -> None:
    """Disabling changes whether the chain ran, not how duration is measured."""

    samples = _sine(0.75)
    _, enabled_applied = process_audio(samples, _RATE, preset=MUSIC_PRESET)
    skipped = skipped_processing_report(_RATE, preset=MUSIC_PRESET, sample_count=samples.size)

    assert skipped["duration_seconds_before"] == enabled_applied["duration_seconds_before"]
    # Music does not trim, so the "after" durations line up too; this is not
    # asserted for speech, which trims and therefore legitimately differs.
    assert skipped["duration_seconds_after"] == enabled_applied["duration_seconds_before"]


@pytest.mark.parametrize("sample_count,expected_seconds", [(0, 0.0), (4_000, 0.5), (-10, 0.0)])
def test_skipped_report_computes_duration_from_sample_count(
    sample_count: int, expected_seconds: float
) -> None:
    # A negative count cannot happen from a real buffer, but the function must
    # not raise or report a negative duration for it either.
    report = skipped_processing_report(_RATE, preset=MUSIC_PRESET, sample_count=sample_count)

    assert report["duration_seconds_before"] == pytest.approx(expected_seconds)
    assert report["duration_seconds_after"] == pytest.approx(expected_seconds)


# --------------------------------------------------------------------------
# Preset validation is shared between the enabled and disabled paths
# --------------------------------------------------------------------------


def test_skipped_report_rejects_the_same_unknown_preset_as_process_audio() -> None:
    with pytest.raises(ValueError, match="narration"):
        process_audio(_sine(0.1), _RATE, preset="narration")
    with pytest.raises(ValueError, match="narration"):
        skipped_processing_report(_RATE, preset="narration", sample_count=100)


@pytest.mark.parametrize("bad_rate", [0, -1])
def test_process_audio_and_skipped_report_reject_a_non_positive_sample_rate(bad_rate: int) -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        process_audio(_sine(0.1), bad_rate, preset=MUSIC_PRESET)
    with pytest.raises(ValueError, match="sample_rate"):
        skipped_processing_report(bad_rate, preset=MUSIC_PRESET, sample_count=100)


def test_preset_constants_are_the_stable_strings_generators_and_manifests_depend_on() -> None:
    # Generators pass these as string literals in job params/metadata
    # (`"preset": "music"` / `"preset": "speech"`); changing the constant
    # values would silently break that contract without touching this module.
    assert MUSIC_PRESET == "music"
    assert SPEECH_PRESET == "speech"


# --------------------------------------------------------------------------
# Effective settings can be persisted in job/result metadata (JSON round trip)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("preset", [MUSIC_PRESET, SPEECH_PRESET])
def test_applied_metadata_round_trips_through_json(preset: str) -> None:
    _, applied = process_audio(_sine(0.5), _RATE, preset=preset)

    round_tripped = json.loads(json.dumps(applied))

    assert round_tripped == applied


@pytest.mark.parametrize("preset", [MUSIC_PRESET, SPEECH_PRESET])
def test_skipped_metadata_round_trips_through_json(preset: str) -> None:
    report = skipped_processing_report(_RATE, preset=preset, sample_count=2_000)

    assert json.loads(json.dumps(report)) == report


def test_silent_buffer_metadata_has_no_nan_or_infinity_and_still_round_trips() -> None:
    """A silent buffer drives dB toward -inf; the module must report `None`.

    `Infinity`/`NaN` are not valid JSON, so a step that emitted them would break
    job metadata persistence the moment a near-silent generation was processed.
    """

    silence = np.zeros(2_000, dtype=np.float32)
    _, applied = process_audio(silence, _RATE, preset=SPEECH_PRESET)

    encoded = json.dumps(applied)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded
    decoded = json.loads(encoded)
    assert decoded == applied

    db_fields = [
        step[key]
        for step in applied["steps"]
        for key in ("peak_db_before", "peak_db_after", "rms_db_before", "rms_db_after")
        if key in step
    ]
    assert db_fields  # sanity: the silent case actually exercised dB fields
    assert all(value is None for value in db_fields)


def test_process_music_channels_metadata_round_trips_through_json() -> None:
    _, applied = process_music_channels(_sine(0.5), _RATE)

    assert json.loads(json.dumps(applied)) == applied


__all__: list[str] = []
