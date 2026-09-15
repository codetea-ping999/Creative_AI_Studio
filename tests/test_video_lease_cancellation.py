"""Cooperative cancellation at video lease boundaries, without real models."""

from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.jobs.context import GenerationCancelled, GenerationContext
from core.models.cache import ModelRuntimeCache
from core.models.runtime_lease import RuntimeBusyError, RuntimeState, RuntimeWaitTimeoutError
from core.models.service import ModelService
from core.schemas import GenerationRequest
from generators.video.generator import VideoGenerator


def _build(tmp_path, monkeypatch, *, admission_capacity=1):
    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime="learned", display_name="fake video",
        )

    loader = Mock()
    loader.load.side_effect = lambda item: {"id": item.id}
    cache = ModelRuntimeCache(max_entries=4)
    service = ModelService(
        registry=None, resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache, admission_capacity=admission_capacity,
    )
    generator = VideoGenerator(service, output_dir=tmp_path)
    renderer = Mock()
    renderer.render.return_value = {
        "output_path": str(tmp_path / "fake.mp4"), "output_id": "fake",
    }
    generator.runtime_router = SimpleNamespace(resolve=lambda runtime: renderer)
    monkeypatch.setattr("generators.video.generator.evaluate_video_output", lambda *a: {})
    monkeypatch.setattr("generators.video.generator.evaluate_video_semantics", lambda *a: {})
    monkeypatch.setattr("generators.video.generator.enrich_quality_report", lambda *a: None)
    return generator, service, cache, loader, renderer


def _request():
    return GenerationRequest(media_type="video", prompt="test", model_id="target")


def test_cancelled_before_acquisition_does_not_load(tmp_path, monkeypatch):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    context = GenerationContext(is_cancelled=lambda: True)
    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=context)
    loader.load.assert_not_called()
    renderer.render.assert_not_called()


def test_cancelled_after_acquisition_reuses_healthy_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    cancelled = Event()
    original_acquire = service.acquire_runtime
    with original_acquire("target", "video") as handle:
        cached_runtime = handle.runtime

    def acquire(*args, **kwargs):
        handle = original_acquire(*args, **kwargs)
        # Cancellation arrives after all locks were acquired but before render.
        cancelled.set()
        return handle

    monkeypatch.setattr(service, "acquire_runtime", acquire)
    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=GenerationContext(is_cancelled=cancelled.is_set))
    renderer.render.assert_not_called()
    assert cache._entries["target"].state is RuntimeState.READY
    assert cache._entries["target"].lease_count == 0
    with original_acquire("target", "video", wait_timeout=0) as handle:
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


def test_cancelled_admission_wait_finishes_before_owner_releases(tmp_path, monkeypatch):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    cancelled, attempted, finished = Event(), Event(), Event()
    errors = []
    owner = service.acquire_runtime("owner", "video")
    monkeypatch.setattr(service._admission, "_semaphore", _ObservedSemaphore(
        service._admission._semaphore, attempted,
    ))

    def work():
        try:
            generator.run(_request(), context=GenerationContext(is_cancelled=cancelled.is_set))
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
        renderer.render.assert_not_called()
    finally:
        cancelled.set()
        owner.release()
        worker.join(3)
    assert not worker.is_alive()
    # No leaked admission slot after cancellation.
    with service.acquire_runtime("target", "video", wait_timeout=0):
        pass


def test_render_cancellation_still_invalidates_used_runtime(tmp_path, monkeypatch):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    cancelled = Event()

    def render(**kwargs):
        assert cache._entries["target"].lease_count == 1
        cancelled.set()
        kwargs["context"].raise_if_cancelled()

    renderer.render.side_effect = render
    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=GenerationContext(is_cancelled=cancelled.is_set))
    assert cache._entries["target"].state is RuntimeState.INVALID
    assert cache._entries["target"].lease_count == 0


def test_non_wait_busy_error_is_not_retried(tmp_path, monkeypatch):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    error = RuntimeBusyError("pinned capacity cannot be resolved by waiting")
    acquire = Mock(side_effect=error)
    monkeypatch.setattr(service, "acquire_runtime", acquire)
    with pytest.raises(RuntimeBusyError) as caught:
        generator.run(_request(), context=GenerationContext(is_cancelled=lambda: False))
    assert caught.value is error
    acquire.assert_called_once()


def test_wait_timeout_retries_and_renders_after_admission_is_available(tmp_path, monkeypatch):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    cancelled, timed_out, retry_allowed = Event(), Event(), Event()
    results, errors = [], []
    acquire = service.acquire_runtime
    owner = acquire("owner", "video")

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
            results.append(generator.run(
                _request(), context=GenerationContext(is_cancelled=cancelled.is_set),
            ))
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
        renderer.render.assert_called_once()
        assert cache._entries["target"].lease_count == 0
    finally:
        cancelled.set()
        owner.release()
        retry_allowed.set()
        worker.join(3)


@pytest.mark.parametrize("contended", ["admission", "load", "execution"])
def test_only_synchronization_deadlines_use_retryable_timeout(tmp_path, monkeypatch, contended):
    generator, service, cache, loader, renderer = _build(
        tmp_path, monkeypatch, admission_capacity=2 if contended == "execution" else 1,
    )
    owner = None
    load_lock = None
    if contended == "load":
        load_lock = cache.lock_for("target")
        load_lock.acquire()
    else:
        owner = service.acquire_runtime("target", "video")
    errors = []

    def work():
        try:
            with service.acquire_runtime("target", "video", wait_timeout=0):
                pass
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=work)
    worker.start()
    try:
        worker.join(2)
        assert not worker.is_alive(), "nonblocking acquisition parked on a lock"
        assert len(errors) == 1 and isinstance(errors[0], RuntimeWaitTimeoutError)
        assert isinstance(errors[0], RuntimeBusyError)  # backwards-compatible catch
    finally:
        if owner is not None:
            owner.release()
        if load_lock is not None:
            load_lock.release()
        worker.join(3)
    # Failed acquisition has returned every earlier resource, including G.
    with service.acquire_runtime("target", "video", wait_timeout=0):
        pass


@pytest.mark.parametrize("with_context", [False, True])
def test_success_releases_before_quality(tmp_path, monkeypatch, with_context):
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)

    def quality(*args):
        assert cache._entries["target"].lease_count == 0
        assert cache._entries["target"].state is RuntimeState.READY
        return {}

    monkeypatch.setattr("generators.video.generator.evaluate_video_output", quality)
    context = GenerationContext(is_cancelled=lambda: False) if with_context else None
    assert generator.run(_request(), context=context).status == "succeeded"
    renderer.render.assert_called_once()
