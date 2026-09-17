"""Cooperative cancellation at video lease boundaries, without real models."""

from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from core.jobs.context import GenerationCancelled, GenerationContext
from core.models.cache import ModelRuntimeCache
from core.models.runtime_lease import RuntimeBusyError, RuntimeState, RuntimeWaitTimeoutError
from core.models.service import ModelService
from core.schemas import GenerationRequest
import generators.video.generator as video_generator_module
from generators.video.generator import VideoGenerator
from generators.video.runtime import ProceduralStoryboardRuntime, encode_frames_as_gif


def _learned_video_generator(tmp_path, monkeypatch, *, renderer):
    """Build a VideoGenerator wired to a real LearnedVideoRuntime (not a mock)."""

    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime="learned", display_name="fake video",
        )

    loader = Mock()
    loader.load.side_effect = lambda item: {
        "runtime_adapter": "learned_text_to_video", "renderer": renderer,
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
    return generator, service, cache, loader


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


def test_precall_cancellation_probe_error_preserves_runtime_and_skips_render(
    tmp_path, monkeypatch
):
    # PR4b P2-A proof (fallible-probe case): production
    # `GenerationContext.is_cancelled()` reads JobRepository and can raise
    # (e.g. a transient DB failure). Observed here, before `runtime.render()`
    # is ever invoked, that is external bookkeeping, not a runtime fault --
    # the lease must exit cleanly (not invalidate) and the exact external
    # exception must be re-raised once it is gone, with render() never
    # called.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    original_acquire = service.acquire_runtime
    with original_acquire("target", "video") as handle:
        cached_runtime = handle.runtime

    probe_error = RuntimeError("job repository unavailable")

    def failing_is_cancelled() -> bool:
        raise probe_error

    with pytest.raises(RuntimeError) as caught:
        generator.run(
            _request(), context=GenerationContext(is_cancelled=failing_is_cancelled)
        )
    assert caught.value is probe_error
    renderer.render.assert_not_called()
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with original_acquire("target", "video", wait_timeout=0) as handle:
        assert handle.runtime is cached_runtime  # never reloaded
    assert loader.load.call_count == 1


def test_post_render_probe_error_preserves_runtime(tmp_path, monkeypatch):
    # Lane A follow-up (code-review finding): the post-render recheck
    # (`late_cancellation`) runs after `runtime.render()` has already
    # returned successfully. The same fallible-probe fault class the
    # pre-render probe guards against reaches this site too -- observed
    # here, it must not invalidate a runtime that just rendered correctly;
    # the lease must exit cleanly and the exact external exception must be
    # re-raised once it is gone.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    original_acquire = service.acquire_runtime
    with original_acquire("target", "video") as handle:
        cached_runtime = handle.runtime

    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s own pre-acquisition check (False)
        # call #2: cancelled_before_render (False) -- render() is called
        # call #3: the post-render recheck, right after render() already
        # returned successfully -- raises.
        if call_count["n"] >= 3:
            raise probe_error
        return False

    with pytest.raises(RuntimeError) as caught:
        generator.run(_request(), context=GenerationContext(is_cancelled=is_cancelled))

    assert caught.value is probe_error
    assert call_count["n"] == 3
    renderer.render.assert_called_once()
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with original_acquire("target", "video", wait_timeout=0) as handle:
        assert handle.runtime is cached_runtime  # never reloaded
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
    # Operation-scoped polling (PR #420): the contended wait happens inside
    # ONE checkpointed acquire_runtime() call, never a generator-side loop of
    # public calls. The generator's own wait_checkpoint is observed instead:
    # its second invocation can only follow a timed-out slice.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    cancelled, timed_out, retry_allowed = Event(), Event(), Event()
    results, errors = [], []
    acquire = service.acquire_runtime
    owner = acquire("owner", "video")
    acquire_calls: list[dict] = []

    def observed_acquire(*args, **kwargs):
        acquire_calls.append(dict(kwargs))
        checkpoint = kwargs["wait_checkpoint"]
        checkpoints = {"n": 0}

        def observed_checkpoint():
            checkpoints["n"] += 1
            if checkpoints["n"] == 2:
                timed_out.set()
                assert retry_allowed.wait(3), "owner never permitted retry"
            checkpoint()

        kwargs["wait_checkpoint"] = observed_checkpoint
        return acquire(*args, **kwargs)

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
        assert len(acquire_calls) == 1
        expected_poll_interval = video_generator_module._CANCELLATION_POLL_SECONDS
        assert acquire_calls[0]["poll_interval"] == expected_poll_interval
        assert "wait_timeout" not in acquire_calls[0]
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


def test_learned_video_precall_cancellation_skips_invocation_and_preserves_runtime(
    tmp_path, monkeypatch
):
    # Codex P2 finding "learned-video pre-call cancellation":
    # LearnedVideoRuntime.render() checks cancellation itself, immediately
    # before invoking the opaque renderer callable, closing the window
    # between VideoGenerator's own `cancelled_before_render` sample and
    # actual inference. An already-cancelled job must never start expensive
    # inference -- zero renderer calls -- and the check must not raise
    # `GenerationCancelled` from inside the lease (which would
    # conservatively invalidate a runtime this call never touched): it
    # returns a sentinel instead, so VideoGenerator lets the lease exit
    # cleanly before raising.
    invoked = {"n": 0}

    def fake_renderer(**kwargs):
        invoked["n"] += 1
        return {"output_path": str(tmp_path / "out.mp4"), "output_id": "out"}

    generator, service, cache, loader = _learned_video_generator(
        tmp_path, monkeypatch, renderer=fake_renderer
    )

    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s pre-acquisition check (False)
        # call #2: VideoGenerator's own cancelled_before_render sample
        # (False) -- render() is therefore entered.
        # call #3: LearnedVideoRuntime.render()'s own pre-invocation check,
        # immediately before calling `fake_renderer` (True).
        return call_count["n"] > 2

    with pytest.raises(GenerationCancelled):
        generator.run(
            GenerationRequest(media_type="video", prompt="test", model_id="target"),
            context=GenerationContext(is_cancelled=is_cancelled),
        )

    assert invoked["n"] == 0  # zero renderer calls
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert loader.load.call_count == 1  # runtime was loaded once, never reloaded


def test_learned_video_precall_probe_error_preserves_runtime(tmp_path, monkeypatch):
    # Lane A follow-up (code-review finding): LearnedVideoRuntime.render()'s
    # own pre-invocation cancellation check -- immediately before calling
    # the (potentially GPU-weight-resident) opaque renderer -- can itself
    # raise the same fallible-probe fault the generator's own
    # cancelled_before_render probe guards against, one call frame up.
    # Observed here, before the renderer is ever invoked, it must not
    # invalidate a healthy, unused runtime; the lease must exit cleanly and
    # the exact external exception must be re-raised once it is gone.
    invoked = {"n": 0}

    def fake_renderer(**kwargs):
        invoked["n"] += 1
        return {"output_path": str(tmp_path / "out.mp4"), "output_id": "out"}

    generator, service, cache, loader = _learned_video_generator(
        tmp_path, monkeypatch, renderer=fake_renderer
    )

    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s pre-acquisition check (False)
        # call #2: VideoGenerator's own cancelled_before_render sample
        # (False) -- render() is therefore entered.
        # call #3: LearnedVideoRuntime.render()'s own pre-invocation check,
        # immediately before calling `fake_renderer` -- raises.
        if call_count["n"] >= 3:
            raise probe_error
        return False

    with pytest.raises(RuntimeError) as caught:
        generator.run(
            GenerationRequest(media_type="video", prompt="test", model_id="target"),
            context=GenerationContext(is_cancelled=is_cancelled),
        )

    assert caught.value is probe_error
    assert call_count["n"] == 3
    assert invoked["n"] == 0  # zero renderer calls
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    with service.acquire_runtime("target", "video", wait_timeout=0):
        pass
    assert loader.load.call_count == 1  # runtime was loaded once, never reloaded


def test_invalid_learned_video_fps_does_not_invalidate_healthy_runtime(
    tmp_path, monkeypatch
):
    # Codex P2 finding "learned-video request parameter parsing": for
    # learned-video paths, a request-owned `fps` (used only to derive the
    # GIF frame duration when the adapter returns raw frames) used to be
    # converted only after expensive inference had already completed --
    # inside the active lease. A malformed value (e.g. `fps="bad"`) then
    # raised `ValueError` from inside the `with` block and incorrectly
    # invalidated a runtime whose inference genuinely succeeded. It is now
    # parsed only after the lease has released, so it fails as a plain
    # input error that never touches the runtime cache.
    invoked = {"n": 0}

    def fake_renderer(**kwargs):
        invoked["n"] += 1
        return [Image.new("RGB", (4, 4)) for _ in range(3)]

    generator, service, cache, loader = _learned_video_generator(
        tmp_path, monkeypatch, renderer=fake_renderer
    )

    warm = generator.run(
        GenerationRequest(media_type="video", prompt="test", model_id="target")
    )
    assert warm.status == "succeeded"
    cached_entry = cache._entries["target"]
    assert cached_entry.state is RuntimeState.READY
    assert invoked["n"] == 1

    with pytest.raises(ValueError):
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"fps": "bad"},
            )
        )

    # The renderer itself ran fine both times -- inference is not at fault.
    assert invoked["n"] == 2
    assert cache._entries["target"] is cached_entry  # never reloaded
    assert cached_entry.state is RuntimeState.READY
    assert cached_entry.lease_count == 0
    assert loader.load.call_count == 1


