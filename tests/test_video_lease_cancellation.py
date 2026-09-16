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
from generators.video.runtime import ProceduralStoryboardRuntime


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


def test_video_successful_render_with_late_cancellation_preserves_runtime(
    tmp_path, monkeypatch
):
    # Safety convergence pass, Codex finding "raise post-render cancellation
    # after a clean lease exit": once `runtime.render()` has returned
    # successfully -- particularly for a fixed-signature renderer that
    # cannot receive a step-level callback, exactly like this mock -- a
    # cancellation only now observable is a request to stop *after*
    # successful use, not a runtime fault, and must not invalidate a
    # healthy entry.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s own pre-acquisition check (False)
        # call #2: cancelled_before_render (False) -- render() is called
        # call #3: the post-render snapshot, reached only after render()
        # already returned successfully (True)
        return call_count["n"] > 2

    with pytest.raises(GenerationCancelled):
        generator.run(_request(), context=GenerationContext(is_cancelled=is_cancelled))

    renderer.render.assert_called_once()
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0


def test_learned_video_late_precall_cancellation_does_not_prevent_invocation_or_invalidate(
    tmp_path, monkeypatch
):
    # Safety convergence pass, Codex finding "preserve the runtime on late
    # pre-render cancellation": LearnedVideoRuntime.render() used to run
    # its OWN pre-call `context.raise_if_cancelled()` check, independent of
    # VideoGenerator's own `cancelled_before_render` sample -- cancellation
    # observed by that second, redundant check escaped as
    # `GenerationCancelled` before the runtime callable was ever invoked,
    # yet was indistinguishable from a genuine mid-inference interruption
    # once it reached VideoGenerator's `with` block, conservatively
    # invalidating a runtime that was never actually used. That check has
    # been removed (generators/video/runtime.py); this proves the callable
    # is still reliably invoked exactly once even when cancellation is
    # already true by the time render() would have performed it, and that
    # the eventual cancellation (now only ever caught by VideoGenerator's
    # own post-render snapshot) still preserves the runtime.
    invoked = {"n": 0}

    def fake_renderer(**kwargs):
        invoked["n"] += 1
        return {"output_path": str(tmp_path / "out.mp4"), "output_id": "out"}

    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime="learned", display_name="fake video",
        )

    loader = Mock()
    loader.load.side_effect = lambda item: {
        "runtime_adapter": "learned_text_to_video", "renderer": fake_renderer,
    }
    cache = ModelRuntimeCache(max_entries=4)
    service = ModelService(
        registry=None, resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache, admission_capacity=1,
    )
    generator = VideoGenerator(service, output_dir=tmp_path)
    monkeypatch.setattr("generators.video.generator.evaluate_video_output", lambda *a: {})
    monkeypatch.setattr("generators.video.generator.evaluate_video_semantics", lambda *a: {})
    monkeypatch.setattr("generators.video.generator.enrich_quality_report", lambda *a: None)

    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s pre-acquisition check (False)
        # call #2: VideoGenerator's own cancelled_before_render sample
        # (False) -- render() is therefore called. Cancellation is "true"
        # for the rest of this run, including the exact position where the
        # now-removed internal pre-call check used to observe it and raise
        # before `fake_renderer` was ever invoked.
        return call_count["n"] > 2

    with pytest.raises(GenerationCancelled):
        generator.run(
            GenerationRequest(media_type="video", prompt="test", model_id="target"),
            context=GenerationContext(is_cancelled=is_cancelled),
        )

    # The callable ran to completion -- no longer silently skipped by the
    # removed redundant internal check.
    assert invoked["n"] == 1
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0


def test_invalid_procedural_numeric_params_do_not_invalidate_healthy_runtime(
    tmp_path, monkeypatch
):
    # Safety convergence pass, Codex finding "parse procedural video
    # parameters before leasing": ProceduralStoryboardRuntime.render()
    # coerces width/height/fps/duration_seconds/num_frames from the
    # request before touching runtime_obj or rendering a single frame --
    # a bad value is a pure input error, not a runtime fault.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    generator.runtime_router = SimpleNamespace(
        resolve=lambda runtime_obj: ProceduralStoryboardRuntime()
    )

    # Warm the cache with a real, successful procedural render first, so
    # there is a genuinely healthy runtime to protect.
    warm = generator.run(
        GenerationRequest(
            media_type="video", prompt="test", model_id="target",
            params={"duration_seconds": 2, "fps": 4},
        )
    )
    assert warm.status == "succeeded"
    cached_entry = cache._entries["target"]
    assert cached_entry.state is RuntimeState.READY

    with pytest.raises(ValueError):
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"width": "not-a-number"},
            )
        )

    assert cache._entries["target"] is cached_entry  # never reloaded
    assert cached_entry.state is RuntimeState.READY
    assert cached_entry.lease_count == 0
    assert loader.load.call_count == 1


def test_video_encoding_failure_after_successful_rendering_preserves_runtime(
    tmp_path, monkeypatch
):
    # Safety convergence pass, Codex finding "exclude video encoding from
    # the runtime fault boundary": ProceduralStoryboardRuntime.render()
    # already finished generating every frame (the actual runtime-use
    # interval) and returned `pending_frames`; only the post-lease GIF
    # encode (a disk-full/permission failure, simulated here) fails.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    generator.runtime_router = SimpleNamespace(
        resolve=lambda runtime_obj: ProceduralStoryboardRuntime()
    )

    def failing_encode(frames, output_dir, frame_duration_ms):
        raise OSError("simulated disk-full while encoding gif")

    monkeypatch.setattr("generators.video.generator.encode_frames_as_gif", failing_encode)

    with pytest.raises(OSError):
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"duration_seconds": 2, "fps": 4},
            )
        )

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert loader.load.call_count == 1
    assert list(tmp_path.glob("*.gif")) == []  # encoding never actually wrote anything


def test_render_exception_still_invalidates_used_runtime(tmp_path, monkeypatch):
    # The other half of the distinction every finding in this pass
    # requires: an exception the render() callable itself raises (not a
    # boundary/encoding/diagnostic failure) is a genuine runtime-use
    # failure and still conservatively invalidates.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    error = RuntimeError("simulated learned-runtime inference failure")
    renderer.render.side_effect = error

    with pytest.raises(RuntimeError) as caught:
        generator.run(_request())
    assert caught.value is error

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0
