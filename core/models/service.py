"""Application-facing entrypoint for model resolution and loading."""

from __future__ import annotations

import time
from threading import Lock, Semaphore, get_ident
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
    docstring) -- a caller that wants a hard wall-clock cap passes
    `wait_timeout`. A `deadline` already in the past acquires
    non-blockingly (`wait_timeout=0` semantics): still race-free (never
    silently skips a lock), just never parks the calling thread.
    """

    if deadline is None:
        lockable.acquire()
        return True
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return lockable.acquire(blocking=False)
    return lockable.acquire(timeout=remaining)


class RuntimeAdmissionController:
    """The process-wide "G" gate: how many heavy load/execute sections may
    be in flight across the *entire process* at once, not per `ModelService`
    instance.

    PR4a's own Codex-review Finding 1 (round 1): `ModelService.__init__`
    used to construct its own private `threading.Semaphore` -- correct for a
    single `ModelService`, but the application graph is not guaranteed to
    have only one. `bootstrap/factories.py`'s `create_default_model_service()`
    is called once per standalone generator factory whenever that factory's
    caller passes `model_service=None` (see e.g. `create_default_image_generator()`);
    each such call used to get its own independent admission slot, so two
    generators built this way could each load/execute a "heavy" runtime at
    the same time -- exactly the unbounded-concurrent-heavy-work outcome G
    exists to prevent.

    Wrapping the semaphore in its own class, with a single process-wide
    default instance (`get_default_admission_controller()`) that every
    `ModelService` constructed *without* an explicit `admission`/
    `admission_capacity` argument shares, makes "at most `capacity` heavy
    sections in flight" a true process-wide invariant by default, while
    still letting test code construct and inject an independent controller
    to isolate the mechanism it is actually testing (see
    `tests/test_runtime_safety_core.py`'s `_build_service()` helper, which
    always passes an explicit `admission_capacity` for exactly this reason).
    """

    def __init__(self, capacity: int = DEFAULT_ADMISSION_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"admission capacity must be at least 1, got {capacity!r}.")
        self.capacity = capacity
        self._semaphore = Semaphore(capacity)
        # Codex re-review, found on this round's own fix commits: a thread
        # that already holds every one of this controller's slots by
        # itself (its own earlier, still-open `acquire_runtime()` calls
        # account for all of `capacity`) and then tries to *block* waiting
        # for one more would deadlock unconditionally -- only that same
        # thread could ever release one of its own slots to free room for
        # itself, and it cannot do that while parked here.
        #
        # Ownership is tracked by *acquiring* thread id in a plain dict
        # guarded by `_held_lock`, deliberately NOT `threading.local()`:
        # this codebase explicitly supports releasing a `RuntimeHandle`
        # from a different thread than the one that acquired it (a
        # cancellation/cleanup thread finishing up after a worker thread --
        # see `RuntimeHandle`'s own `_owner_thread_id`, captured at
        # acquisition time and threaded through to this class's `release()`
        # so the *original* acquirer's count is what gets decremented, not
        # whichever thread happens to call `release()`). A first version of
        # this used `threading.local()`, which cannot be updated for
        # another thread at all -- a cross-thread release left the
        # original acquiring thread's local counter permanently stuck,
        # rejecting its next, perfectly legitimate acquisition (Codex
        # re-review caught this too).
        self._held_lock = Lock()
        self._held_by_thread: dict[int, int] = {}

    def acquire(self, deadline: float | None) -> bool:
        thread_id = get_ident()
        would_block = deadline is None or deadline - time.monotonic() > 0
        with self._held_lock:
            held = self._held_by_thread.get(thread_id, 0)
        if would_block and held >= self.capacity:
            raise RuntimeError(
                "Recursive acquire_runtime() call detected: this thread "
                f"already holds all {self.capacity} process-wide admission "
                "slot(s) from earlier, still-open acquire_runtime() "
                "call(s). Release at least one RuntimeHandle before "
                "acquiring another -- waiting here could never be "
                "resolved by any other thread."
            )
        acquired = _acquire_with_deadline(self._semaphore, deadline)
        if acquired:
            with self._held_lock:
                self._held_by_thread[thread_id] = self._held_by_thread.get(thread_id, 0) + 1
        return acquired

    def release(self, owner_thread_id: int) -> None:
        with self._held_lock:
            current = self._held_by_thread.get(owner_thread_id, 0)
            if current > 1:
                self._held_by_thread[owner_thread_id] = current - 1
            else:
                self._held_by_thread.pop(owner_thread_id, None)
        self._semaphore.release()


_default_admission_controller: RuntimeAdmissionController | None = None
_default_admission_controller_lock = Lock()


def get_default_admission_controller() -> RuntimeAdmissionController:
    """Return the process-wide default `RuntimeAdmissionController`, creating
    it once, lazily, on first use.

    Every `ModelService` constructed without an explicit `admission` or
    `admission_capacity` argument -- which includes every call to
    `bootstrap/factories.py`'s `create_default_model_service()`, no matter
    how many separate call sites make that call -- shares this exact
    instance. This is what makes "one process-wide admission domain" true by
    default even though nothing forces the application graph to construct
    only one `ModelService`.
    """

    global _default_admission_controller
    with _default_admission_controller_lock:
        if _default_admission_controller is None:
            _default_admission_controller = RuntimeAdmissionController(
                DEFAULT_ADMISSION_CAPACITY
            )
        return _default_admission_controller


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

    Release order mirrors acquisition in reverse, but with one deliberate
    twist over a naive E -> M -> G reversal -- see `release()`'s own
    docstring for why INVALID-marking happens *before* E is released, not
    folded into the same short metadata transaction as the lease decrement.
    """

    __slots__ = (
        "manifest",
        "runtime",
        "_cache",
        "_canonical_id",
        "_entry",
        "_admission",
        "_owner_thread_id",
        "_released",
        "_release_guard",
    )

    def __init__(
        self,
        *,
        manifest: ModelManifest,
        runtime: Any,
        cache: ModelRuntimeCache,
        canonical_id: str,
        entry: RuntimeEntry,
        admission: RuntimeAdmissionController,
        owner_thread_id: int,
    ) -> None:
        self.manifest = manifest
        self.runtime = runtime
        self._cache = cache
        self._canonical_id = canonical_id
        self._entry = entry
        self._admission = admission
        # The thread that originally acquired G for this handle -- not
        # necessarily whichever thread calls `release()` (a cancellation/
        # cleanup thread may release a handle a different worker thread
        # acquired). `RuntimeAdmissionController.release()` needs this
        # exact id to credit the release back to its true owner; see that
        # class's own docstring for why `threading.local()` cannot do this.
        self._owner_thread_id = owner_thread_id
        self._released = False
        self._release_guard = Lock()

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
        """Mark INVALID (if needed) while E is still held, then release
        E -> lease -> G, in that order. Idempotent.

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

        PR4a's own Codex-review Finding 2 (round 1): the INVALID transition
        must become visible *before* E is released, not after -- otherwise a
        second caller already parked waiting on E (it pinned the same entry
        while this caller was still using it) can acquire E and start using
        a runtime this caller has already deemed unsafe, in the gap between
        "E released" and "lease decremented + marked INVALID" that a naive
        single combined step would leave open. So this method does the
        INVALID transition as its own short metadata transaction *first*,
        while E is still owned; only then releases E; only then, separately,
        decrements the lease (a runtime otherwise reused instantly by that
        same second caller, with its lease not yet decremented, would look
        briefly over-leased -- harmless, since the count is only ever a
        lower bound on "do not evict", but decrementing before the INVALID
        transition is even visible is the actual hazard, not this ordering).
        The corresponding other half of this fix is on the *acquiring* side:
        see `ModelService._acquire_execution_lock()`, which revalidates the
        entry immediately after winning E, precisely to catch this window
        from a waiter's perspective too.

        Codex re-review (found on this round's own fix commits): the
        `_released` check-and-set below is guarded by `_release_guard`
        (a private, per-handle `Lock`) because it is not otherwise atomic
        -- two threads calling `release()` on the exact same handle at once
        (e.g. the `with` block's own `__exit__` racing a separate
        cancellation/cleanup path that also holds a reference to this
        handle) could both observe `_released is False` before either sets
        it `True`, both proceed into the unwind below, and the second
        `execution_lock.release()` would raise (releasing an unlocked
        `Lock`) -- whose `finally` chain would still reach
        `self._admission.release()` a *second* time, permanently inflating
        the process-wide admission capacity by one. Only the flag
        transition itself needs the lock; at most one caller can ever pass
        it, so the actual release work below never needs it too.
        """

        with self._release_guard:
            if self._released:
                return
            self._released = True
        try:
            if had_exception:
                self._cache.mark_invalid(self._canonical_id, self._entry)
        finally:
            try:
                self._entry.execution_lock.release()
            finally:
                try:
                    self._cache.release_lease(self._canonical_id, self._entry)
                finally:
                    self._admission.release(self._owner_thread_id)