def test_learned_video_renderer_exception_still_invalidates_runtime(
    tmp_path, monkeypatch
):
    # The other half of finding 1's distinction: a genuine exception raised
    # by the opaque renderer callable itself (not the pre-call cancellation
    # check, not the deferred fps parse) is a real runtime-use failure and
    # must still conservatively invalidate.
    error = RuntimeError("simulated learned-runtime inference failure")

    def failing_renderer(**kwargs):
        raise error

    generator, service, cache, loader = _learned_video_generator(
        tmp_path, monkeypatch, renderer=failing_renderer
    )

    with pytest.raises(RuntimeError) as caught:
        generator.run(
            GenerationRequest(media_type="video", prompt="test", model_id="target")
        )
    assert caught.value is error

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0


def test_cancellation_during_deferred_gif_encode_removes_encoded_output(
    tmp_path, monkeypatch
):
    # Codex P2 finding "cancellation during deferred GIF encoding": GIF
    # encoding happens outside the runtime lease, but a cancellation
    # requested while it runs was previously never checked before
    # quality/semantic scoring -- JobRunner discards the cancelled result
    # afterwards, leaving the just-written GIF behind as an orphan file.
    # `cancelled` only flips to True once the real encode has actually
    # written the file, so this proves the check fires strictly after
    # encoding (not before) and that the file it wrote is cleaned up.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    generator.runtime_router = SimpleNamespace(
        resolve=lambda runtime_obj: ProceduralStoryboardRuntime()
    )
    cancelled = Event()

    def encode_then_cancel(frames, output_dir, frame_duration_ms):
        result = encode_frames_as_gif(frames, output_dir, frame_duration_ms)
        cancelled.set()
        return result

    monkeypatch.setattr(
        "generators.video.generator.encode_frames_as_gif", encode_then_cancel
    )

    with pytest.raises(GenerationCancelled):
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"duration_seconds": 2, "fps": 4},
            ),
            context=GenerationContext(is_cancelled=cancelled.is_set),
        )

    assert list(tmp_path.glob("*.gif")) == []  # orphaned encode output removed
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


