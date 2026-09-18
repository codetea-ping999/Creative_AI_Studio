"""Cooperative cancellation and lease boundaries for AudioGenerator, without real models.

Mirrors tests/test_video_lease_cancellation.py's/tests/test_image_lease_cancellation.py's
structure (PR4b Music lane): every concurrency claim below is proven with a
real `ModelService`/`ModelRuntimeCache` and deterministic call-count-driven
stubs, never a bare sleep, per the concurrency-safety skill's
deterministic-proof requirement.
"""

import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.jobs.context import GenerationCancelled
from core.models.cache import ModelRuntimeCache
from core.models.runtime_lease import RuntimeState
from core.models.service import ModelService
from core.schemas import GenerationRequest
from generators.audio.generator import AudioGenerator, LongFormGenerationCancelled


class _FakeTensor:
    """Stand-in for a transformers `BatchEncoding` tensor value."""

    def to(self, device):
        return self


class _FakeProcessor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"input_ids": _FakeTensor()}


class _FakeShortFormModel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.generate_calls = 0

    def generate(self, **kwargs):
        import torch

        self.generate_calls += 1
        if self.fail:
            raise RuntimeError("provider failed")
        sample_count = 4000
        time = torch.arange(sample_count, dtype=torch.float32) / 32000
        audio = 0.05 * torch.sin(2 * torch.pi * 220 * time)
        return audio.reshape(1, 1, -1)


def _short_form_runtime(model, processor):
    return {
        "model": model,
        "processor": processor,
        "device": "cpu",
        "sampling_rate": 32000,
        "frame_rate": 50,
        "torch_dtype": "float32",
        "load_dtype": "float32",
    }


def _build_short_form(tmp_path, monkeypatch, *, model=None, processor=None):
    model = model if model is not None else _FakeShortFormModel()
    processor = processor if processor is not None else _FakeProcessor()

    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime="transformers", display_name="fake music",
            tags=[],
        )

    loader = Mock()
    loader.load.side_effect = lambda item: _short_form_runtime(model, processor)
    cache = ModelRuntimeCache(max_entries=4)
    service = ModelService(
        registry=None, resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache, admission_capacity=1,
    )
    generator = AudioGenerator(service, output_dir=tmp_path)
    monkeypatch.setattr("generators.audio.generator.evaluate_audio_output", lambda *a: {})
    monkeypatch.setattr("generators.audio.generator.evaluate_audio_semantics", lambda *a: {})
    monkeypatch.setattr("generators.audio.generator.enrich_quality_report", lambda *a: None)
    return generator, service, cache, loader, model, processor


def _short_form_request(**params):
    return GenerationRequest(
        media_type="audio", prompt="test", model_id="target",
        params={"duration_seconds": 4, **params},
    )


class _FakeAudioCraftModel:
    sample_rate = 32_000
    frame_rate = 50
    max_duration = 30.0

    def __init__(self, *, fail_segment: int | None = None, before_segment=None) -> None:
        self.fail_segment = fail_segment
        self.before_segment = before_segment
        self.progress_callback = None
        self.params: dict[str, object] = {}
        self.callback_cleared_count = 0

    def set_generation_params(self, **kwargs) -> None:
        self.params = kwargs

    def set_custom_progress_callback(self, callback=None) -> None:
        self.progress_callback = callback
        if callback is None:
            self.callback_cleared_count += 1

    def generate(self, prompts, *, progress: bool):
        import torch

        duration = float(self.params["duration"])
        stride = float(self.params["extend_stride"])
        segment_count = 1 + math.ceil(max(0.0, duration - self.max_duration) / stride)
        boundaries = [
            min(duration, self.max_duration + index * stride)
            for index in range(segment_count)
        ]
        for segment, boundary in enumerate(boundaries, start=1):
            if self.before_segment is not None:
                self.before_segment(segment)
            if self.fail_segment == segment:
                raise RuntimeError(f"segment {segment} failed")
            if progress and self.progress_callback is not None:
                self.progress_callback(
                    round(boundary * self.frame_rate),
                    round(duration * self.frame_rate),
                )
        sample_count = round(duration * self.sample_rate)
        time = torch.arange(sample_count, dtype=torch.float32) / self.sample_rate
        audio = 0.05 * torch.sin(2 * torch.pi * 220 * time)
        return audio.reshape(1, 1, -1)


