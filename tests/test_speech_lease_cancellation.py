"""Cooperative cancellation and lease boundaries for SpeechGenerator, without real models.

Mirrors tests/test_video_lease_cancellation.py's/tests/test_audio_lease_cancellation.py's
structure (PR4b Speech lane): every concurrency claim below is proven with a
real `ModelService`/`ModelRuntimeCache` and deterministic synchronization
(`Event`/non-blocking `wait_timeout=0` contention), never a bare sleep, per
the concurrency-safety skill's deterministic-proof requirement.
"""

from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from core.jobs.context import GenerationCancelled, GenerationContext
from core.models.cache import ModelRuntimeCache
from core.models.runtime_lease import RuntimeBusyError, RuntimeState
from core.models.service import ModelService
from core.schemas import GenerationRequest
from generators.audio.speech import SpeechGenerator


def _fake_synthesize(*, rate: int = 24_000):
    calls: list[dict] = []

    def synthesize(text, *, voice=None, speed=1.0, pitch=0.0):
        calls.append({"text": text, "voice": voice, "speed": speed, "pitch": pitch})
        samples = np.zeros(int(rate * 0.1), dtype=np.float32)
        return samples, rate

    return synthesize, calls


def _build(tmp_path, monkeypatch, *, synthesize=None, admission_capacity: int = 1):
    if synthesize is None:
        synthesize, _ = _fake_synthesize()

    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime="speech", display_name="fake speech",
        )

    runtime_dict = {
        "synthesize": synthesize,
        "default_voice": "alpha",
        "default_speed": 1.0,
        "sample_rate": 24_000,
        "device": "cpu",
    }
    loader = Mock()
    loader.load.side_effect = lambda item: runtime_dict
    cache = ModelRuntimeCache(max_entries=4)
    service = ModelService(
        registry=None, resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache, admission_capacity=admission_capacity,
    )
    generator = SpeechGenerator(service, output_dir=tmp_path)
    monkeypatch.setattr("generators.audio.speech.evaluate_audio_output", lambda *a: {})
    return generator, service, cache, loader, runtime_dict


def _request(**params):
    return GenerationRequest(
        media_type="audio", task_type="text-to-speech",
        prompt="一文目です。二文目です。三文目です。",
        model_id="target",
        params={"max_chunk_characters": 7, "chunk_gap_seconds": 0.0, **params},
    )


class _ObservedSemaphore:
    def __init__(self, semaphore, attempted):
        self._semaphore = semaphore
        self._attempted = attempted

    def acquire(self, *args, **kwargs):
        # The owner already holds the real semaphore; this is genuine contention.
        self._attempted.set()
        return self._semaphore.acquire(*args, **kwargs)

    def release(self):
        return self._semaphore.release()


def test_multi_chunk_narration_completes_within_one_lease(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)
    acquire_calls = {"n": 0}
    original_acquire = service.acquire_runtime

    def counting_acquire(*args, **kwargs):
        acquire_calls["n"] += 1
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(service, "acquire_runtime", counting_acquire)

    result = generator.run(_request())

    assert result.status == "succeeded"
    assert result.metadata["chunk_count"] > 1  # genuinely multi-chunk
    assert acquire_calls["n"] == 1  # exactly one lease for the whole narration
    assert loader.load.call_count == 1
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0


def test_synthesis_holds_lease_across_all_chunks_blocking_concurrent_acquire(
    tmp_path, monkeypatch
):
    # Real contention, not a sleep-based race: a contender thread is
    # spawned *from inside* the first chunk's synthesize() call -- i.e.
    # while the narration's one lease is genuinely still held -- and
    # attempts a non-blocking acquire (wait_timeout=0). It must observe the
    # runtime busy, proving the lease spans every chunk rather than being
    # released and reacquired between them.
    contender_attempted = Event()
    contender_result: dict[str, object] = {}
    service_holder: dict[str, object] = {}
    calls: list[str] = []

    def contender() -> None:
        contender_attempted.set()
        try:
            with service_holder["service"].acquire_runtime(
                "target", "audio", wait_timeout=0
            ):
                contender_result["acquired"] = True
        except RuntimeBusyError:
            contender_result["acquired"] = False

    def synthesize(text, *, voice=None, speed=1.0, pitch=0.0):
        calls.append(text)
        if len(calls) == 1:
            thread = Thread(target=contender)
            thread.start()
            assert contender_attempted.wait(timeout=5), "contender never attempted"
            thread.join(5)
            assert not thread.is_alive()
        samples = np.zeros(2_400, dtype=np.float32)
        return samples, 24_000

    generator, service, cache, loader, runtime_dict = _build(
        tmp_path, monkeypatch, synthesize=synthesize
    )
    service_holder["service"] = service

    result = generator.run(_request())

    assert result.status == "succeeded"
    assert len(calls) > 1  # more than one chunk actually ran
    assert contender_result.get("acquired") is False  # blocked while narration held the lease
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0