def test_procedural_frame_loop_probe_error_preserves_runtime(tmp_path, monkeypatch):
    # Lane A/B follow-up (code-review finding): ProceduralStoryboardRuntime's
    # own per-frame cancellation check (frame_index=0, before any frame is
    # rendered) can itself raise the same fallible-probe fault the
    # generator's own cancelled_before_render probe guards against, one
    # call frame deeper. Observed here, it must not invalidate a healthy
    # runtime -- the lease must exit cleanly and the exact external
    # exception re-raised once it is gone. A genuine `GenerationCancelled`
    # (the ordinary case) is covered by
    # test_video_successful_render_with_late_cancellation_preserves_runtime
    # and the procedural-specific tests above; this proves only the
    # probe's own external exception is isolated, not the cancellation
    # semantics themselves.
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

    probe_error = RuntimeError("job repository unavailable")
    call_count = {"n": 0}

    def is_cancelled() -> bool:
        call_count["n"] += 1
        # call #1: _acquire_runtime()'s own pre-acquisition check (False)
        # call #2: VideoGenerator's own cancelled_before_render probe
        # (False) -- render() is therefore entered.
        # call #3: ProceduralStoryboardRuntime.render()'s own frame-loop
        # check, frame_index=0, before any frame is rendered -- raises.
        if call_count["n"] >= 3:
            raise probe_error
        return False

    with pytest.raises(RuntimeError) as caught:
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"duration_seconds": 2, "fps": 4},
            ),
            context=GenerationContext(is_cancelled=is_cancelled),
        )

    assert caught.value is probe_error
    assert call_count["n"] == 3
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