def _long_form_runtime(model: _FakeAudioCraftModel):
    return {
        "model": model,
        "device": "cpu",
        "sampling_rate": model.sample_rate,
        "frame_rate": model.frame_rate,
        "max_duration": model.max_duration,
    }


def _build_long_form(tmp_path, monkeypatch, *, model=None):
    model = model if model is not None else _FakeAudioCraftModel()

    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime="audiocraft", display_name="fake long-form music",
            tags=["long-form"],
        )

    loader = Mock()
    loader.load.side_effect = lambda item: _long_form_runtime(model)
    cache = ModelRuntimeCache(max_entries=4)
    service = ModelService(
        registry=None, resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache, admission_capacity=1,
    )
    generator = AudioGenerator(service, output_dir=tmp_path)
    monkeypatch.setattr("generators.audio.generator.evaluate_audio_output", lambda *a: {})
    monkeypatch.setattr("generators.audio.generator.evaluate_audio_semantics", lambda *a: {})
    monkeypatch.setattr("generators.audio.generator.enrich_quality_report", lambda *a: None)
    return generator, service, cache, loader, model


def _long_form_request(**params):
    return GenerationRequest(
        media_type="audio", prompt="test", model_id="target",
        params={"duration_seconds": 45, "extend_stride_seconds": 10, **params},
    )


# --------------------------------------------------------------------- short-form


