"""Application-facing entrypoint for model resolution and loading."""

from __future__ import annotations

from collections.abc import Callable
import math
import time
from threading import Lock, Semaphore, Thread, current_thread
from typing import Any

from .cache import ModelRuntimeCache
from .cloud_guard import ensure_cloud_provider_enabled
from .loader import LoaderRegistry
from .manifest import ModelManifest
from .registry import ModelRegistry
from .resolver import ModelResolver
from .runtime_lease import (
    RuntimeBusyError,
    RuntimeEntry,
    RuntimeState,
    RuntimeWaitTimeoutError,
    _RuntimeInvalidEntryDrainingError,
)

# PR4a (issue #414): the one process-wide "may a heavy runtime load or run
# right now" slot -- see `ModelService.acquire_runtime()`'s own docstring for
# why this is not per-generator, per-media, or per-runtime-class. Every
# runtime is classified "heavy" in PR4a; an explicitly audited lighter
# classification (cloud/procedural) is PR4b+ scope, and even then the
# default for anything unclassified stays protected ("unknown => protected").
DEFAULT_ADMISSION_CAPACITY = 1

# Upper bound on one synchronization slice of a checkpointed
# `acquire_runtime(wait_checkpoint=...)` call -- see that method's docstring.
DEFAULT_WAIT_POLL_INTERVAL = 0.1


def _validate_wait_parameters(
    wait_timeout: float | None,
    wait_checkpoint: Callable[[], None] | None,
    poll_interval: float,
) -> None:
    """Reject malformed waiting parameters before any resolution or resource.

    `poll_interval` is always validated (an invalid explicit value is a
    programming error even where it is unused). `wait_timeout` is validated
    only in checkpoint mode: plain mode keeps its long-standing, lenient
    semantics (a negative value is simply a non-blocking attempt).
    """

    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)):
        raise TypeError(f"poll_interval must be a number, got {poll_interval!r}.")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError(f"poll_interval must be finite and > 0, got {poll_interval!r}.")
    if wait_checkpoint is None:
        return
    if not callable(wait_checkpoint):
        raise TypeError(f"wait_checkpoint must be callable, got {wait_checkpoint!r}.")
    if wait_timeout is None:
        return
    if isinstance(wait_timeout, bool) or not isinstance(wait_timeout, (int, float)):
        raise TypeError(f"wait_timeout must be a number or None, got {wait_timeout!r}.")
    if not math.isfinite(wait_timeout) or wait_timeout < 0:
        raise ValueError(
            "wait_timeout must be None or finite and >= 0 when wait_checkpoint "
            f"is given, got {wait_timeout!r}."
        )


class _StaleDrainOperation:
    """Stale-drain eligibility owned by exactly one `acquire_runtime()` call.

    Created empty on public entry and cleared in that call's own `finally`
    -- never stored on `ModelService`, never keyed by thread, never visible
    to any other call. `entry` is armed only by this call's own post-E
    `_RuntimeExecutionRevalidationFailed`, and is only ever compared by
    object identity. Internal code reads it inline and never binds it to a
    local variable, so no frame on a surfaced traceback can keep the stale
    entry reachable once this holder is cleared.
    """

    __slots__ = ("entry",)

    def __init__(self) -> None:
        self.entry: RuntimeEntry | None = None

    def armed_entry(self) -> RuntimeEntry:
        if self.entry is None:
            raise RuntimeError("stale-drain state was used before it was armed")
        return self.entry


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

    Known, accepted limitation (Codex re-review, this round): if a
    `BaseException` (an async signal -- `KeyboardInterrupt`, a forced
    cancellation) lands after `lockable.acquire()` has *internally*
    succeeded but before this function's `return` actually reaches its
    caller, the caller never learns it owns `lockable` and so never
    releases it -- a leaked slot indistinguishable, from every caller
    above this function, from one that legitimately timed out. Closing
    this precisely would require either OS-level signal masking around
    the single instruction boundary between "acquired" and "returned"
    (well outside plain `try`/`finally`, and outside PR4a's scope, which
    does not touch process/thread signal handling) or accepting a
    fundamentally different acquisition primitive; every call site in
    this module already wraps its own `_acquire_with_deadline()` call in
    the surrounding `try`/`finally` chain that would release the
    resource in the overwhelmingly common case (the exception arriving
    at any other point), so this narrows an already-vanishingly-small
    window rather than leaving it wide open.
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
        # PR4a v1 (final-convergence pass on issue #414, addressing a
        # Codex-review finding that survived several earlier, narrower fix
        # rounds): a thread that already holds even *one* open
        # `RuntimeHandle` is unconditionally refused another, regardless of
        # `capacity` and regardless of canonical id. This used to only
        # fire once a thread had exhausted every one of `capacity`'s slots
        # by itself, which left a real cross-thread deadlock cycle open
        # whenever `capacity > 1`: thread A holds entry X and, while still
        # holding it, blocks acquiring G for a *different* entry Y; thread
        # B holds the process's other G slot and is itself blocked waiting
        # on X's E, which only A can release -- but A can never release X
        # until its own nested call for Y returns. `capacity > 1` allowing
        # one thread to hold multiple simultaneous runtimes is not a PR4a
        # requirement (v1 favors simplicity over that scheduling
        # machinery); a "hold N runtimes on one thread at once" use case,
        # if ever needed, is a separate, future, deliberately-designed
        # deadlock-free multi-runtime acquisition protocol, not something
        # this gate should permit implicitly. `capacity > 1` still has a
        # meaning here: it bounds how many *different* threads may each
        # hold one runtime at the same time.
        #
        # Ownership is tracked by *acquiring* thread in a plain dict guarded
        # by `_held_lock`, deliberately NOT `threading.local()`: this
        # codebase explicitly supports releasing a `RuntimeHandle` from a
        # different thread than the one that acquired it (a cancellation/
        # cleanup thread finishing up after a worker thread -- see
        # `RuntimeHandle`'s own `_owner_thread`, captured at acquisition
        # time and threaded through to this class's `release()` so the
        # *original* acquirer's count is what gets decremented, not
        # whichever thread happens to call `release()`). A first version of
        # this used `threading.local()`, which cannot be updated for
        # another thread at all -- a cross-thread release left the original
        # acquiring thread's local counter permanently stuck, rejecting its
        # next, perfectly legitimate acquisition (Codex re-review caught
        # this too).
        #
        # Keyed by the `threading.Thread` object itself (`current_thread()`),
        # not `get_ident()`'s raw integer: a second Codex re-review caught
        # that OS-level thread ids *are* recycled once a thread exits -- a
        # worker that acquires a handle and then exits before a supported
        # cleanup thread releases it could have its numeric id reassigned
        # to an unrelated, later-started thread, which would then inherit
        # its held-count and be wrongly rejected as "recursive". A `Thread`
        # object's identity is not recycled this way: a new OS thread
        # always gets a new Python `Thread` object, never the exited one's.
        self._held_lock = Lock()
        self._held_by_thread: dict[Thread, int] = {}

    def acquire(self, deadline: float | None) -> bool:
        """Acquire one process-wide admission slot for `current_thread()`.

        Unconditionally rejects (via `RuntimeError`, immediately, before
        touching the semaphore at all) any attempt from a thread that
        already holds an open slot from an earlier, still-open
        `acquire_runtime()` call -- whether this call would block
        (`deadline is None` or still in the future) or not (`deadline`
        already past, i.e. `wait_timeout=0`). See `__init__`'s own comment
        for why this is unconditional on `capacity` rather than only
        firing once every slot is exhausted, and why a non-blocking probe
        is refused the same way rather than being allowed to succeed in
        creating a second, nested handle.
        """

        owner_thread = current_thread()
        with self._held_lock:
            held = self._held_by_thread.get(owner_thread, 0)
        if held >= 1:
            raise RuntimeError(
                "Nested acquire_runtime() call detected: this thread "
                "already holds an open RuntimeHandle from an earlier, "
                "still-open acquire_runtime() call. PR4a v1 does not "
                "support one thread holding more than one RuntimeHandle "
                "at a time, regardless of canonical id or admission "
                "capacity -- release the existing RuntimeHandle before "
                "acquiring another."
            )
        acquired = _acquire_with_deadline(self._semaphore, deadline)
        if acquired:
            with self._held_lock:
                self._held_by_thread[owner_thread] = self._held_by_thread.get(owner_thread, 0) + 1
        return acquired

    def release(self, owner_thread: Thread) -> None:
        """Release one slot previously credited to `owner_thread`.

        Known, accepted limitation (Codex re-review, this round): if a
        `BaseException` lands after the `with self._held_lock:` block
        below has already updated the ownership bookkeeping but before
        `self._semaphore.release()` actually executes, the slot is
        consumed forever -- bookkeeping already shows nobody holds it,
        so a later retry of this exact call is a silent no-op (the
        caller's own `RuntimeHandle` is also already marked released by
        this point, so nothing would even prompt a retry). Splitting the
        two steps further would not remove the gap, only move it; closing
        it needs OS-level signal masking around the whole
        bookkeeping-then-return transition, which is outside PR4a's scope
        -- see `_acquire_with_deadline()`'s own docstring for the same
        class of window on the acquiring side.
        """

        with self._held_lock:
            current = self._held_by_thread.get(owner_thread, 0)
            if current > 1:
                self._held_by_thread[owner_thread] = current - 1
            else:
                self._held_by_thread.pop(owner_thread, None)
        self._semaphore.release()


