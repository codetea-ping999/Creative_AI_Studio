"""Process-wide admission-domain sharing for CLIP/CLAP semantic backends.

Mirrors tests/test_video_lease_cancellation.py's / tests/test_speech_lease_cancellation.py's
structure (PR4b Semantic admission lane): every concurrency claim below is
proven with a real `RuntimeAdmissionController` -- the exact class
`ModelService` itself uses -- and deterministic synchronization (`Event` plus
an `_ObservedSemaphore`/`_ObservedLock` wrapper that fires the instant a
second thread genuinely reaches the real primitive), never a bare sleep, per
the concurrency-safety skill's deterministic-proof requirement.
"""

from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock
import wave

from PIL import Image
import pytest

from core.models import ModelService, RuntimeAdmissionController, get_default_admission_controller
from core.models.cache import ModelRuntimeCache
from core.quality.semantic import (
    ScoreCache,
    SemanticJudge,
    SemanticJudgeConfig,
    _build_cache_key,
    _ClapAudioBackend,
    _ClipImageBackend,
    _VideoFrameBackend,
)

_FAKE_SCORE = {
    "status": "scored",
    "media_type": "image",
    "mode": "local_transformers",
    "backend": "clip",
    "model_id": "fake-clip",
    "semantic_alignment_score": 50.0,
    "semantic_alignment_level": "usable",
    "details": {},
}

_FAKE_AUDIO_SCORE = {
    "status": "scored",
    "media_type": "audio",
    "mode": "local_transformers",
    "backend": "clap",
    "model_id": "fake-clap",
    "semantic_alignment_score": 50.0,
    "semantic_alignment_level": "usable",
    "details": {},
}


def _judge_config(tmp_path: Path, **overrides) -> SemanticJudgeConfig:
    defaults = dict(
        enabled=True,
        local_files_only=True,
        cache_dir=tmp_path / "semantic-cache",
        video_backend="image_frames",
        video_sample_frames=2,
        image_model_id="fake-clip",
        audio_model_id="fake-clap",
        video_model_id="fake-clip",
        image_model_path=None,
        audio_model_path=None,
        video_model_path=None,
        image_enabled=True,
        audio_enabled=True,
        video_enabled=True,
    )
    defaults.update(overrides)
    return SemanticJudgeConfig(**defaults)


def _write_wav(path: Path, *, seconds: float = 0.1, sample_rate: int = 8_000) -> None:
    sample_count = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * sample_count)


def _build_model_service(admission: RuntimeAdmissionController) -> ModelService:
    def manifest(model_id, media_type, task_type=None):
        return SimpleNamespace(
            id=model_id, public_model_id=model_id, provider="local", loader="fake",
            default_params={}, runtime=media_type, display_name="fake",
        )

    loader = Mock()
    loader.load.side_effect = lambda item: {"id": item.id}
    cache = ModelRuntimeCache(max_entries=4)
    return ModelService(
        registry=None, resolver=SimpleNamespace(resolve=manifest),
        loader_registry=SimpleNamespace(get=lambda name: loader),
        runtime_cache=cache, admission=admission,
    )


class _ObservedSemaphore:
    """Fires `attempted` the instant a caller reaches the real semaphore's
    own `acquire()` -- proving genuine contention, not a timing guess."""

    def __init__(self, semaphore, attempted: Event):
        self._semaphore = semaphore
        self._attempted = attempted

    def acquire(self, *args, **kwargs):
        self._attempted.set()
        return self._semaphore.acquire(*args, **kwargs)

    def release(self):
        return self._semaphore.release()


class _ObservedLock:
    """Fires `second_attempt` the instant a *second* caller reaches the real
    lock's own `acquire()` -- proving the first caller still holds it."""

    def __init__(self, real_lock: Lock, second_attempt: Event):
        self._lock = real_lock
        self._second_attempt = second_attempt
        self._enter_count = 0
        self._count_lock = Lock()

    def __enter__(self):
        with self._count_lock:
            self._enter_count += 1
            is_second = self._enter_count == 2
        if is_second:
            self._second_attempt.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self._lock.release()
        return False


# --------------------------------------------------------------------- wiring


def test_semantic_judge_defaults_to_the_process_wide_admission_domain(tmp_path):
    judge = SemanticJudge(_judge_config(tmp_path))
    assert judge.admission is get_default_admission_controller()
    assert judge.image_backend._admission is judge.admission
    assert judge.audio_backend._admission is judge.admission


