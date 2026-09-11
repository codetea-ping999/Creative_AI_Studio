"""Runtime lease/admission primitives shared by the model cache and service.

Part of PR4a (issue #414): a runtime-safety core that makes "who may load,
use, replace, and unload a cached runtime" an explicit, race-free state
machine instead of a bare dict of runtime objects. This module defines the
*vocabulary* only (state, entry, the busy exception); the synchronization
choreography that uses it lives in ``core/models/cache.py`` (metadata lock,
retirement/reservation transitions) and ``core/models/service.py``
(process-wide admission, the ``acquire_runtime()`` context-manager API).

Every runtime ownership domain (cache slot, load lock, lease count,
execution lock, unload target, eviction/replacement victim) is keyed by
exactly one identity: ``manifest.id`` (the canonical id) -- never a public
id or alias. Callers resolve a public id/alias to a canonical id via
``ModelResolver`` *before* touching anything in this module.
"""

from __future__ import annotations

from enum import Enum
from threading import Lock
from typing import Any


class RuntimeState(Enum):
    """Lifecycle state of one cached runtime slot, keyed by canonical id.

    Transitions (v1, all single-step -- no state re-enters a state it left):

    - (absent) -> LOADING: a reservation was made; ``loader.load()`` has not
      returned yet. No runtime object exists in the entry.
    - LOADING -> READY: ``loader.load()`` succeeded; the entry is published
      and the caller that reserved it receives the first lease atomically.
    - LOADING -> (absent): ``loader.load()`` raised; the reservation is
      torn down with no runtime ever having existed for it.
    - READY -> INVALID: an exception escaped a caller's use of the runtime
      (see ``RuntimeHandle.release()``); a conservative safety policy, not a
      classification of *which* exceptions are actually unsafe to reuse
      after (see that method's own docstring).
    - READY|INVALID -> RETIRING: chosen as an eviction/replacement victim,
      or targeted by ``unload_model()``/``unload_all()``, while unleased.
    - RETIRING -> (absent): cleanup finished; the slot is free again.
    """

    LOADING = "loading"
    READY = "ready"
    INVALID = "invalid"
    RETIRING = "retiring"


class RuntimeBusyError(RuntimeError):
    """Raised instead of waiting indefinitely for a runtime operation.

    PR4a's own hard rule: no unbounded wait for a condition only some other,
    unrelated caller can resolve (a pinned eviction victim, an in-flight
    retirement/load for the same canonical id, a leased unload/replace
    target). Every one of those conditions gets an immediate, deterministic
    ``RuntimeBusyError`` instead of blocking -- the caller decides whether
    and when to retry. This is distinct from ordinary lock contention (the
    process-wide admission slot, the per-canonical-id load lock, a runtime's
    own execution lock), which may legitimately block a bounded
    ``wait_timeout`` -- see ``ModelService.acquire_runtime()``.
    """


class RuntimeEntry:
    """Cache-slot metadata for one canonical runtime id.

    Deliberately *not* a dataclass: ``execution_lock`` is a real
    ``threading.Lock``, which has no meaningful ``==``/``repr`` -- a
    hand-written ``__repr__`` below is more useful for logs/debugging than a
    dataclass-generated one would be, and there is never a reason to compare
    two entries for equality (object identity is what every caller in this
    module actually needs, since two RuntimeEntry objects are never
    interchangeable even if their fields happen to match).

    ``generation`` is an explicit, monotonically increasing counter bumped
    every time a fresh entry replaces a previous one under the same
    canonical id. Object identity (``cache._entries.get(id) is entry``) is
    already sufficient on its own to detect a stale handle from an earlier
    incarnation -- a brand-new ``RuntimeEntry`` object is always constructed
    per load, never reused -- but the generation counter is kept anyway as
    an explicit, loggable value: it is what a caller (or a future refactor)
    can compare without needing to hold a live reference to the *old*
    object, and it is what issue #414's own "generation/token" requirement
    names directly.
    """

    __slots__ = (
        "canonical_id",
        "media_bucket",
        "state",
        "runtime",
        "lease_count",
        "generation",
        "execution_lock",
        "execution_lock_owner",
        "admission_class",
    )

    def __init__(
        self,
        canonical_id: str,
        media_bucket: str,
        state: RuntimeState,
        *,
        generation: int,
        runtime: Any | None = None,
        admission_class: str = "heavy",
    ) -> None:
        self.canonical_id = canonical_id
        self.media_bucket = media_bucket
        self.state = state
        self.runtime = runtime
        self.lease_count = 0
        self.generation = generation
        # Per-entry exclusive execution lock ("E"): held by exactly one
        # RuntimeHandle at a time, for the duration that handle's caller is
        # actually using the runtime. This is what makes "same canonical
        # entry exclusive" (issue #414) true even for two callers that both
        # hold a lease on it (a cache hit does not imply execution access).
        self.execution_lock = Lock()
        # Codex re-review, found on this round's own fix commits: the
        # thread id currently holding `execution_lock`, or `None` when it
        # is free. `threading.Lock` has no owner-thread concept of its own
        # (unlike `threading.RLock`), so with `admission_capacity > 1` a
        # thread already holding this exact entry's E could pin it again
        # (the lease-hit path doesn't know about E at all) and then block
        # forever trying to acquire its own already-held, non-reentrant
        # lock. `ModelService._acquire_execution_lock()` checks this
        # *before* attempting to acquire E, to fail fast instead of
        # deadlocking; not synchronized by a separate lock of its own --
        # only the thread that already owns `execution_lock` could ever
        # legitimately match here, so a stale read only ever matters to
        # the one thread it is actually about.
        self.execution_lock_owner: int | None = None
        # PR4a always classifies every runtime as "heavy" -- the one
        # process-wide admission slot (see ModelService._admission) applies
        # uniformly. This field exists so a future, explicitly audited
        # classifier (PR4b+) has somewhere to record its answer without
        # another schema change; it must default to the protected
        # classification ("heavy"), never a lighter one, so an unclassified
        # or newly added runtime is safe by default ("unknown => protected").
        self.admission_class = admission_class

    def is_pinned(self) -> bool:
        """Whether any caller currently holds a lease on this entry.

        A pinned entry must never be evicted, unloaded, replaced, or handed
        to cleanup -- see every retirement/eviction path in
        ``ModelRuntimeCache`` -- regardless of whether a *local variable*
        elsewhere still references the runtime object; only this counter is
        authoritative.
        """

        return self.lease_count > 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"RuntimeEntry(canonical_id={self.canonical_id!r}, "
            f"state={self.state.value}, lease_count={self.lease_count}, "
            f"generation={self.generation}, media_bucket={self.media_bucket!r})"
        )


__all__ = ["RuntimeBusyError", "RuntimeEntry", "RuntimeState"]
