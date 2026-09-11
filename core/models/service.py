"""Application-facing entrypoint for model resolution and loading."""

from __future__ import annotations

import time
from threading import Lock, Semaphore
from typing import Any

from .cache import ModelRuntimeCache
from .cloud_guard import ensure_cloud_provider_enabled
from .loader import LoaderRegistry
from .manifest import ModelManifest
from .registry import ModelRegistry
from .resolver import ModelResolver
from .runtime_lease import RuntimeBusyError, RuntimeEntry, RuntimeState

# PR4a (issue #414): the one process-wide "may a heavy runtime load or run
# right now" slot -- see `ModelService.acquire_runtime()`'s own docstring for
# why this is not per-generator, per-media, or per-runtime-class. Every
# runtime is classified "heavy" in PR4a; an explicitly audited lighter
# classification (cloud/procedural) is PR4b+ scope, and even then the
# default for anything unclassified stays protected ("unknown => protected").
DEFAULT_ADMISSION_CAPACITY = 1


def _acquire_with_deadline(lockable: Lock | Semaphore, deadline: float | None) -> bool:
    """Try to acquire `lockable`, bounded by one overall `deadline` (or none).

    `deadline`, if given, is an absolute `time.monotonic()` value shared
    across an entire `acquire_runtime()` call -- never reset per lock (see
    that method's own docstring). `deadline is None` blocks the ordinary,
    unbounded way (identical to a bare `lockable.acquire()`) -- legitimate
    mutex contention (someone else briefly holds G/L/E) is bounded by
    whoever holds it finishing their own, already-bounded work, which is not
    the "wait indefinitely for a condition only another, unrelated caller
    resolves" pattern issue #414 forbids (see `RuntimeBusyError`'s own
    docstring) -- a caller that wants a hard wall-clock cap passes `timeout`.
    A `deadline` already in the past acquires non-blockingly (`timeout=0`
    semantics): still race-free (never silently skips a lock), just never
    parks the calling thread.
    """

    if deadline is None:
        lockable.acquire()
        return True
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return lockable.acquire(blocking=False)
    return lockable.acquire(timeout=remaining)


class RuntimeHandle:
    """A leased, execution-locked runtime -- the safe-use unit of PR4a.

    Obtained from `ModelService.acquire_runtime()`, which performs the
    *entire* admission/lease/execution-lock acquisition before this object
    ever exists; `RuntimeHandle` itself only ever releases what was already
    acquired. Use as a context manager:

        with model_service.acquire_runtime(model_id, media_type, task_type) as handle:
            manifest = handle.manifest
            runtime = handle.runtime
            ...  # generate

    Release order mirrors acquisition in reverse (E, then a short metadata
    transaction, then G) -- see `ModelService.acquire_runtime()`'s own
    docstring for the full G/L/E/M ordering this is one half of.
    """

    __slots__ = (
        "manifest",
        "runtime",
        "_cache",
        "_canonical_id",
        "_entry",
        "_admission",
        "_released",
    )

    def __init__(
        self,
        *,
        manifest: ModelManifest,
        runtime: Any,
        cache: ModelRuntimeCache,
        canonical_id: str,
        entry: RuntimeEntry,
        admission: Semaphore,
    ) -> None:
        self.manifest = manifest
        self.runtime = runtime
        self._cache = cache
        self._canonical_id = canonical_id
        self._entry = entry
        self._admission = admission
        self._released = False

    def __enter__(self) -> "RuntimeHandle":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        self.release(had_exception=exc_type is not None)

    def release(self, *, had_exception: bool = False) -> None:
        """Release E, then the lease (M), then G -- idempotent.

        `had_exception=True` (always true when released via `__exit__` for
        an exception that propagated out of the `with` block) marks the
        entry `INVALID` rather than leaving it `READY` for reuse -- a
        conservative correctness-first policy for v1, *not* a classification
        of which exceptions actually leave a runtime unsafe to reuse (a
        cancellation, or a pure input-validation failure the runtime never
        even started acting on, is treated identically to a genuine
        mid-inference crash). This may cause extra reloads; PR4b, once
        generator boundaries are migrated onto this API, is where that
        policy can be narrowed with real evidence about which exception
        classes are actually safe to shrug off.
        """

        if self._released:
            return
        self._released = True
        try:
            self._entry.execution_lock.release()
        finally:
            try:
                self._cache.release_lease(
                    self._canonical_id, self._entry, mark_invalid=had_exception
                )
            finally:
                self._admission.release()