def test_semantic_judge_uses_an_injected_admission_controller(tmp_path):
    private = RuntimeAdmissionController(capacity=3)
    judge = SemanticJudge(_judge_config(tmp_path), admission=private)
    assert judge.admission is private
    assert judge.image_backend._admission is private
    assert judge.audio_backend._admission is private
    assert judge.admission is not get_default_admission_controller()


# ------------------------------------------------------------- failure paths


def test_clip_load_exception_releases_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)

    def failing_load():
        raise RuntimeError("simulated from_pretrained failure")

    backend._load_runtime = failing_load
    image = Image.new("RGB", (4, 4))
    with pytest.raises(RuntimeError, match="simulated from_pretrained failure"):
        backend.score_image_object(image, prompt="test")

    # G released: a subsequent call is not left permanently blocked.
    backend._load_runtime = lambda: (object(), object())
    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)
    result = backend.score_image_object(image, prompt="test")
    assert result["status"] == "scored"


def test_clip_load_returning_none_releases_admission_and_reports_unavailable(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)
    backend._load_runtime = lambda: None
    backend._error = "simulated unavailable"

    image = Image.new("RGB", (4, 4))
    result = backend.score_image_object(image, prompt="test")
    assert result["status"] == "unavailable"
    assert result["reason"] == "simulated unavailable"

    backend._load_runtime = lambda: (object(), object())
    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)
    result2 = backend.score_image_object(image, prompt="test")
    assert result2["status"] == "scored"


def test_clip_inference_failure_releases_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)
    backend._load_runtime = lambda: (object(), object())

    def failing_score(*args, **kwargs):
        raise RuntimeError("simulated inference failure")

    backend._score_image = failing_score
    image = Image.new("RGB", (4, 4))
    with pytest.raises(RuntimeError, match="simulated inference failure"):
        backend.score_image_object(image, prompt="test")

    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)
    result = backend.score_image_object(image, prompt="test")
    assert result["status"] == "scored"


def test_clap_load_exception_releases_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClapAudioBackend(config, ScoreCache(config.cache_dir), admission)

    def failing_load():
        raise RuntimeError("simulated from_pretrained failure")

    backend._load_runtime = failing_load
    wav_path = tmp_path / "clap_load_fail.wav"
    _write_wav(wav_path)
    with pytest.raises(RuntimeError, match="simulated from_pretrained failure"):
        backend.evaluate(wav_path, "prompt a")

    backend._load_runtime = lambda: (object(), object())
    backend._score_audio = lambda *a, **k: dict(_FAKE_AUDIO_SCORE)
    result = backend.evaluate(wav_path, "prompt b")  # distinct prompt: no cache collision
    assert result["status"] == "scored"


def test_clap_load_returning_none_releases_admission_and_reports_unavailable(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClapAudioBackend(config, ScoreCache(config.cache_dir), admission)
    backend._load_runtime = lambda: None
    backend._error = "simulated unavailable"

    wav_path = tmp_path / "clap_unavailable.wav"
    _write_wav(wav_path)
    result = backend.evaluate(wav_path, "prompt a")
    assert result["status"] == "unavailable"
    assert result["reason"] == "simulated unavailable"

    backend._load_runtime = lambda: (object(), object())
    backend._score_audio = lambda *a, **k: dict(_FAKE_AUDIO_SCORE)
    result2 = backend.evaluate(wav_path, "prompt b")
    assert result2["status"] == "scored"


def test_clap_inference_failure_releases_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClapAudioBackend(config, ScoreCache(config.cache_dir), admission)
    backend._load_runtime = lambda: (object(), object())

    def failing_score(*args, **kwargs):
        raise RuntimeError("simulated inference failure")

    backend._score_audio = failing_score
    wav_path = tmp_path / "clap_infer_fail.wav"
    _write_wav(wav_path)
    with pytest.raises(RuntimeError, match="simulated inference failure"):
        backend.evaluate(wav_path, "prompt a")

    backend._score_audio = lambda *a, **k: dict(_FAKE_AUDIO_SCORE)
    result = backend.evaluate(wav_path, "prompt b")
    assert result["status"] == "scored"


# ------------------------------------------------------------------- warm runtime


def test_warm_clip_inference_still_acquires_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)
    backend._runtime = (object(), object())  # pre-warmed: _load_runtime() short-circuits
    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)

    acquire_calls = {"n": 0}
    release_calls = {"n": 0}
    original_acquire = admission.acquire
    original_release = admission.release

    def counting_acquire(deadline):
        acquire_calls["n"] += 1
        return original_acquire(deadline)

    def counting_release(owner_thread):
        release_calls["n"] += 1
        return original_release(owner_thread)

    admission.acquire = counting_acquire
    admission.release = counting_release

    image = Image.new("RGB", (4, 4))
    result = backend.score_image_object(image, prompt="warm test")

    assert result["status"] == "scored"
    assert acquire_calls["n"] == 1  # a warm, already-loaded runtime still goes through G
    assert release_calls["n"] == 1