class _RuntimeExecutionRevalidationFailed(RuntimeBusyError):
    """Private signal: `E` was won, but the pinned entry is no longer usable.

    Raised only by `_acquire_execution_lock()`, and only for the one
    condition documented on that method: this caller's own lease was taken
    on a `READY` entry, another lease-holder's conservative failure-unwind
    (`RuntimeHandle.release(had_exception=True)`) marked it `INVALID` while
    this caller was still parked waiting on `E`, and this caller then won
    `E` onto an entry that is now known-unsafe.

    This is a `RuntimeBusyError` subtype purely so it is caught by the same
    exact clause `_acquire_entry_and_execution_lock()`'s callers already use
    for lease cleanup -- it changes nothing about what any *existing*
    `except RuntimeBusyError:` catches. It is deliberately never exported
    (not in this module's `__all__`, not re-exported from `core.models`):
    the only code that may ever catch it by name is
    `ModelService.acquire_runtime()` itself, which uses it to decide,
    internally, whether this specific handoff is worth one bounded retry
    (see that method's own docstring) -- no other `RuntimeBusyError` raise
    site in this module or in `ModelRuntimeCache` (pinned capacity, busy
    unload/reservation targets, ...) is reclassified by this type; every one
    of those keeps raising the plain, public `RuntimeBusyError`, unretried,
    exactly as before.

    `entry` is the exact `RuntimeEntry` this caller's own lease was pinned
    on and just failed to revalidate -- carried (multi-waiter INVALID-entry
    drain, approved contract, PR #420) so
    `_acquire_entry_after_revalidation_failure()` can tell "the same stale
    entry I already know about" apart from any other entry by identity, not
    only by canonical id.
    """

    def __init__(self, message: str, *, entry: RuntimeEntry) -> None:
        super().__init__(message)
        self.entry = entry


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
        "_owner_thread",
        "_released",
        "_release_guard",
        "_entered",
        "_pending_release",
        "_pending_invalidation",
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
        owner_thread: Thread,
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
        self._owner_thread = owner_thread
        self._released = False
        self._release_guard = Lock()
        self._entered = False
        # PR4a v1 final-convergence pass (issue #414): `_entered` now marks
        # this handle "entering-or-active" for its *entire* remaining
        # lifetime, not just the brief `__enter__()`/`release()` handoff --
        # see `release()`'s own docstring for what that buys. Set once,
        # inside `_release_guard`, in `__enter__()`; only ever cleared by
        # going `_released = True` (never back to `False`).
        self._pending_release = False
        self._pending_invalidation = False

    def __enter__(self) -> "RuntimeHandle":
        # Codex re-review, found on this round's own fix commits: this
        # used to return `self` unconditionally, so a handle could be
        # entered again after `release()` (its E/lease/G already returned,
        # with the next `__exit__` then a silent no-op) or nested inside
        # its own `with` block (the inner block's `__exit__` would release
        # everything while the outer block kept using the runtime
        # unprotected). This class is the advertised safe-use unit for
        # PR4a; both are now rejected immediately instead of silently
        # exposing an unprotected runtime.
        #
        # Codex re-review (found on this fix in turn): the check above
        # must share `_release_guard` with `release()`'s own check-and-set
        # -- reading `_released` unguarded here could observe a stale
        # `False` while a concurrent cross-thread `release()` call (an
        # explicitly supported pattern: a supervisor/cleanup thread
        # reclaiming a handle a worker thread acquired) is *in the middle
        # of* its own guarded transition, letting this method return a
        # handle whose E/lease/G have already been (or are about to be)
        # returned. Using the same lock makes the two checks mutually
        # exclusive: this method can never observe `_released` mid-flight,
        # only fully before or fully after any given `release()` call's
        # own transition.
        # Codex re-review, PR4a v1 final-convergence pass: the process-wide
        # admission slot (G) this handle owns is credited to
        # `self._owner_thread` -- the thread whose `acquire_runtime()` call
        # actually acquired it -- not to whichever thread happens to call
        # `__enter__()`. If a handle were entered on a *different* thread
        # (acquired on thread A, handed off, entered by thread B), G's own
        # nested-acquisition ban (`RuntimeAdmissionController.acquire()`)
        # would see no holding recorded for B at all: B could then start a
        # second, unrelated `acquire_runtime()` call of its own and block
        # on G, while a third thread contending for this handle's own E
        # can only ever be unblocked by *this* handle's `__exit__()` --
        # which only B, now the context owner, will ever call. That is
        # exactly the cross-thread deadlock cycle the unconditional G-level
        # ban exists to prevent, reintroduced through a path the ban's own
        # per-thread bookkeeping cannot see. Rather than re-keying admission
        # tracking by "whichever thread enters" (real complexity, and still
        # wouldn't cover a handle used as a context manager on one thread
        # then handed to *another* for `release()`), v1 keeps this simple:
        # a handle must be entered on the exact same thread that acquired
        # it. Cross-thread hand-off remains supported for `release()` only
        # (see that method's own docstring).
        owner_thread = current_thread()
        with self._release_guard:
            if self._released:
                raise RuntimeError(
                    f"Cannot re-enter a RuntimeHandle for {self._canonical_id!r} "
                    "as a context manager after it has already been released."
                )
            if self._entered:
                raise RuntimeError(
                    f"Cannot nest `with` blocks on the same RuntimeHandle for "
                    f"{self._canonical_id!r} -- the inner block's __exit__ "
                    "would release E/lease/G while the outer block is still "
                    "using them."
                )
            if owner_thread is not self._owner_thread:
                raise RuntimeError(
                    f"Cannot enter a RuntimeHandle for {self._canonical_id!r} "
                    "as a context manager on a different thread than the one "
                    "that acquired it via acquire_runtime() -- the process-wide "
                    "admission slot this handle holds is credited to the "
                    "acquiring thread, and a different thread entering the "
                    "context could then independently acquire another runtime "
                    "and deadlock against this handle's own execution lock, "
                    "which only the entering thread's own __exit__() could "
                    "ever release."
                )
            self._entered = True
        # PR4a v1 final-convergence pass (issue #414): no handoff signal is
        # needed here any more -- `_entered = True`, set above, is now a
        # persistent "entering-or-active" marker for this handle's entire
        # remaining lifetime (see `release()`'s own docstring). A
        # concurrent `release()` that observes `_entered` already `True`
        # never proceeds to unwind E/lease/G itself, no matter how soon
        # after the line above it runs -- it only records a deferred
        # request and returns immediately. There is therefore no window,
        # of any size, between committing `_entered` here and this
        # method's own `return self` in which a concurrent `release()`
        # could tear down what this call is about to hand its caller.
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        """Perform the real E -> lease -> G unwind for this handle's `with` block.

        PR4a v1 final-convergence pass (issue #414): `__exit__()` is the
        *only* call path allowed to actually release E/lease/G while
        `_entered` is `True` -- it always runs on the context owner's own
        thread (Python guarantees a `with` block's `__exit__` runs on
        whichever thread is executing that block), at the exact point the
        body has finished, whether normally or via an exception. Any
        *other* caller reaching `release()` during this same window (see
        that method's own docstring) only records a deferred request; this
        method is what actually acts on it.
        """

        with self._release_guard:
            if self._released:
                # Already unwound: only reachable if something outside this
                # `with` block already called `release()` before entering
                # (impossible -- `_entered` would then be `False` at
                # `__enter__()` time and this method could not be running),
                # so this is purely a defensive no-op, not a real path.
                return
            self._released = True
            had_exception = exc_type is not None
            # A cross-thread `release(had_exception=True)` call during this
            # `with` block's own execution already published the entry as
            # INVALID *before* it ever touched `_release_guard` (see that
            # method's own docstring) -- calling `mark_invalid()` again
            # here is a harmless no-op in that case. This still needs to
            # run when *this* call's own `had_exception` is `True` even if
            # no deferred request ever arrived, and OR-combines with any
            # deferred request's own flag so either side lands on the safe
            # (INVALID) side.
            if had_exception or self._pending_invalidation:
                self._cache.mark_invalid(self._canonical_id, self._entry)
        self._teardown()

    def release(self, *, had_exception: bool = False) -> None:
        """Manually release this handle. Idempotent; safe from any thread.

        Two entirely different behaviors depending on this handle's state,
        both required by PR4a v1's final-convergence pass on issue #414:

        1. Not yet entered as a context manager (`_entered` is still
           `False`): releases E -> lease -> G immediately, exactly as
           before -- the "acquire, use without `with`, then call
           `release()` when done" pattern this method has always supported.
        2. Entering or active (`_entered` is `True`, `_released` is still
           `False`): this call, whichever thread it runs on, does **not**
           touch E/lease/G at all -- it only records a deferred request
           (`_pending_release = True`, OR-combined into
           `_pending_invalidation`) and returns immediately. The actual
           unwind is performed later by this handle's own `__exit__()`
           (see that method's docstring). This is the fix for a Codex
           re-review finding that survived several earlier, narrower
           rounds: previously, a cross-thread `release()` racing the tail
           end of `__enter__()` could tear down E/lease/G while the `with`
           body was about to start (or had already started) running,
           reachable via ordinary thread scheduling alone, no exception
           required. Deferring unconditionally whenever `_entered` is
           `True` -- rather than adding yet another `Event` to narrow the
           handoff window further -- removes the race structurally: there
           is no longer any call path by which anything other than this
           handle's own `__exit__()` can free a resource the `with` body
           might still be using, for as long as that body is running.
           This also means an *intentional* same-thread `release()` call
           made from *inside* the handle's own `with` body no longer frees
           anything early either -- the actual unwind still waits for
           `__exit__()` -- which is the simpler of the two v1-acceptable
           designs (the alternative, turning this into an explicit error
           in that case, adds a new failure mode for no added safety, since
           the body keeps E/lease/G either way).

        `had_exception=True` marks the entry `INVALID` (rather than leaving
        it `READY` for reuse), so a second caller already parked waiting on
        E for this exact entry can never win E and observe a still-`READY`
        runtime this caller has already deemed unsafe (PR4a's own
        Codex-review Finding 2, round 1). This is a conservative,
        correctness-first policy for v1, not a classification of which
        exceptions actually leave a runtime unsafe to reuse; PR4b can
        narrow it with real evidence once generator boundaries migrate onto
        this API.

        PR4a v1 final-convergence pass, Codex-review finding "Publish
        racing invalidation before releasing E": the `mark_invalid()` call
        below runs *before* this method ever touches `_release_guard`, not
        after acquiring it. A prior version called it only once this
        method had won the guard -- but an ordinary, no-exception
        `__exit__()` racing this exact call could win that same guard
        *first* (this call still blocked waiting for it), see no
        invalidation recorded yet, proceed straight to `_teardown()`, and
        release E, letting an existing E-waiter (reachable with
        `admission_capacity > 1`) win E, revalidate a still-`READY` entry,
        and start using a runtime this call already knew was unsafe --
        entirely via ordinary thread scheduling, no exception required
        (unlike the accepted, signal/`BaseException`-only windows
        documented elsewhere in this module). `mark_invalid()` is a short,
        standalone metadata transaction with its own lock (`M`, held only
        for the duration of that one call -- see its own docstring: exact
        current entry only, no-op if stale or already non-`READY`,
        idempotent), fully released before this method ever attempts
        `_release_guard`, so this introduces no new lock-ordering hazard
        (M is still never held while waiting on G/L/E/`_release_guard`
        anywhere in this module -- the two are strictly sequential here,
        never nested). Publishing first, independent of `_release_guard`
        entirely, means: once an `had_exception=True` call has been
        *invoked* at all -- even if still blocked waiting for the guard --
        the entry is already `INVALID` before any other call on this
        handle can possibly reach `_teardown()`'s own E-release. Calling
        it unconditionally, before even checking `self._released`, is
        always safe (idempotent, and a no-op once the entry is stale) and
        needs no branch of its own.

        Codex re-review (found on an earlier round's fix commits): the
        `_released`/`_entered` reads and the `_released = True` write below
        are guarded by `_release_guard` (a private, per-handle `Lock`)
        because they are not otherwise atomic -- two threads calling
        `release()` on the exact same handle at once could both observe
        `_released is False` before either sets it `True` and both proceed
        into the immediate-unwind path, double-releasing E/lease/G and
        permanently inflating the process-wide admission capacity by one.
        Only the flag transition itself needs the lock; the actual release
        work (case 1) or deferred-request bookkeeping (case 2) either needs
        no further synchronization or is itself entirely inside the guard.

        Known, accepted limitation (Codex re-review, an earlier round): if
        a `BaseException` lands after `_release_guard` has already set
        `self._released = True` (case 1) but before `_teardown()` actually
        runs, none of E/lease/G is ever released, and every later call to
        this method returns immediately -- a permanent leak of all three.
        Closing this needs OS-level signal masking around the whole
        transition, which is outside PR4a's scope; see
        `_acquire_with_deadline()`'s own docstring for the identical
        conclusion on the acquiring side of this exact class of window.
        Not reachable through normal cooperative job cancellation, and
        does not include ordinary thread-scheduling races -- see this
        module's `RuntimeAdmissionController.release()` for the same
        scoping on the process-wide-admission side of this class of
        window.
        """

        if had_exception:
            self._cache.mark_invalid(self._canonical_id, self._entry)

        with self._release_guard:
            if self._released:
                return
            if had_exception:
                self._pending_invalidation = True
            if self._entered:
                # Entering or active: defer the real unwind to __exit__().
                self._pending_release = True
                return
            self._released = True
        self._teardown()

    def _teardown(self) -> None:
        """Actually release E -> lease -> G, in that order. Called at most
        once per handle, only by `release()`'s not-yet-entered path or by
        `__exit__()` -- both already hold the sole `self._released = True`
        transition guaranteeing this runs exactly once.
        """

        try:
            self._entry.execution_lock.release()
        finally:
            try:
                self._cache.release_lease(self._canonical_id, self._entry)
            finally:
                self._admission.release(self._owner_thread)


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
        """Legacy raw-runtime API. Test/diagnostic use only -- see
        `resolve_runtime()` for the full contract; this is a thin wrapper that
        discards the manifest half of its return value.
        """

        _, runtime_obj = self.resolve_runtime(model_id, media_type, task_type)
        return runtime_obj

    def resolve_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> tuple[ModelManifest, Any]:
        """Legacy raw-runtime API. **Test/diagnostic use only.**

        PR4b (issue #414) is complete: every production generator -- text,
        image, video, music and speech -- now obtains runtimes through
        `acquire_runtime()`, and the semantic CLIP/CLAP judge participates in
        the same process-wide admission domain. A repo-wide static guard
        (`tests/test_runtime_surface_guard.py`) asserts that no module under
        `apps/`, `bootstrap/`, `core/`, `generators/` or `scripts/` calls this
        method or `get_runtime()`; the single allowlisted exception is
        `get_runtime()`'s own delegation just above.

        This method is retained for backwards compatibility, not because a
        safe production use was found -- the audit that closed PR4b found
        none. What it does **not** give you, in contrast to
        `acquire_runtime()`:

        - no lease: the entry is not pinned, so cache-pressure eviction, a
          concurrent `unload_model()`/`unload_all()`, or an
          `acquire_runtime()` caller's own replacement may retire this exact
          runtime object while you still hold the reference;
        - no execution exclusion (`E`): another caller may be executing
          against this same mutable runtime -- including LoRA mutation --
          concurrently;
        - no admission (`G`): the heavyweight `loader.load()` this may
          trigger is not counted against the process-wide local-heavy budget,
          so it can run alongside a generator's own load/inference;
        - no INVALID-state participation: a runtime another caller has
          already deemed unsafe may still be returned from cache here.

        Permitted: tests and diagnostics that deliberately exercise this
        legacy path itself (its cache reuse, its cloud opt-in guard, its
        publication-rejection disposal), where the caller accepts the absence
        of every guarantee above.

        Forbidden: any production path, and in particular any caller that
        would execute the returned runtime, mutate it, or retain it across
        another operation that can unload, evict or replace it.

        Deliberately not deprecated with a runtime `DeprecationWarning` (it
        would fire only in the tests that legitimately exercise this path) and
        deliberately not renamed to a private symbol (that would break
        external compatibility for no additional safety, since the static
        guard already prevents reintroduction inside this repo). Making it
        private remains a future option once no out-of-tree caller is a
        concern.
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
        wait_checkpoint: Callable[[], None] | None = None,
        poll_interval: float = DEFAULT_WAIT_POLL_INTERVAL,
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

        Do not call this method again, on the same thread, while it already
        holds an open `RuntimeHandle` from an earlier, still-open
        `acquire_runtime()` call -- release that handle first.
        `RuntimeAdmissionController.acquire()` rejects any such nested
        attempt immediately with a plain `RuntimeError`, unconditionally,
        regardless of canonical id (same or different) and regardless of
        `admission_capacity` (see that class's own docstring). PR4a v1
        deliberately does not support one thread holding more than one
        `RuntimeHandle` at a time: `admission_capacity > 1` still has a
        real meaning -- it bounds how many *different* threads may each
        hold one runtime at once -- but it is not a way to let a single
        thread hold several. A prior version of this rule only fired once
        a thread had exhausted every one of `capacity`'s slots by itself,
        which left a real cross-thread deadlock cycle open whenever
        `capacity > 1`: thread A holds entry X and, while still holding
        it, blocks acquiring G for a *different* entry Y; thread B holds
        the process's other G slot and is itself blocked waiting on X's E,
        which only A can release -- but A can never release X until its
        own nested call for Y returns. Rejecting every nested acquisition
        unconditionally (this method never needs full wait-for-graph
        deadlock detection to do it) closes this cycle outright. A
        genuine "hold several runtimes on one thread at once" use case, if
        ever needed, is out of scope for PR4a and belongs in a separate,
        deliberately designed deadlock-free multi-runtime acquisition
        protocol, not an implicit side effect of raising `capacity`.

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
        that ends up loading. In plain mode, `wait_timeout=None` (the
        default) blocks the ordinary way on G/L/E (checkpoint mode instead
        waits in bounded slices -- see below) -- see `_acquire_with_deadline()`'s own
        docstring for why that is not the "indefinite wait" issue #414
        forbids. (Codex-review Finding 8, round 1: this parameter was
        previously named `timeout` and its docstring claimed to be "a single
        overall deadline covering the entire `acquire_runtime()` call",
        which was never true once a cache miss reached `loader.load()` --
        renamed, since PR4a has not shipped/stabilized yet, rather than kept
        under a name that promised more than the implementation -- deliberately
        not turned into a general preemption framework -- delivers.)

        A G/L/E wait deadline raises `RuntimeWaitTimeoutError`, a subtype
        of `RuntimeBusyError`. Only that subtype is ever waited out again
        (see checkpoint mode below); pinned capacity and invalid-entry
        errors remain immediate failures rather than being silently
        converted into indefinite waits.

        **Checkpoint mode** (`wait_checkpoint` given; PR #420, issue #414
        follow-up -- the supported way to wait cooperatively). One public
        call is one logical waiting operation:

        - `wait_checkpoint()` is called once before the first
          synchronization attempt, and again after every synchronization
          slice that ended in `RuntimeWaitTimeoutError` -- always with no
          G, L, E, M, or lease held by this call. Only after it returns
          normally does another slice begin. Whatever it raises propagates
          unchanged (a cooperative cancellation, a failing probe, a
          `BaseException`), and the operation ends there.
        - Each slice is the ordinary G -> L -> E acquisition bounded by
          `min(now + poll_interval, overall deadline)`.
        - `wait_timeout` is the overall deadline of the whole call,
          measured from entry and never reset per slice. `None` waits
          indefinitely, but only as repeated bounded slices separated by
          checkpoints -- never an unbroken block. `0` stays a single
          non-blocking attempt, never a polling wait. It must be `None` or
          finite and `>= 0`.
        - The model id is resolved once, after the first checkpoint; the
          cloud-provider opt-in guard is re-checked before every slice.
        - Only `RuntimeWaitTimeoutError` starts another slice. Every other
          `RuntimeBusyError` (and every other exception) ends the call.

        **Plain mode** (`wait_checkpoint` is `None`): exactly one slice
        bounded by `wait_timeout` as described above; `poll_interval` is
        validated but otherwise ignored. Separate public calls -- including
        a caller's own hand-written retry loop of plain calls -- are always
        independent, fresh callers: nothing carries over between them.

        Invalid `poll_interval`/`wait_checkpoint` (and, in checkpoint mode,
        `wait_timeout`) values raise `TypeError`/`ValueError` before the
        model id is resolved or any resource is touched.

        Two narrow exceptions to "immediate failure", both scoped to a
        caller that has already had its own post-E revalidation failure on
        one *specific* entry -- see `_acquire_entry_with_execution_lock()`
        and `_acquire_entry_after_revalidation_failure()` for the full
        mechanics:

        1. If step 6's revalidation fails -- this caller won E onto an
           entry another lease-holder's failure-unwind invalidated during
           the wait -- steps 3-6 (L -> E, including a fresh `loader.load()`
           if needed) are retried internally, still inside this same call's
           G hold and still bounded by this same `deadline` (never reset,
           never given a fresh budget). This is safe to retry, unlike every
           other `RuntimeBusyError` cause here: by the time this caller
           observes the failure, its own losing attempt has already
           released its own stale lease on the now-`INVALID` entry (see
           `_acquire_entry_and_execution_lock()`), which is exactly what
           makes that entry immediately evictable/reloadable -- not a wait
           on some other, unrelated caller's future action, the case
           `RuntimeBusyError` exists to refuse. If the retry hits this
           exact condition a second time -- a genuinely new revalidation
           failure, on any entry -- this method gives up and raises the
           plain, public `RuntimeBusyError`; this half of the contract is
           completely unchanged from PR4a v1.
        2. Multi-waiter INVALID-entry drain (approved contract, PR #420,
           issue #414 follow-up): that first retry can itself land back on
           the *exact same* entry, still `INVALID` and still pinned --
           not because it is stuck, but because `admission_capacity >= 2`
           let more than one caller pin it before it was invalidated, and
           this caller's own retry raced ahead of another stale
           lease-holder's still-in-flight, but provably bounded, O(1)
           unwind (see `_RuntimeInvalidEntryDrainingError`'s own docstring
           for the proof that this is always safe to wait for, never an
           indefinite wait on unrelated future action). This method then
           waits -- via a `Condition` notified on every lease release, never
           a poll loop -- for that exact entry's last stale lease to
           release or for it to be replaced, still bounded by this same
           shared `deadline`, and retries once it does. This wait is
           unbounded in *attempt count* but not in *time*: it ends either
           by converging (the entry drains) or by the shared `deadline`
           expiring into the same `RuntimeWaitTimeoutError` path described
           below. It is scoped strictly to the one entry this caller's own
           revalidation failure was about -- a *different* entry (a newer
           generation) that happens to also be INVALID and pinned is never
           waited for by this mechanism; it fails immediately like any
           other unrelated `RuntimeBusyError`.

        Operation-scoped stale-drain ownership (PR #420, issue #414
        follow-up): the eligibility #1 and #2 describe belongs to exactly
        ONE public call and never survives it. It lives in a private,
        call-local `_StaleDrainOperation` created empty on entry and
        cleared in this method's own `finally` on every exit -- success,
        overall timeout, checkpoint exception, any other exception. It is
        never stored on this instance, never keyed by thread, and never
        visible to another call, so a worker thread later reused for an
        unrelated job can never inherit it. In checkpoint mode it is
        preserved across slices of the same call: a slice whose first
        attempt meets `_RuntimeInvalidEntryDrainingError` for the *exact*
        entry (object identity) this call's own earlier revalidation
        failure armed resumes #2's wait instead of failing fast; any other
        entry/generation still fails fast. The #1 budget is per logical
        call as well: once armed, a further revalidation failure in any
        later slice raises the plain `RuntimeBusyError`.

        Every other cause of `RuntimeBusyError` from steps 3-6 (pinned
        eviction victim, a `LOADING`/`RETIRING` busy target, capacity
        claimed by a concurrent reservation, an INVALID-and-pinned entry
        encountered with no revalidation failure of this call's own yet,
        ...) is never retried or waited on by this method; it still fails
        immediately, exactly as before. If a slice's deadline is exhausted
        during either retry path, `RuntimeWaitTimeoutError` surfaces (from
        the normal G/L/E-timeout path for #1, or explicitly for #2's drain
        wait). This method has no knowledge of job cancellation; a caller
        supplies it only through `wait_checkpoint`. Neither exception
        widens what `unload_model()`/`unload_all()` may act on, and neither
        changes the G -> L -> E acquisition ordering above.

        Exception retention: every `RuntimeBusyError`/
        `RuntimeWaitTimeoutError` this method surfaces is a fresh instance
        of the same public type with the same message, raised outside the
        scope of the internal exception it replaces -- so its
        `__cause__`/`__context__`/traceback never keeps a private
        stale-entry signal (whose `.entry` pins a `RuntimeEntry` and its
        runtime) reachable after the call has ended.
        """

        _validate_wait_parameters(wait_timeout, wait_checkpoint, poll_interval)
        drain = _StaleDrainOperation()
        try:
            if wait_checkpoint is None:
                manifest = self.get_manifest(model_id, media_type, task_type)
                if manifest.provider == "cloud":
                    ensure_cloud_provider_enabled(manifest.id)
                deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
                return self._acquire_runtime_slice(manifest, media_type, deadline, drain)
            return self._acquire_runtime_with_checkpoints(
                model_id, media_type, task_type,
                wait_timeout=wait_timeout,
                wait_checkpoint=wait_checkpoint,
                poll_interval=poll_interval,
                drain=drain,
            )
        finally:
            drain.entry = None

    def _acquire_runtime_with_checkpoints(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None,
        *,
        wait_timeout: float | None,
        wait_checkpoint: Callable[[], None],
        poll_interval: float,
        drain: _StaleDrainOperation,
    ) -> RuntimeHandle:
        """Checkpoint mode of `acquire_runtime()`: bounded slices, one operation.

        `wait_checkpoint()` is only ever called here, at statement level,
        never inside an `except` block -- so neither the checkpoint's own
        exception nor anything it chains to ever carries a runtime-internal
        exception as its `__context__`. Every slice has already released
        all of G/L/E/lease (and M is never held outside the cache's own
        short methods) before control returns to this loop.
        """

        overall_deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        wait_checkpoint()
        manifest = self.get_manifest(model_id, media_type, task_type)
        while True:
            if manifest.provider == "cloud":
                ensure_cloud_provider_enabled(manifest.id)
            slice_deadline = time.monotonic() + poll_interval
            if overall_deadline is not None:
                slice_deadline = min(slice_deadline, overall_deadline)
            timeout_message = ""
            try:
                return self._acquire_runtime_slice(manifest, media_type, slice_deadline, drain)
            except RuntimeWaitTimeoutError as exc:
                timeout_message = str(exc)
            if overall_deadline is not None and time.monotonic() >= overall_deadline:
                raise RuntimeWaitTimeoutError(timeout_message)
            wait_checkpoint()

    def _acquire_runtime_slice(
        self,
        manifest: ModelManifest,
        media_type: str,
        deadline: float | None,
        drain: _StaleDrainOperation,
    ) -> RuntimeHandle:
        """One G -> L -> E acquisition bounded by `deadline`.

        Any busy/timeout failure is re-raised as a fresh instance of the
        same public type, after leaving the `except` scope that caught it:
        the internal exception (and its traceback frames, whose locals may
        reference a `RuntimeEntry`) becomes unreachable instead of riding
        along as `__context__`. All of G/L/E/lease this slice took are
        already released by the time that happens.
        """

        public_type: type[RuntimeBusyError] = RuntimeBusyError
        message = ""
        try:
            return self._acquire_runtime_slice_resources(manifest, media_type, deadline, drain)
        except RuntimeWaitTimeoutError as exc:
            public_type, message = RuntimeWaitTimeoutError, str(exc)
        except RuntimeBusyError as exc:
            message = str(exc)
        raise public_type(message)

    def _acquire_runtime_slice_resources(
        self,
        manifest: ModelManifest,
        media_type: str,
        deadline: float | None,
        drain: _StaleDrainOperation,
    ) -> RuntimeHandle:
        owner_thread = current_thread()

        if not self._admission.acquire(deadline):
            raise RuntimeWaitTimeoutError(
                f"No process-wide runtime admission slot available for "
                f"{manifest.id!r} within the given timeout."
            )

        try:
            # `_acquire_entry_with_execution_lock()` releases its OWN stale
            # lease internally whenever ITS OWN attempt(s) fail (see that
            # method and `_acquire_entry_and_execution_lock()`) -- so if
            # this call raises, there is nothing further to release here;
            # if it returns, `entry` carries a live lease and a held E that
            # nothing else will release except what follows below.
            entry = self._acquire_entry_with_execution_lock(
                manifest, media_type, deadline, drain
            )
            try:
                return RuntimeHandle(
                    manifest=manifest,
                    runtime=entry.runtime,
                    cache=self.runtime_cache,
                    canonical_id=manifest.id,
                    entry=entry,
                    admission=self._admission,
                    owner_thread=owner_thread,
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
                self.runtime_cache.release_lease(manifest.id, entry)
                raise
        except BaseException:
            # This unwind always runs on the same thread that just called
            # `self._admission.acquire()` above (no handle has been handed
            # to any other thread yet), so `owner_thread` is trivially
            # this call's own -- unlike `RuntimeHandle.release()`, which
            # may run on a different thread than the one that acquired.
            self._admission.release(owner_thread)
            raise

    def _acquire_entry_with_execution_lock(
        self,
        manifest: ModelManifest,
        media_type: str,
        deadline: float | None,
        drain: _StaleDrainOperation,
    ) -> RuntimeEntry:
        """L -> E for this manifest, with one internal retry for one
        specific, provably-safe-to-retry condition -- plus, only once that
        condition has actually happened within this logical call, a bounded
        wait for any *other* stale lease-holder still draining the same entry.

        Called with G already held by this slice; this method never touches
        G itself.

        Contract (see `acquire_runtime()`'s own docstring for the full
        rationale), decided entirely from `drain` -- state owned by this one
        public call, never by the thread or the instance:

        - `_RuntimeExecutionRevalidationFailed` with `drain` not yet armed
          (this caller won E onto an entry another lease-holder's
          failure-unwind invalidated during the wait): arms `drain` with
          that exact entry, then `_acquire_entry_after_revalidation_failure()`
          takes over.
        - `_RuntimeExecutionRevalidationFailed` with `drain` already armed
          by an earlier slice of this same call: this call's one retry is
          already spent -- plain, public `RuntimeBusyError`.
        - `_RuntimeInvalidEntryDrainingError` for the *exact* entry `drain`
          is armed with (an earlier slice of this call timed out waiting for
          it): resume that drain wait.
        - `_RuntimeInvalidEntryDrainingError` otherwise (a caller with no
          revalidation failure of its own in this call, or a different
          entry/generation): plain, public `RuntimeBusyError` immediately --
          keeps `test_invalid_entry_still_leased_by_another_caller_denies_new_acquires`
          true unchanged.

        Every public exception is raised after leaving the private one's
        `except` scope, so no private signal (whose `.entry` pins a
        `RuntimeEntry`) is ever attached as `__context__`; the armed entry is
        read from `drain` inline, never bound to a local of this frame.
        """

        resume = False
        message = ""
        try:
            return self._acquire_entry_and_execution_lock(manifest, media_type, deadline)
        except _RuntimeInvalidEntryDrainingError as exc:
            if drain.entry is not None and exc.entry is drain.entry:
                resume = True
            else:
                message = str(exc)
        except _RuntimeExecutionRevalidationFailed as exc:
            if drain.entry is None:
                drain.entry = exc.entry
                resume = True
            else:
                message = str(exc)
        if not resume:
            raise RuntimeBusyError(message)
        return self._acquire_entry_after_revalidation_failure(
            manifest, media_type, deadline, drain
        )

    def _acquire_entry_after_revalidation_failure(
        self,
        manifest: ModelManifest,
        media_type: str,
        deadline: float | None,
        drain: _StaleDrainOperation,
    ) -> RuntimeEntry:
        """Retry L -> E after this call's own revalidation failure on `drain.entry`.

        Multi-waiter INVALID-entry drain (approved contract, PR #420): the
        single-caller "exactly one retry" rule this method used to
        implement alone is insufficient once `admission_capacity >= 2` lets
        more than one caller pin the same entry before it is invalidated --
        this caller's own retry can otherwise lose a race against another
        stale lease-holder's still-outstanding pin on the exact same entry,
        even though that pin is provably draining and about to release (see
        `_RuntimeInvalidEntryDrainingError`'s own docstring for the proof).

        Loop body, each iteration:

        - `_RuntimeExecutionRevalidationFailed` (this caller wins E again,
          onto *any* entry, and it is invalid): not the bounded drain this
          method exists for -- plain, public `RuntimeBusyError` immediately.
          The revalidation-failure retry budget therefore stays exactly one
          attempt beyond the first per logical call (see
          `test_invalid_handoff_retry_is_bounded_to_one_attempt`).
        - `_RuntimeInvalidEntryDrainingError` for a *different* entry than
          `drain.entry` (a different object/generation): plain, public
          `RuntimeBusyError` immediately (scope guard; generation isolation).
        - `_RuntimeInvalidEntryDrainingError` for `drain.entry` itself: the
          exact condition this method exists to wait out.
          `wait_for_stale_entry_drain()` blocks (no polling -- see that
          method) until that entry's last stale lease releases or it is
          replaced, bounded by this slice's `deadline`. On `False` (deadline
          elapsed, still blocking) this raises `RuntimeWaitTimeoutError`;
          `drain` stays armed, so a later slice of the same checkpointed
          call can resume the wait -- the state never leaves the call. On
          `True`, the loop retries immediately.
        """

        while True:
            drain_hit = False
            message = ""
            try:
                return self._acquire_entry_and_execution_lock(manifest, media_type, deadline)
            except _RuntimeExecutionRevalidationFailed as exc:
                message = str(exc)
            except _RuntimeInvalidEntryDrainingError as exc:
                if exc.entry is drain.entry:
                    drain_hit = True
                else:
                    message = str(exc)
            if not drain_hit:
                raise RuntimeBusyError(message)
            if not self.runtime_cache.wait_for_stale_entry_drain(
                manifest.id, drain.armed_entry(), deadline
            ):
                raise RuntimeWaitTimeoutError(
                    f"Timed out waiting for stale leases on {manifest.id!r} "
                    "to drain after a revalidation handoff."
                )
            # Converged: the armed entry is no longer current, or no longer
            # blocks reacquisition -- retry immediately.

    def _acquire_entry_and_execution_lock(
        self, manifest: ModelManifest, media_type: str, deadline: float | None
    ) -> RuntimeEntry:
        """One L -> E attempt: pin/load an entry, then win and revalidate E.

        Releases the lease this one attempt itself took if
        `_acquire_execution_lock()` raises for any reason -- including
        `_RuntimeExecutionRevalidationFailed`, which is exactly what makes a
        follow-up attempt (see `_acquire_entry_with_execution_lock()`) safe:
        by the time that follow-up runs, this attempt's own stale lease on
        the now-`INVALID` entry is already gone, so the entry is genuinely
        free to be retired and replaced rather than looking, to a fresh
        `acquire_or_reserve()` call, still pinned by this same caller.
        """

        entry = self._acquire_or_load_entry(manifest, media_type, deadline)
        try:
            self._acquire_execution_lock(manifest.id, entry, deadline)
        except BaseException:
            # We hold a lease (a pin) but will never use it -- release it
            # rather than leaking a lease with no corresponding handle,
            # which would otherwise make this entry permanently
            # un-evictable/un-unloadable ("pinned runtime" is absolute).
            self.runtime_cache.release_lease(manifest.id, entry)
            raise
        return entry

    def _acquire_or_load_entry(
        self, manifest: ModelManifest, media_type: str, deadline: float | None
    ) -> RuntimeEntry:
        load_lock = self.runtime_cache.lock_for(manifest.id)
        if not _acquire_with_deadline(load_lock, deadline):
            raise RuntimeWaitTimeoutError(
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
        self,
        canonical_id: str,
        entry: RuntimeEntry,
        deadline: float | None,
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
        `_RuntimeExecutionRevalidationFailed` (a private `RuntimeBusyError`
        subtype -- see its own docstring) -- this method only ever cleans up
        what it itself acquired (E); the caller's own lease is the caller's
        own cleanup. `acquire_runtime()` catches this exact private type,
        internally, to retry this one condition exactly once (see that
        method's own docstring); every other busy/capacity/pinned condition
        in this module still raises the plain, public `RuntimeBusyError`
        this private type only narrowly, deliberately shadows here.

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

        PR4a v1 final-convergence pass (issue #414): a same-thread
        recursive call that could reach this exact entry a second time --
        the scenario a same-thread E-level ownership check used to guard
        against here -- is now categorically impossible: any nested
        `acquire_runtime()` call from a thread that already holds an open
        `RuntimeHandle` is rejected at the G level, unconditionally, before
        `ModelService.acquire_runtime()` ever reaches
        `_acquire_or_load_entry()` or this method (see
        `RuntimeAdmissionController.acquire()`'s own docstring). This
        method therefore no longer needs its own owner-thread bookkeeping
        on `entry`; `entry.execution_lock` is used exactly like an ordinary
        non-reentrant `threading.Lock`, contended only across genuinely
        different threads.
        """

        if not _acquire_with_deadline(entry.execution_lock, deadline):
            raise RuntimeWaitTimeoutError(
                f"Timed out waiting for exclusive execution access to {canonical_id!r}."
            )
        try:
            if not self.runtime_cache.is_current_and_ready(canonical_id, entry):
                raise _RuntimeExecutionRevalidationFailed(
                    f"{canonical_id!r} became invalid while waiting for exclusive "
                    "execution access; retry.",
                    entry=entry,
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


__all__ = [
    "ModelService",
    "RuntimeAdmissionController",
    "RuntimeHandle",
    "get_default_admission_controller",
]
