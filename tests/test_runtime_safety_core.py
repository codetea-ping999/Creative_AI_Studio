"""PR4a (issue #414): deterministic concurrency proofs for the runtime-safety
core -- lease/pin, per-runtime execution exclusion, process-wide admission,
load-before-capacity safety, canonical acquire/unload, and atomic
unload_all.

No sleep-based concurrency assertions anywhere in this file: every ordering
claim is proved either by a real mutual-exclusion primitive (the code under
test genuinely blocking one thread on another) combined with
`threading.Event`/`threading.Barrier` handoffs, or by a recorded event
*sequence* asserted only after every thread has fully joined -- never by a
short wait-then-check race. See `docs/model-system.md` for the production
contract this suite exists to hold in place.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
import unittest

from bootstrap.factories import create_default_model_service
from core.models.cache import ModelRuntimeCache
from core.models.loader import LoaderRegistry
from core.models.registry import ModelRegistry
from core.models.resolver import ModelResolver
from core.models.runtime_lease import RuntimeBusyError, RuntimeState
from core.models.service import ModelService, RuntimeAdmissionController
import core.models.service as service_module
from core.models.cloud_guard import CloudProviderDisabledError


class _FakeCancellation(BaseException):
    """A stand-in for a non-`Exception` interruption (a custom cancellation
    signal, e.g.) -- deliberately not literal `KeyboardInterrupt`/`SystemExit`
    so raising it in a test never actually interrupts the test runner, while
    still exercising the same "escapes a bare `except Exception:`" property
    that matters for Codex-review Finding 6 (round 1)."""


# --------------------------------------------------------------------------
# Shared fakes -- deliberately decoupled from any real loader/torch code so
# every test here proves cache/service concurrency semantics, never a real
# model's own behavior.
# --------------------------------------------------------------------------


class _FakeManifest:
    """Minimal stand-in satisfying every attribute `ModelService`/`ModelRuntimeCache`
    actually read off a manifest (see `core/models/service.py`'s own call sites)."""

    def __init__(self, model_id: str, *, loader: str = "fake", provider: str = "local"):
        self.id = model_id
        self.loader = loader
        self.provider = provider
        self.public_model_id = model_id


class _FakeResolver:
    """`.resolve()` returns a fixed manifest per requested id; `.resolve_manifest_id()`
    maps a public id/alias to its canonical id, falling back to identity (a
    manifest id, or an unknown id) -- the same contract `ModelResolver.resolve_manifest_id()`
    documents."""

    def __init__(
        self,
        manifests: dict[str, _FakeManifest],
        *,
        alias_map: dict[str, str] | None = None,
    ):
        self._manifests = manifests
        self._alias_map = alias_map or {}

    def resolve(self, model_id, media_type, task_type=None):
        canonical = self.resolve_manifest_id(model_id)
        return self._manifests[canonical]

    def resolve_manifest_id(self, model_id: str) -> str:
        return self._alias_map.get(model_id, model_id)


class _FakeLoaderRegistry:
    def __init__(self, loaders: dict[str, object]):
        self._loaders = loaders

    def get(self, name):
        return self._loaders[name]


class _ImmediateLoader:
    """Returns a fresh, distinct runtime object synchronously; counts calls."""

    def __init__(self):
        self.load_calls = 0

    def load(self, manifest):
        self.load_calls += 1
        return {"id": manifest.id, "instance": object()}


class _ControllableLoader:
    """Blocks inside `load()` until released -- for forcing a real in-flight window."""

    def __init__(
        self,
        *,
        started: Event | None = None,
        release: Event | None = None,
        sequence=None,
        seq_lock=None,
    ):
        self.load_calls = 0
        self.started = started or Event()
        self.release = release or Event()
        self._sequence = sequence
        self._seq_lock = seq_lock

    def _record(self, label: str) -> None:
        if self._sequence is not None:
            with self._seq_lock:
                self._sequence.append(label)

    def load(self, manifest):
        self.load_calls += 1
        self._record(f"{manifest.id}-load-start")
        self.started.set()
        assert self.release.wait(timeout=5), "release event was never set"
        self._record(f"{manifest.id}-load-end")
        return {"id": manifest.id, "instance": object()}


class _RecordingImmediateLoader:
    """Like `_ImmediateLoader`, but appends to a shared, lock-protected sequence."""

    def __init__(self, sequence: list, seq_lock: Lock):
        self.load_calls = 0
        self._sequence = sequence
        self._seq_lock = seq_lock

    def load(self, manifest):
        self.load_calls += 1
        with self._seq_lock:
            self._sequence.append(f"{manifest.id}-load-start")
            self._sequence.append(f"{manifest.id}-load-end")
        return {"id": manifest.id, "instance": object()}


class _FailNTimesLoader:
    """Raises `RuntimeError` on the first `fail_count` calls, then succeeds."""

    def __init__(self, fail_count: int = 1):
        self.load_calls = 0
        self.fail_count = fail_count

    def load(self, manifest):
        self.load_calls += 1
        if self.load_calls <= self.fail_count:
            raise RuntimeError(f"injected: load failure #{self.load_calls}")
        return {"id": manifest.id, "instance": object()}


class _RecordingCleanup:
    """`on_evict` hook: records every call; optionally blocks on an Event."""

    def __init__(self, *, block: Event | None = None, sequence=None, seq_lock=None):
        self.calls: list[str] = []
        self._block = block
        self._sequence = sequence
        self._seq_lock = seq_lock

    def __call__(self, model_id: str, runtime_obj: object) -> None:
        if self._sequence is not None:
            with self._seq_lock:
                self._sequence.append(f"{model_id}-cleanup-start")
        self.calls.append(model_id)
        if self._block is not None:
            assert self._block.wait(timeout=5), "cleanup release event was never set"
        if self._sequence is not None:
            with self._seq_lock:
                self._sequence.append(f"{model_id}-cleanup-end")


def _build_service(
    manifests: dict[str, _FakeManifest],
    loaders: dict[str, object],
    *,
    alias_map: dict[str, str] | None = None,
    max_entries: int = 1,
    media_limits: dict[str, int] | None = None,
    on_evict=None,
    admission_capacity: int = 1,
) -> tuple[ModelService, ModelRuntimeCache]:
    cache = ModelRuntimeCache(max_entries=max_entries, media_limits=media_limits, on_evict=on_evict)
    service = ModelService(
        registry=None,
        resolver=_FakeResolver(manifests, alias_map=alias_map),
        loader_registry=_FakeLoaderRegistry(loaders),
        runtime_cache=cache,
        admission_capacity=admission_capacity,
    )
    return service, cache


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class RuntimeSafetyCoreTests(unittest.TestCase):
    # ---------------------------------------------------------------- 1
    def test_pinned_eviction_returns_busy_without_touching_the_pinned_entry(self):
        manifest_a = _FakeManifest("model-a")
        manifest_b = _FakeManifest("model-b")
        loader_a = _ImmediateLoader()
        loader_b = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        # A generous admission capacity isolates the mechanism this test
        # actually targets -- the CACHE's own "no eligible eviction victim"
        # busy path -- from the separate, process-wide admission gate (G),
        # which would otherwise block B's second acquire attempt before it
        # ever reached the cache-capacity check at all (production defaults
        # to admission_capacity=1, where both mechanisms would normally be
        # exercised together; see test_uncached_loads_of_different_models_
        # serialize_through_admission for that one).
        service, cache = _build_service(
            {"model-a": manifest_a, "model-b": manifest_b},
            {"fake": loader_a},  # model-b uses a different loader name below
            max_entries=1,
            on_evict=cleanup,
            admission_capacity=10,
        )
        # Route model-b through its own loader instance.
        service.loader_registry._loaders["fake-b"] = loader_b
        manifest_b.loader = "fake-b"

        handle_a = service.acquire_runtime("model-a", "image")
        self.addCleanup(handle_a.release)

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-b", "image")

        self.assertEqual(loader_b.load_calls, 0)
        self.assertEqual(cleanup.calls, [])
        self.assertEqual(cache.loaded_ids(), ["model-a"])
        self.assertIs(handle_a.runtime, cache._entries["model-a"].runtime)

    # ---------------------------------------------------------------- 2
    def test_unload_model_refuses_a_leased_runtime_via_any_identifier(self):
        manifest_a = _FakeManifest("stable-diffusion-xl")
        loader = _ImmediateLoader()
        service, cache = _build_service(
            {"stable-diffusion-xl": manifest_a},
            {"fake": loader},
            alias_map={"sdxl": "stable-diffusion-xl", "sdxl-local": "stable-diffusion-xl"},
        )

        handle = service.acquire_runtime("sdxl", "image")
        self.addCleanup(handle.release)

        for identifier in ("sdxl", "sdxl-local", "stable-diffusion-xl"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(RuntimeBusyError):
                    service.unload_model(identifier)

        entry = cache._entries["stable-diffusion-xl"]
        self.assertEqual(entry.state, RuntimeState.READY)
        self.assertEqual(entry.lease_count, 1)
        self.assertIs(entry.runtime, handle.runtime)

    # ---------------------------------------------------------------- 3
    def test_replacement_of_a_leased_entry_is_refused_without_cleanup(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=cleanup,
        )

        handle = service.acquire_runtime("model-a", "image")
        self.addCleanup(handle.release)
        original_entry = cache._entries["model-a"]

        # Legacy put() attempting to replace the same canonical id directly.
        with self.assertRaises(RuntimeBusyError):
            cache.put("model-a", {"id": "model-a", "instance": object()}, media_type="image")

        self.assertEqual(cleanup.calls, [])
        self.assertIs(cache._entries["model-a"], original_entry)
        self.assertIs(cache._entries["model-a"].runtime, handle.runtime)

    # ---------------------------------------------------------------- 4
    def test_same_canonical_runtime_execution_is_mutually_exclusive(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, max_entries=2,
        )

        state_lock = Lock()
        state = {"current": 0, "max": 0}
        first_inside = Event()
        release_first = Event()
        second_done = Event()
        errors: list[BaseException] = []

        def bump():
            with state_lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])

        def unbump():
            with state_lock:
                state["current"] -= 1

        def first_worker():
            try:
                with service.acquire_runtime("model-a", "image"):
                    bump()
                    first_inside.set()
                    assert release_first.wait(timeout=5)
                    unbump()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def second_worker():
            try:
                with service.acquire_runtime("model-a", "image"):
                    bump()
                    unbump()
                    second_done.set()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = Thread(target=first_worker)
        t2 = Thread(target=second_worker)
        t1.start()
        self.assertTrue(first_inside.wait(timeout=5))
        t2.start()
        release_first.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(second_done.wait(timeout=5))
        self.assertEqual(state["max"], 1)
        self.assertEqual(loader.load_calls, 1)  # second caller hit the cache, never reloaded

    # ---------------------------------------------------------------- 5
    def test_public_id_alias_and_manifest_id_converge_on_one_entry(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_manifest(
                root / "image" / "sdxl-local.json",
                {
                    "id": "stable-diffusion-xl",
                    "public_id": "sdxl",
                    "display_name": "SDXL Local",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sdxl",
                    "loader": "fake",
                    "aliases": ["sdxl-local"],
                    "is_default": True,
                },
            )
            registry = ModelRegistry(manifest_root=root)
            resolver = ModelResolver(registry)
            loader = _ImmediateLoader()
            cache = ModelRuntimeCache(max_entries=1)
            service = ModelService(
                registry=registry,
                resolver=resolver,
                loader_registry=_FakeLoaderRegistry({"fake": loader}),
                runtime_cache=cache,
            )

            handle = service.acquire_runtime("sdxl", "image", "text-to-image")
            entry_via_public_id = cache._entries["stable-diffusion-xl"]
            self.assertEqual(entry_via_public_id.lease_count, 1)
            handle.release()
            self.assertEqual(entry_via_public_id.lease_count, 0)

            # Prove the *lease domain* itself is shared (not just that each
            # identifier resolves to the same entry) by pinning through two
            # different identifiers at once, at the cache layer directly --
            # `service.acquire_runtime()` also acquires E (execution
            # exclusivity), and this same thread holding a second, nested
            # lease on the SAME entry's execution lock would deadlock on E,
            # which is issue #414's OWN "same canonical entry exclusive"
            # invariant working as intended (see
            # test_same_canonical_runtime_execution_is_mutually_exclusive
            # for the cross-thread proof of that), not something this test
            # should trigger.
            public_canonical_id = resolver.resolve_manifest_id("sdxl")
            alias_canonical_id = resolver.resolve_manifest_id("sdxl-local")
            manifest_canonical_id = resolver.resolve_manifest_id("stable-diffusion-xl")
            self.assertEqual(public_canonical_id, alias_canonical_id)
            self.assertEqual(public_canonical_id, manifest_canonical_id)

            pinned_via_public_id = cache.acquire_or_reserve(public_canonical_id, "image")
            self.assertIs(pinned_via_public_id, entry_via_public_id)
            self.assertEqual(entry_via_public_id.lease_count, 1)

            pinned_via_alias = cache.acquire_or_reserve(alias_canonical_id, "image")
            self.assertIs(pinned_via_alias, entry_via_public_id)
            self.assertEqual(entry_via_public_id.lease_count, 2)  # same lease domain

            cache.release_lease(public_canonical_id, pinned_via_public_id)
            cache.release_lease(alias_canonical_id, pinned_via_alias)
            self.assertEqual(entry_via_public_id.lease_count, 0)

            handle3 = service.acquire_runtime("stable-diffusion-xl", "image", "text-to-image")
            self.assertIs(cache._entries["stable-diffusion-xl"], entry_via_public_id)
            self.assertIs(handle3._entry.execution_lock, entry_via_public_id.execution_lock)
            handle3.release()

            self.assertEqual(loader.load_calls, 1)  # loaded exactly once across all three ids

            # unload via a third identifier still reaches the one entry.
            service.unload_model("stable-diffusion-xl")
            self.assertEqual(cache.loaded_ids(), [])

    # ---------------------------------------------------------------- 6
    def test_uncached_loads_of_different_models_serialize_through_admission(self):
        sequence: list[str] = []
        seq_lock = Lock()
        a_started = Event()
        release_a = Event()
        loader_a = _ControllableLoader(
            started=a_started, release=release_a, sequence=sequence, seq_lock=seq_lock,
        )
        loader_b = _RecordingImmediateLoader(sequence, seq_lock)

        manifest_a = _FakeManifest("model-a", loader="loader-a")
        manifest_b = _FakeManifest("model-b", loader="loader-b")
        cache = ModelRuntimeCache(max_entries=2, media_limits={"image": 2})
        service = ModelService(
            registry=None,
            resolver=_FakeResolver({"model-a": manifest_a, "model-b": manifest_b}),
            loader_registry=_FakeLoaderRegistry({"loader-a": loader_a, "loader-b": loader_b}),
            runtime_cache=cache,
        )

        errors: list[BaseException] = []

        def worker_a():
            try:
                with service.acquire_runtime("model-a", "image"):
                    pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def worker_b():
            try:
                with service.acquire_runtime("model-b", "image"):
                    pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ta = Thread(target=worker_a)
        ta.start()
        self.assertTrue(a_started.wait(timeout=5))  # A holds G, blocked inside its own load()

        tb = Thread(target=worker_b)
        tb.start()
        # B cannot proceed past G until A releases it.
        release_a.set()
        ta.join(timeout=5)
        tb.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(loader_a.load_calls, 1)
        self.assertEqual(loader_b.load_calls, 1)
        # B's own load only ever starts after A's load fully finished -- if
        # admission were broken (both holding G "at once"), B's immediate
        # loader would have recorded its start/end while A was still parked
        # inside release_a.wait(), producing a different, interleaved order.
        self.assertEqual(
            sequence,
            ["model-a-load-start", "model-a-load-end", "model-b-load-start", "model-b-load-end"],
        )

    # ---------------------------------------------------------------- 7
    def test_load_failure_unwinds_reservation_lease_and_admission(self):
        manifest_a = _FakeManifest("model-a")
        loader = _FailNTimesLoader(fail_count=1)
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        with self.assertRaises(RuntimeError):
            service.acquire_runtime("model-a", "image")

        self.assertEqual(cache.loaded_ids(), [])
        self.assertNotIn("model-a", cache._entries)  # reservation fully unwound, no leak

        # G/L were both released on the failure path -- proved by a bounded,
        # non-blocking retry succeeding immediately rather than raising busy.
        handle = service.acquire_runtime("model-a", "image", wait_timeout=0)
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()
        self.assertEqual(loader.load_calls, 2)

    # ---------------------------------------------------------------- 8
    def test_pinned_only_budget_raises_busy_with_multiple_victims(self):
        manifest_a = _FakeManifest("model-a")
        manifest_b = _FakeManifest("model-b")
        manifest_c = _FakeManifest("model-c")
        loader = _ImmediateLoader()
        loader_c = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        # See the comment in test_pinned_eviction_... above: a generous
        # admission capacity isolates the cache-level "pinned-only budget"
        # mechanism from process-wide admission (G), which would otherwise
        # block a second/third simultaneous acquire_runtime() call before
        # it ever reached the cache-capacity check.
        service, cache = _build_service(
            {"model-a": manifest_a, "model-b": manifest_b, "model-c": manifest_c},
            {"fake": loader},
            max_entries=2,
            on_evict=cleanup,
            admission_capacity=10,
        )
        service.loader_registry._loaders["fake-c"] = loader_c
        manifest_c.loader = "fake-c"

        handle_a = service.acquire_runtime("model-a", "image")
        handle_b = service.acquire_runtime("model-b", "image")
        self.addCleanup(handle_a.release)
        self.addCleanup(handle_b.release)

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-c", "image")

        self.assertEqual(loader_c.load_calls, 0)
        self.assertEqual(cleanup.calls, [])
        self.assertEqual(sorted(cache.loaded_ids()), ["model-a", "model-b"])

    # ---------------------------------------------------------------- 9
    def test_unload_all_refuses_when_any_entry_is_leased(self):
        manifest_a = _FakeManifest("model-a")
        manifest_b = _FakeManifest("model-b")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        # A generous admission capacity lets A stay held while B is
        # separately acquired-then-released -- isolating unload_all()'s own
        # atomic-preflight behavior from process-wide admission (G).
        service, cache = _build_service(
            {"model-a": manifest_a, "model-b": manifest_b},
            {"fake": loader},
            max_entries=2,
            on_evict=cleanup,
            admission_capacity=10,
        )

        handle_a = service.acquire_runtime("model-a", "image")
        self.addCleanup(handle_a.release)
        handle_b = service.acquire_runtime("model-b", "image")
        handle_b.release()  # B is idle -- READY, unleased

        ids_before = cache.loaded_ids()

        with self.assertRaises(RuntimeBusyError):
            service.unload_all()

        self.assertEqual(cleanup.calls, [])
        self.assertEqual(cache.loaded_ids(), ids_before)  # LRU/order unchanged

    # ---------------------------------------------------------------- 10
    def test_unload_all_cleans_up_every_idle_entry_exactly_once(self):
        manifest_a = _FakeManifest("model-a")
        manifest_b = _FakeManifest("model-b")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a, "model-b": manifest_b},
            {"fake": loader},
            max_entries=2,
            on_evict=cleanup,
        )

        service.acquire_runtime("model-a", "image").release()
        service.acquire_runtime("model-b", "image").release()

        service.unload_all()

        self.assertEqual(sorted(cleanup.calls), ["model-a", "model-b"])
        self.assertEqual(cache.loaded_ids(), [])
        self.assertEqual(len(cache._entries), 0)

    # ---------------------------------------------------------------- 11
    def test_retiring_entry_is_never_reacquired_while_cleanup_is_in_flight(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        sequence: list[str] = []
        seq_lock = Lock()
        cleanup_release = Event()
        cleanup_started = Event()

        def on_evict(model_id, runtime_obj):
            with seq_lock:
                sequence.append(f"{model_id}-cleanup-start")
            cleanup_started.set()
            assert cleanup_release.wait(timeout=5)
            with seq_lock:
                sequence.append(f"{model_id}-cleanup-end")

        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=on_evict,
        )

        service.acquire_runtime("model-a", "image").release()  # idle, READY

        errors: list[BaseException] = []

        def unloader():
            try:
                service.unload_model("model-a")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t_unload = Thread(target=unloader)
        t_unload.start()
        self.assertTrue(cleanup_started.wait(timeout=5))

        # A concurrent acquire attempt while cleanup is mid-flight must be
        # refused immediately -- never block, never return the old runtime.
        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image", wait_timeout=0)

        cleanup_release.set()
        t_unload.join(timeout=5)
        self.assertEqual(errors, [])

        with seq_lock:
            self.assertEqual(sequence, ["model-a-cleanup-start", "model-a-cleanup-end"])

        # Now that cleanup is fully done, a fresh acquire succeeds and loads
        # a genuinely new runtime -- proving cleanup and reload never
        # overlapped (the sequence list would show an interleaved order if
        # a reload had started before "model-a-cleanup-end").
        handle = service.acquire_runtime("model-a", "image")
        try:
            self.assertEqual(loader.load_calls, 2)
        finally:
            handle.release()

    # ---------------------------------------------------------------- 12
    def test_stale_generation_release_never_decrements_a_newer_entrys_lease(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        handle = service.acquire_runtime("model-a", "image")
        old_entry = handle._entry
        handle.release()  # back to lease_count == 0, still generation 0

        cache.unload("model-a")  # fully retire generation 0

        handle2 = service.acquire_runtime("model-a", "image")
        new_entry = handle2._entry
        self.assertIsNot(new_entry, old_entry)
        self.assertEqual(new_entry.generation, old_entry.generation + 1)
        self.assertEqual(new_entry.lease_count, 1)

        # A stale release against the retired generation-0 entry must be a
        # pure no-op against the live (generation-1) entry.
        cache.release_lease("model-a", old_entry)

        self.assertEqual(new_entry.lease_count, 1)
        handle2.release()
        self.assertEqual(new_entry.lease_count, 0)

    # ---------------------------------------------------------------- 13
    def test_unleased_lru_eviction_is_unchanged_by_pr4a(self):
        cache = ModelRuntimeCache(max_entries=1)
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})

        self.assertFalse(cache.has("model-a"))
        self.assertTrue(cache.has("model-b"))
        self.assertEqual(cache.loaded_ids(), ["model-b"])

    # ---------------------------------------------------------------- 14
    def test_per_media_budget_is_unchanged_by_pr4a(self):
        cache = ModelRuntimeCache(max_entries=1, media_limits={"text": 1, "image": 1})
        cache.put("text-a", {"id": "text-a"}, media_type="text")
        cache.put("image-a", {"id": "image-a"}, media_type="image")

        self.assertTrue(cache.has("text-a"))
        self.assertTrue(cache.has("image-a"))
        self.assertEqual(set(cache.loaded_ids()), {"text-a", "image-a"})

        cache.put("text-b", {"id": "text-b"}, media_type="text")  # evicts text-a only
        self.assertFalse(cache.has("text-a"))
        self.assertTrue(cache.has("image-a"))
        self.assertTrue(cache.has("text-b"))

    # ---------------------------------------------------------------- 15
    def test_cloud_guard_denies_acquisition_before_any_admission_side_effect(self):
        manifest = _FakeManifest("cloud-model", provider="cloud")
        loader = _ImmediateLoader()
        service, cache = _build_service({"cloud-model": manifest}, {"fake": loader})

        # Simulate "cached while opt-in was previously on" via the legacy path.
        cache.put("cloud-model", {"id": "cloud-model", "instance": object()}, media_type="image")
        entry = cache._entries["cloud-model"]
        self.assertEqual(entry.lease_count, 0)

        with self.assertRaises(CloudProviderDisabledError):
            service.acquire_runtime("cloud-model", "image")

        # No pin, no admission slot consumed -- denial happened before any
        # side effect, so a normal, non-cloud acquisition attempt right
        # after must find the admission semaphore still fully available.
        self.assertEqual(entry.lease_count, 0)
        self.assertEqual(entry.state, RuntimeState.READY)

        other_manifest = _FakeManifest("local-model")
        service.resolver._manifests["local-model"] = other_manifest
        other_loader = _ImmediateLoader()
        service.loader_registry._loaders["fake-other"] = other_loader
        other_manifest.loader = "fake-other"
        handle = service.acquire_runtime("local-model", "image", wait_timeout=0)
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()

    # ---------------------------------------------------------------- 16
    def test_bootstrap_never_triggers_loader_or_model_work(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_manifest(
                root / "image" / "sdxl-local.json",
                {
                    "id": "sdxl-local",
                    "public_id": "sdxl",
                    "display_name": "SDXL Local",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sdxl",
                    "loader": "diffusers_image_loader",
                    "is_default": True,
                },
            )
            service = create_default_model_service(manifest_root=root)

            self.assertEqual(service.runtime_cache.loaded_ids(), [])
            self.assertEqual(len(service.runtime_cache._entries), 0)
            # Idle-application invariant: constructing the service graph
            # allocates only lightweight synchronization/data structures --
            # no loader has ever been asked to load anything.
            self.assertIsInstance(service.runtime_cache._metadata_lock, type(Lock()))

    # ------------------------------------------------- acquire_runtime deadline

    def test_acquire_runtime_timeout_zero_denies_when_admission_is_full(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        handle = service.acquire_runtime("model-a", "image")
        self.addCleanup(handle.release)

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image", wait_timeout=0)

        # No side effect from the denied attempt: the original holder's
        # lease is exactly what it was, and G still has zero free slots.
        self.assertEqual(cache._entries["model-a"].lease_count, 1)

    def test_acquire_runtime_timeout_zero_rolls_back_the_lease_when_e_times_out(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        # admission_capacity=2 so the second attempt reaches E (not G);
        # max_entries=2 so it also clears the cache-capacity check (not the
        # "pinned-only budget" busy path) and genuinely reaches E's own
        # timeout.
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, admission_capacity=2, max_entries=2,
        )

        handle = service.acquire_runtime("model-a", "image")
        self.addCleanup(handle.release)
        entry = cache._entries["model-a"]
        self.assertEqual(entry.lease_count, 1)

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image", wait_timeout=0)

        # The second attempt's own pin (taken by the acquire_or_reserve()
        # hit path) must have been rolled back once E's own acquisition
        # timed out -- never leaked, which would otherwise make this entry
        # permanently un-evictable/un-unloadable.
        self.assertEqual(entry.lease_count, 1)
        # G's second slot was released too -- proved by a bounded,
        # wait_timeout=0 third attempt for a DIFFERENT model succeeding
        # immediately.
        other_manifest = _FakeManifest("model-b")
        other_loader = _ImmediateLoader()
        service.resolver._manifests["model-b"] = other_manifest
        service.loader_registry._loaders["fake-b"] = other_loader
        other_manifest.loader = "fake-b"
        handle_b = service.acquire_runtime("model-b", "image", wait_timeout=0)
        try:
            self.assertIsNotNone(handle_b.runtime)
        finally:
            handle_b.release()

    def test_acquire_runtime_timeout_zero_denies_when_load_lock_is_held(self):
        manifest_a = _FakeManifest("model-a")
        started = Event()
        release = Event()
        loader = _ControllableLoader(started=started, release=release)
        # admission_capacity=2 so the second attempt's own G acquisition
        # succeeds and it genuinely reaches (and times out on) L, isolating
        # L's own timeout path from G's.
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, admission_capacity=2,
        )

        errors: list[BaseException] = []

        def first_worker():
            try:
                with service.acquire_runtime("model-a", "image"):
                    pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = Thread(target=first_worker)
        t1.start()
        self.assertTrue(started.wait(timeout=5))  # t1 holds G and L, blocked in load()

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image", wait_timeout=0)

        release.set()
        t1.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(loader.load_calls, 1)

    def test_release_is_idempotent(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        handle = service.acquire_runtime("model-a", "image")
        entry = cache._entries["model-a"]
        handle.release()
        self.assertEqual(entry.lease_count, 0)

        handle.release()  # must be a pure no-op, not a double-decrement
        self.assertEqual(entry.lease_count, 0)

    # ----------------------------------------------------- INVALID entries

    def test_invalid_entry_still_leased_by_another_caller_denies_new_acquires(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        handle = service.acquire_runtime("model-a", "image")
        entry = cache._entries["model-a"]
        # A second, independent lease on the same entry (e.g. a genuinely
        # concurrent caller elsewhere) -- taken directly at the cache layer
        # to avoid this same thread deadlocking on E (see
        # test_public_id_alias_and_manifest_id_converge_on_one_entry's own
        # comment for why).
        second_pin = cache.acquire_or_reserve("model-a", "image")
        self.assertIs(second_pin, entry)
        self.assertEqual(entry.lease_count, 2)

        # The first caller's use raised -- mark INVALID -- but the second
        # lease is still outstanding.
        handle.release(had_exception=True)
        self.assertEqual(entry.state, RuntimeState.INVALID)
        self.assertEqual(entry.lease_count, 1)

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image", wait_timeout=0)

        # Once the last outstanding lease releases, the (still INVALID,
        # now unleased) entry becomes a normal eviction/replacement
        # candidate for the next acquire -- reloaded fresh, never reused.
        cache.release_lease("model-a", entry)
        self.assertEqual(entry.lease_count, 0)

        handle2 = service.acquire_runtime("model-a", "image")
        try:
            self.assertIsNot(handle2._entry, entry)
            self.assertEqual(handle2._entry.state, RuntimeState.READY)
        finally:
            handle2.release()
        self.assertEqual(loader.load_calls, 2)

    # ------------------------------------- legacy put() pin-supremacy over budget

    def test_legacy_put_never_evicts_a_pinned_entry_even_over_budget(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader}, max_entries=1)

        handle = service.acquire_runtime("model-a", "image")
        self.addCleanup(handle.release)

        # A legacy caller inserting a second, unrelated runtime into the
        # same (default) bucket -- pin supremacy is absolute, so put() must
        # leave the bucket over its budget=1 rather than evict the pinned
        # entry; it has no busy/failure contract of its own to report that.
        cache.put("model-b", {"id": "model-b", "instance": object()})

        self.assertTrue(cache.has("model-a"))
        self.assertTrue(cache.has("model-b"))
        self.assertEqual(set(cache.loaded_ids()), {"model-a", "model-b"})
        self.assertEqual(cache._entries["model-a"].lease_count, 1)


    # ---------------------------- Codex round-1 review (PR #415), 8 findings

    # ---------------------------------------------------- Finding 1 (P1)
    def test_admission_is_shared_process_wide_across_model_service_instances(self):
        # Force a pristine, freshly-created shared default controller for
        # this test regardless of what earlier tests in this process may
        # have touched (every other test in this file passes an explicit
        # `admission_capacity`, which never touches the shared default -- see
        # `_build_service()` -- so this reset is defensive, not a workaround
        # for real cross-test pollution).
        service_module._default_admission_controller = None

        manifest_a = _FakeManifest("model-a")
        manifest_b = _FakeManifest("model-b")

        state_lock = Lock()
        state = {"current": 0, "max": 0}

        def bump():
            with state_lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])

        def unbump():
            with state_lock:
                state["current"] -= 1

        class _BumpingLoader:
            def __init__(self, started: Event, release: Event):
                self.load_calls = 0
                self.started = started
                self.release = release

            def load(self, manifest):
                self.load_calls += 1
                bump()
                self.started.set()
                assert self.release.wait(timeout=5)
                unbump()
                return {"id": manifest.id, "instance": object()}

        a_started = Event()
        release_a = Event()
        b_started = Event()
        release_b = Event()
        loader_a = _BumpingLoader(a_started, release_a)
        loader_b = _BumpingLoader(b_started, release_b)

        # Two SEPARATE ModelService instances -- mimicking two independent
        # bootstrap/factories.py create_default_model_service() call sites
        # (e.g. two standalone generator factories each constructing their
        # own default service). Neither passes admission/admission_capacity,
        # so both must fall back to ONE shared, process-wide default
        # controller (Finding 1) rather than each getting its own private
        # admission slot.
        service_a = ModelService(
            registry=None,
            resolver=_FakeResolver({"model-a": manifest_a}),
            loader_registry=_FakeLoaderRegistry({"fake": loader_a}),
            runtime_cache=ModelRuntimeCache(max_entries=1),
        )
        service_b = ModelService(
            registry=None,
            resolver=_FakeResolver({"model-b": manifest_b}),
            loader_registry=_FakeLoaderRegistry({"fake": loader_b}),
            runtime_cache=ModelRuntimeCache(max_entries=1),
        )

        # Codex re-review: the primary, scheduling-independent proof --
        # both instances hold the exact same controller object. (The
        # concurrency proof below is a secondary, *behavioral* check that
        # sharing actually blocks concurrent access in practice; on its
        # own, if the scheduler happened to run worker_b only after A had
        # already released, the old independent-semaphore bug would also
        # have recorded state["max"] == 1, since neither semaphore would
        # ever have been contended -- this identity assertion has no such
        # gap.)
        self.assertIs(service_a._admission, service_b._admission)

        errors: list[BaseException] = []

        def worker_a():
            try:
                with service_a.acquire_runtime("model-a", "image"):
                    pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def worker_b():
            try:
                with service_b.acquire_runtime("model-b", "image"):
                    pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ta = Thread(target=worker_a)
        ta.start()
        self.assertTrue(a_started.wait(timeout=5))  # A holds the shared G slot

        tb = Thread(target=worker_b)
        tb.start()
        # B cannot proceed past the (shared) G until A releases it -- a
        # same-ModelService-only test could never distinguish this from
        # ordinary same-instance serialization; using two independent
        # instances is what actually proves the domain is shared.
        release_a.set()
        ta.join(timeout=5)
        self.assertTrue(b_started.wait(timeout=5))
        release_b.set()
        tb.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(state["max"], 1)  # never both "in flight" at once
        self.assertEqual(loader_a.load_calls, 1)
        self.assertEqual(loader_b.load_calls, 1)

    # ---------------------------------------------------- Finding 7 (P2)
    def test_admission_controller_rejects_nonpositive_capacity(self):
        for bad_capacity in (0, -1):
            with self.subTest(capacity=bad_capacity):
                with self.assertRaises(ValueError):
                    RuntimeAdmissionController(bad_capacity)

    # ---------------------------------------------------- Finding 2 (P2)
    def test_invalid_marking_is_visible_to_an_e_waiter_before_it_wins_e(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        # admission_capacity=2 so B's own acquire_runtime() call reaches E
        # (not denied earlier by G) -- isolating the E/INVALID race this
        # test targets from ordinary G contention.
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, admission_capacity=2,
        )

        handle_a = service.acquire_runtime("model-a", "image")
        entry = handle_a._entry
        self.assertEqual(entry.lease_count, 1)

        b_thread_started = Event()
        errors: list[BaseException] = []
        b_result: dict[str, object] = {}

        def worker_b():
            b_thread_started.set()
            try:
                b_result["handle"] = service.acquire_runtime("model-a", "image")
            except RuntimeBusyError as exc:
                b_result["busy"] = exc
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        tb = Thread(target=worker_b)
        tb.start()
        self.assertTrue(b_thread_started.wait(timeout=5))

        # A's use of the runtime failed. Release with had_exception=True
        # while B is (or is about to be) contending for E via its own
        # acquire_runtime() call. This is deterministic regardless of the
        # exact scheduling interleaving: `RuntimeHandle.release()` marks
        # INVALID (under a short M transaction) strictly *before* it calls
        # `entry.execution_lock.release()`, in that order, on this same
        # thread; and a `Lock.acquire()` can never return `True` before a
        # matching `release()` call has already completed. So by the time
        # B's own `entry.execution_lock.acquire()` returns -- whether B was
        # already parked waiting, or arrives afterward -- the INVALID
        # transition has unconditionally already happened, and B's own
        # revalidation (`is_current_and_ready()`) is guaranteed to see it.
        handle_a.release(had_exception=True)
        self.assertEqual(entry.state, RuntimeState.INVALID)

        tb.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertNotIn("handle", b_result)  # B's user code must never run
        self.assertIsInstance(b_result.get("busy"), RuntimeBusyError)

        # Zero lease leak, zero G leak from B's own failed attempt: A's own
        # lease was already released above, and B's own pin -- taken, then
        # unwound, inside its own acquire_runtime() call -- left nothing
        # behind.
        self.assertEqual(entry.lease_count, 0)
        # A fresh, immediate (wait_timeout=0) acquisition succeeds -- proving
        # neither the lease nor the process-wide admission slot were leaked.
        handle_retry = service.acquire_runtime("model-a", "image", wait_timeout=0)
        try:
            self.assertIsNotNone(handle_retry.runtime)
        finally:
            handle_retry.release()

    # ---------------------------------------------------- Finding 3 (P2)
    def test_capacity_reservation_is_atomic_for_racing_misses_into_an_empty_bucket(self):
        manifest_a = _FakeManifest("model-a", loader="loader-a")
        manifest_b = _FakeManifest("model-b", loader="loader-b")
        a_started = Event()
        release_a = Event()
        loader_a = _ControllableLoader(started=a_started, release=release_a)
        loader_b = _ImmediateLoader()

        # budget=1 for "image"; the bucket starts completely empty.
        cache = ModelRuntimeCache(max_entries=5, media_limits={"image": 1})
        service = ModelService(
            registry=None,
            resolver=_FakeResolver({"model-a": manifest_a, "model-b": manifest_b}),
            loader_registry=_FakeLoaderRegistry({"loader-a": loader_a, "loader-b": loader_b}),
            runtime_cache=cache,
            admission_capacity=2,
        )

        errors: list[BaseException] = []
        results: dict[str, object] = {}

        def worker_a():
            try:
                results["a"] = service.acquire_runtime("model-a", "image")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ta = Thread(target=worker_a)
        ta.start()
        # A holds G+L, blocked inside load() -- its LOADING reservation was
        # already created atomically (Finding 3) before load() was ever
        # called.
        self.assertTrue(a_started.wait(timeout=5))

        self.assertEqual(len(cache._entries), 1)
        self.assertEqual(cache._entries["model-a"].state, RuntimeState.LOADING)

        # B races the SAME (now full, budget=1) bucket for a DIFFERENT id,
        # concurrently -- must be refused immediately, never silently
        # overbooking the bucket and never starting its own load.
        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-b", "image")
        self.assertEqual(loader_b.load_calls, 0)
        self.assertEqual(len(cache._entries), 1)  # still only A's reservation

        release_a.set()
        ta.join(timeout=5)
        self.assertEqual(errors, [])

        handle_a = results["a"]
        try:
            self.assertIsNotNone(handle_a.runtime)
        finally:
            handle_a.release()
        self.assertEqual(loader_a.load_calls, 1)

    # ---------------------------------------------------- Finding 4 (P2)
    def test_resolve_runtime_disposes_the_loaded_runtime_when_publication_is_rejected(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=cleanup,
        )

        # Force put()'s own publication to be rejected: a LOADING
        # reservation for this exact id, created out-of-band (as a
        # concurrent acquire_runtime() call in flight would), makes put()
        # see a busy existing entry once resolve_runtime() reaches it.
        reservation = cache.acquire_or_reserve("model-a", "image")
        self.assertEqual(reservation.state, RuntimeState.LOADING)

        with self.assertRaises(RuntimeBusyError):
            service.resolve_runtime("model-a", "image")

        self.assertEqual(loader.load_calls, 1)  # the load itself succeeded...
        self.assertEqual(cleanup.calls, ["model-a"])  # ...but was disposed exactly once
        # The pre-existing reservation itself was never touched by the
        # rejected publication attempt -- put() raises before mutating
        # `_entries` in this case.
        self.assertIs(cache._entries["model-a"], reservation)
        self.assertEqual(reservation.state, RuntimeState.LOADING)

    # ------------------------- bonus: found during this round's own adversarial review
    def test_acquire_runtime_disposes_the_loaded_runtime_when_publish_is_rejected(self):
        # `publish_ready_and_pin()` only raises its own defensive backstop
        # if a LOADING reservation was somehow lost before publish --
        # believed unreachable in production given `_acquire_or_load_entry()`
        # holds L for its entire duration, but the analogous gap on the
        # legacy `resolve_runtime()` path (Finding 4) shows this class of
        # bug is real when it does happen, so `_acquire_or_load_entry()`
        # disposes the freshly-loaded runtime the same way. Forced here via
        # a stand-in for `publish_ready_and_pin()` rather than by actually
        # reaching the believed-unreachable precondition-violation.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=cleanup,
        )

        def failing_publish(canonical_id, reserved_entry, runtime_obj):
            raise RuntimeBusyError("simulated: reservation lost before publish")

        cache.publish_ready_and_pin = failing_publish

        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image")

        self.assertEqual(loader.load_calls, 1)  # the load itself succeeded...
        self.assertEqual(cleanup.calls, ["model-a"])  # ...but was disposed exactly once

    def test_acquire_runtime_releases_the_orphaned_lease_when_publish_mutates_then_raises(self):
        # Codex's own review of the fix above (round 1-of-round-1) found a
        # real gap: publish_ready_and_pin() mutates `entry` in place (READY,
        # lease_count=1, runtime set) *before* it can raise -- an async
        # BaseException landing right after that mutation but before the
        # call returns (believed rare, but KeyboardInterrupt/SystemExit can
        # land between any two bytecode instructions in CPython) would have
        # made the old fix wrongly dispose an already-published, now-live
        # cached runtime, corrupting it out from under any other caller,
        # while also leaking the lease this call took (no RuntimeHandle is
        # ever returned to release it). Simulated here by actually
        # performing the publish, then raising.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=cleanup,
        )

        original_publish = cache.publish_ready_and_pin

        def publish_then_raise(canonical_id, reserved_entry, runtime_obj):
            original_publish(canonical_id, reserved_entry, runtime_obj)
            raise _FakeCancellation("injected: interrupted right after publish")

        cache.publish_ready_and_pin = publish_then_raise

        with self.assertRaises(_FakeCancellation):
            service.acquire_runtime("model-a", "image")

        entry = cache._entries["model-a"]
        # Published successfully -- must NOT have been disposed (that would
        # corrupt a runtime other callers can now see as cached).
        self.assertEqual(cleanup.calls, [])
        self.assertEqual(entry.state, RuntimeState.READY)
        # The lease this call took is released, not orphaned.
        self.assertEqual(entry.lease_count, 0)

        # The entry is fully usable afterward -- never disposed, never
        # reloaded.
        handle = service.acquire_runtime("model-a", "image")
        try:
            self.assertIs(handle.runtime, entry.runtime)
        finally:
            handle.release()
        self.assertEqual(loader.load_calls, 1)

    # ------------------------- bonus 2: legacy put() overflow-victim visibility
    def test_legacy_put_overflow_victim_stays_visible_until_cleanup_finishes(self):
        # Also found by Codex's round-1-of-round-1 re-review: the old
        # `_evict_bucket_overflow_locked()` popped its victim from
        # `_entries` immediately, under M, well before that victim's
        # cleanup (run afterward, outside M, by put()'s own caller code)
        # ever ran. In the window between M being released and cleanup
        # actually finishing, the victim's id looked like a plain cache
        # miss to any concurrent caller -- unlike every other
        # eviction/retirement path in this class, which marks RETIRING and
        # keeps the entry visible (busy) until cleanup completes.
        manifest_a = _FakeManifest("model-a")
        loader_a = _ImmediateLoader()
        cleanup_started = Event()
        cleanup_release = Event()

        def blocking_cleanup(model_id, runtime_obj):
            cleanup_started.set()
            assert cleanup_release.wait(timeout=5)

        cache = ModelRuntimeCache(max_entries=1, on_evict=blocking_cleanup)
        service = ModelService(
            registry=None,
            resolver=_FakeResolver({"model-a": manifest_a}),
            loader_registry=_FakeLoaderRegistry({"fake": loader_a}),
            runtime_cache=cache,
        )

        cache.put("model-a", {"id": "model-a", "instance": object()})  # occupies the sole slot

        errors: list[BaseException] = []

        def evictor():
            try:
                # A second legacy put() for a different id overflows
                # model-a out of the (budget=1) default bucket.
                cache.put("model-b", {"id": "model-b", "instance": object()})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = Thread(target=evictor)
        t.start()
        self.assertTrue(cleanup_started.wait(timeout=5))

        # model-a's cleanup is mid-flight (blocked) -- it must still be
        # visible as RETIRING (busy), never silently absent.
        self.assertEqual(cache._entries["model-a"].state, RuntimeState.RETIRING)
        with self.assertRaises(RuntimeBusyError):
            service.acquire_runtime("model-a", "image", wait_timeout=0)
        self.assertEqual(loader_a.load_calls, 0)  # no competing load started

        cleanup_release.set()
        t.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertNotIn("model-a", cache._entries)
        self.assertTrue(cache.has("model-b"))

        # Now a fresh acquire for model-a succeeds and genuinely reloads.
        handle = service.acquire_runtime("model-a", "image")
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()
        self.assertEqual(loader_a.load_calls, 1)

    # ------------------------- bonus 3: legacy put() same-object busy-state ordering
    def test_put_rejects_same_object_reinsertion_of_a_retiring_entry(self):
        # Also found by Codex's round-1-of-round-1 re-review: the
        # same-object fast path ran *before* the busy-state check, so a
        # caller retaining a reference to a RETIRING entry's runtime (an
        # unload() already in flight, cleanup mid-run outside M) could
        # still pass that same object back into put(), moving it to a
        # different bucket and triggering eviction there -- all while
        # _finish_retirement() was concurrently tearing the exact same
        # object down and about to remove the entry regardless.
        cleanup_started = Event()
        cleanup_release = Event()

        def blocking_cleanup(model_id, runtime_obj):
            cleanup_started.set()
            assert cleanup_release.wait(timeout=5)

        cache = ModelRuntimeCache(
            max_entries=1, media_limits={"image": 1, "text": 1}, on_evict=blocking_cleanup,
        )
        runtime_a = {"id": "model-a", "instance": object()}
        cache.put("model-a", runtime_a, media_type="image")

        errors: list[BaseException] = []

        def unloader():
            try:
                cache.unload("model-a")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = Thread(target=unloader)
        t.start()
        self.assertTrue(cleanup_started.wait(timeout=5))
        self.assertEqual(cache._entries["model-a"].state, RuntimeState.RETIRING)

        # A caller still holding the old runtime object must not be able to
        # move/revive it into another bucket while it is RETIRING.
        with self.assertRaises(RuntimeBusyError):
            cache.put("model-a", runtime_a, media_type="text")

        cleanup_release.set()
        t.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertNotIn("model-a", cache._entries)

    # ------------------------- bonus 4: loader-lookup-failure reservation unwind
    def test_acquire_runtime_aborts_reservation_when_loader_lookup_fails(self):
        # Also found by Codex's round-1-of-round-1 re-review (P1):
        # `loader_registry.get()` used to run *before* the try block that
        # aborts the reservation on failure, so a manifest naming an
        # unregistered loader left the fresh LOADING reservation stuck
        # forever -- permanently denying every future acquisition for that
        # id, and (with a single-entry bucket) blocking every other model
        # sharing it too. Uses the real `LoaderRegistry`, not the fake one,
        # so this exercises its actual `LookupError`.
        manifest_a = _FakeManifest("model-a", loader="unregistered-loader")
        service, cache = _build_service(
            {"model-a": manifest_a}, {}, max_entries=1,
        )
        service.loader_registry = LoaderRegistry()  # empty -- no loaders registered

        with self.assertRaises(LookupError):
            service.acquire_runtime("model-a", "image")

        # The reservation is fully unwound -- not stuck at LOADING.
        self.assertNotIn("model-a", cache._entries)

        # A subsequent, correctly-configured attempt succeeds normally.
        real_loader = _ImmediateLoader()
        service.loader_registry.register("unregistered-loader", real_loader)
        handle = service.acquire_runtime("model-a", "image")
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()
        self.assertEqual(real_loader.load_calls, 1)

    # ---------------------------------------------------- Finding 5 (P2)
    def test_same_object_bucket_move_enforces_the_destination_budget(self):
        cleanup = _RecordingCleanup()
        cache = ModelRuntimeCache(
            max_entries=1, media_limits={"image": 1, "text": 1}, on_evict=cleanup,
        )

        runtime_a = {"id": "model-a", "instance": object()}
        cache.put("model-a", runtime_a, media_type="image")
        cache.put("model-b", {"id": "model-b", "instance": object()}, media_type="text")
        self.assertEqual(set(cache.loaded_ids()), {"model-a", "model-b"})

        # Move model-a's SAME runtime object from "image" into "text" -- a
        # bucket that is already at its own budget=1 (occupied by model-b).
        cache.put("model-a", runtime_a, media_type="text")

        # model-a survives (it is the object being moved, excluded from its
        # own eviction candidacy); model-b -- the destination bucket's prior
        # LRU occupant -- is evicted exactly once to keep "text" at budget.
        self.assertTrue(cache.has("model-a"))
        self.assertFalse(cache.has("model-b"))
        self.assertEqual(cache._entries["model-a"].media_bucket, "text")
        self.assertEqual(cleanup.calls, ["model-b"])
        text_bucket_count = sum(
            1 for entry in cache._entries.values() if entry.media_bucket == "text"
        )
        self.assertEqual(text_bucket_count, 1)

    # ---------------------------------------------------- Finding 6 (P2)
    def test_finish_retirement_never_strands_retiring_after_a_base_exception(self):
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()

        def failing_cleanup(model_id, runtime_obj):
            raise _FakeCancellation("injected: cleanup interrupted")

        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=failing_cleanup,
        )

        service.acquire_runtime("model-a", "image").release()  # idle, READY

        with self.assertRaises(_FakeCancellation):
            service.unload_model("model-a")

        # No permanently-RETIRING zombie: the entry is fully gone, not stuck.
        self.assertNotIn("model-a", cache._entries)

        # Future calls for the same id can make progress -- a fresh acquire
        # reloads cleanly rather than being refused as still "busy".
        handle = service.acquire_runtime("model-a", "image")
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()
        self.assertEqual(loader.load_calls, 2)

    def test_unload_all_finalizes_every_target_even_after_a_base_exception(self):
        manifest_a = _FakeManifest("model-a")
        manifest_b = _FakeManifest("model-b")
        loader = _ImmediateLoader()
        cleanup_calls: list[str] = []

        def mixed_cleanup(model_id, runtime_obj):
            cleanup_calls.append(model_id)
            if model_id == "model-a":
                raise _FakeCancellation("injected: model-a's cleanup interrupted")

        service, cache = _build_service(
            {"model-a": manifest_a, "model-b": manifest_b},
            {"fake": loader},
            max_entries=2,
            on_evict=mixed_cleanup,
        )

        service.acquire_runtime("model-a", "image").release()
        service.acquire_runtime("model-b", "image").release()

        with self.assertRaises(_FakeCancellation):
            service.unload_all()

        # BOTH targets were finalized -- model-b's cleanup was attempted too,
        # not skipped just because model-a's raised first.
        self.assertEqual(cleanup_calls, ["model-a", "model-b"])
        # Neither is left stuck at RETIRING.
        self.assertEqual(cache.loaded_ids(), [])
        self.assertEqual(len(cache._entries), 0)

        # Future calls can make progress for both ids.
        service.acquire_runtime("model-a", "image").release()
        service.acquire_runtime("model-b", "image").release()
        self.assertEqual(loader.load_calls, 4)

    # ---------------------------------------------------- Finding 8 (P2)
    def test_wait_timeout_zero_does_not_bound_a_synchronous_load(self):
        manifest_a = _FakeManifest("model-a")
        load_running = Event()
        release_load = Event()

        class _SlowLoader:
            def __init__(self):
                self.load_calls = 0

            def load(self, manifest):
                self.load_calls += 1
                load_running.set()
                assert release_load.wait(timeout=5)
                return {"id": manifest.id, "instance": object()}

        loader = _SlowLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        result: dict[str, object] = {}

        def worker():
            # wait_timeout=0 means "do not wait for a contended G/L/E" -- G
            # and L are both uncontended here (nothing else holds them), so
            # this call proceeds straight into loader.load(), which is NOT
            # bounded by wait_timeout=0 and is free to take as long as it
            # needs (Finding 8's corrected contract).
            result["handle"] = service.acquire_runtime("model-a", "image", wait_timeout=0)

        t = Thread(target=worker)
        t.start()
        self.assertTrue(load_running.wait(timeout=5))  # load() genuinely started
        release_load.set()
        t.join(timeout=5)

        self.assertIn("handle", result)
        handle = result["handle"]
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()
        self.assertEqual(loader.load_calls, 1)

    # ------------------------- bonus 5: overflow eviction evicts only the excess
    def test_legacy_put_overflow_evicts_only_the_excess_not_every_eligible_entry(self):
        # Found by a further Codex re-review pass on the RETIRING-visibility
        # fix (bonus 2) above: marking a victim RETIRING (rather than
        # popping it) leaves it counted in the bucket's raw membership on
        # the loop's next iteration, so the budget check must subtract
        # this call's own already-decided victims back out, or it keeps
        # finding "still over budget" and evicts every remaining eligible
        # entry -- not just the genuine excess.
        cleanup = _RecordingCleanup()
        cache = ModelRuntimeCache(max_entries=2, on_evict=cleanup)
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})
        self.assertEqual(set(cache.loaded_ids()), {"model-a", "model-b"})

        cache.put("model-c", {"id": "model-c"})  # overflow by exactly 1

        # Only the LRU entry (model-a) is evicted -- not both.
        self.assertEqual(cleanup.calls, ["model-a"])
        self.assertEqual(set(cache.loaded_ids()), {"model-b", "model-c"})

    # ------------------------- bonus 6: E released when revalidation is interrupted
    def test_acquire_execution_lock_releases_e_when_revalidation_is_interrupted(self):
        # Found by the same re-review pass: an async BaseException landing
        # during is_current_and_ready() (after E was already won) used to
        # leave _acquire_execution_lock() exiting without releasing E --
        # the caller's own exception handling only releases the lease and
        # G, never E, permanently deadlocking every future caller of this
        # exact entry.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        original_is_current_and_ready = cache.is_current_and_ready

        def interrupted_check(canonical_id, entry):
            raise _FakeCancellation("injected: interrupted during revalidation")

        cache.is_current_and_ready = interrupted_check

        with self.assertRaises(_FakeCancellation):
            service.acquire_runtime("model-a", "image")

        entry = cache._entries["model-a"]
        # E must not be leaked -- a non-blocking acquire proves it's free.
        self.assertTrue(entry.execution_lock.acquire(blocking=False))
        entry.execution_lock.release()
        # The lease is released too (acquire_runtime()'s own cleanup).
        self.assertEqual(entry.lease_count, 0)

        cache.is_current_and_ready = original_is_current_and_ready
        # G was released too -- a fresh, immediate acquire succeeds.
        handle = service.acquire_runtime("model-a", "image", wait_timeout=0)
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()

    # ------------------------- bonus 7: publish interrupted before state flips
    def test_acquire_or_load_entry_aborts_reservation_when_publish_is_interrupted(self):
        # Found by the same re-review pass: if an async BaseException lands
        # during publish_ready_and_pin() *before* it actually flips
        # entry.state to READY (e.g. between assigning .runtime and
        # assigning .state), the old fix's state-only check took the
        # "dispose the runtime" branch correctly, but never called
        # abort_reservation() -- leaving the entry stuck at LOADING
        # forever, exactly like an un-aborted load failure would.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=cleanup,
        )

        original_publish = cache.publish_ready_and_pin

        def interrupted_before_ready(canonical_id, reserved_entry, runtime_obj):
            # Simulates an interruption strictly before publication -- the
            # entry is never mutated at all (the strictest sub-case of
            # "still LOADING when the exception arrives").
            raise _FakeCancellation("injected: interrupted before publish")

        cache.publish_ready_and_pin = interrupted_before_ready

        with self.assertRaises(_FakeCancellation):
            service.acquire_runtime("model-a", "image")

        # The reservation is fully unwound -- not stuck at LOADING -- and
        # the never-published runtime was disposed exactly once.
        self.assertNotIn("model-a", cache._entries)
        self.assertEqual(cleanup.calls, ["model-a"])

        cache.publish_ready_and_pin = original_publish

        # A subsequent attempt succeeds normally.
        handle = service.acquire_runtime("model-a", "image")
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()
        self.assertEqual(loader.load_calls, 2)

    # ------------------------- bonus 8: concurrent handle releases never double-release G
    def test_concurrent_handle_releases_never_double_release_admission(self):
        # Found by the same re-review pass: RuntimeHandle.release()'s own
        # `if self._released: return; self._released = True` was not
        # atomic -- two threads calling release() on the exact same handle
        # at once could both observe `_released is False` before either
        # set it `True`, both entering the unwind path. The second
        # execution_lock.release() would raise, but its own nested
        # `finally` chain would still reach `self._admission.release()` a
        # second time, permanently inflating the process-wide admission
        # capacity by one.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        handle = service.acquire_runtime("model-a", "image")
        # capacity=1, one slot taken -- semaphore's internal counter is 0.
        self.assertEqual(service._admission._semaphore._value, 0)

        barrier = Barrier(2)
        errors: list[BaseException] = []

        def release_racer():
            barrier.wait(timeout=5)
            try:
                handle.release()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = Thread(target=release_racer)
        t2 = Thread(target=release_racer)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # release() itself must never raise, even racing itself.
        self.assertEqual(errors, [])
        # Exactly one net release -- the semaphore's counter is back to
        # its full capacity (1), never 2 (which a double-release causes).
        self.assertEqual(service._admission._semaphore._value, 1)

    # ------------------------- bonus 9: handle-construction-interrupted unwind
    def test_acquire_runtime_unwinds_g_lease_and_e_when_handle_construction_fails(self):
        # Found by a further Codex re-review pass: by the time
        # `RuntimeHandle(...)` is constructed, G, the lease, and E have all
        # already been acquired -- but that construction call used to sit
        # outside every protective try/except. An exception during
        # `__init__` itself (an async BaseException, a hypothetical
        # allocation failure, ...) left all three held with no handle ever
        # created to release them.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        original_init = service_module.RuntimeHandle.__init__

        def failing_init(self, **kwargs):
            raise _FakeCancellation("injected: interrupted during handle construction")

        service_module.RuntimeHandle.__init__ = failing_init
        try:
            with self.assertRaises(_FakeCancellation):
                service.acquire_runtime("model-a", "image")
        finally:
            service_module.RuntimeHandle.__init__ = original_init

        entry = cache._entries["model-a"]
        self.assertEqual(entry.lease_count, 0)
        self.assertTrue(entry.execution_lock.acquire(blocking=False))
        entry.execution_lock.release()

        # G was released too -- a fresh, immediate acquire succeeds.
        handle = service.acquire_runtime("model-a", "image", wait_timeout=0)
        try:
            self.assertIsNotNone(handle.runtime)
        finally:
            handle.release()

    # ------------------------- bonus 10: reject same-thread recursive acquisition
    def test_acquire_runtime_rejects_recursive_acquisition_on_the_same_thread(self):
        # Found by the same re-review pass: a thread calling
        # acquire_runtime() again while it already holds a handle from an
        # earlier, still-open call would otherwise block on G forever --
        # only that same (now blocked) thread could ever release the slot
        # it is waiting for -- even for a completely different model_id,
        # since G is process-wide, not per-canonical-id.
        manifest_a = _FakeManifest("model-a", loader="fake-a")
        manifest_b = _FakeManifest("model-b", loader="fake-b")
        loader_a = _ImmediateLoader()
        loader_b = _ImmediateLoader()
        service, cache = _build_service(
            {"model-a": manifest_a, "model-b": manifest_b},
            {"fake-a": loader_a, "fake-b": loader_b},
        )

        handle = service.acquire_runtime("model-a", "image")
        try:
            with self.assertRaises(RuntimeError) as cm:
                service.acquire_runtime("model-b", "image")
            self.assertNotIsInstance(cm.exception, RuntimeBusyError)
        finally:
            handle.release()

        # Once released, a fresh acquisition on this same thread succeeds
        # normally -- the "held" flag was correctly cleared, not stuck
        # permanently rejecting this thread.
        handle2 = service.acquire_runtime("model-b", "image")
        try:
            self.assertIsNotNone(handle2.runtime)
        finally:
            handle2.release()

    # ------------------------- bonus 11: put() finalizes every victim under BaseException
    def test_put_finalizes_all_victims_even_after_a_base_exception(self):
        # Found by a further Codex re-review pass: put()'s own
        # replace-then-overflow cleanup sequence had no equivalent of
        # unload_all()'s "finalize everyone before re-raising" guarantee --
        # an interrupted replaced-victim cleanup skipped the overflow-victim
        # loop entirely, stranding those already-RETIRING entries forever.
        cleanup_calls: list[str] = []

        def mixed_cleanup(model_id, runtime_obj):
            cleanup_calls.append(model_id)
            if model_id == "model-a":
                raise _FakeCancellation("injected: model-a's replaced runtime cleanup interrupted")

        cache = ModelRuntimeCache(
            max_entries=1, media_limits={"image": 1, "text": 2}, on_evict=mixed_cleanup,
        )
        cache.put("model-a", {"id": "model-a", "instance": object()}, media_type="image")
        cache.put("model-b", {"id": "model-b", "instance": object()}, media_type="text")
        cache.put("model-c", {"id": "model-c", "instance": object()}, media_type="text")
        self.assertEqual(set(cache.loaded_ids()), {"model-a", "model-b", "model-c"})

        # Replace model-a's runtime AND move it into "text" -- already at
        # budget=2 -- forcing both a replace-cleanup and an overflow-
        # cleanup (evicting the LRU "text" occupant, model-b) in one call.
        with self.assertRaises(_FakeCancellation):
            cache.put("model-a", {"id": "model-a", "instance": object()}, media_type="text")

        # Both targets were finalized -- model-b's overflow cleanup ran
        # too, not skipped just because model-a's raised first.
        self.assertEqual(cleanup_calls, ["model-a", "model-b"])
        self.assertNotIn("model-b", cache._entries)
        self.assertEqual(set(cache.loaded_ids()), {"model-a", "model-c"})

    # ------------------------- bonus 12: reject same-object reinsertion of an INVALID entry
    def test_put_rejects_same_object_reinsertion_of_an_invalid_entry(self):
        # Found by the same re-review pass: INVALID is not in
        # _BUSY_FOR_UNLOAD_STATES, so an unpinned INVALID entry passed the
        # busy check and reached the same-object fast path -- which
        # refreshes bucket/LRU position but never restores .state to
        # READY, so get()/has() kept treating it as absent while it could
        # still evict a healthy destination-bucket entry for no benefit.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        service, cache = _build_service({"model-a": manifest_a}, {"fake": loader})

        handle = service.acquire_runtime("model-a", "image")
        runtime_obj = handle.runtime
        handle.release(had_exception=True)  # marks INVALID, lease_count back to 0
        entry = cache._entries["model-a"]
        self.assertEqual(entry.state, RuntimeState.INVALID)
        self.assertEqual(entry.lease_count, 0)

        with self.assertRaises(RuntimeBusyError):
            cache.put("model-a", runtime_obj)  # same object, still INVALID

        self.assertEqual(cache._entries["model-a"].state, RuntimeState.INVALID)

    # ------------------------- bonus 13: acquire_or_reserve never overwrites a concurrent put()
    def test_acquire_or_reserve_never_overwrites_a_concurrently_republished_target(self):
        # Found by the same re-review pass: a legacy put() does not
        # participate in L, so it could republish the exact id
        # acquire_or_reserve() is trying to reserve while that call's own
        # victim cleanup was running outside M. The post-cleanup recheck
        # only checked bucket *capacity* -- if that happened to look fine,
        # it would blindly overwrite the concurrently published entry,
        # leaking its runtime with zero cleanup.
        cleanup_started = Event()
        cleanup_release = Event()

        def blocking_cleanup(model_id, runtime_obj):
            cleanup_started.set()
            assert cleanup_release.wait(timeout=5)

        # budget=1 for "image"; a generous budget=5 for "text" -- the
        # concurrent put() below republishes "model-b" into "text", a
        # *different* bucket than the one acquire_or_reserve() is
        # reserving into ("image"). This is what actually distinguishes
        # the fix from the old code: if the concurrent put() landed in the
        # *same* bucket, removing one victim and adding one entry back
        # nets to exactly the same occupancy either way, so the old
        # bucket-capacity-only recheck would coincidentally still see (and
        # reject) the occupied slot -- masking the bug. Publishing into an
        # unrelated bucket makes the *target id itself* the only thing
        # that would have caught the collision.
        cache = ModelRuntimeCache(
            max_entries=1, media_limits={"image": 1, "text": 5}, on_evict=blocking_cleanup,
        )
        cache.put("model-a", {"id": "model-a", "instance": object()}, media_type="image")

        errors: list[BaseException] = []
        result: dict[str, object] = {}

        def reserver():
            try:
                result["entry"] = cache.acquire_or_reserve("model-b", "image")
            except RuntimeBusyError as exc:
                result["busy"] = exc
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = Thread(target=reserver)
        t.start()
        self.assertTrue(cleanup_started.wait(timeout=5))  # model-a's victim cleanup is mid-flight

        # A concurrent legacy put() republishes "model-b" -- the exact id
        # acquire_or_reserve() is trying to reserve -- while it's outside M,
        # into a different (spacious) bucket.
        republished_runtime = {"id": "model-b", "instance": object()}
        cache.put("model-b", republished_runtime, media_type="text")

        cleanup_release.set()
        t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertNotIn("entry", result)  # never silently overwritten
        self.assertIsInstance(result.get("busy"), RuntimeBusyError)

        # The concurrently published entry survives, fully intact.
        self.assertIs(cache._entries["model-b"].runtime, republished_runtime)
        self.assertEqual(cache._entries["model-b"].state, RuntimeState.READY)

    # ------------------------- bonus 14: deferred overflow eviction on final unpin
    def test_release_lease_evicts_deferred_overflow_once_the_last_pin_drains(self):
        # Found by the same re-review pass: legacy put() intentionally
        # leaves a bucket over budget while every over-budget member is
        # pinned (pin supremacy). Nothing previously re-triggered eviction
        # once the last such pin released -- the overage could persist
        # indefinitely.
        manifest_a = _FakeManifest("model-a")
        loader = _ImmediateLoader()
        cleanup = _RecordingCleanup()
        service, cache = _build_service(
            {"model-a": manifest_a}, {"fake": loader}, on_evict=cleanup, max_entries=1,
        )

        handle = service.acquire_runtime("model-a", "image")  # pins model-a

        # A legacy put() overflows the (budget=1) bucket while model-a is
        # pinned -- pin supremacy leaves the bucket over budget, as designed.
        cache.put("model-b", {"id": "model-b", "instance": object()})
        self.assertEqual(set(cache.loaded_ids()), {"model-a", "model-b"})
        self.assertEqual(cleanup.calls, [])

        # Releasing the last pin on model-a must now retire the deferred
        # overflow -- model-a itself, being the LRU entry.
        handle.release()

        self.assertEqual(cleanup.calls, ["model-a"])
        self.assertEqual(cache.loaded_ids(), ["model-b"])


if __name__ == "__main__":
    unittest.main()