def test_warm_clap_inference_still_acquires_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    backend = _ClapAudioBackend(config, ScoreCache(config.cache_dir), admission)
    backend._runtime = (object(), object())
    backend._score_audio = lambda *a, **k: dict(_FAKE_AUDIO_SCORE)

    acquire_calls = {"n": 0}
    original_acquire = admission.acquire

    def counting_acquire(deadline):
        acquire_calls["n"] += 1
        return original_acquire(deadline)

    admission.acquire = counting_acquire

    wav_path = tmp_path / "clap_warm.wav"
    _write_wav(wav_path)
    result = backend.evaluate(wav_path, "warm test")

    assert result["status"] == "scored"
    assert acquire_calls["n"] == 1


# --------------------------------------------------------- backend-local exclusion


def test_concurrent_cold_start_serializes_on_the_backend_lock(tmp_path):
    # capacity=2 so both threads may pass G itself concurrently -- what must
    # still hold is the backend-local lock preventing a double from_pretrained().
    admission = RuntimeAdmissionController(capacity=2)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)

    load_calls = {"n": 0}
    first_entered = Event()
    release_first = Event()
    second_attempted = Event()

    def fake_load_runtime():
        load_calls["n"] += 1
        if load_calls["n"] == 1:
            first_entered.set()
            release_first.wait(timeout=5)
        backend._runtime = (object(), object())
        return backend._runtime

    backend._load_runtime = fake_load_runtime
    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)
    backend._lock = _ObservedLock(Lock(), second_attempted)

    image = Image.new("RGB", (4, 4))
    results: dict[str, dict] = {}

    def call(name: str) -> None:
        results[name] = backend.score_image_object(image, prompt="test")

    thread_a = Thread(target=call, args=("a",))
    thread_a.start()
    assert first_entered.wait(timeout=5), "thread A never started its cold-start load"

    thread_b = Thread(target=call, args=("b",))
    thread_b.start()
    assert second_attempted.wait(timeout=5), "thread B never attempted the backend lock"
    # Deterministic: B has reached the real lock's own acquire() call, which
    # blocks because A still holds it (A is parked inside fake_load_runtime,
    # waiting on release_first). By program order this check is strictly
    # before B's own fake_load_runtime() call could possibly run.
    assert load_calls["n"] == 1, "a second cold-start load ran concurrently with the first"

    release_first.set()
    thread_a.join(5)
    thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert load_calls["n"] == 2  # both eventually ran, but never concurrently
    assert results["a"]["status"] == "scored"
    assert results["b"]["status"] == "scored"
    assert backend._runtime is not None


def test_capacity_two_admission_still_serializes_same_backend_inference(tmp_path):
    # The minimum required proof: two threads may both be admitted through G
    # at once (capacity=2), yet the same CLIP model object never executes
    # two inferences concurrently.
    admission = RuntimeAdmissionController(capacity=2)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)
    backend._runtime = (object(), object())  # pre-warmed

    call_count = {"n": 0}
    first_entered = Event()
    release_first = Event()
    second_attempted = Event()

    def fake_score_image(image, *, prompt, negative_prompt, processor, model):
        call_count["n"] += 1
        if call_count["n"] == 1:
            first_entered.set()
            release_first.wait(timeout=5)
        return dict(_FAKE_SCORE)

    backend._score_image = fake_score_image
    backend._lock = _ObservedLock(Lock(), second_attempted)

    image = Image.new("RGB", (4, 4))
    results: dict[str, dict] = {}

    def call(name: str) -> None:
        results[name] = backend.score_image_object(image, prompt="test")

    thread_a = Thread(target=call, args=("a",))
    thread_a.start()
    assert first_entered.wait(timeout=5), "thread A never started inference"

    thread_b = Thread(target=call, args=("b",))
    thread_b.start()
    assert second_attempted.wait(timeout=5), "thread B never attempted the backend lock"
    assert call_count["n"] == 1, "a second inference ran concurrently with the first"

    release_first.set()
    thread_a.join(5)
    thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert call_count["n"] == 2
    assert results["a"]["status"] == "scored"
    assert results["b"]["status"] == "scored"