def test_provider_failure_still_invalidates(tmp_path, monkeypatch):
    def failing_synthesize(text, *, voice=None, speed=1.0, pitch=0.0):
        raise RuntimeError("provider failed")

    generator, service, cache, loader, runtime_dict = _build(
        tmp_path, monkeypatch, synthesize=failing_synthesize
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        generator.run(_request())

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0


def test_precancellation_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio") as handle:
        cached_runtime = handle.runtime

    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=GenerationContext(is_cancelled=lambda: True))

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "audio", wait_timeout=0) as handle:
        assert handle.runtime is cached_runtime  # never reloaded
    assert loader.load.call_count == 1


def test_precancellation_probe_error_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "audio") as handle:
        cached_runtime = handle.runtime

    probe_error = RuntimeError("job repository unavailable")

    def failing_is_cancelled() -> bool:
        raise probe_error

    with pytest.raises(RuntimeError) as caught:
        generator.run(_request(), context=GenerationContext(is_cancelled=failing_is_cancelled))
    assert caught.value is probe_error
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "audio", wait_timeout=0) as handle:
        assert handle.runtime is cached_runtime  # never reloaded
    assert loader.load.call_count == 1


def test_cancellation_during_admission_wait_skips_synthesis(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)
    cancelled, attempted, finished = Event(), Event(), Event()
    errors: list[BaseException] = []
    owner = service.acquire_runtime("owner", "audio")
    monkeypatch.setattr(
        service._admission,
        "_semaphore",
        _ObservedSemaphore(service._admission._semaphore, attempted),
    )

    def work() -> None:
        try:
            generator.run(
                _request(), context=GenerationContext(is_cancelled=cancelled.is_set)
            )
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    worker = Thread(target=work)
    worker.start()
    try:
        assert attempted.wait(2), "worker never attempted contended admission"
        cancelled.set()
        assert finished.wait(2), "cancelled worker still waits for unrelated synthesis"
        assert len(errors) == 1 and isinstance(errors[0], GenerationCancelled)
        assert loader.load.call_count == 1  # only the owner; target never loaded
    finally:
        cancelled.set()
        owner.release()
        worker.join(3)
    assert not worker.is_alive()
    # No leaked admission slot after cancellation.
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass


def test_late_cancellation_after_successful_synthesis_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s own pre-acquisition check (False)
        # call #2: the pre-use probe (False)
        # call #3: the post-synthesis recheck, after every chunk already
        # completed successfully -- True.
        return call_count["n"] > 2

    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=GenerationContext(is_cancelled=is_cancelled))

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []  # nothing written post-lease either


def test_late_cancellation_probe_error_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)
    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        if call_count["n"] > 2:
            raise probe_error
        return False

    with pytest.raises(RuntimeError) as caught:
        generator.run(_request(), context=GenerationContext(is_cancelled=is_cancelled))

    assert caught.value is probe_error
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.wav")) == []
    with service.acquire_runtime("target", "audio", wait_timeout=0):
        pass
    assert loader.load.call_count == 1


def test_post_lease_quality_evaluation_failure_preserves_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, runtime_dict = _build(tmp_path, monkeypatch)

    def failing_quality(*args):
        raise RuntimeError("quality evaluator crashed")

    monkeypatch.setattr("generators.audio.speech.evaluate_audio_output", failing_quality)

    with pytest.raises(RuntimeError, match="quality evaluator crashed"):
        generator.run(_request())

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0


def test_no_lease_leak_across_success_error_and_cancellation_paths(tmp_path, monkeypatch):
    should_fail = {"value": False}

    def flaky_synthesize(text, *, voice=None, speed=1.0, pitch=0.0):
        if should_fail["value"]:
            raise RuntimeError("simulated failure")
        samples = np.zeros(2_400, dtype=np.float32)
        return samples, 24_000

    generator, service, cache, loader, runtime_dict = _build(
        tmp_path, monkeypatch, synthesize=flaky_synthesize
    )

    # Success path.
    result = generator.run(_request())
    assert result.status == "succeeded"
    assert cache._entries["target"].lease_count == 0

    # Provider-failure path -- invalidates, but releases cleanly (no leak).
    should_fail["value"] = True
    with pytest.raises(RuntimeError, match="simulated failure"):
        generator.run(_request())
    assert cache._entries["target"].lease_count == 0
    assert cache._entries["target"].state is RuntimeState.INVALID
    should_fail["value"] = False

    # Cancellation path -- the prior INVALID entry reloads; this path must
    # also release cleanly.
    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=GenerationContext(is_cancelled=lambda: True))
    assert cache._entries["target"].lease_count == 0