class ModelService:
    """Facade combining registry, resolver, loader registry, and cache."""

    def __init__(
        self,
        registry: ModelRegistry,
        resolver: ModelResolver,
        loader_registry: LoaderRegistry,
        runtime_cache: ModelRuntimeCache,
        *,
        admission: RuntimeAdmissionController | None = None,
        admission_capacity: int | None = None,
    ) -> None:
        self.registry = registry
        self.resolver = resolver
        self.loader_registry = loader_registry
        self.runtime_cache = runtime_cache
        if admission is not None and admission_capacity is not None:
            raise ValueError(
                "Pass at most one of `admission` or `admission_capacity`, not both."
            )
        if admission is not None:
            # Caller-supplied controller (tests wanting an independent
            # admission domain; a future caller that legitimately needs a
            # second, separate process-wide-equivalent domain).
            self._admission = admission
        elif admission_capacity is not None:
            # A private, non-shared controller at an explicit capacity --
            # used by tests that need to isolate the cache-level mechanism
            # under test from G contention (see `_build_service()` in
            # `tests/test_runtime_safety_core.py`). Not process-wide by
            # design: two `ModelService` instances each passing their own
            # `admission_capacity` do NOT share a domain.
            self._admission = RuntimeAdmissionController(admission_capacity)
        else:
            # PR4a (issue #414), Codex-review Finding 1 (round 1): the
            # default, production path shares ONE process-wide controller
            # across every `ModelService` instance that does not opt out of
            # it -- see `get_default_admission_controller()`'s own
            # docstring. `bootstrap/factories.py` relies on exactly this: it
            # never passes `admission`/`admission_capacity`, so every
            # `create_default_model_service()` call (there can be more than
            # one -- see that function's own callers) still shares one gate.
            self._admission = get_default_admission_controller()

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
            try:
                self.runtime_cache.put(manifest.id, runtime_obj, media_type=media_type)
            except RuntimeBusyError:
                # PR4a Codex-review Finding 4 (round 1): `put()` can refuse
                # to publish (the entry it would replace became leased,
                # loading, or retiring between our cache-miss check above and
                # this call -- e.g. a concurrent `acquire_runtime()` reserved
                # it first). `runtime_obj` was already fully loaded and never
                # published anywhere, so nothing else in the process can ever
                # reach it once this call unwinds -- without disposal it just
                # leaks (accelerator memory included). `dispose_unpublished()`
                # runs the exact same cleanup an eviction would, on this
                # object only, never touching whatever `put()` actually left
                # cached under `manifest.id` (put() raised *before* mutating
                # `_entries` in this case -- see its own docstring). Runs
                # with M already released (put()'s own `with` block exited
                # before this exception reached here) and while this call
                # still holds L, so it is serialized against a concurrent
                # `resolve_runtime()` for the same id the same way loading
                # already was.
                self.runtime_cache.dispose_unpublished(manifest.id, runtime_obj)
                raise
            return manifest, runtime_obj

    # -------------------------------------------------- PR4a safe-use API

    def acquire_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
        *,
        wait_timeout: float | None = None,
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
        while this method waits for G, L, or E. "E held, then a short M
        probe" (marking INVALID before releasing E -- see
        `RuntimeHandle.release()`) is explicitly permitted; "M held, then
        wait on E" remains absolutely forbidden. Concretely, this method:

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
        6. Acquires E (`entry.execution_lock`), then revalidates the entry
           is still current and `READY` (see `_acquire_execution_lock()`) --
           the entry this caller pinned in step 4 may have been marked
           `INVALID` by another lease-holder's exception-unwind while this
           caller was still waiting on E (Codex-review Finding 2, round 1);
           a caller that loses that race never receives a handle at all.
        7. Returns a `RuntimeHandle` owning G's slot, the lease, and E; the
           handle's own `release()`/`__exit__` reverses this -- see that
           method's own docstring for the exact order (INVALID-marking
           happens *before* E is released, not folded into the same
           transaction as the lease decrement).

        No two blocking resources are ever acquired in the reverse of this
        order, and M is never held while waiting for G, L, or E, so this
        ordering cannot deadlock against itself. A capacity-exhaustion case
        that only a currently-pinned caller (never a future event) could
        resolve raises `RuntimeBusyError` immediately instead of waiting --
        see `ModelRuntimeCache.acquire_or_reserve()`'s own docstring.

        Do not call this method again, on the same thread, once it already
        holds every one of G's slots by itself (its own earlier, still-open
        `acquire_runtime()` calls -- production defaults to
        `admission_capacity=1`, so ordinarily this means holding even one
        handle at all) -- release at least one handle first. A *blocking*
        attempt in that state is rejected immediately with a plain
        `RuntimeError` (Codex re-review, found on this round's own fix
        commits): only this same thread could ever release one of its own
        slots to free room for itself, so waiting here would deadlock it
        against itself forever. Holding *fewer* than `capacity` slots and
        acquiring one more is ordinary, legitimate usage (e.g. a job step
        that needs two different models loaded at once, with capacity
        raised to allow it), and a non-blocking attempt (`wait_timeout=0`,
        or any deadline already past) is never rejected this way either --
        it cannot deadlock, since it already fails fast with an ordinary,
        retryable `RuntimeBusyError` on its own. Recursing onto the exact
        same canonical entry's E is not separately detected --
        `entry.execution_lock` is a plain, non-reentrant `threading.Lock`
        with no owner-thread bookkeeping of its own -- and will hang the
        same way acquiring any non-reentrant lock twice on one thread
        always does; avoid it the same way.

        `wait_timeout`, if given, is a *single* overall deadline bounding
        ONLY how long this call waits on contended G/L/E synchronization --
        never reset per lock (a caller cannot wait `wait_timeout` seconds
        for G and then another `wait_timeout` seconds for E).
        `wait_timeout=0` means "do not wait for a contended synchronization
        resource", not "do not perform synchronous work": it does NOT bound,
        preempt, or cancel `loader.load()` itself, which this method always
        calls synchronously and to completion once it actually starts (on a
        genuine cache miss, after G/L are both already held) -- there is no
        general loader-preemption/cancellation mechanism in PR4a, and this
        parameter makes no promise about total wall-clock time for a call
        that ends up loading. `wait_timeout=None` (the default) blocks the
        ordinary way on G/L/E -- see `_acquire_with_deadline()`'s own
        docstring for why that is not the "indefinite wait" issue #414
        forbids. (Codex-review Finding 8, round 1: this parameter was
        previously named `timeout` and its docstring claimed to be "a single
        overall deadline covering the entire `acquire_runtime()` call",
        which was never true once a cache miss reached `loader.load()` --
        renamed, since PR4a has not shipped/stabilized yet, rather than kept
        under a name that promised more than the implementation -- deliberately
        not turned into a general preemption framework -- delivers.)
        """

        manifest = self.get_manifest(model_id, media_type, task_type)
        if manifest.provider == "cloud":
            ensure_cloud_provider_enabled(manifest.id)

        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        owner_thread_id = get_ident()

        if not self._admission.acquire(deadline):
            raise RuntimeBusyError(
                f"No process-wide runtime admission slot available for "
                f"{manifest.id!r} within the given timeout."
            )

        try:
            entry = self._acquire_or_load_entry(manifest, media_type, deadline)
            try:
                self._acquire_execution_lock(manifest.id, entry, deadline)
                try:
                    return RuntimeHandle(
                        manifest=manifest,
                        runtime=entry.runtime,
                        cache=self.runtime_cache,
                        canonical_id=manifest.id,
                        entry=entry,
                        admission=self._admission,
                        owner_thread_id=owner_thread_id,
                    )
                except BaseException:
                    # Codex re-review, found on this round's own fix
                    # commits: G, the lease, and E were all already
                    # acquired above by the time construction reaches
                    # here -- an exception during `RuntimeHandle.__init__`
                    # itself (an async BaseException, a hypothetical
                    # allocation failure, ...) must not leave any of them
                    # held with no handle ever created to release them.
                    entry.execution_lock.release()
                    raise
            except BaseException:
                # We hold a lease (a pin) but will never use it -- release it
                # rather than leaking a lease with no corresponding handle,
                # which would otherwise make this entry permanently
                # un-evictable/un-unloadable ("pinned runtime" is absolute).
                self.runtime_cache.release_lease(manifest.id, entry)
                raise
        except BaseException:
            # This unwind always runs on the same thread that just called
            # `self._admission.acquire()` above (no handle has been handed
            # to any other thread yet), so `owner_thread_id` is trivially
            # this call's own -- unlike `RuntimeHandle.release()`, which
            # may run on a different thread than the one that acquired.
            self._admission.release(owner_thread_id)
            raise

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
                try:
                    # Codex re-review, found on the round-1 fix commit
                    # (P1): `loader_registry.get()` -- e.g. a manifest
                    # naming an unregistered loader -- must abort the
                    # reservation exactly like `loader.load()` itself
                    # raising. It used to run *before* this try block, so
                    # a bad `manifest.loader` value left the reservation
                    # stuck at LOADING forever, permanently denying every
                    # future acquisition for this id (and, in a
                    # single-entry bucket, every other id sharing it too).
                    loader = self.loader_registry.get(manifest.loader)
                    runtime_obj = loader.load(manifest)
                except BaseException:
                    self.runtime_cache.abort_reservation(manifest.id, entry)
                    raise
                try:
                    entry = self.runtime_cache.publish_ready_and_pin(
                        manifest.id, entry, runtime_obj
                    )
                except BaseException:
                    # Found during this round's adversarial self-review,
                    # then refined twice more after Codex's own re-reviews
                    # of each fix in turn: `publish_ready_and_pin()`
                    # mutates `entry` in place (sets `.runtime`,
                    # `.state = READY`, `.lease_count = 1`) *before* it can
                    # raise its own defensive backstop (see that method's
                    # docstring) -- believed unreachable in practice given
                    # this method holds L for its entire duration, but an
                    # async `BaseException` (KeyboardInterrupt, ...) could
                    # in principle land at any point during that mutation,
                    # including between the `.runtime`/`.state` assignments
                    # themselves. `entry` is the exact same object
                    # `publish_ready_and_pin()` mutates, so checking its
                    # `.state` here tells us, unambiguously, whether
                    # publication actually completed before the exception
                    # arrived:
                    #   - READY: already published and pinned -- disposing
                    #     the runtime here would corrupt one other callers
                    #     can now see as cached. Instead release the lease
                    #     this call just took, since no RuntimeHandle will
                    #     ever be returned to release it otherwise.
                    #   - still LOADING (never published, or interrupted
                    #     mid-mutation before `.state` itself flipped):
                    #     `runtime_obj` is ours alone to dispose, AND the
                    #     reservation itself must be aborted -- left
                    #     LOADING otherwise, it would block every future
                    #     acquisition for this id forever, exactly like an
                    #     un-aborted load failure would.
                    if entry.state is RuntimeState.READY:
                        self.runtime_cache.release_lease(manifest.id, entry)
                    else:
                        # Codex re-review, found on this round's own fix
                        # commits: `dispose_unpublished()` -> `_run_cleanup()`
                        # only swallows `Exception`; a `BaseException`
                        # escaping the `on_evict` hook itself used to skip
                        # `abort_reservation()` entirely, leaving this
                        # entry stuck at `LOADING` forever -- the exact
                        # failure this branch exists to prevent. The
                        # `finally` guarantees the reservation is always
                        # aborted once disposal has been attempted,
                        # whatever it raised.
                        try:
                            self.runtime_cache.dispose_unpublished(manifest.id, runtime_obj)
                        finally:
                            self.runtime_cache.abort_reservation(manifest.id, entry)
                    raise
            return entry
        finally:
            load_lock.release()

    def _acquire_execution_lock(
        self, canonical_id: str, entry: RuntimeEntry, deadline: float | None
    ) -> None:
        """Acquire `entry.execution_lock` ("E") and revalidate it afterward.

        PR4a Codex-review Finding 2 (round 1): winning E is not, by itself,
        proof that `entry` is still safe to hand to user code -- this
        caller's own lease (taken in `_acquire_or_load_entry()`, before this
        method runs) may have been sitting on an entry another lease-holder
        marked `INVALID` while this caller was still parked waiting on E
        (see `RuntimeHandle.release()`'s own docstring for the other half of
        this fix). So immediately after acquiring E, this checks
        `runtime_cache.is_current_and_ready()`; a `False` result means this
        entry was invalidated (or, in principle, replaced) out from under
        this caller during the wait, and E is released again before raising
        `RuntimeBusyError` -- this method only ever cleans up what it itself
        acquired (E); the caller's own lease is the caller's own cleanup.

        Codex re-review (found on this round's own fix commit): everything
        after winning E is wrapped in `try`/`except BaseException` below,
        not just the explicit `RuntimeBusyError` this method itself raises
        -- an async `BaseException` (`KeyboardInterrupt`, a cancellation
        signal, ...) landing during `is_current_and_ready()` used to leave
        this method exiting without ever releasing E, and the caller's own
        exception handling only releases the lease and G, never E -- a
        permanent, unrecoverable deadlock on this exact entry for every
        future caller. This method now guarantees E is released on any
        exception it does not itself successfully return past.
        """

        if not _acquire_with_deadline(entry.execution_lock, deadline):
            raise RuntimeBusyError(
                f"Timed out waiting for exclusive execution access to {canonical_id!r}."
            )
        try:
            if not self.runtime_cache.is_current_and_ready(canonical_id, entry):
                raise RuntimeBusyError(
                    f"{canonical_id!r} became invalid while waiting for exclusive "
                    "execution access; retry."
                )
        except BaseException:
            entry.execution_lock.release()
            raise

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


__all__ = ["ModelService", "RuntimeAdmissionController", "RuntimeHandle"]