# ------------------------------------------------------------------------ cache


def test_cache_hit_never_touches_admission(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path)
    score_cache = ScoreCache(config.cache_dir)
    backend = _ClipImageBackend(config, score_cache, admission)

    image_path = tmp_path / "out.png"
    Image.new("RGB", (4, 4)).save(image_path)
    cache_key = _build_cache_key(
        media_type="image",
        output_path=image_path,
        prompt="cached prompt",
        negative_prompt=None,
        model_ref=backend._model_ref(),
    )
    score_cache.put(cache_key, dict(_FAKE_SCORE))

    admission_touched = {"value": False}
    original_acquire = admission.acquire

    def spy_acquire(deadline):
        admission_touched["value"] = True
        return original_acquire(deadline)

    admission.acquire = spy_acquire
    backend._load_runtime = lambda: (_ for _ in ()).throw(
        AssertionError("must not load the runtime on a cache hit")
    )

    result = backend.evaluate(image_path, "cached prompt")
    assert result["status"] == "scored"
    assert result["cache_hit"] is True
    assert admission_touched["value"] is False


# -------------------------------------------------------- ModelService contention


def test_model_service_holding_admission_blocks_clip_semantic_scoring(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    service = _build_model_service(admission)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)

    clip_reached = Event()

    def observed_load_runtime():
        clip_reached.set()
        return (object(), object())

    backend._load_runtime = observed_load_runtime
    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)

    a_holds = Event()
    release_a = Event()

    def thread_a_work() -> None:
        with service.acquire_runtime("m", "image"):
            a_holds.set()
            release_a.wait(timeout=5)

    thread_a = Thread(target=thread_a_work)
    thread_a.start()
    assert a_holds.wait(timeout=5), "thread A never acquired ModelService's runtime"

    attempted = Event()
    admission._semaphore = _ObservedSemaphore(admission._semaphore, attempted)

    image = Image.new("RGB", (4, 4))
    b_finished = Event()
    b_result: dict[str, dict] = {}

    def thread_b_work() -> None:
        try:
            b_result["value"] = backend.score_image_object(image, prompt="test")
        finally:
            b_finished.set()

    thread_b = Thread(target=thread_b_work)
    thread_b.start()
    try:
        assert attempted.wait(2), "worker never attempted contended admission"
        # Deterministic: B has reached G's real semaphore acquire() call,
        # which blocks because A still holds the only slot. Its own further
        # progress (setting clip_reached) can only happen after that call
        # returns, i.e. after A releases -- so this check does not race B.
        assert not clip_reached.is_set()
    finally:
        release_a.set()
        thread_a.join(5)
        assert b_finished.wait(5)
        thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert clip_reached.is_set()
    assert b_result["value"]["status"] == "scored"


def test_clip_holding_admission_blocks_model_service_runtime_acquisition(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    service = _build_model_service(admission)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)
    backend._runtime = (object(), object())  # pre-warmed

    inference_started = Event()
    release_clip = Event()

    def fake_score_image(image, *, prompt, negative_prompt, processor, model):
        inference_started.set()
        release_clip.wait(timeout=5)
        return dict(_FAKE_SCORE)

    backend._score_image = fake_score_image

    image = Image.new("RGB", (4, 4))
    a_result: dict[str, dict] = {}

    def thread_a_work() -> None:
        a_result["value"] = backend.score_image_object(image, prompt="test")

    thread_a = Thread(target=thread_a_work)
    thread_a.start()
    assert inference_started.wait(timeout=5), "thread A never started CLIP inference"

    attempted = Event()
    admission._semaphore = _ObservedSemaphore(admission._semaphore, attempted)

    model_service_reached = Event()
    loader = service.loader_registry.get("fake")

    def observed_load(item):
        model_service_reached.set()
        return {"id": item.id}

    loader.load.side_effect = observed_load

    b_finished = Event()

    def thread_b_work() -> None:
        try:
            with service.acquire_runtime("m", "image"):
                pass
        finally:
            b_finished.set()

    thread_b = Thread(target=thread_b_work)
    thread_b.start()
    try:
        assert attempted.wait(2), "worker never attempted contended admission"
        assert not model_service_reached.is_set()
    finally:
        release_clip.set()
        thread_a.join(5)
        assert b_finished.wait(5)
        thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert model_service_reached.is_set()
    assert a_result["value"]["status"] == "scored"