def test_malformed_runtime_owned_palette_invalidates_healthy_runtime(
    tmp_path, monkeypatch
):
    # Codex P2 finding "Video: restrict deferred procedural parameter
    # failures": the broad `except (ValueError, TypeError)` that used to
    # wrap the entire `render()` call also caught a genuine runtime
    # defect -- a malformed palette hex string *owned by the cached
    # runtime*, not the request -- and incorrectly treated it as a
    # harmless procedural-parameter input error. A bad palette must
    # invalidate; only the five request-owned numeric params (width,
    # height, fps, duration_seconds, num_frames) are input errors.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    generator.runtime_router = SimpleNamespace(
        resolve=lambda runtime_obj: ProceduralStoryboardRuntime()
    )
    # 6 hex characters but not valid hex digits -- passes the runtime's own
    # length check, then raises inside `_hex_to_rgb()`'s `int(..., 16)`.
    loader.load.side_effect = lambda item: {"id": item.id, "palette": ["zzzzzz"]}

    with pytest.raises(ValueError):
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"duration_seconds": 2, "fps": 4},
            )
        )

    entry = cache._entries["target"]
    assert entry.state is RuntimeState.INVALID
    assert entry.lease_count == 0


def test_procedural_progress_publication_failure_does_not_invalidate_runtime(
    tmp_path, monkeypatch
):
    # Codex P2 finding "Video: progress publication must not invalidate
    # procedural runtime": `context.report_progress()` writes through
    # JobRepository/event publication in production -- a failure there is
    # boundary bookkeeping, not a rendering fault, and must not escape the
    # active lease and invalidate an otherwise-healthy runtime. It must
    # still be raised, but only after the lease has released.
    generator, service, cache, loader, renderer = _build(tmp_path, monkeypatch)
    generator.runtime_router = SimpleNamespace(
        resolve=lambda runtime_obj: ProceduralStoryboardRuntime()
    )
    progress_error = RuntimeError("simulated JobRepository failure")

    def failing_on_progress(fraction):
        raise progress_error

    context = GenerationContext(
        is_cancelled=lambda: False,
        on_progress=failing_on_progress,
        min_interval_seconds=0,
        min_progress_delta=0,
    )

    with pytest.raises(RuntimeError) as caught:
        generator.run(
            GenerationRequest(
                media_type="video", prompt="test", model_id="target",
                params={"duration_seconds": 2, "fps": 4},
            ),
            context=context,
        )
    assert caught.value is progress_error

    # Rendering itself was never aborted by the progress failure.
    assert list(tmp_path.glob("*.gif")) == []  # raised before the deferred encode
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY
    assert entry.lease_count == 0
    assert loader.load.call_count == 1


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


def test_learned_video_direct_output_late_cancellation_removes_produced_artifact(
    tmp_path, monkeypatch
):
    # P2 finding "Learned Video: remove direct output artifact on late
    # cancellation": a learned renderer that writes its own file and
    # returns `output_path` directly (rather than `pending_frames`, the
    # deferred-encode case already covered by
    # `test_cancellation_during_deferred_gif_encode_removes_encoded_output`
    # above) leaves that file behind once `VideoGenerator.generate()`
    # observes late cancellation and raises `GenerationCancelled` --
    # `VideoGenerator.cleanup()` is a no-op, so JobRunner discarding the
    # cancelled result never removes it. `cancelled` only flips to True
    # once the fake renderer has actually written the real file and
    # returned, proving the check fires (and cleans up) strictly after a
    # successful direct-output render, not before.
    cancelled = Event()
    created_path = {}

    def fake_renderer(**kwargs):
        output_path = Path(kwargs["output_dir"]) / "direct_output.mp4"
        output_path.write_bytes(b"fake rendered video bytes")
        created_path["path"] = output_path
        cancelled.set()
        return {"output_path": str(output_path), "output_id": "direct"}

    generator, service, cache, loader = _learned_video_generator(
        tmp_path, monkeypatch, renderer=fake_renderer
    )

    with pytest.raises(GenerationCancelled):
        generator.run(
            GenerationRequest(media_type="video", prompt="test", model_id="target"),
            context=GenerationContext(is_cancelled=cancelled.is_set),
        )

    assert created_path["path"].exists() is False  # produced artifact removed
    assert list(tmp_path.glob("*")) == []  # no unrelated files touched either
    entry = cache._entries["target"]
    assert entry.state is RuntimeState.READY  # healthy runtime, not invalidated
    assert entry.lease_count == 0
    assert entry.lease_count == 0
