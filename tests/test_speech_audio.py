"""Tests for shared audio post-processing, TTS runtimes, and SpeechGenerator.

No model weights and no network: the speech runtimes are exercised through the
documented ``synthesize`` contract with fakes, and the HTTP engine is driven with a
monkeypatched httpx.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from bootstrap import create_application_services
from core.audio import (
    apply_fades,
    duck,
    duck_envelope,
    normalize_peak,
    normalize_rms,
    process_audio,
    process_music_channels,
    trim_silence,
)
from core.models import ModelRegistry, create_default_loader_registry
from core.models.audio_runtimes import (
    build_kokoro_runtime,
    build_voicevox_runtime,
    decode_wav_bytes,
    resolve_audio_endpoint,
    resolve_kokoro_language_code,
    to_mono_float32,
)
from core.schemas import GenerationRequest
from generators.audio import SpeechGenerator, split_into_chunks, split_into_sentences
from generators.audio.providers import AudioProviderError

_RATE = 1_000
_FAKE_TTS_RATE = 24_000
_SECONDS_PER_CHARACTER = 0.02


def _sine(
    seconds: float,
    *,
    sample_rate: int = _RATE,
    frequency: float = 100.0,
    amplitude: float = 0.5,
) -> np.ndarray:
    times = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
    return (amplitude * np.sin(2 * np.pi * frequency * times)).astype(np.float32)


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


# --------------------------------------------------------------------------
# Post-processing
# --------------------------------------------------------------------------


def test_normalize_peak_moves_the_peak_to_the_target_without_mutating_input():
    samples = _sine(0.5, amplitude=0.25)
    original = samples.copy()

    processed, info = normalize_peak(samples, target_peak_db=-1.0)

    assert info["applied"] is True
    assert info["peak_db_after"] == pytest.approx(-1.0, abs=0.05)
    assert float(np.max(np.abs(processed))) == pytest.approx(0.8913, abs=0.005)
    assert np.array_equal(samples, original)


def test_normalize_peak_skips_empty_and_silent_buffers():
    empty, empty_info = normalize_peak(np.zeros(0, dtype=np.float32))
    assert empty.size == 0
    assert empty_info["applied"] is False
    assert empty_info["skipped_reason"] == "empty buffer"
    assert empty_info["peak_db_before"] is None

    silence, silence_info = normalize_peak(np.zeros(256, dtype=np.float32))
    assert silence_info["applied"] is False
    assert silence_info["skipped_reason"] == "buffer is silent"
    assert np.array_equal(silence, np.zeros(256, dtype=np.float32))


def test_normalize_peak_attenuate_only_skips_a_boost_but_still_attenuates():
    quiet = _sine(0.5, amplitude=0.01)
    quiet_processed, quiet_info = normalize_peak(
        quiet, target_peak_db=-1.0, attenuate_only=True
    )
    assert quiet_info["applied"] is False
    assert quiet_info["skipped_reason"] == (
        "attenuate_only: buffer is already below target peak"
    )
    assert np.array_equal(quiet_processed, quiet)

    loud_processed, loud_info = normalize_peak(
        _sine(0.5, amplitude=0.99), target_peak_db=-1.0, attenuate_only=True
    )
    assert loud_info["applied"] is True
    assert loud_info["peak_db_after"] == pytest.approx(-1.0, abs=0.05)


def test_normalize_rms_reaches_the_target_for_a_normal_level():
    processed, info = normalize_rms(_sine(0.5, amplitude=0.4), target_rms_db=-20.0)

    assert info["applied"] is True
    assert info["gain_capped"] is False
    assert info["rms_db_after"] == pytest.approx(-20.0, abs=0.1)
    assert _rms(processed) == pytest.approx(0.1, abs=0.005)


def test_normalize_rms_caps_the_boost_on_a_near_silent_take():
    # ~-69 dBFS: reaching -20 dB would need ~49 dB of gain, which would lift noise
    # to the level of the words. The cap has to win.
    processed, info = normalize_rms(
        _sine(0.5, amplitude=0.0005),
        target_rms_db=-20.0,
        max_gain_db=12.0,
    )

    assert info["applied"] is True
    assert info["gain_capped"] is True
    assert info["gain_db"] == pytest.approx(12.0)
    assert info["requested_gain_db"] > 40.0
    assert info["rms_db_after"] == pytest.approx(info["rms_db_before"] + 12.0, abs=0.1)
    assert float(np.max(np.abs(processed))) < 0.01


def test_normalize_rms_skips_all_zero_input():
    processed, info = normalize_rms(np.zeros(128, dtype=np.float32))

    assert info["applied"] is False
    assert info["skipped_reason"] == "buffer is silent"
    assert info["rms_db_before"] is None
    assert not np.any(processed)


def test_trim_silence_drops_edges_but_keeps_padding():
    samples = np.concatenate(
        [
            np.zeros(500, dtype=np.float32),
            _sine(1.0),
            np.zeros(700, dtype=np.float32),
        ]
    )

    processed, info = trim_silence(
        samples,
        _RATE,
        threshold_db=-45.0,
        keep_padding_seconds=0.05,
    )

    assert info["applied"] is True
    # 0.5 s of leading silence minus the 0.05 s of padding that is kept.
    assert info["trimmed_leading_seconds"] == pytest.approx(0.45, abs=0.01)
    assert info["trimmed_trailing_seconds"] == pytest.approx(0.65, abs=0.01)
    assert 1_050 <= processed.size <= 1_150
    assert info["duration_seconds_after"] == pytest.approx(processed.size / _RATE)


def test_trim_silence_keeps_an_all_zero_buffer_and_reports_why():
    samples = np.zeros(400, dtype=np.float32)

    processed, info = trim_silence(samples, _RATE)

    assert info["applied"] is False
    assert info["skipped_reason"] == "no sample above threshold"
    assert processed.size == 400

    empty, empty_info = trim_silence(np.zeros(0, dtype=np.float32), _RATE)
    assert empty.size == 0
    assert empty_info["skipped_reason"] == "empty buffer"


def test_trim_silence_rejects_a_non_positive_sample_rate():
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        trim_silence(_sine(0.1), 0)


def test_apply_fades_ramps_both_edges():
    samples = np.ones(100, dtype=np.float32)

    processed, info = apply_fades(
        samples,
        _RATE,
        fade_in_seconds=0.02,
        fade_out_seconds=0.08,
    )

    assert info["fade_in_samples"] == 20
    assert info["fade_out_samples"] == 80
    assert processed[0] == pytest.approx(0.0)
    assert processed[-1] == pytest.approx(0.0)
    assert np.all(np.diff(processed[:20]) > 0)
    assert np.all(np.diff(processed[20:]) < 0)


def test_apply_fades_shrinks_overlapping_ramps_instead_of_multiplying_them():
    processed, info = apply_fades(
        np.ones(10, dtype=np.float32),
        _RATE,
        fade_in_seconds=0.01,
        fade_out_seconds=0.01,
    )

    assert info["fade_in_samples"] == 5
    assert info["fade_out_samples"] == 5
    # The two ramps meet exactly once instead of overlapping and notching the clip.
    assert float(processed.max()) == pytest.approx(1.0, abs=1e-6)


def test_apply_fades_skips_an_empty_buffer():
    processed, info = apply_fades(np.zeros(0, dtype=np.float32), _RATE)

    assert processed.size == 0
    assert info["applied"] is False
    assert info["skipped_reason"] == "empty buffer"


def test_duck_lowers_music_only_inside_narration_spans():
    music = np.full(4_000, 0.5, dtype=np.float32)
    narration = np.zeros(4_000, dtype=np.float32)
    narration[2_000:3_000] = _sine(1.0)

    ducked, info = duck(
        music,
        narration,
        _RATE,
        reduction_db=-12.0,
        attack_seconds=0.15,
        release_seconds=0.4,
        threshold_db=-40.0,
    )

    gain = ducked / 0.5
    assert info["applied"] is True
    assert info["ducked_spans"] == 1
    assert info["min_gain_db"] == pytest.approx(-12.0, abs=0.1)

    # Untouched well before the attack ramp and well after the release ramp.
    assert np.allclose(gain[:1_800], 1.0, atol=1e-6)
    assert np.allclose(gain[3_600:], 1.0, atol=1e-6)
    # Fully ducked inside the phrase.
    assert np.allclose(gain[2_200:2_800], 0.2512, atol=0.005)
    assert _rms(ducked[2_200:2_800]) == pytest.approx(
        _rms(music[2_200:2_800]) * 0.2512, rel=0.02
    )


def test_duck_ramps_the_gain_at_both_edges():
    music = np.full(4_000, 0.5, dtype=np.float32)
    narration = np.zeros(4_000, dtype=np.float32)
    narration[2_000:3_000] = _sine(1.0)

    ducked, _ = duck(music, narration, _RATE)
    gain = ducked / 0.5

    # Mid-attack the gain is on its way down, not at either end point.
    assert 0.2512 < gain[1_900] < 1.0
    assert np.all(np.diff(gain[1_800:1_990]) <= 1e-6)
    # Mid-release it is on its way back up.
    assert 0.2512 < gain[3_200] < 1.0
    assert np.all(np.diff(gain[3_050:3_400]) >= -1e-6)


def test_duck_is_a_no_op_for_empty_or_silent_narration():
    music = np.full(1_000, 0.4, dtype=np.float32)

    unchanged, info = duck(music, np.zeros(0, dtype=np.float32), _RATE)
    assert np.array_equal(unchanged, music)
    assert info["skipped_reason"] == "empty narration buffer"

    unchanged, info = duck(music, np.zeros(1_000, dtype=np.float32), _RATE)
    assert np.array_equal(unchanged, music)
    assert info["skipped_reason"] == "no narration above threshold"

    empty_music, info = duck(np.zeros(0, dtype=np.float32), _sine(0.5), _RATE)
    assert empty_music.size == 0
    assert info["skipped_reason"] == "empty music buffer"


def test_duck_applies_exactly_the_shared_envelope():
    """duck() must be the envelope and nothing else, so both callers agree."""

    music = np.full(4_000, 0.5, dtype=np.float32)
    narration = np.zeros(4_000, dtype=np.float32)
    narration[2_000:3_000] = _sine(1.0)

    ducked, info = duck(music, narration, _RATE, reduction_db=-9.0)
    envelope = duck_envelope(
        [(2_000, 3_000)], music.size, _RATE, reduction_db=-9.0
    )

    assert info["ducked_spans"] == 1
    # The span boundaries the level follower finds may sit a few samples wide of
    # the phrase, so compare where the curve is settled rather than sample-exact.
    assert np.allclose(ducked[2_200:2_800], music[2_200:2_800] * envelope[2_200:2_800])
    assert np.allclose(ducked[:1_700], music[:1_700] * envelope[:1_700])


def test_duck_envelope_keeps_the_ramp_slope_when_a_span_starts_early():
    """A span nearer the head than the attack gets a shorter dip, same slope."""

    attack_seconds = 0.4
    envelope = duck_envelope(
        [(100, 400)],
        1_000,
        _RATE,
        reduction_db=-12.0,
        attack_seconds=attack_seconds,
        release_seconds=0.1,
    )
    full = duck_envelope(
        [(400, 700)],
        1_000,
        _RATE,
        reduction_db=-12.0,
        attack_seconds=attack_seconds,
        release_seconds=0.1,
    )

    # Truncating the ramp must not steepen it: the clipped attack is the tail of
    # the full one, so the buffer opens part-way down instead of at unity.
    assert envelope[0] < 1.0
    assert np.allclose(envelope[:100], full[300:400])
    assert np.allclose(envelope[100:400], 0.2512, atol=0.005)


def test_duck_envelope_clips_spans_to_the_buffer():
    envelope = duck_envelope(
        [(-500, 200), (900, 5_000), (5_000, 6_000)],
        1_000,
        _RATE,
        reduction_db=-12.0,
        attack_seconds=0.05,
        release_seconds=0.05,
    )

    assert envelope.shape == (1_000,)
    assert np.allclose(envelope[:200], 0.2512, atol=0.005)
    # A span running past the end stays ducked to the last sample: there is no
    # room left to release into.
    assert np.allclose(envelope[900:], 0.2512, atol=0.005)
    assert float(envelope[500]) == pytest.approx(1.0)


def test_duck_envelope_is_flat_without_spans():
    envelope = duck_envelope([], 500, _RATE)

    assert envelope.shape == (500,)
    assert np.array_equal(envelope, np.ones(500, dtype=np.float32))
    assert duck_envelope([(0, 10)], 0, _RATE).size == 0


def test_process_audio_runs_the_speech_chain_and_stays_json_safe():
    processed, applied = process_audio(_sine(1.0, amplitude=0.2), _RATE, preset="speech")

    assert applied["preset"] == "speech"
    assert applied["chain"] == [
        "trim_silence",
        "normalize_rms",
        "apply_fades",
        "normalize_peak",
    ]
    assert float(np.max(np.abs(processed))) == pytest.approx(0.8913, abs=0.01)
    # The chain is recorded in job metadata, so it must survive JSON serialization.
    assert json.loads(json.dumps(applied))["chain"] == applied["chain"]


def test_process_audio_runs_the_music_chain_without_trimming():
    processed, applied = process_audio(_sine(1.0, amplitude=0.2), _RATE, preset="music")

    assert applied["chain"] == ["normalize_rms", "apply_fades", "normalize_peak"]
    assert processed.size == _RATE  # nothing was trimmed away
    assert applied["duration_seconds_after"] == pytest.approx(1.0)


def test_process_audio_music_chain_does_not_amplify_near_silent_noise_past_the_rms_cap():
    """A buffer whose RMS boost gets capped must not be un-capped by peak.

    The RMS stage's max_gain_db cap exists specifically to leave anomalously
    quiet content (model noise, not real signal) too quiet rather than
    amplifying it. Without the fix, the peak stage running last scales
    straight up to the peak target regardless of how quiet the RMS stage
    left the buffer, silently undoing that protection.
    """

    near_silent = np.full(4_000, 2e-6, dtype=np.float32)
    processed, applied = process_audio(near_silent, _RATE, preset="music")

    rms_step, _fade_step, peak_step = applied["steps"]
    assert rms_step["gain_capped"] is True
    assert peak_step["applied"] is False
    assert peak_step["skipped_reason"] == (
        "attenuate_only: buffer is already below target peak"
    )
    assert float(np.max(np.abs(processed))) < 0.001


def test_process_music_channels_preserves_relative_transient_dynamics_after_a_capped_boost():
    """A capped RMS boost can push distinct transients above 1.0; clipping
    right there would flatten them together before the peak stage gets a
    chance to measure the true peak and scale everything back down
    proportionally, which is what actually preserves their dynamics.
    """

    samples = np.zeros(4_000, dtype=np.float32)
    samples[1_000] = 0.8
    samples[2_000] = 0.9

    processed, applied = process_music_channels(samples, _RATE)

    rms_step = applied["steps"][0]
    # -34 dB RMS from these two spikes needs +16 dB to reach the -18 dB
    # target, exceeding normalize_rms's default 12 dB cap.
    assert rms_step["gain_capped"] is True

    quieter_peak = float(np.max(np.abs(processed[900:1_100])))
    louder_peak = float(np.max(np.abs(processed[1_900:2_100])))
    assert louder_peak == pytest.approx(0.8913, abs=0.01)  # hits the -1 dB target
    # A premature clip after the RMS boost would flatten both transients to
    # the same value (ratio 1.0); the ratio should instead track the
    # original 0.8:0.9 samples.
    assert louder_peak / quieter_peak == pytest.approx(0.9 / 0.8, abs=0.02)


def test_process_audio_handles_empty_and_all_zero_buffers():
    empty, applied = process_audio(np.zeros(0, dtype=np.float32), _RATE, preset="speech")
    assert empty.size == 0
    assert all(step["applied"] is False for step in applied["steps"])

    silence, applied = process_audio(np.zeros(500, dtype=np.float32), _RATE, preset="speech")
    assert silence.size == 500
    assert not np.any(silence)
    assert applied["steps"][1]["skipped_reason"] == "buffer is silent"


def test_process_audio_rejects_an_unknown_preset():
    with pytest.raises(ValueError, match="narration"):
        process_audio(_sine(0.1), _RATE, preset="narration")


def test_post_processing_rejects_multichannel_input():
    with pytest.raises(ValueError, match="must be mono"):
        normalize_peak(np.zeros((2, 100), dtype=np.float32))


# --------------------------------------------------------------------------
# Sentence splitting
# --------------------------------------------------------------------------


def test_japanese_text_splits_on_full_width_terminators():
    sentences = split_into_sentences(
        "こんにちは。今日はいい天気ですね！散歩に行きませんか？"
    )

    assert sentences == [
        "こんにちは。",
        "今日はいい天気ですね！",
        "散歩に行きませんか？",
    ]


def test_japanese_closing_quote_stays_with_its_sentence():
    assert split_into_sentences("彼は「行こう。」と言った。") == [
        "彼は「行こう。」",
        "と言った。",
    ]


def test_english_text_splits_on_latin_terminators_but_not_decimals():
    sentences = split_into_sentences(
        "The lab is quiet. It is 3.5 degrees outside! Shall we go?"
    )

    assert sentences == [
        "The lab is quiet.",
        "It is 3.5 degrees outside!",
        "Shall we go?",
    ]


def test_line_breaks_are_treated_as_boundaries():
    assert split_into_sentences("第一幕\n第二幕\n") == ["第一幕", "第二幕"]


def test_chunks_group_whole_sentences_under_the_budget():
    text = "こんにちは。今日はいい天気ですね。散歩に行きませんか。"

    chunks = split_into_chunks(text, max_characters=24)

    assert chunks == ["こんにちは。今日はいい天気ですね。", "散歩に行きませんか。"]
    assert all(len(chunk) <= 24 for chunk in chunks)


def test_english_chunks_keep_the_word_space_between_sentences():
    chunks = split_into_chunks("One two. Three four.", max_characters=64)

    assert chunks == ["One two. Three four."]


def test_an_over_budget_sentence_is_split_at_clause_separators():
    text = "朝の光が差し込み、風が静かに動き、遠くで鐘が鳴り、街が目を覚ました。"

    chunks = split_into_chunks(text, max_characters=18)

    assert len(chunks) > 1
    assert all(len(chunk) <= 18 for chunk in chunks)
    # Nothing is lost: the chunks still contain every non-separator character.
    assert "".join(chunks).replace(" ", "") == text


def test_a_sentence_without_any_boundary_is_hard_cut_at_the_budget():
    chunks = split_into_chunks("あ" * 25, max_characters=10)

    assert chunks == ["あ" * 10, "あ" * 10, "あ" * 5]


def test_an_over_budget_english_sentence_prefers_word_boundaries():
    chunks = split_into_chunks(
        "alpha beta gamma delta epsilon zeta.",
        max_characters=12,
    )

    assert chunks == ["alpha beta", "gamma delta", "epsilon", "zeta."]
    assert all(len(chunk) <= 12 for chunk in chunks)


def test_blank_text_produces_no_chunks():
    assert split_into_chunks("   \n  ") == []


def test_chunk_budget_must_be_positive():
    with pytest.raises(ValueError, match="max_characters must be at least 1"):
        split_into_chunks("text", max_characters=0)


# --------------------------------------------------------------------------
# SpeechGenerator
# --------------------------------------------------------------------------


class _FakeModelService:
    """Minimal ModelService stand-in returning a fixed manifest and runtime."""

    def __init__(self, manifest, runtime_obj) -> None:
        self._manifest = manifest
        self._runtime_obj = runtime_obj
        self.resolved_with: tuple | None = None

    def resolve_runtime(self, model_id, media_type, task_type=None):
        self.resolved_with = (model_id, media_type, task_type)
        return self._manifest, self._runtime_obj

    def get_manifest(self, model_id, media_type, task_type=None):
        return self._manifest


def _kokoro_manifest():
    registry = ModelRegistry()
    registry.load_all()
    return registry.get("kokoro-tts-local")


def _fake_speech_runtime(
    *,
    endpoint_base_url: str | None = None,
    sample_rates: list[int] | None = None,
) -> dict:
    """A runtime whose synthesize() honours the documented contract deterministically."""

    calls: list[dict] = []
    rates = iter(sample_rates) if sample_rates is not None else None

    def synthesize(text, *, voice=None, speed=1.0, pitch=0.0):
        calls.append({"text": text, "voice": voice, "speed": speed, "pitch": pitch})
        rate = next(rates) if rates is not None else _FAKE_TTS_RATE
        samples = _sine(
            _SECONDS_PER_CHARACTER * len(text),
            sample_rate=rate,
            frequency=180.0,
            amplitude=0.4,
        )
        return samples, rate

    runtime = {
        "synthesize": synthesize,
        "voices": ["jf_alpha"],
        "default_voice": "jf_alpha",
        "default_speed": 1.0,
        "device": "cpu",
        "sample_rate": _FAKE_TTS_RATE,
        "calls": calls,
    }
    if endpoint_base_url is not None:
        runtime["endpoint_base_url"] = endpoint_base_url
    return runtime


def _speech_request(**params) -> GenerationRequest:
    return GenerationRequest(
        media_type="audio",
        task_type="text-to-speech",
        prompt=(
            "静かな朝だった。少女は窓を開けて空を見上げた。"
            "遠くで鐘が鳴っている。今日が最後の一日になる。"
        ),
        model_id="kokoro-tts",
        params={"max_chunk_characters": 24, "chunk_gap_seconds": 0.2, **params},
    )


def test_speech_generator_writes_a_wav_and_records_the_chain(tmp_path: Path):
    runtime = _fake_speech_runtime()
    service = _FakeModelService(_kokoro_manifest(), runtime)
    generator = SpeechGenerator(service, output_dir=tmp_path)

    result = generator.run(_speech_request(source_asset_id="ast_123"))

    assert service.resolved_with == ("kokoro-tts", "audio", "text-to-speech")
    assert result.status == "succeeded"

    output_path = Path(result.outputs[0])
    assert output_path.exists()
    assert output_path.suffix == ".wav"
    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == _FAKE_TTS_RATE
        assert wav_file.getnframes() > 0

    metadata = result.metadata
    assert metadata["chunk_count"] == len(runtime["calls"]) > 1
    assert metadata["voice"] == "jf_alpha"
    assert metadata["speed"] == 1.0
    assert metadata["pitch"] == 0.0
    assert metadata["sample_rate"] == _FAKE_TTS_RATE
    assert metadata["channels"] == 1
    assert metadata["output_format"] == "wav"
    assert metadata["endpoint_base_url"] is None
    assert metadata["available_voices"] == ["jf_alpha"]
    assert metadata["supports_pitch"] is False
    assert metadata["source_asset_id"] == "ast_123"
    assert metadata["params"]["postprocess"] is True
    assert metadata["audio_postprocess"]["preset"] == "speech"
    assert metadata["audio_postprocess"]["enabled"] is True
    assert metadata["audio_postprocess"]["chain"] == [
        "trim_silence",
        "normalize_rms",
        "apply_fades",
        "normalize_peak",
    ]
    assert metadata["quality_report"]["method"] == "heuristic_local_v1"
    assert isinstance(metadata["quality_report"]["quality_score"], float)
    # The whole payload is persisted as JSON by the job store.
    assert json.loads(json.dumps(metadata))["chunk_count"] == metadata["chunk_count"]


def test_speech_generator_can_disable_postprocessing(tmp_path: Path):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    result = generator.run(_speech_request(postprocess=False))

    metadata = result.metadata
    assert metadata["params"]["postprocess"] is False
    assert metadata["audio_postprocess"]["preset"] == "speech"
    assert metadata["audio_postprocess"]["enabled"] is False
    assert metadata["audio_postprocess"]["chain"] == []

    output_path = Path(result.outputs[0])
    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnframes() > 0


def test_speech_generator_rejects_non_boolean_postprocess(tmp_path: Path):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="postprocess"):
        generator.run(_speech_request(postprocess="false"))


def test_speech_generator_rejects_invalid_manifest_postprocess_default(tmp_path: Path):
    # A request that doesn't set postprocess must still catch an invalid
    # manifest default at validate_request() time rather than only failing
    # once the worker has resolved the runtime and started the job.
    manifest = _kokoro_manifest().model_copy(
        update={"default_params": {"postprocess": "not-a-bool"}}
    )
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(manifest, runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="postprocess"):
        generator.validate_request(_speech_request())


def test_speech_generator_defers_to_generation_time_when_manifest_is_unresolvable(
    tmp_path: Path,
):
    # A model that can't be resolved (unknown, disabled, wrong task type) is
    # left for resolve_runtime() to reject at generation time, as before;
    # validate_request() must not raise here just because the lookup failed.
    class _UnresolvableModelService:
        def get_manifest(self, model_id, media_type, task_type=None):
            raise LookupError(f"Model is disabled: {model_id}")

    generator = SpeechGenerator(_UnresolvableModelService(), output_dir=tmp_path)

    generator.validate_request(_speech_request())


def test_speech_generator_rejects_a_cloud_manifest_missing_the_speech_capability(
    tmp_path: Path,
):
    # #234's cloud-provider capability guard (AudioGenerator, music) had no
    # equivalent on SpeechGenerator, so a provider: cloud manifest that never
    # declared support was silently accepted for narration. Mirrors
    # AudioGenerator's own guard: reject before generate() resolves anything.
    manifest = _kokoro_manifest().model_copy(
        update={"provider": "cloud", "default_params": {}}
    )
    generator = SpeechGenerator(
        _FakeModelService(manifest, _fake_speech_runtime()),
        output_dir=tmp_path,
    )

    with pytest.raises(AudioProviderError, match="capabilit"):
        generator.validate_request(_speech_request())


def test_speech_generator_accepts_a_cloud_manifest_declaring_the_speech_capability(
    tmp_path: Path,
):
    manifest = _kokoro_manifest().model_copy(
        update={
            "provider": "cloud",
            "default_params": {"capabilities": ["text-to-speech"]},
        }
    )
    generator = SpeechGenerator(
        _FakeModelService(manifest, _fake_speech_runtime()),
        output_dir=tmp_path,
    )

    # Must not raise.
    generator.validate_request(_speech_request())


def test_speech_generator_splits_at_sentences_and_pads_between_chunks(tmp_path: Path):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    result = generator.run(_speech_request())

    # Four sentences, packed two per chunk by the 24 character budget, and every
    # chunk ends on a sentence boundary rather than mid-phrase.
    chunk_texts = [call["text"] for call in runtime["calls"]]
    assert len(chunk_texts) == 2
    assert all(chunk.endswith("。") for chunk in chunk_texts)
    assert all(len(chunk) <= 24 for chunk in chunk_texts)
    assert "".join(chunk_texts) == _speech_request().prompt

    speech_seconds = sum(_SECONDS_PER_CHARACTER * len(chunk) for chunk in chunk_texts)
    gap_seconds = 0.2 * (len(chunk_texts) - 1)
    assert result.metadata["duration_seconds_generated"] == pytest.approx(
        speech_seconds + gap_seconds, abs=0.02
    )


def test_speech_generator_records_the_endpoint_it_used(tmp_path: Path):
    runtime = _fake_speech_runtime(endpoint_base_url="http://127.0.0.1:50021")
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    result = generator.run(_speech_request())

    assert result.metadata["endpoint_base_url"] == "http://127.0.0.1:50021"


def test_speech_generator_forwards_voice_speed_and_pitch(tmp_path: Path):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    result = generator.run(_speech_request(voice="jm_kumo", speed=1.15, pitch=0.05))

    assert {call["voice"] for call in runtime["calls"]} == {"jm_kumo"}
    assert {call["speed"] for call in runtime["calls"]} == {1.15}
    assert {call["pitch"] for call in runtime["calls"]} == {0.05}
    assert result.metadata["params"]["voice"] == "jm_kumo"
    assert result.metadata["speed"] == 1.15


def test_speech_generator_rejects_mixed_sample_rates(tmp_path: Path):
    runtime = _fake_speech_runtime(sample_rates=[_FAKE_TTS_RATE, 22_050])
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="single sample rate"):
        generator.run(_speech_request())


def test_speech_generator_requires_a_synthesize_runtime(tmp_path: Path):
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), {"device": "cpu"}),
        output_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="exposes no synthesize"):
        generator.run(_speech_request())


@pytest.mark.parametrize(
    ("params", "expected_message"),
    [
        ({"speed": 0}, "speed must be positive"),
        ({"speed": float("inf")}, "speed must be positive"),
        ({"pitch": float("nan")}, "pitch must be finite"),
        ({"max_chunk_characters": 0}, "must be at least 1"),
        ({"chunk_gap_seconds": -0.1}, "must be non-negative"),
        ({"chunk_gap_seconds": float("inf")}, "must be non-negative"),
    ],
)
def test_speech_generator_rejects_invalid_controls_before_synthesis(
    tmp_path: Path,
    params,
    expected_message,
):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match=expected_message):
        generator.run(_speech_request(**params))
    assert runtime["calls"] == []


@pytest.mark.parametrize(
    ("audio", "expected_message"),
    [
        (np.zeros((2, 10), dtype=np.float32), "requires mono"),
        (np.array([0.0, np.nan], dtype=np.float32), "non-finite"),
    ],
)
def test_speech_generator_rejects_invalid_runtime_audio(
    tmp_path: Path,
    audio,
    expected_message,
):
    def synthesize(text, **kwargs):
        return audio, _FAKE_TTS_RATE

    runtime = {
        **_fake_speech_runtime(),
        "synthesize": synthesize,
    }
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match=expected_message):
        generator.run(_speech_request())


@pytest.mark.parametrize(
    ("request_kwargs", "expected_message"),
    [
        ({"media_type": "image"}, "only supports audio"),
        ({"prompt": "   "}, "must not be empty"),
        ({"output_format": "mp3"}, "wav output only"),
    ],
)
def test_speech_generator_validation_rejections(tmp_path, request_kwargs, expected_message):
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), _fake_speech_runtime()),
        output_dir=tmp_path,
    )
    payload = {
        "media_type": "audio",
        "task_type": "text-to-speech",
        "prompt": "ナレーションの本文。",
        "model_id": "kokoro-tts",
        **request_kwargs,
    }

    with pytest.raises(ValueError, match=expected_message):
        generator.validate_request(GenerationRequest(**payload))


def test_speech_generator_rejects_an_overlong_prompt_before_loading_a_runtime(
    tmp_path: Path,
):
    runtime = _fake_speech_runtime()
    service = _FakeModelService(_kokoro_manifest(), runtime)
    generator = SpeechGenerator(service, output_dir=tmp_path)
    request = GenerationRequest(
        media_type="audio",
        task_type="text-to-speech",
        prompt="あ" * 20_001,
        model_id="kokoro-tts",
    )

    with pytest.raises(ValueError, match="20000 character limit"):
        generator.run(request)
    assert service.resolved_with is None
    assert runtime["calls"] == []


@pytest.mark.parametrize(
    ("params", "expected_message"),
    [
        ({"speed": 0.49}, "between 0.5 and 2.0"),
        ({"speed": 2.01}, "between 0.5 and 2.0"),
        ({"pitch": -0.151}, "between -0.15 and 0.15"),
        ({"pitch": 0.151}, "between -0.15 and 0.15"),
        ({"max_chunk_characters": 2_001}, "must not exceed 2000"),
        ({"chunk_gap_seconds": 2.01}, "must not exceed 2.0"),
    ],
)
def test_speech_generator_enforces_control_upper_and_lower_bounds(
    tmp_path: Path,
    params,
    expected_message,
):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match=expected_message):
        generator.run(_speech_request(**params))
    assert runtime["calls"] == []


def test_speech_generator_caps_chunk_fanout_before_synthesis(tmp_path: Path):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )
    request = GenerationRequest(
        media_type="audio",
        task_type="text-to-speech",
        prompt="あ" * 129,
        model_id="kokoro-tts",
        params={"max_chunk_characters": 1, "chunk_gap_seconds": 0},
    )

    with pytest.raises(ValueError, match="128 chunk limit"):
        generator.run(request)
    assert runtime["calls"] == []


def test_speech_generator_rejects_estimated_audio_over_ten_minutes(
    tmp_path: Path,
):
    runtime = _fake_speech_runtime()
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )
    request = GenerationRequest(
        media_type="audio",
        task_type="text-to-speech",
        prompt="あ" * 3_601,
        model_id="kokoro-tts",
        params={"max_chunk_characters": 2_000, "chunk_gap_seconds": 0},
    )

    with pytest.raises(ValueError, match="600 second output limit"):
        generator.run(request)
    assert runtime["calls"] == []


def test_estimated_limit_does_not_depend_on_loader_sample_rate_metadata(
    tmp_path: Path,
):
    runtime = _fake_speech_runtime()
    runtime.pop("sample_rate")
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )
    request = GenerationRequest(
        media_type="audio",
        task_type="text-to-speech",
        prompt="あ" * 3_601,
        model_id="kokoro-tts",
        params={"max_chunk_characters": 2_000, "chunk_gap_seconds": 0},
    )

    with pytest.raises(ValueError, match="600 second output limit"):
        generator.run(request)
    assert runtime["calls"] == []


def test_speech_generator_counts_gaps_before_allocating_join_buffer(
    tmp_path: Path,
):
    calls: list[str] = []

    def synthesize(text, **kwargs):
        calls.append(text)
        return np.zeros(2_991, dtype=np.float32), 10

    runtime = {
        **_fake_speech_runtime(),
        "sample_rate": 10,
        "synthesize": synthesize,
    }
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )
    request = GenerationRequest(
        media_type="audio",
        task_type="text-to-speech",
        prompt="一。二。",
        model_id="kokoro-tts",
        params={"max_chunk_characters": 2, "chunk_gap_seconds": 2},
    )

    # 2 * 2,991 speech samples + 20 gap samples exceeds 10 Hz * 600 s.
    with pytest.raises(RuntimeError, match="including gaps exceeds"):
        generator.run(request)
    assert calls == ["一。", "二。"]
    assert list(tmp_path.glob("*.wav")) == []


def test_speech_generator_rejects_unsafe_sample_rate_before_gap_allocation(
    tmp_path: Path,
):
    runtime = _fake_speech_runtime()
    runtime["sample_rate"] = 96_001
    generator = SpeechGenerator(
        _FakeModelService(_kokoro_manifest(), runtime),
        output_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="96000 Hz speech limit"):
        generator.run(_speech_request())
    assert runtime["calls"] == []


# --------------------------------------------------------------------------
# Runtimes and the loopback guard
# --------------------------------------------------------------------------


def test_loopback_audio_endpoints_are_allowed(monkeypatch):
    monkeypatch.delenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", raising=False)

    assert resolve_audio_endpoint("http://127.0.0.1:50021/") == "http://127.0.0.1:50021"
    assert resolve_audio_endpoint("http://localhost:50021") == "http://localhost:50021"


def test_speech_manifests_and_loaders_are_registered():
    registry = ModelRegistry()
    registry.load_all()

    kokoro = registry.get("kokoro-tts-local")
    voicevox = registry.get("voicevox-endpoint")
    assert kokoro.task_type == "text-to-speech"
    assert kokoro.default_params["language"] == "ja"
    assert kokoro.loader == "kokoro_tts_loader"
    assert voicevox.task_type == "text-to-speech"
    assert voicevox.remote_ref == "http://127.0.0.1:50021"
    assert voicevox.loader == "voicevox_http_loader"
    assert voicevox.enabled is True
    assert voicevox.is_default is True

    loaders = create_default_loader_registry()
    assert loaders.get("kokoro_tts_loader").__class__.__name__ == "KokoroTtsLoader"
    assert loaders.get("voicevox_http_loader").__class__.__name__ == "VoicevoxHttpLoader"


def test_non_loopback_audio_endpoint_is_refused_without_the_flag(monkeypatch):
    monkeypatch.delenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", raising=False)
    # Allowing remote *text* endpoints must not silently allow shipping narration
    # text to a remote speech engine: the opt-ins are deliberately separate.
    monkeypatch.setenv("ALLOW_REMOTE_TEXT_ENDPOINTS", "true")

    with pytest.raises(ValueError, match="ALLOW_REMOTE_AUDIO_ENDPOINTS"):
        resolve_audio_endpoint("http://192.168.1.50:50021")


def test_non_loopback_audio_endpoint_is_allowed_with_the_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", "true")

    assert resolve_audio_endpoint("http://192.168.1.50:50021") == "http://192.168.1.50:50021"


def test_audio_endpoint_scheme_and_emptiness_are_checked(monkeypatch):
    monkeypatch.setenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", "true")

    with pytest.raises(ValueError, match="must not be empty"):
        resolve_audio_endpoint("  ")
    with pytest.raises(ValueError, match="http or https"):
        resolve_audio_endpoint("ws://127.0.0.1:50021")
    with pytest.raises(ValueError, match="include a host"):
        resolve_audio_endpoint("http:///50021")


@pytest.mark.parametrize(
    ("endpoint", "expected_message"),
    [
        ("http://user:secret@127.0.0.1:50021", "must not include userinfo"),
        ("http://127.0.0.1:50021?token=secret", "must not include a query"),
        ("http://127.0.0.1:50021?", "must not include a query"),
        ("http://127.0.0.1:50021#secret", "must not include a fragment"),
        ("http://127.0.0.1:50021#", "must not include a fragment"),
    ],
)
def test_audio_endpoint_rejects_metadata_secrets(
    monkeypatch,
    endpoint,
    expected_message,
):
    monkeypatch.delenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", raising=False)

    with pytest.raises(ValueError, match=expected_message):
        resolve_audio_endpoint(endpoint)


@pytest.mark.skipif(
    importlib.util.find_spec("kokoro") is not None,
    reason="kokoro is installed, so the dependency guard cannot trigger",
)
def test_kokoro_runtime_names_the_install_command_when_the_package_is_missing():
    with pytest.raises(RuntimeError, match="pip install kokoro"):
        build_kokoro_runtime(language="ja")


def test_kokoro_language_codes_cover_japanese_and_english():
    assert resolve_kokoro_language_code("ja") == "j"
    assert resolve_kokoro_language_code("Japanese") == "j"
    assert resolve_kokoro_language_code("en-us") == "a"
    assert resolve_kokoro_language_code("en-gb") == "b"

    with pytest.raises(ValueError, match="Unsupported kokoro language"):
        resolve_kokoro_language_code("kr")


def test_kokoro_runtime_uses_local_model_and_voice_pack(monkeypatch, tmp_path: Path):
    calls: list[dict] = []

    class FakeModel:
        def __init__(self, **kwargs):
            calls.append({"kind": "model", **kwargs})

        def to(self, device):
            calls.append({"kind": "to", "device": device})
            return self

        def eval(self):
            calls.append({"kind": "eval"})
            return self

    class FakePipeline:
        def __init__(self, **kwargs):
            calls.append({"kind": "pipeline", **kwargs})

        def __call__(self, text, **kwargs):
            calls.append({"kind": "synthesize", "text": text, **kwargs})
            return [SimpleNamespace(audio=np.array([0.1, -0.1], dtype=np.float32))]

    fake_kokoro = ModuleType("kokoro")
    fake_kokoro.KModel = FakeModel
    fake_kokoro.KPipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)

    model_path = tmp_path / "kokoro"
    (model_path / "voices").mkdir(parents=True)
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    (model_path / "kokoro-v1_0.pth").write_bytes(b"fake")
    (model_path / "voices" / "jf_alpha.pt").write_bytes(b"fake")

    runtime = build_kokoro_runtime(
        model_path=model_path,
        language="ja",
        default_voice="jf_alpha",
        voices=["jf_alpha"],
        device="cpu",
    )
    samples, rate = runtime["synthesize"](
        "こんにちは。",
        voice="jf_alpha",
        speed=1.1,
    )

    model_call = next(call for call in calls if call["kind"] == "model")
    assert model_call["config"] == str(model_path / "config.json")
    assert model_call["model"] == str(model_path / "kokoro-v1_0.pth")
    assert {"kind": "to", "device": "cpu"} in calls
    synthesis_call = next(call for call in calls if call["kind"] == "synthesize")
    assert synthesis_call["text"] == "こんにちは。"
    assert synthesis_call["voice"] == str(
        (model_path / "voices" / "jf_alpha.pt").resolve()
    )
    assert synthesis_call["speed"] == pytest.approx(1.1)
    assert samples.dtype == np.float32
    assert rate == _FAKE_TTS_RATE
    assert runtime["language_code"] == "j"
    assert runtime["device"] == "cpu"

    with pytest.raises(ValueError, match="no pitch control"):
        runtime["synthesize"]("こんにちは。", pitch=0.1)
    with pytest.raises(ValueError, match="positive finite"):
        runtime["synthesize"]("こんにちは。", speed=0)


def test_kokoro_runtime_refuses_to_download_a_missing_voice(monkeypatch, tmp_path: Path):
    class FakeModel:
        def __init__(self, **kwargs):
            pass

        def to(self, device):
            return self

        def eval(self):
            return self

    fake_kokoro = ModuleType("kokoro")
    fake_kokoro.KModel = FakeModel
    fake_kokoro.KPipeline = lambda **kwargs: pytest.fail(
        "pipeline must not be created when the local voice is missing"
    )
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)

    model_path = tmp_path / "kokoro"
    model_path.mkdir()
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    (model_path / "kokoro-v1_0.pth").write_bytes(b"fake")
    runtime = build_kokoro_runtime(
        model_path=model_path,
        default_voice="jf_alpha",
        device="cpu",
    )

    with pytest.raises(FileNotFoundError, match="voice pack"):
        runtime["synthesize"]("こんにちは。")


def _wav_bytes(samples: np.ndarray, sample_rate: int, *, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes((np.clip(samples, -1.0, 1.0) * 32_767).astype("<i2").tobytes())
    return buffer.getvalue()


def test_decode_wav_bytes_returns_mono_float32_samples():
    samples = _sine(0.25, sample_rate=8_000, amplitude=0.5)

    decoded, rate = decode_wav_bytes(_wav_bytes(samples, 8_000))

    assert rate == 8_000
    assert decoded.dtype == np.float32
    assert decoded.shape == samples.shape
    assert np.allclose(decoded, samples, atol=1e-4)


def test_to_mono_float32_downmixes_a_stereo_buffer():
    stereo = np.stack([np.full(10, 0.5), np.full(10, -0.1)]).astype(np.float32)

    mono = to_mono_float32(stereo)

    assert mono.shape == (10,)
    assert np.allclose(mono, 0.2, atol=1e-6)


class _FakeHttpResponse:
    def __init__(self, *, json_body=None, content: bytes = b"") -> None:
        self._json_body = json_body
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._json_body


def test_voicevox_runtime_resolves_speakers_and_applies_speed_and_pitch(monkeypatch):
    import httpx

    posted: list[dict] = []
    engine_wav = _wav_bytes(_sine(0.2, sample_rate=_FAKE_TTS_RATE), _FAKE_TTS_RATE)

    def fake_get(url, **kwargs):
        assert url.endswith("/speakers")
        return _FakeHttpResponse(
            json_body=[
                {"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3}]},
            ]
        )

    def fake_post(url, **kwargs):
        posted.append({"url": url, **kwargs})
        if url.endswith("/audio_query"):
            return _FakeHttpResponse(json_body={"speedScale": 1.0, "pitchScale": 0.0})
        return _FakeHttpResponse(content=engine_wav)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", raising=False)

    runtime = build_voicevox_runtime("http://127.0.0.1:50021", default_speaker_id=3)

    assert runtime["endpoint_base_url"] == "http://127.0.0.1:50021"
    assert runtime["voices"] == ["3 ずんだもん (ノーマル)"]
    assert runtime["speakers_error"] is None

    samples, rate = runtime["synthesize"]("こんにちは。", voice="ずんだもん", speed=1.2, pitch=0.05)

    assert rate == _FAKE_TTS_RATE
    assert samples.dtype == np.float32
    assert samples.size > 0
    assert posted[0]["params"] == {"text": "こんにちは。", "speaker": 3}
    assert posted[1]["json"]["speedScale"] == pytest.approx(1.2)
    assert posted[1]["json"]["pitchScale"] == pytest.approx(0.05)


def test_voicevox_runtime_persists_only_the_endpoint_origin(monkeypatch):
    import httpx

    requested: list[str] = []

    def fake_get(url, **kwargs):
        requested.append(url)
        return _FakeHttpResponse(json_body=[])

    monkeypatch.setattr(httpx, "get", fake_get)
    runtime = build_voicevox_runtime("http://localhost:50021/private/tenant-a")

    assert requested == ["http://localhost:50021/private/tenant-a/speakers"]
    assert runtime["endpoint_base_url"] == "http://localhost:50021"
    assert "private" not in runtime["endpoint_base_url"]
    assert runtime["runtime_status"] == "ready"


def test_voicevox_runtime_distinguishes_configured_but_unreachable(monkeypatch):
    import httpx

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("engine is not running")

    monkeypatch.setattr(httpx, "get", unavailable)
    runtime = build_voicevox_runtime(
        "http://127.0.0.1:50021",
        voices=["3 ずんだもん (ノーマル)"],
    )

    assert runtime["endpoint_base_url"] == "http://127.0.0.1:50021"
    assert runtime["runtime_status"] == "configured_unreachable"
    assert runtime["voices"] == ["3 ずんだもん (ノーマル)"]
    assert "ConnectError" in runtime["speakers_error"]


def test_voicevox_runtime_reports_an_unknown_voice(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: _FakeHttpResponse(
            json_body=[{"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3}]}]
        ),
    )
    monkeypatch.delenv("ALLOW_REMOTE_AUDIO_ENDPOINTS", raising=False)

    runtime = build_voicevox_runtime("http://127.0.0.1:50021")

    with pytest.raises(ValueError, match="Unknown VOICEVOX voice"):
        runtime["synthesize"]("こんにちは。", voice="存在しない話者")


def test_voicevox_runtime_rejects_invalid_controls_before_posting(monkeypatch):
    import httpx

    posted: list[str] = []
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: _FakeHttpResponse(json_body=[]),
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: posted.append(url),
    )
    runtime = build_voicevox_runtime("http://127.0.0.1:50021")

    with pytest.raises(ValueError, match="positive finite"):
        runtime["synthesize"]("こんにちは。", speed=0)
    with pytest.raises(ValueError, match="between 0.5 and 2.0"):
        runtime["synthesize"]("こんにちは。", speed=2.1)
    with pytest.raises(ValueError, match="pitch must be finite"):
        runtime["synthesize"]("こんにちは。", pitch=float("nan"))
    with pytest.raises(ValueError, match="between -0.15 and 0.15"):
        runtime["synthesize"]("こんにちは。", pitch=0.2)
    assert posted == []


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_voicevox_runtime_requires_a_positive_finite_timeout(monkeypatch, timeout):
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: pytest.fail("invalid timeout must fail before HTTP"),
    )

    with pytest.raises(ValueError, match="positive finite"):
        build_voicevox_runtime(
            "http://127.0.0.1:50021",
            timeout_seconds=timeout,
        )


def test_fake_tts_loader_completes_a_speech_job_through_the_api(tmp_path: Path):
    manifest_root = tmp_path / "manifests"
    manifest_path = manifest_root / "audio" / "fake-speech.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "id": "fake-speech",
                "public_id": "fake-speech",
                "display_name": "Fake Speech",
                "media_type": "audio",
                "task_type": "text-to-speech",
                "provider": "test",
                "runtime": "fake_tts",
                "remote_ref": "http://127.0.0.1:50021",
                "loader": "fake_tts_loader",
                "default_params": {
                    "voice": "test-voice",
                    "speed": 1.0,
                    "max_chunk_characters": 200,
                    "chunk_gap_seconds": 0.1,
                },
                "is_default": True,
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )

    class FakeTtsLoader:
        def load(self, manifest):
            def synthesize(text, *, voice=None, speed=1.0, pitch=0.0):
                return _sine(0.25, sample_rate=_FAKE_TTS_RATE), _FAKE_TTS_RATE

            return {
                "synthesize": synthesize,
                "voices": ["test-voice"],
                "default_voice": "test-voice",
                "default_speed": 1.0,
                "sample_rate": _FAKE_TTS_RATE,
                "device": "cpu",
            }

    services = create_application_services(
        manifest_root=manifest_root,
        db_path=tmp_path / "jobs.db",
        output_dir=tmp_path / "outputs" / "images",
    )
    services.model_service.loader_registry.register("fake_tts_loader", FakeTtsLoader())
    client = TestClient(create_app(services, start_job_runner=False))

    created = client.post(
        "/generate/speech",
        json={"prompt": "統合テストのナレーション。", "model_id": "fake-speech"},
    )
    assert created.status_code == 201

    completed = services.job_runner.run_once()
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.result is not None
    assert Path(completed.result.outputs[0]).is_file()

    persisted = client.get(f"/jobs/{created.json()['job_id']}").json()
    assert persisted["status"] == "succeeded"
    assert persisted["result"]["metadata"]["generator"] == "SpeechGenerator"
