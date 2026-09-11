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
from threading import Event, Lock, Thread
import unittest

from bootstrap.factories import create_default_model_service
from core.models.cache import ModelRuntimeCache
from core.models.registry import ModelRegistry
from core.models.resolver import ModelResolver
from core.models.runtime_lease import RuntimeBusyError, RuntimeState
from core.models.service import ModelService
from core.models.cloud_guard import CloudProviderDisabledError


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

            cache.release_lease(public_canonical_id, pinned_via_public_id, mark_invalid=False)
            cache.release_lease(alias_canonical_id, pinned_via_alias, mark_invalid=False)
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
        handle = service.acquire_runtime("model-a", "image", timeout=0)
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
            service.acquire_runtime("model-a", "image", timeout=0)

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
        cache.release_lease("model-a", old_entry, mark_invalid=False)

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
        handle = service.acquire_runtime("local-model", "image", timeout=0)
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
            service.acquire_runtime("model-a", "image", timeout=0)

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
            service.acquire_runtime("model-a", "image", timeout=0)

        # The second attempt's own pin (taken by the acquire_or_reserve()
        # hit path) must have been rolled back once E's own acquisition
        # timed out -- never leaked, which would otherwise make this entry
        # permanently un-evictable/un-unloadable.
        self.assertEqual(entry.lease_count, 1)
        # G's second slot was released too -- proved by a bounded, timeout=0
        # third attempt for a DIFFERENT model succeeding immediately.
        other_manifest = _FakeManifest("model-b")
        other_loader = _ImmediateLoader()
        service.resolver._manifests["model-b"] = other_manifest
        service.loader_registry._loaders["fake-b"] = other_loader
        other_manifest.loader = "fake-b"
        handle_b = service.acquire_runtime("model-b", "image", timeout=0)
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
            service.acquire_runtime("model-a", "image", timeout=0)

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
            service.acquire_runtime("model-a", "image", timeout=0)

        # Once the last outstanding lease releases, the (still INVALID,
        # now unleased) entry becomes a normal eviction/replacement
        # candidate for the next acquire -- reloaded fresh, never reused.
        cache.release_lease("model-a", entry, mark_invalid=False)
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


if __name__ == "__main__":
    unittest.main()