def test_model_service_holding_admission_blocks_clap_semantic_scoring(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    service = _build_model_service(admission)
    config = _judge_config(tmp_path)
    backend = _ClapAudioBackend(config, ScoreCache(config.cache_dir), admission)

    clap_reached = Event()

    def observed_load_runtime():
        clap_reached.set()
        return (object(), object())

    backend._load_runtime = observed_load_runtime
    backend._score_audio = lambda *a, **k: dict(_FAKE_AUDIO_SCORE)

    a_holds = Event()
    release_a = Event()

    def thread_a_work() -> None:
        with service.acquire_runtime("m", "audio"):
            a_holds.set()
            release_a.wait(timeout=5)

    thread_a = Thread(target=thread_a_work)
    thread_a.start()
    assert a_holds.wait(timeout=5), "thread A never acquired ModelService's runtime"

    attempted = Event()
    admission._semaphore = _ObservedSemaphore(admission._semaphore, attempted)

    wav_path = tmp_path / "clap_contention.wav"
    _write_wav(wav_path)
    b_finished = Event()
    b_result: dict[str, dict] = {}

    def thread_b_work() -> None:
        try:
            b_result["value"] = backend.evaluate(wav_path, "test")
        finally:
            b_finished.set()

    thread_b = Thread(target=thread_b_work)
    thread_b.start()
    try:
        assert attempted.wait(2), "worker never attempted contended admission"
        assert not clap_reached.is_set()
    finally:
        release_a.set()
        thread_a.join(5)
        assert b_finished.wait(5)
        thread_b.join(5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert clap_reached.is_set()
    assert b_result["value"]["status"] == "scored"


# ------------------------------------------------------------------ nested ban


def test_generator_lease_then_semantic_evaluation_same_thread_fails_fast(tmp_path):
    admission = RuntimeAdmissionController(capacity=1)
    service = _build_model_service(admission)
    config = _judge_config(tmp_path)
    backend = _ClipImageBackend(config, ScoreCache(config.cache_dir), admission)
    backend._runtime = (object(), object())
    backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)

    image = Image.new("RGB", (4, 4))
    with service.acquire_runtime("m", "image"):
        with pytest.raises(RuntimeError, match="Nested acquire_runtime"):
            backend.score_image_object(image, prompt="test")

    # PR4a's nested-acquisition ban is unconditional but not sticky: once
    # the generator lease has actually released, the same thread acquiring
    # semantic G on its own is an ordinary, unrelated acquisition.
    result = backend.score_image_object(image, prompt="test")
    assert result["status"] == "scored"


# ------------------------------------------------------------------------- video


def test_video_backend_scores_frames_through_the_normal_admitted_path_without_nesting(
    tmp_path,
):
    admission = RuntimeAdmissionController(capacity=1)
    config = _judge_config(tmp_path, video_sample_frames=3)
    score_cache = ScoreCache(config.cache_dir)
    image_backend = _ClipImageBackend(config, score_cache, admission)
    image_backend._runtime = (object(), object())
    image_backend._score_image = lambda *a, **k: dict(_FAKE_SCORE)

    acquire_calls = {"n": 0}
    original_acquire = admission.acquire

    def counting_acquire(deadline):
        acquire_calls["n"] += 1
        return original_acquire(deadline)

    admission.acquire = counting_acquire

    video_backend = _VideoFrameBackend(config, score_cache, image_backend)
    frames = [Image.new("RGB", (4, 4), color=(i * 40, 0, 0)) for i in range(3)]
    video_path = tmp_path / "clip.gif"
    frames[0].save(video_path, save_all=True, append_images=frames[1:], duration=50, loop=0)

    result = video_backend.evaluate(video_path, "test prompt")

    assert result["status"] == "scored"
    # Each sampled frame acquired G independently, proving the video backend
    # never held one admission slot across the whole video -- no same-thread
    # nested acquisition (which the video backend's own G-less design makes
    # structurally impossible, since it never calls admission.acquire()
    # itself at all -- only the image backend's normal admitted path does,
    # once per frame).
    assert acquire_calls["n"] == config.video_sample_frames