class ModelService:
    """Facade combining registry, resolver, loader registry, and cache."""

    def __init__(
        self,
        registry: ModelRegistry,
        resolver: ModelResolver,
        loader_registry: LoaderRegistry,
        runtime_cache: ModelRuntimeCache,
        *,
        admission_capacity: int = DEFAULT_ADMISSION_CAPACITY,
    ) -> None:
        self.registry = registry
        self.resolver = resolver
        self.loader_registry = loader_registry
        self.runtime_cache = runtime_cache
        # PR4a (issue #414): ONE process-wide semaphore, constructed once
        # here and shared by every caller of `acquire_runtime()` through
        # this exact `ModelService` instance -- not one per generator, not
        # one per media type. `bootstrap/factories.py` constructs exactly
        # one `ModelService` for the whole application graph, so this is
        # already process-wide in practice; it is never re-created per
        # request or per generator instantiation.
        self._admission = Semaphore(admission_capacity)

    def list_models(
        self,
        *,
        media_type: str | None = None,
        task_type: str | None = None,
    ) -> list[ModelManifest]:
        manifests = self.registry.list_all()
        if media_type is not None:
            manifests = [
                manifest for manifest in manifests if manifest.media_type == media_type
            ]
        if task_type is not None:
            manifests = [
                manifest for manifest in manifests if manifest.task_type == task_type
            ]
        return manifests

    def get_manifest(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> ModelManifest:
        """Resolve a public model id, alias, or manifest id, or use the default."""

        return self.resolver.resolve(model_id, media_type, task_type)

    # ------------------------------------------------------------ legacy API

    def get_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> Any:
        """Legacy raw-runtime API. Not concurrency-safe; transitional only.

        See `resolve_runtime()`'s own docstring -- this is a thin wrapper
        that discards the manifest half of its return value.
        """

        _, runtime_obj = self.resolve_runtime(model_id, media_type, task_type)
        return runtime_obj

    def resolve_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> tuple[ModelManifest, Any]:
        """Legacy raw-runtime API. Not concurrency-safe; transitional only.

        The returned runtime object carries no lease: nothing stops a
        concurrent `unload_model()` call, or an `acquire_runtime()`-driven
        eviction, from retiring it while this caller is still using it.
        Kept, unchanged in external behavior, until PR4b migrates every
        generator onto `acquire_runtime()` (issue #414) -- do not add new
        callers of this method or `get_runtime()`; use `acquire_runtime()`.
        """

        manifest = self.get_manifest(model_id, media_type, task_type)
        # Checked before the cache lookup so revoking either cloud opt-in
        # switch blocks a cached runtime too -- otherwise a runtime cached
        # while both switches were on keeps serving jobs (and holding its
        # API key) after either switch is turned back off, without a process
        # restart.
        if manifest.provider == "cloud":
            ensure_cloud_provider_enabled(manifest.id)

        cached_runtime = self.runtime_cache.get(manifest.id)
        if cached_runtime is not None:
            return manifest, cached_runtime

        # Single-flight: only one caller loads a given uncached model_id at a
        # time (see ModelRuntimeCache.lock_for's docstring for why -- a
        # second concurrent loader's put() would otherwise evict, and run
        # cleanup on, the runtime the first caller already returned and may
        # still be using). A second caller blocks here rather than also
        # loading; re-checking the cache after acquiring the lock picks up
        # whatever the first caller already loaded instead of loading again.
        with self.runtime_cache.lock_for(manifest.id):
            cached_runtime = self.runtime_cache.get(manifest.id)
            if cached_runtime is not None:
                return manifest, cached_runtime

            loader = self.loader_registry.get(manifest.loader)
            runtime_obj = loader.load(manifest)
            # `media_type` selects this runtime's eviction bucket (issue
            # #182): a per-media budget in `runtime_cache.media_limits` lets
            # this media family stay resident independently of others;
            # absent one, it falls back to the cache's single shared budget
            # unchanged.
            self.runtime_cache.put(manifest.id, runtime_obj, media_type=media_type)
            return manifest, runtime_obj

    # -------------------------------------------------- PR4a safe-use API

    def acquire_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
        *,
        timeout: float | None = None,
    ) -> RuntimeHandle:
        """Acquire a leased, execution-locked runtime as a context manager.

        The safe path for PR4a (issue #414): unlike `resolve_runtime()`, the
        returned `RuntimeHandle` guarantees exclusive execution access to
        this exact runtime for as long as it is held, and the runtime is
        never evicted/unloaded/replaced out from under it.

        Lock/admission ordering (the invariant every deadlock proof in
        `tests/test_runtime_safety_core.py` relies on):

            G (process-wide admission) -> L (per-canonical-id load lock) -> E (execution lock)

        with `M` (the cache's own metadata lock) used only as short,
        standalone probes inside `ModelRuntimeCache` methods -- never held
        while this method waits for G, L, or E. Concretely, this method:

        1. Resolves `model_id` to a manifest and checks the cloud-provider
           opt-in guard *before* any admission/pin side effect (so a
           revoked opt-in denies acquisition even for an already-cached
           runtime, matching `resolve_runtime()`'s own ordering).
        2. Acquires G.
        3. Acquires L(canonical_id) -- reuses `runtime_cache.lock_for()`,
           the same lock `resolve_runtime()` already serializes on.
        4. Calls `runtime_cache.acquire_or_reserve()` (which internally
           manages its own short M sections and any eviction-victim
           cleanup, entirely outside M -- see that method's docstring). On
           a cache miss this calls `loader.load()` *outside* any lock this
           method holds other than G/L, then
           `runtime_cache.publish_ready_and_pin()`.
        5. Releases L.
        6. Acquires E (`entry.execution_lock`).
        7. Returns a `RuntimeHandle` owning G's slot, the lease, and E; the
           handle's own `release()`/`__exit__` reverses this in the order
           E -> short M transaction (decrement lease, mark INVALID on
           exception) -> G, exactly matching this module's own top-level
           lock-ordering contract.

        No two blocking resources are ever acquired in the reverse of this
        order, and M is never held while waiting for G, L, or E, so this
        ordering cannot deadlock against itself. A capacity-exhaustion case
        that only a currently-pinned caller (never a future event) could
        resolve raises `RuntimeBusyError` immediately instead of waiting --
        see `ModelRuntimeCache.acquire_or_reserve()`'s own docstring.

        `timeout`, if given, is a *single* overall deadline covering G, L,
        and E acquisition combined -- never reset per lock (a caller cannot
        wait `timeout` seconds for G and then another `timeout` seconds for
        E). `timeout=0` is non-blocking: returns immediately with
        `RuntimeBusyError` unless every acquisition succeeds without
        waiting. `timeout=None` (the default) blocks the ordinary way on
        G/L/E -- see `_acquire_with_deadline()`'s own docstring for why that
        is not the "indefinite wait" issue #414 forbids.
        """

        manifest = self.get_manifest(model_id, media_type, task_type)
        if manifest.provider == "cloud":
            ensure_cloud_provider_enabled(manifest.id)

        deadline = None if timeout is None else time.monotonic() + timeout

        if not _acquire_with_deadline(self._admission, deadline):
            raise RuntimeBusyError(
                f"No process-wide runtime admission slot available for "
                f"{manifest.id!r} within the given timeout."
            )

        try:
            entry = self._acquire_or_load_entry(manifest, media_type, deadline)
            try:
                if not _acquire_with_deadline(entry.execution_lock, deadline):
                    raise RuntimeBusyError(
                        "Timed out waiting for exclusive execution access to "
                        f"{manifest.id!r}."
                    )
            except BaseException:
                # We hold a lease (a pin) but will never use it -- release it
                # rather than leaking a lease with no corresponding handle,
                # which would otherwise make this entry permanently
                # un-evictable/un-unloadable ("pinned runtime" is absolute).
                self.runtime_cache.release_lease(manifest.id, entry, mark_invalid=False)
                raise
        except BaseException:
            self._admission.release()
            raise

        return RuntimeHandle(
            manifest=manifest,
            runtime=entry.runtime,
            cache=self.runtime_cache,
            canonical_id=manifest.id,
            entry=entry,
            admission=self._admission,
        )

    def _acquire_or_load_entry(
        self, manifest: ModelManifest, media_type: str, deadline: float | None
    ) -> RuntimeEntry:
        load_lock = self.runtime_cache.lock_for(manifest.id)
        if not _acquire_with_deadline(load_lock, deadline):
            raise RuntimeBusyError(
                f"Timed out waiting for the load lock for {manifest.id!r}."
            )
        try:
            entry = self.runtime_cache.acquire_or_reserve(manifest.id, media_type)
            if entry.state is RuntimeState.LOADING:
                loader = self.loader_registry.get(manifest.loader)
                try:
                    runtime_obj = loader.load(manifest)
                except BaseException:
                    self.runtime_cache.abort_reservation(manifest.id, entry)
                    raise
                entry = self.runtime_cache.publish_ready_and_pin(
                    manifest.id, entry, runtime_obj
                )
            return entry
        finally:
            load_lock.release()

    # ------------------------------------------------------- canonical unload

    def unload_model(self, model_id: str) -> None:
        """Unload `model_id` (public id, alias, or manifest id) if idle.

        Resolves through `ModelResolver` first, so `unload_model("sdxl")`,
        `unload_model("sdxl-local")` (an alias), and
        `unload_model("stable-diffusion-xl")` (the manifest id) all target
        the exact same cache entry -- fixing the pre-PR4a bug where an
        alias/public-id argument silently missed the cache (keyed only by
        canonical manifest id) and no-opped. Raises `RuntimeBusyError`
        (never waits) if the resolved entry is currently leased, loading, or
        retiring; a runtime that was never cached is a safe no-op.
        """

        canonical_id = self.resolver.resolve_manifest_id(model_id)
        self.runtime_cache.unload(canonical_id)

    def unload_all(self) -> None:
        """Unload every cached runtime, atomically -- see `ModelRuntimeCache.unload_all()`."""

        self.runtime_cache.unload_all()


__all__ = ["ModelService", "RuntimeHandle"]