def test_short_form_precancellation_probe_error_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model, processor = _build_short_form(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio"):
        pass
    assert cache._entries["target"].state is RuntimeState.READY

    probe_error = RuntimeError("job repository unavailable")

    with pytest.raises(RuntimeError) as caught:
        generator.run_with_control(
            _short_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=lambda: (_ for _ in ()).throw(probe_error),
        )
    assert caught.value is probe_error
    assert model.generate_calls == 0
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_short_form_ordinary_precancellation_exits_cleanly(tmp_path, monkeypatch):
    generator, service, cache, loader, model, processor = _build_short_form(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio"):
        pass

    with pytest.raises(GenerationCancelled):
        generator.run_with_control(
            _short_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=lambda: True,
        )
    assert model.generate_calls == 0
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert loader.load.call_count == 1


def test_short_form_late_cancellation_probe_error_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model, processor = _build_short_form(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio"):
        pass

    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def cancel_requested() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s own pre-acquisition check (False)
        # call #2: the pre-generation probe (False)
        # call #3: the post-generation recheck, after generate() already
        # completed successfully -- raises.
        if call_count["n"] >= 3:
            raise probe_error
        return False

    with pytest.raises(RuntimeError) as caught:
        generator.run_with_control(
            _short_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=cancel_requested,
        )
    assert caught.value is probe_error
    assert model.generate_calls == 1  # generation genuinely ran
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []  # nothing written post-lease either
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_short_form_ordinary_late_cancellation_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model, processor = _build_short_form(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio"):
        pass

    call_count = {"n": 0}

    def cancel_requested() -> bool:
        call_count["n"] += 1
        return call_count["n"] > 2

    with pytest.raises(GenerationCancelled):
        generator.run_with_control(
            _short_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=cancel_requested,
        )
    assert model.generate_calls == 1
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []


def test_short_form_generation_success_produces_wav_and_reuses_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model, processor = _build_short_form(tmp_path, monkeypatch)

    result = generator.run_with_control(
        _short_form_request(),
        progress_callback=lambda *a: None,
        cancel_requested=lambda: False,
    )

    assert result.status == "succeeded"
    assert model.generate_calls == 1
    assert len(processor.calls) == 1
    assert list(tmp_path.glob("*.wav"))
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_short_form_provider_failure_still_invalidates(tmp_path, monkeypatch):
    generator, service, cache, loader, model, processor = _build_short_form(
        tmp_path, monkeypatch, model=_FakeShortFormModel(fail=True)
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        generator.run_with_control(
            _short_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=lambda: False,
        )
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0


# --------------------------------------------------------------------- long-form


def test_long_form_precancellation_probe_error_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model = _build_long_form(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio"):
        pass

    probe_error = RuntimeError("job repository unavailable")

    with pytest.raises(RuntimeError) as caught:
        generator.run_with_control(
            _long_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=lambda: (_ for _ in ()).throw(probe_error),
        )
    assert caught.value is probe_error
    assert model.params == {}  # set_generation_params() never called
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_long_form_callback_probe_error_preserves_runtime_and_clears_callback(
    tmp_path, monkeypatch
):
    # The Music lane's key distinction: cancel_requested() itself raising
    # from *inside* report_token_progress() (mid-generate()) is a
    # bookkeeping failure, not a genuine mid-execution cancellation -- it
    # must not invalidate the runtime, and set_custom_progress_callback(None)
    # must still run before the lease releases.
    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def cancel_requested() -> bool:
        call_count["n"] += 1
        # call #1: pre-acquisition check (False)
        # call #2: pre-generation probe (False)
        # call #3: first segment-boundary probe, inside report_token_progress -- raises.
        if call_count["n"] >= 3:
            raise probe_error
        return False

    generator, service, cache, loader, model = _build_long_form(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError) as caught:
        generator.run_with_control(
            _long_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=cancel_requested,
        )
    assert caught.value is probe_error
    assert model.progress_callback is None  # cleared before the lease released
    assert model.callback_cleared_count == 1
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_long_form_genuine_mid_execution_cancellation_still_invalidates(tmp_path, monkeypatch):
    # The distinction the Music lane explicitly protects: cancel_requested()
    # genuinely returning True while model.generate() is running is a real
    # mid-execution interruption, not a clean boundary -- it must keep
    # conservatively invalidating exactly as before this migration.
    holder: dict[str, object] = {}

    def before_segment(segment: int) -> None:
        if segment == 2:
            holder["cancel_now"] = True

    model = _FakeAudioCraftModel(before_segment=before_segment)
    generator, service, cache, loader, model = _build_long_form(tmp_path, monkeypatch, model=model)

    def cancel_requested() -> bool:
        return bool(holder.get("cancel_now"))

    with pytest.raises(LongFormGenerationCancelled):
        generator.run_with_control(
            _long_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=cancel_requested,
        )
    assert model.progress_callback is None  # still cleared before release
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []


def test_long_form_late_cancellation_probe_error_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model = _build_long_form(tmp_path, monkeypatch)

    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def cancel_requested() -> bool:
        call_count["n"] += 1
        # calls 1-2: pre-acquisition + pre-generation probe (False).
        # calls 3-5: one segment-boundary probe per segment (False) -- this
        # fake model has max_duration=30s, extend_stride=10s, duration=45s
        # -> 1 + ceil(15/10) = 3 segments, so 3 calls land here.
        # call #6: the post-generation recheck, after generate() already
        # completed successfully -- raises.
        if call_count["n"] >= 6:
            raise probe_error
        return False

    with pytest.raises(RuntimeError) as caught:
        generator.run_with_control(
            _long_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=cancel_requested,
        )
    assert caught.value is probe_error
    assert model.progress_callback is None
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_long_form_progress_publication_error_deferred_past_lease(tmp_path, monkeypatch):
    generator, service, cache, loader, model = _build_long_form(tmp_path, monkeypatch)
    publish_error = RuntimeError("event publication unavailable")
    calls = {"n": 0}

    def progress_callback(fraction, segment, segment_count):
        calls["n"] += 1
        raise publish_error

    with pytest.raises(RuntimeError) as caught:
        generator.run_with_control(
            _long_form_request(),
            progress_callback=progress_callback,
            cancel_requested=lambda: False,
        )
    assert caught.value is publish_error
    assert calls["n"] >= 1
    assert model.progress_callback is None  # cleared before release regardless
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY  # bookkeeping failure, not a runtime fault
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []


def test_long_form_generation_success_produces_wav_and_reuses_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, model = _build_long_form(tmp_path, monkeypatch)

    result = generator.run_with_control(
        _long_form_request(),
        progress_callback=lambda *a: None,
        cancel_requested=lambda: False,
    )

    assert result.status == "succeeded"
    assert model.progress_callback is None
    assert list(tmp_path.glob("*.wav"))
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_long_form_provider_failure_still_invalidates(tmp_path, monkeypatch):
    generator, service, cache, loader, model = _build_long_form(
        tmp_path, monkeypatch, model=_FakeAudioCraftModel(fail_segment=1)
    )

    with pytest.raises(RuntimeError, match="segment 1 failed"):
        generator.run_with_control(
            _long_form_request(),
            progress_callback=lambda *a: None,
            cancel_requested=lambda: False,
        )
    assert model.progress_callback is None  # teardown still runs on a genuine fault
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0
