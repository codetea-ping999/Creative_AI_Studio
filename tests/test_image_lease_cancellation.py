"""Cooperative cancellation and lease boundaries for ImageGenerator, without real models.

Mirrors tests/test_video_lease_cancellation.py's structure (PR4b): every
concurrency claim below is proven with real synchronization (Event/Barrier or
genuine lock contention), never a bare sleep, per the concurrency-safety
skill's deterministic-proof requirement.
"""

from threading import Event, Thread
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from PIL import Image
import pytest

from core.jobs.context import GenerationCancelled, GenerationContext
from core.models.cache import ModelRuntimeCache
from core.models.runtime_lease import RuntimeBusyError, RuntimeState, RuntimeWaitTimeoutError
from core.models.service import ModelService
from core.schemas import GenerationRequest
from generators.image.generator import ImageGenerator
from generators.image.providers import UnsupportedImageParameterError


class _FakePipelineResult:
    def __init__(self, image: Image.Image) -> None:
        self.images = [image]


class _FakePipeline:
    """Diffusers-pipeline-shaped fake with no step callback parameter."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.load_lora_calls: list[tuple[str, dict[str, Any]]] = []
        self.set_adapters_calls: list[tuple[str, float]] = []
        self.unload_calls = 0

    def __call__(self, **kwargs: Any) -> _FakePipelineResult:
        self.calls.append(kwargs)
        width = int(kwargs.get("width", 64))
        height = int(kwargs.get("height", 64))
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(1, 2, 3)))

    def to(self, device: str) -> "_FakePipeline":
        return self

    def load_lora_weights(self, path: str, **kwargs: Any) -> None:
        self.load_lora_calls.append((path, kwargs))

    def set_adapters(self, adapter_name: str, adapter_weights: float | None = None) -> None:
        self.set_adapters_calls.append((adapter_name, adapter_weights))

    def delete_adapters(self, adapter_name: str) -> None:
        pass

    def unload_lora_weights(self) -> None:
        self.unload_calls += 1


class _FakeStepAwarePipeline(_FakePipeline):
    """Same as `_FakePipeline`, but declares `callback_on_step_end` so
    `ImageGenerator._pipeline_accepts_step_callback` routes through it."""

    def __init__(self) -> None:
        super().__init__()
        self.steps_invoked = 0

    def __call__(  # type: ignore[override]
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        width: int = 64,
        height: int = 64,
        guidance_scale: float = 7.5,
        num_inference_steps: int = 30,
        callback_on_step_end=None,
        **kwargs: Any,
    ) -> _FakePipelineResult:
        self.calls.append(
            {"prompt": prompt, "width": width, "height": height, **kwargs}
        )
        for step_index in range(num_inference_steps):
            self.steps_invoked = step_index + 1
            if callback_on_step_end is not None:
                callback_on_step_end(self, step_index, 0, {})
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(1, 2, 3)))

    def to(self, device: str) -> "_FakeStepAwarePipeline":
        return self


def _build(tmp_path, monkeypatch, *, admission_capacity=1, pipeline_cls=_FakePipeline):
    pipelines: dict[str, Any] = {}

    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id,
            public_model_id=model_id,
            provider="local",
            loader="fake",
            default_params={},
            runtime="diffusers",
            display_name="fake image",
        )

    def load(item):
        pipeline = pipeline_cls()
        pipelines[item.id] = pipeline
        return {
            "pipeline": pipeline,
            "img2img_pipeline": None,
            "device": "cpu",
            "torch_dtype": "float32",
            "load_dtype": "float32",
        }

    loader = Mock()
    loader.load.side_effect = load
    cache = ModelRuntimeCache(max_entries=4)
    service = ModelService(
        registry=None,
        resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache,
        admission_capacity=admission_capacity,
    )
    generator = ImageGenerator(service, output_dir=tmp_path)
    monkeypatch.setattr("generators.image.generator.evaluate_image_output", lambda *a: {})
    monkeypatch.setattr("generators.image.generator.evaluate_image_semantics", lambda *a: {})
    monkeypatch.setattr("generators.image.generator.enrich_quality_report", lambda *a: None)
    return generator, service, cache, loader, pipelines


def _request(**params):
    return GenerationRequest(
        media_type="image", prompt="test", model_id="target", params=params
    )


def test_image_production_path_never_uses_bare_resolve_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service,
        "resolve_runtime",
        Mock(side_effect=AssertionError("must not call bare resolve_runtime()")),
    )
    result = generator.run(_request(width=64, height=64, steps=1))
    assert result.status == "succeeded"


def test_all_variations_run_inside_one_runtime_lease(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    result = generator.run(
        _request(width=64, height=64, steps=1, variation_count=3)
    )
    assert result.status == "succeeded"
    # A single load: every variation reused the one leased runtime instead
    # of each re-acquiring/reloading it.
    assert loader.load.call_count == 1
    assert len(pipelines["target"].calls) == 3
    assert cache._entries["target"].lease_count == 0
    assert cache._entries["target"].state is RuntimeState.READY


def test_lora_runtime_mutation_occurs_while_lease_is_active(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    lora_file = tmp_path / "style.safetensors"
    lora_file.write_bytes(b"fake-lora")
    mutation_observed = {}

    original_load_lora = _FakePipeline.load_lora_weights

    def observed_load_lora(self, path, **kwargs):
        entry = cache._entries["target"]
        mutation_observed["lease_count"] = entry.lease_count
        mutation_observed["state"] = entry.state
        return original_load_lora(self, path, **kwargs)

    monkeypatch.setattr(_FakePipeline, "load_lora_weights", observed_load_lora)

    result = generator.run(
        _request(
            width=64, height=64, steps=1, lora_path=str(lora_file), lora_scale=0.8
        )
    )

    assert result.status == "succeeded"
    assert mutation_observed == {"lease_count": 1, "state": RuntimeState.READY}
    assert pipelines["target"].load_lora_calls
    assert result.metadata["lora_path"] == str(lora_file)
    assert result.metadata["lora_scale"] == 0.8


def test_invalid_numeric_parameters_do_not_invalidate_a_healthy_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    # Populate the cache with a healthy, already-loaded runtime first.
    with service.acquire_runtime("target", "image"):
        pass
    assert cache._entries["target"].state is RuntimeState.READY

    # A width outside the local provider's declared [64, 2048] range is
    # discovered via validate_capabilities() -- runtime *inspection*
    # (reference-capability probing), never mutation -- so it must not
    # touch a healthy entry's state.
    with pytest.raises(UnsupportedImageParameterError):
        generator.run(_request(width=8192, height=64, steps=1))

    assert loader.load.call_count == 1  # no reload
    assert cache._entries["target"].state is RuntimeState.READY
    assert cache._entries["target"].lease_count == 0
    assert pipelines["target"].calls == []  # inference never started


def test_invalid_lora_path_discovered_before_mutation_does_not_invalidate(
    tmp_path, monkeypatch
):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    with service.acquire_runtime("target", "image"):
        pass
    assert cache._entries["target"].state is RuntimeState.READY

    missing_path = str(tmp_path / "does_not_exist.safetensors")
    with pytest.raises(FileNotFoundError):
        generator.run(_request(width=64, height=64, steps=1, lora_path=missing_path))

    # The bad path is pure input parsing (preflight, before any lease is
    # even acquired) -- no second load, no mutation, no invalidation.
    assert loader.load.call_count == 1
    assert cache._entries["target"].state is RuntimeState.READY
    assert cache._entries["target"].lease_count == 0
    assert pipelines["target"].load_lora_calls == []
    assert pipelines["target"].calls == []


def test_cancelled_before_acquisition_does_not_load(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    context = GenerationContext(is_cancelled=lambda: True)
    with pytest.raises(GenerationCancelled):
        generator.run(_request(width=64, height=64, steps=1), context=context)
    loader.load.assert_not_called()


def test_precancellation_does_not_invalidate_an_unused_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    # Load once, normally, to prove the *same* cached runtime survives.
    with service.acquire_runtime("target", "image") as handle:
        cached_runtime = handle.runtime

    cancelled = Event()
    original_acquire = service.acquire_runtime

    def acquire(*args, **kwargs):
        handle = original_acquire(*args, **kwargs)
        # Cancellation arrives after every lock was acquired but before any
        # mutation/inference started.
        cancelled.set()
        return handle

    monkeypatch.setattr(service, "acquire_runtime", acquire)
    with pytest.raises(GenerationCancelled):
        generator.run(
            _request(width=64, height=64, steps=1),
            context=GenerationContext(is_cancelled=cancelled.is_set),
        )

    assert pipelines["target"].calls == []
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with original_acquire("target", "image", wait_timeout=0) as handle:
        assert handle.runtime is cached_runtime
    assert loader.load.call_count == 1


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


def test_cancellation_remains_observable_while_waiting_for_admission(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    cancelled, attempted, finished = Event(), Event(), Event()
    errors: list[BaseException] = []
    owner = service.acquire_runtime("owner", "image")
    monkeypatch.setattr(
        service._admission,
        "_semaphore",
        _ObservedSemaphore(service._admission._semaphore, attempted),
    )

    def work():
        try:
            generator.run(
                _request(width=64, height=64, steps=1),
                context=GenerationContext(is_cancelled=cancelled.is_set),
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
        assert finished.wait(2), "cancelled worker still waits for unrelated inference"
        assert len(errors) == 1 and isinstance(errors[0], GenerationCancelled)
        assert loader.load.call_count == 1  # only the owner; target never loaded
    finally:
        cancelled.set()
        owner.release()
        worker.join(3)
    assert not worker.is_alive()
    # No leaked admission slot after cancellation.
    with service.acquire_runtime("target", "image", wait_timeout=0):
        pass


def test_only_synchronization_deadlines_use_retryable_timeout(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    error = RuntimeBusyError("pinned capacity cannot be resolved by waiting")
    acquire = Mock(side_effect=error)
    monkeypatch.setattr(service, "acquire_runtime", acquire)
    with pytest.raises(RuntimeBusyError) as caught:
        generator.run(
            _request(width=64, height=64, steps=1),
            context=GenerationContext(is_cancelled=lambda: False),
        )
    assert caught.value is error
    acquire.assert_called_once()


def test_wait_timeout_retries_and_succeeds_after_admission_is_available(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    cancelled, timed_out, retry_allowed = Event(), Event(), Event()
    results: list[Any] = []
    errors: list[BaseException] = []
    acquire = service.acquire_runtime
    owner = acquire("owner", "image")

    def observed_acquire(*args, **kwargs):
        try:
            return acquire(*args, **kwargs)
        except RuntimeWaitTimeoutError:
            timed_out.set()
            assert retry_allowed.wait(3), "owner never permitted retry"
            raise

    monkeypatch.setattr(service, "acquire_runtime", observed_acquire)

    def work():
        try:
            results.append(
                generator.run(
                    _request(width=64, height=64, steps=1),
                    context=GenerationContext(is_cancelled=cancelled.is_set),
                )
            )
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=work)
    worker.start()
    try:
        assert timed_out.wait(2), "worker did not bound the contended wait"
        owner.release()
        retry_allowed.set()
        worker.join(3)
        assert not worker.is_alive()
        assert errors == []
        assert len(results) == 1 and results[0].status == "succeeded"
        assert cache._entries["target"].lease_count == 0
    finally:
        cancelled.set()
        owner.release()
        retry_allowed.set()
        worker.join(3)


def test_inference_failure_follows_conservative_invalidation(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(
        tmp_path, monkeypatch, pipeline_cls=_FakeStepAwarePipeline
    )

    def failing_call(self, **kwargs):
        raise RuntimeError("simulated diffusers failure mid-inference")

    monkeypatch.setattr(_FakeStepAwarePipeline, "__call__", failing_call)

    with pytest.raises(RuntimeError):
        generator.run(_request(width=64, height=64, steps=2))

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*")) == []


def test_cancellation_mid_inference_follows_conservative_invalidation(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(
        tmp_path, monkeypatch, pipeline_cls=_FakeStepAwarePipeline
    )
    cancellation_state = {"requested": False}

    def _record_progress(fraction: float) -> None:
        if fraction >= 0.5:
            cancellation_state["requested"] = True

    context = GenerationContext(
        is_cancelled=lambda: cancellation_state["requested"],
        on_progress=_record_progress,
        min_interval_seconds=0.0,
        min_progress_delta=0.0,
    )

    with pytest.raises(GenerationCancelled):
        generator.run(_request(width=64, height=64, steps=4), context=context)

    entry = cache._entries["target"]
    assert pipelines["target"].steps_invoked == 2
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*")) == []


def test_semantic_scoring_observes_lease_inactive(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    observed = {}

    def quality(*args):
        entry = cache._entries["target"]
        observed["lease_count"] = entry.lease_count
        observed["state"] = entry.state
        return {}

    def semantics(*args):
        entry = cache._entries["target"]
        observed["semantic_lease_count"] = entry.lease_count
        observed["semantic_state"] = entry.state
        # Not nested inside the lease's own execution-lock/lease ownership:
        # a same-thread re-acquisition of the exact entry this generation
        # just used must succeed immediately (wait_timeout=0), which would
        # deadlock/refuse if this call still ran under the original lease.
        with service.acquire_runtime("target", "image", wait_timeout=0):
            pass
        return {}

    monkeypatch.setattr("generators.image.generator.evaluate_image_output", quality)
    monkeypatch.setattr("generators.image.generator.evaluate_image_semantics", semantics)

    result = generator.run(_request(width=64, height=64, steps=1))

    assert result.status == "succeeded"
    assert observed == {
        "lease_count": 0,
        "state": RuntimeState.READY,
        "semantic_lease_count": 0,
        "semantic_state": RuntimeState.READY,
    }


def test_post_lease_semantic_failure_preserves_output_cleanup(tmp_path, monkeypatch):
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def flaky_semantics(output_path, *args):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated semantic scorer failure")
        return {}

    monkeypatch.setattr(
        "generators.image.generator.evaluate_image_semantics", flaky_semantics
    )

    with pytest.raises(RuntimeError):
        generator.run(
            _request(width=64, height=64, steps=1, variation_count=2)
        )

    # The first variation's file was written (post-lease) before the second
    # variation's semantic scoring failed -- the existing cleanup contract
    # removes it rather than leaving a partial batch on disk.
    assert list(tmp_path.glob("*.png")) == []
    # The generation itself succeeded (both images were rendered inside the
    # single lease); only post-lease scoring failed.
    assert cache._entries["target"].state is RuntimeState.READY
    assert len(pipelines["target"].calls) == 2


def test_late_cancellation_after_successful_provider_return_preserves_runtime(
    tmp_path, monkeypatch
):
    # Safety convergence pass, Codex finding "raise late image cancellation
    # after releasing the lease": `_FakePipeline` has no
    # `callback_on_step_end` parameter (see `_pipeline_accepts_step_callback`),
    # so cancellation can only become observable at the generator's own
    # loop-boundary checks -- exactly the "pipeline without a supported step
    # callback" scenario the finding describes.
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s own pre-acquisition check (False)
        # call #2: cancelled_before_mutation (False)
        # call #3: top-of-loop check for variation 0 (False)
        # call #4: the check right after the provider call returns (True)
        return call_count["n"] > 3

    with pytest.raises(GenerationCancelled):
        generator.run(
            _request(width=64, height=64, steps=1),
            context=GenerationContext(is_cancelled=is_cancelled),
        )

    # The provider call genuinely ran and returned successfully before
    # cancellation was observed.
    assert len(pipelines["target"].calls) == 1
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert list(tmp_path.glob("*.png")) == []  # nothing written post-lease either


def test_post_processing_cancellation_removes_partial_outputs(tmp_path, monkeypatch):
    # Safety convergence pass, Codex finding "keep observing cancellation
    # during image post-processing": cancellation observed *during* the
    # post-lease save/quality loop (after the lease has already released
    # cleanly) must still remove whatever this loop already wrote, via the
    # existing cleanup path, rather than leaving an orphaned PNG behind.
    generator, service, cache, loader, pipelines = _build(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # calls 1-6: _acquire_runtime()'s pre-acquisition check,
        # cancelled_before_mutation, and top-of-loop/after-provider for
        # both variations -- all inside the (uncancelled) lease.
        # call #7: post-lease loop's own check for the *first* variation
        # (False -- it gets written and scored normally).
        # call #8: post-lease loop's own check for the *second* variation,
        # reached only after the first variation's file was already
        # written and scored.
        return call_count["n"] > 7

    with pytest.raises(GenerationCancelled):
        generator.run(
            _request(width=64, height=64, steps=1, variation_count=2),
            context=GenerationContext(is_cancelled=is_cancelled),
        )

    assert len(pipelines["target"].calls) == 2  # both variations rendered inside the lease
    assert list(tmp_path.glob("*.png")) == []  # the first variation's file was cleaned up
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
