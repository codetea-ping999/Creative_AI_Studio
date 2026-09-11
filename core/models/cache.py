"""Runtime cache for loaded model instances.

PR4a (issue #414) unifies what used to be a bare ``{canonical_id: runtime}``
dict into ``{canonical_id: RuntimeEntry}`` (see ``core/models/runtime_lease.py``),
so every caller -- the legacy ``get()``/``put()``/``unload()``/``unload_all()``
pair *and* the new lease-aware ``acquire_or_reserve()``/``publish_ready_and_pin()``
/``release_lease()`` pair -- observes and mutates the exact same state. This is
what lets ``unload_model()`` correctly refuse to touch a runtime the new
``ModelService.acquire_runtime()`` API currently has leased, even though the
call arrived through the old, non-leasing entry point.

Lock/ordering model (see also ``ModelService.acquire_runtime()``'s own
docstring, which owns the *global* ordering across G/L/E/M):

- ``M`` (``self._metadata_lock``): guards ``self._entries`` (state, lease
  counts, LRU order, generation) and nothing else. Every method in this
  class that touches ``self._entries`` does so inside a short ``with
  self._metadata_lock:`` block and never calls anything that can block --
  no ``loader.load()``, no cleanup hook, no waiting on another lock -- while
  holding it. A multi-step operation (evict-then-load-then-publish) instead
  acquires/releases M several times, doing the slow work in between with M
  released (see ``acquire_or_reserve()``/``_finish_retirement()`` below).
- ``L`` (per-canonical-id, ``lock_for()``): unchanged from before PR4a --
  serializes "resolve this exact id's cache miss" so two concurrent misses
  for the same id never both load and ``put()`` (the second ``put()`` would
  evict, and run cleanup on, the runtime the first caller already received).
  PR4a's new ``acquire_or_reserve()`` is designed to be called while the
  caller (``ModelService.acquire_runtime()``) already holds this same lock.
- ``E`` (per-entry, ``RuntimeEntry.execution_lock``): exclusive use of one
  already-loaded runtime. Owned and acquired by the caller (see
  ``ModelService.acquire_runtime()``), not by this class -- this class only
  ever *creates* the lock as part of a fresh ``RuntimeEntry``.

Never assume a locking primitive here provides thread-safety for the
runtime object's own internals (LoRA mutation, pipeline inference,
AudioCraft generation state, ...) -- E only ever guarantees *this cache*
never hands the same entry to two callers to use at once; the codebase's
own PR4a test suite (``tests/test_runtime_safety_core.py``) proves that
guarantee with a real fake runtime, not an assumption.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
import logging
import os
from threading import Lock
from typing import Any, Callable

from .runtime_lease import RuntimeBusyError, RuntimeEntry, RuntimeState

logger = logging.getLogger(__name__)

OnEvictCallback = Callable[[str, Any], None]
"""`(canonical_id, runtime_obj) -> None`, called when a runtime leaves the cache.

Must never call back into `ModelService` or `ModelRuntimeCache` (directly,
or indirectly through something that eventually reaches
`acquire_runtime()`/`unload_model()`/`unload_all()`). PR4a's admission
semaphore (`ModelService._admission`, "G") is held by the calling thread for
the whole duration of the `acquire_runtime()` call an eviction can happen
inside of -- a callback that tried to acquire G itself, on that same
thread, would deadlock against itself, since only that thread could ever
release the slot it is still waiting to acquire. The production callback
(`core.models.cleanup.release_runtime`) only mutates the runtime object and
touches accelerator memory, never this module or `ModelService`.
"""

# Bucket key used for any entry that has no per-media budget configured for
# it -- either because the caller never passed `media_type` to `put()`, or
# because `media_limits` has no entry for that media type. Every such entry
# shares the single `max_entries` budget, which is exactly the pre-#182
# single-cache behavior (see "Missing per-media settings retain
# backward-compatible behavior" in issue #182's acceptance criteria).
_DEFAULT_BUCKET = "__default__"

# The media families the model system currently loads runtimes for (see
# `core.schemas.generation.MediaType`). Kept local to this module rather than
# importing `MediaType` so this cache stays a plain data structure that does
# not need to know about the generation-request schema.
DEFAULT_MEDIA_TYPES: tuple[str, ...] = ("image", "video", "audio", "text")

# Entry states a normal eviction/replacement pass may pick a victim from --
# both are "idle" in the sense that nothing is actively loading or already
# tearing them down. An INVALID-but-unleased entry is just as evictable as a
# READY one (arguably more so -- it is already known to need reloading).
_EVICTABLE_STATES = (RuntimeState.READY, RuntimeState.INVALID)

# Entry states `unload_model()`/`unload_all()` must never silently overwrite
# or skip past -- both mean "something else already owns this slot's next
# transition"; the caller gets `RuntimeBusyError`, never a guess.
_BUSY_FOR_UNLOAD_STATES = (RuntimeState.LOADING, RuntimeState.RETIRING)


class ModelRuntimeCache:
    """Small in-memory cache for runtime reuse, with lease/pin safety.

    Entries are grouped into "buckets": one per media type named in
    `media_limits`, plus a shared default bucket for everything else. Each
    bucket is evicted independently and deterministically -- oldest
    (least-recently-used, unleased) entry in that bucket first -- so, for
    example, configuring an image budget and a text budget lets one image
    runtime and one text runtime stay resident at the same time instead of
    the text load evicting the image runtime (or vice versa).
    """

    def __init__(
        self,
        max_entries: int = 1,
        *,
        media_limits: Mapping[str, int] | None = None,
        on_evict: OnEvictCallback | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        resolved_media_limits: dict[str, int] = {}
        for media_type, limit in (media_limits or {}).items():
            if limit < 1:
                raise ValueError(
                    f"media_limits[{media_type!r}] must be at least 1, got {limit!r}."
                )
            resolved_media_limits[media_type] = limit

        self.max_entries = max_entries
        self.media_limits = resolved_media_limits
        self._on_evict = on_evict
        # canonical_id -> RuntimeEntry, in LRU order (oldest first) exactly
        # like the pre-PR4a `OrderedDict[str, Any]` it replaces: a hit
        # (legacy `get()`, or a new-API pin) moves its id to the end.
        self._entries: OrderedDict[str, RuntimeEntry] = OrderedDict()
        self._load_locks: dict[str, Lock] = {}
        self._load_locks_guard = Lock()
        self._metadata_lock = Lock()
        # Highest generation ever assigned to a canonical id, kept even
        # after that id's entry is fully removed (unload/retirement) --
        # deliberately NOT derived from "the entry being replaced" alone,
        # since an id can be unloaded down to no entry at all and later
        # reloaded fresh, with nothing left in `self._entries` to read a
        # prior generation off of. Guarantees `RuntimeEntry.generation` is
        # monotonic per canonical id for the lifetime of this cache, not
        # just within one evict-then-replace transition.
        self._last_generation: dict[str, int] = {}

    # ------------------------------------------------------------- L (load lock)

    def lock_for(self, model_id: str) -> Lock:
        """A lock serializing "load and put" for one `model_id`.

        Without this, two callers racing to resolve the same uncached
        `model_id` (e.g. two `JOB_LANES` workers picking up jobs for the same
        model at once) can both miss the cache, both load their own runtime,
        and both call `put()` -- the second `put()` then evicts (and runs
        `on_evict` cleanup on) the runtime the first caller already received
        and may still be using mid-generation. Callers should re-check
        `get(model_id)` after acquiring this lock: another caller may have
        already loaded and cached the model while this one was waiting.
        Locks are per-`model_id`, so unrelated models still load
        concurrently -- only a genuine same-model race is serialized.

        PR4a's ``ModelService.acquire_runtime()`` reuses this exact lock as
        "L" in its own G -> L -> E ordering -- a second, separate per-id load
        lock would only create a new way for the two APIs to disagree about
        whether a given id's load is in progress.
        """

        with self._load_locks_guard:
            lock = self._load_locks.get(model_id)
            if lock is None:
                lock = Lock()
                self._load_locks[model_id] = lock
            return lock

    # --------------------------------------------------- legacy, non-leasing API
    #
    # `get()`/`put()`/`unload()`/`unload_all()`/`has()`/`loaded_ids()` keep
    # their exact pre-PR4a external contracts (a bare runtime object or
    # `None`, no lease/pin concept) for `ModelService.resolve_runtime()` and
    # every other caller written before PR4a. They are internally backed by
    # the same `RuntimeEntry` table the new lease-aware API uses, so a
    # runtime loaded through either path is visible -- and pin-protected --
    # to the other. Not concurrency-safe in the way `acquire_runtime()` is:
    # a runtime returned by `get()` carries no lease, so nothing stops a
    # concurrent `unload_model()`/eviction from retiring it out from under a
    # caller still using it. Transitional only -- PR4b migrates every
    # generator off this path.

    def has(self, model_id: str) -> bool:
        with self._metadata_lock:
            entry = self._entries.get(model_id)
            return entry is not None and entry.state is RuntimeState.READY

    def get(self, model_id: str) -> Any | None:
        with self._metadata_lock:
            entry = self._entries.get(model_id)
            if entry is None or entry.state is not RuntimeState.READY:
                return None
            self._entries.move_to_end(model_id)
            return entry.runtime

    def put(
        self,
        model_id: str,
        runtime_obj: Any,
        *,
        media_type: str | None = None,
    ) -> None:
        """Insert `runtime_obj`, then evict this entry's bucket down to budget.

        `media_type` selects which per-media budget (if any) governs this
        entry. Omitting it -- or passing a media type absent from
        `media_limits` -- puts the entry in the shared default bucket bounded
        by `max_entries`, unchanged from pre-#182 behavior.

        Raises `RuntimeBusyError` if `model_id` currently names a leased,
        loading, or retiring entry (created via `acquire_or_reserve()`) --
        a legacy caller replacing a runtime the new API is actively using
        would otherwise silently corrupt that lease.

        PR4a Codex-review Finding 5 (round 1): the *destination* bucket's
        budget is enforced even on a same-object reinsertion (a caller
        moving an already-cached runtime into a different `media_type`'s
        bucket) -- previously this fast path only updated `media_bucket`/LRU
        position and skipped eviction entirely, so moving a runtime into an
        already-full bucket could push it silently over budget. The moved
        object itself is always excluded from its own eviction candidacy
        (`exclude_id=model_id` below), exactly like a fresh insert.
        """

        bucket = media_type if media_type in self.media_limits else _DEFAULT_BUCKET
        same_object_reinsertion = False
        replaced_victim: tuple[str, Any] | None = None

        with self._metadata_lock:
            existing = self._entries.get(model_id)
            if existing is not None:
                # Codex re-review, found on the round-1 fix commit (P2):
                # the busy check must gate the same-object path too, not
                # only the different-object (replace) path -- checked
                # first here, unconditionally, rather than nested inside
                # an `elif` a caller could route around by simply passing
                # back the runtime object it already holds a reference to.
                # Without this, a caller retaining a reference to a
                # RETIRING entry's runtime (unload() already in flight,
                # cleanup mid-run outside M) could still move it to
                # another bucket and trigger eviction there, even though
                # `_finish_retirement()` is concurrently tearing it down
                # and about to remove it regardless.
                if existing.is_pinned() or existing.state in _BUSY_FOR_UNLOAD_STATES:
                    raise RuntimeBusyError(
                        f"Cannot replace {model_id!r}: state={existing.state.value}, "
                        f"lease_count={existing.lease_count}."
                    )
                if existing.runtime is runtime_obj:
                    # Codex re-review, found on this round's own fix
                    # commits (P2): an unpinned INVALID entry passes the
                    # busy check above (INVALID is not in
                    # `_BUSY_FOR_UNLOAD_STATES`), so without this,
                    # reinserting its own runtime object here would take
                    # this same-object path -- moving/refreshing it -- but
                    # leave `.state` at INVALID. `get()`/`has()` would keep
                    # treating it as absent (they require READY), so this
                    # "reinsertion" fixes nothing while still being able to
                    # evict a healthy entry in the destination bucket for
                    # no benefit. There is no legitimate reason to pass the
                    # same, already-invalidated object back in -- a legacy
                    # caller "fixing" an INVALID entry loads a genuinely
                    # new runtime and passes *that* instead, which the
                    # different-object branch below already handles.
                    if existing.state is RuntimeState.INVALID:
                        raise RuntimeBusyError(
                            f"Cannot reinsert {model_id!r}: the existing "
                            "entry is invalid; load a fresh runtime instead "
                            "of reusing the same object."
                        )
                    # A caller reinserting the *same* runtime instance under
                    # its own model_id (e.g. to refresh LRU position or
                    # change its media bucket) is not a replacement --
                    # evicting it here would strip pipeline/model/processor
                    # from the very object being kept.
                    existing.media_bucket = bucket
                    self._entries.move_to_end(model_id)
                    same_object_reinsertion = True
                else:
                    # A replaced entry must run the same cleanup as
                    # unload()/unload_all() -- on_evict is what actually
                    # returns GPU/MPS memory (torch.mps.empty_cache() etc.
                    # in bootstrap/factories.py's wiring), which plain
                    # Python GC does not reliably do on its own (#373).
                    del self._entries[model_id]
                    if existing.runtime is not None:
                        replaced_victim = (model_id, existing.runtime)

            if not same_object_reinsertion:
                self._entries[model_id] = RuntimeEntry(
                    model_id, bucket, RuntimeState.READY,
                    generation=self._next_generation_locked(model_id),
                    runtime=runtime_obj,
                )
            # Enforced for BOTH a fresh insert and a same-object bucket move
            # (Finding 5) -- the destination bucket's budget must never be
            # silently exceeded either way.
            overflow_victims = self._evict_bucket_overflow_locked(
                bucket, exclude_id=model_id
            )

        # Codex re-review, found on this round's own fix commits (P2):
        # `replaced_victim`'s cleanup and every `overflow_victims` entry's
        # finalization must all be attempted, even if an earlier one raises
        # `BaseException` -- otherwise an interrupted `replaced_victim`
        # cleanup (which uses `_run_cleanup()` directly, not
        # `_finish_retirement()`, since it was already removed from
        # `_entries` atomically above) skips the overflow-victim loop
        # entirely, stranding those already-`RETIRING` entries forever,
        # exactly the same failure mode `unload_all()` already guards
        # against. The first exception encountered is re-raised once every
        # target has been attempted.
        first_exception: BaseException | None = None
        if replaced_victim is not None:
            try:
                self._run_cleanup(*replaced_victim)
            except BaseException as exc:  # noqa: BLE001 - every target must still be attempted; see above
                first_exception = exc
        for victim_id, victim_entry in overflow_victims:
            try:
                self._finish_retirement(victim_id, victim_entry)
            except BaseException as exc:  # noqa: BLE001 - every target must still be attempted; see above
                if first_exception is None:
                    first_exception = exc
        if first_exception is not None:
            raise first_exception

    def unload(self, canonical_id: str) -> None:
        """Retire `canonical_id`'s entry if idle; safe no-op if not cached.

        Raises `RuntimeBusyError` if it is leased, loading, or already
        retiring -- never waits, never mutates the entry in that case.
        `canonical_id` must already be resolved (public id/alias -> manifest
        id) by the caller; see `ModelService.unload_model()`.
        """

        with self._metadata_lock:
            entry = self._entries.get(canonical_id)
            if entry is None:
                return
            if entry.is_pinned() or entry.state in _BUSY_FOR_UNLOAD_STATES:
                raise RuntimeBusyError(
                    f"Cannot unload {canonical_id!r}: state={entry.state.value}, "
                    f"lease_count={entry.lease_count}."
                )
            entry.state = RuntimeState.RETIRING
        self._finish_retirement(canonical_id, entry)

    def unload_all(self) -> None:
        """Atomically retire every entry, or none at all.

        Preflight (under `M`, one pass): if *any* entry is leased, loading,
        or retiring, raises `RuntimeBusyError` and modifies nothing --
        zero entries change state, zero cleanup calls happen. Only once
        every entry is confirmed idle does this mark them all `RETIRING`
        (still under the same `M` section, so no interleaved
        `acquire_or_reserve()`/`unload()` call can observe a partially
        transitioned cache). Cleanup itself runs outside `M`, one entry at a
        time, using the same best-effort `on_evict` contract `unload()`
        already has -- "atomic" describes the preflight decision, not a
        rollback guarantee over cleanup, which was never transactional.

        PR4a Codex-review Finding 6 (round 1): once a target has been marked
        `RETIRING` here, it is *always* finalized (cleanup attempted, then
        removed from `self._entries` -- see `_finish_retirement()`'s own
        `try`/`finally`), even if an earlier target's cleanup raised a
        `BaseException` (a custom cancellation signal, `KeyboardInterrupt`,
        ...). The chosen contract is "finalize every target before
        re-raising", not "revert untouched targets to their pre-RETIRING
        state": reverting would mean guessing that an entry whose cleanup
        never got a chance to run is still safe to hand out as `READY`
        again, which this method has no way to know; finalizing it instead
        (removing it, so the next `acquire_or_reserve()` simply reloads it
        fresh) is always safe. The *first* exception encountered across all
        targets is re-raised once every target has been finalized, so a
        `KeyboardInterrupt`/`SystemExit` still ultimately propagates -- just
        only after this method has guaranteed no entry is left stuck at
        `RETIRING` forever (which would otherwise permanently block every
        future `acquire_or_reserve()`/`unload()`/`unload_all()` call for
        that id -- see `_BUSY_FOR_UNLOAD_STATES`).
        """

        with self._metadata_lock:
            busy = [
                (canonical_id, entry)
                for canonical_id, entry in self._entries.items()
                if entry.is_pinned() or entry.state in _BUSY_FOR_UNLOAD_STATES
            ]
            if busy:
                busy_ids = ", ".join(canonical_id for canonical_id, _ in busy)
                raise RuntimeBusyError(
                    f"Cannot unload all: {len(busy)} runtime(s) are currently "
                    f"leased or mid-transition ({busy_ids})."
                )
            targets = list(self._entries.items())
            for _, entry in targets:
                entry.state = RuntimeState.RETIRING

        first_exception: BaseException | None = None
        for canonical_id, entry in targets:
            try:
                self._finish_retirement(canonical_id, entry)
            except BaseException as exc:  # noqa: BLE001 - every target must still be finalized; see docstring
                if first_exception is None:
                    first_exception = exc
        if first_exception is not None:
            raise first_exception

    def loaded_ids(self) -> list[str]:
        with self._metadata_lock:
            return [
                canonical_id
                for canonical_id, entry in self._entries.items()
                if entry.state is RuntimeState.READY
            ]

    # ------------------------------------------------------ lease-aware API
    #
    # Used by `ModelService.acquire_runtime()`. Every method below either
    # completes without blocking (raising `RuntimeBusyError` instead of
    # waiting when it cannot proceed), or explicitly documents the short `M`
    # sections it uses and the fact that it releases `M` before doing any
    # slow work (cleanup) in between them.

    def acquire_or_reserve(self, canonical_id: str, media_type: str | None) -> RuntimeEntry:
        """Pin an existing READY entry, or reserve a fresh LOADING slot.

        Must be called while the caller already holds `lock_for(canonical_id)`
        ("L") -- this method never acquires or waits on L itself, and relies
        on the caller's L to guarantee no other thread is concurrently
        inside this same method for the same `canonical_id` (so a target
        entry can never legitimately be `LOADING` here; see below).

        Returns a `RuntimeEntry` whose `.state` is either:
        - `READY` -- an existing runtime, already pinned (lease_count
          incremented). The caller uses it directly; no load needed.
        - `LOADING` -- a fresh reservation with `.runtime is None`. The
          caller must run `loader.load()` and then call exactly one of
          `publish_ready_and_pin()` (success) or `abort_reservation()`
          (failure) with this exact entry object.

        Raises `RuntimeBusyError` -- never waits -- when:
        - the target entry is `RETIRING` (another unload/eviction is
          mid-flight for this exact id) or, defensively, `LOADING` (should
          be unreachable given the L precondition above, but treated
          identically rather than asserted, since a caller violating that
          precondition must never see undefined behavior),
        - the target entry is `INVALID` and still leased by another caller
          ("existing lease unwind" is waited *for*, never waited *through*
          here -- the caller may retry once those leases release),
        - no capacity is available in this id's media bucket and every
          existing entry that could be evicted to make room is itself
          leased (the "pinned-only eviction" case -- there is nothing this
          call could wait for that only time, not another caller's
          eventual `release()`, would resolve),
        - capacity that a victim's cleanup freed got claimed by a different,
          concurrent reservation before this call could re-claim it (see
          the "no cascading eviction" note below).

        PR4a Codex-review Finding 3 (round 1): reservation creation is
        atomic with its own capacity check. Two cases:

        - No victim needed (room already available, or no existing entry to
          replace): the fresh `LOADING` reservation is created in the *same*
          short `M` transaction that decided room was available -- nothing
          else can observe "room available" and also claim it first, since
          both checks happen under the same, uninterrupted `M` hold.
        - A victim is needed: cleanup runs outside `M` (it can be slow), so
          by the time this method re-acquires `M` afterward, a *different*
          concurrent caller may already have claimed the capacity the
          cleanup just freed (this call's own `M` hold ended the moment
          cleanup started). The follow-up `M` transaction below therefore
          does not blindly insert -- it removes the victim (via
          `_finish_retirement`, which is itself what frees the slot) and
          then re-runs the exact same capacity check used for the no-victim
          case. If that recheck finds room, the reservation is created in
          that same transaction. If it does not (someone else took the slot
          in the interim), this raises `RuntimeBusyError` rather than
          selecting and retiring a second victim -- deliberately not a
          cascading/looping eviction: that would let one caller's miss
          retire an unbounded number of other entries, whereas a caller that
          loses this narrow, genuinely rare race can simply retry.
        """

        bucket = media_type if media_type in self.media_limits else _DEFAULT_BUCKET
        victim_id: str | None = None
        victim_entry: RuntimeEntry | None = None

        # --- short M: hit-check, or victim selection + provisional retirement ---
        with self._metadata_lock:
            existing = self._entries.get(canonical_id)
            if existing is not None:
                if existing.state is RuntimeState.READY:
                    existing.lease_count += 1
                    self._entries.move_to_end(canonical_id)
                    return existing
                if existing.state in _BUSY_FOR_UNLOAD_STATES:
                    raise RuntimeBusyError(
                        f"{canonical_id!r} is currently {existing.state.value}; "
                        "try again shortly rather than waiting indefinitely."
                    )
                # INVALID.
                if existing.is_pinned():
                    raise RuntimeBusyError(
                        f"{canonical_id!r} is invalid and still in use by "
                        f"{existing.lease_count} active lease(s); retry once "
                        "those release."
                    )
                victim_id, victim_entry = canonical_id, existing
            else:
                victim_id, victim_entry = self._select_capacity_victim_locked(
                    bucket, budget_for=canonical_id
                )

            if victim_entry is None:
                # Finding 3, no-victim case: reserve atomically, in this
                # exact transaction -- nothing can race this, since nothing
                # else can observe "room available" without also holding M.
                reserved = RuntimeEntry(
                    canonical_id, bucket, RuntimeState.LOADING,
                    generation=self._next_generation_locked(canonical_id),
                )
                self._entries[canonical_id] = reserved
                return reserved

            # Left in `self._entries` (not yet removed) so a concurrent
            # caller sees it as RETIRING, never as silently absent --
            # required for the "retiring reacquire" contract (issue
            # #414's own required test 11): absent would look like a
            # normal cache miss and invite a second, overlapping load.
            victim_entry.state = RuntimeState.RETIRING

        # --- cleanup + removal outside M ---
        self._finish_retirement(victim_id, victim_entry)  # type: ignore[arg-type]

        # --- short M: recheck the target AND capacity, then establish the
        #     reservation (Finding 3, victim case: never assume the slot
        #     cleanup just freed is still free) ---
        with self._metadata_lock:
            # Codex re-review, found on this round's own fix commits: a
            # legacy `put(canonical_id, ...)` call does not participate in
            # `L` (the per-canonical-id load lock this method's own caller
            # holds), so nothing stops it from republishing a fresh READY
            # entry under this exact id while `_finish_retirement()` above
            # was running outside M. Only rechecking bucket *capacity*
            # (below) would miss that -- if capacity happened to look fine,
            # this would blindly overwrite the concurrently published
            # entry, leaking its runtime with zero cleanup while a legacy
            # caller might already be using it.
            if self._entries.get(canonical_id) is not None:
                raise RuntimeBusyError(
                    f"{canonical_id!r} was concurrently republished while a "
                    "previous occupant was being retired; retry."
                )
            retry_victim_id, retry_victim_entry = self._select_capacity_victim_locked(
                bucket, budget_for=canonical_id
            )
            if retry_victim_entry is not None:
                raise RuntimeBusyError(
                    f"Capacity for {canonical_id!r} in bucket {bucket!r} was "
                    "claimed by another reservation while a previous "
                    "occupant was being retired; retry."
                )
            reserved = RuntimeEntry(
                canonical_id, bucket, RuntimeState.LOADING,
                generation=self._next_generation_locked(canonical_id),
            )
            self._entries[canonical_id] = reserved
        return reserved

    def publish_ready_and_pin(
        self, canonical_id: str, reserved_entry: RuntimeEntry, runtime_obj: Any
    ) -> RuntimeEntry:
        """Publish a successful load into its reservation and pin it (short M).

        `reserved_entry` must be the exact object `acquire_or_reserve()`
        returned. Publishing and taking the first lease happen in the same
        `M` critical section -- an entry a caller can observe as `READY` is
        never, even momentarily, unleased and freshly loaded at once (which
        would make it a legal, but pointless, eviction victim the instant
        it appeared).
        """

        with self._metadata_lock:
            current = self._entries.get(canonical_id)
            if current is not reserved_entry:
                # Nothing else may legally touch a LOADING reservation for
                # this id (the hit/retiring/invalid branches above all raise
                # before reaching here) -- this is a defensive backstop, not
                # an expected path.
                raise RuntimeBusyError(
                    f"Lost the LOADING reservation for {canonical_id!r} before "
                    "the load completed; refusing to publish into a slot this "
                    "call no longer owns."
                )
            reserved_entry.runtime = runtime_obj
            reserved_entry.state = RuntimeState.READY
            reserved_entry.lease_count = 1
            self._entries.move_to_end(canonical_id)
        return reserved_entry

    def abort_reservation(self, canonical_id: str, reserved_entry: RuntimeEntry) -> None:
        """Undo a LOADING reservation after `loader.load()` raised.

        Never runs cleanup -- nothing was ever loaded into this
        reservation, so there is nothing for `on_evict` to release -- and
        frees the slot immediately so a retry is never blocked by a
        reservation nobody will ever finish. Partial resource allocation
        inside a failed `loader.load()` call is that loader's own ownership
        (see `LearnedVideoLoader`'s docstring for the one loader in this
        codebase that can leave state behind on failure); this method only
        ever forgets the cache-level reservation.
        """

        with self._metadata_lock:
            current = self._entries.get(canonical_id)
            if current is reserved_entry:
                del self._entries[canonical_id]

    def release_lease(self, canonical_id: str, entry: RuntimeEntry) -> None:
        """Decrement `entry`'s lease count (short M).

        Silently does nothing if `entry` is no longer the current entry for
        `canonical_id` (a stale handle from an earlier generation -- see
        `RuntimeEntry.generation`'s own docstring): whoever replaced it
        already owns this slot's lease count, and a stale release must never
        decrement a lease that was never its own to begin with.

        Does not touch `entry.state` -- see `mark_invalid()` for that, which
        `RuntimeHandle.release()` deliberately calls as its own, earlier,
        separate transaction (Codex-review Finding 2, round 1: the two used
        to be one call with a `mark_invalid=` flag, combined into the same
        transaction as the lease decrement; splitting them is what lets the
        INVALID transition become visible to an E-waiter *before* this
        decrement -- and before E itself is released -- rather than after).

        Codex re-review, found on this round's own fix commits: legacy
        `put()` deliberately leaves a bucket over its budget when every
        over-budget entry is pinned (pin supremacy is absolute -- see
        `_evict_bucket_overflow_locked()`'s own docstring). Without this,
        nothing ever revisits that bucket once the last such pin releases
        -- the overage could persist indefinitely, until some unrelated
        future `put()` into the *same* bucket happened to trigger its own
        overflow eviction. So the moment a lease drops to zero, this
        re-runs the same lenient overflow eviction for `entry`'s own
        bucket (outside `M`, once cleanup is actually needed) -- a no-op
        if the bucket is not (or no longer) over budget.
        """

        deferred_victims: list[tuple[str, RuntimeEntry]] = []
        with self._metadata_lock:
            current = self._entries.get(canonical_id)
            if current is not entry:
                return
            entry.lease_count = max(0, entry.lease_count - 1)
            if entry.lease_count == 0:
                deferred_victims = self._evict_bucket_overflow_locked(entry.media_bucket)
        # Codex re-review, found on this round's own fix commits: every
        # selected victim must be finalized regardless of an earlier one
        # raising `BaseException`, exactly like `put()` and `unload_all()`
        # already guarantee -- otherwise an interrupted first victim's
        # cleanup would strand every later one (already marked `RETIRING`
        # above) permanently.
        first_exception: BaseException | None = None
        for victim_id, victim_entry in deferred_victims:
            try:
                self._finish_retirement(victim_id, victim_entry)
            except BaseException as exc:  # noqa: BLE001 - every target must still be attempted; see above
                if first_exception is None:
                    first_exception = exc
        if first_exception is not None:
            raise first_exception

    def mark_invalid(self, canonical_id: str, entry: RuntimeEntry) -> None:
        """Mark `entry` INVALID if it is still current and `READY` (short M).

        Called by `RuntimeHandle.release()` *before* it releases E (see that
        method's own docstring) -- so any other caller already parked
        waiting on E for this exact entry can never win E and observe a
        still-`READY` runtime that is about to be invalidated out from under
        it. A no-op if `entry` is stale (already replaced/removed) or
        already non-`READY`.
        """

        with self._metadata_lock:
            current = self._entries.get(canonical_id)
            if current is not entry:
                return
            if entry.state is RuntimeState.READY:
                entry.state = RuntimeState.INVALID

    def is_current_and_ready(self, canonical_id: str, entry: RuntimeEntry) -> bool:
        """Whether `entry` is still `canonical_id`'s live, usable entry (short M).

        Used by `ModelService._acquire_execution_lock()` immediately after
        winning E: the entry this caller pinned earlier may have been marked
        `INVALID` (or, in principle, replaced) by another lease-holder's
        exception-unwind while this caller was still waiting on E -- see
        `RuntimeHandle.release()`'s own docstring (Codex-review Finding 2,
        round 1). A caller that gets `False` back must never read
        `entry.runtime`; it must release E itself and raise
        `RuntimeBusyError` instead of handing a handle to user code.
        """

        with self._metadata_lock:
            current = self._entries.get(canonical_id)
            return current is entry and entry.state is RuntimeState.READY

    def _next_generation_locked(self, canonical_id: str) -> int:
        """Next monotonic generation for `canonical_id`. Caller must hold M."""

        next_generation = self._last_generation.get(canonical_id, -1) + 1
        self._last_generation[canonical_id] = next_generation
        return next_generation

    # ------------------------------------------------------------- internals

    def _select_capacity_victim_locked(
        self, bucket: str, *, budget_for: str
    ) -> tuple[str | None, RuntimeEntry | None]:
        """Choose an eviction victim for a fresh `budget_for` id, or none.

        Must be called while holding `self._metadata_lock`. Returns
        `(None, None)` if the bucket already has room; raises
        `RuntimeBusyError` if capacity is required and every candidate in
        the bucket is leased or itself mid-transition -- the "pinned-only
        eviction" case, which must fail immediately rather than wait (see
        `acquire_or_reserve()`'s own docstring for why).

        A bucket at budget purely because another id sharing it is
        currently `RETIRING` (someone else's `unload()`/`unload_all()`
        cleanup is in flight, not yet removed from `self._entries` -- see
        that method's own two-phase removal) denies `budget_for` too, by
        the exact same "never wait" rule: nothing here can wait for that
        cleanup to finish without violating it. The caller may simply
        retry.
        """

        budget = self.media_limits.get(bucket, self.max_entries)
        bucket_ids = [
            entry_id for entry_id, entry in self._entries.items()
            if entry.media_bucket == bucket
        ]
        if len(bucket_ids) < budget:
            return None, None

        evictable = [
            entry_id for entry_id in bucket_ids
            if not self._entries[entry_id].is_pinned()
            and self._entries[entry_id].state in _EVICTABLE_STATES
        ]
        if not evictable:
            leased_count = sum(
                1 for entry_id in bucket_ids if self._entries[entry_id].is_pinned()
            )
            transitioning_count = len(bucket_ids) - leased_count
            raise RuntimeBusyError(
                f"No capacity available for {budget_for!r} in bucket "
                f"{bucket!r} (budget={budget}): {leased_count} leased, "
                f"{transitioning_count} mid-transition (loading/retiring)."
            )
        # `self._entries` preserves LRU order (oldest first); `evictable`
        # was built by iterating it, so its own first element is the
        # least-recently-used eligible victim.
        victim_id = evictable[0]
        return victim_id, self._entries[victim_id]

    def _evict_bucket_overflow_locked(
        self, bucket: str, *, exclude_id: str | None = None
    ) -> list[tuple[str, RuntimeEntry]]:
        """Legacy `put()`'s own overflow eviction (short M; caller finalizes).

        Unlike `_select_capacity_victim_locked()` (which raises when no
        victim is available), this is lenient: `put()` has always been a
        void method with no busy/failure contract of its own, so if every
        over-budget entry in the bucket happens to be leased by the new
        lease-aware API, this simply leaves the bucket over budget rather
        than violating "a pinned runtime is never evicted" (an absolute
        invariant, not one scoped to only the new API).

        `exclude_id` -- the id `put()` just inserted -- is never itself a
        candidate: without this, a bucket whose only *other* member is
        pinned would "evict" the very entry the caller just asked to
        insert (the sole remaining unleased, thus "evictable", id), making
        `put()` a silent no-op instead of leaving the bucket over budget.

        Each victim is marked `RETIRING` here (short M) and left in
        `self._entries` -- never popped immediately -- returned to the
        caller to finalize via `_finish_retirement()` outside M, exactly
        like every other eviction/retirement path in this class. A second
        finding from Codex's round-1 review of this same round's fixes: the
        previous behavior (`self._entries.pop(victim_id)` right here, under
        M, with cleanup running only afterward in `put()`'s own caller code)
        made the victim's id appear fully absent -- a normal cache miss --
        for the entire window between this method returning and that
        cleanup actually finishing. A concurrent `acquire_runtime()` for
        that exact id could reach that window, see nothing there, and start
        loading a fresh replacement while the old runtime was still being
        destructively cleaned up (`torch.cuda.empty_cache()`, a pipeline's
        own `.to("cpu")`, ...) -- the same "old and new coexist" hazard
        `RETIRING` exists everywhere else in this class to prevent.

        Marking a victim `RETIRING` rather than popping it means it is
        still counted in the bucket's raw membership on the *next* loop
        iteration (see `bucket_ids` below) -- a caller-visible fact this
        method's own budget check must account for, or it would keep
        finding "still over budget" and evict every remaining eligible
        entry, not just the actual excess. `retired_this_call` tracks how
        many victims *this call* has already committed to evicting, so the
        loop's own effective-occupancy check subtracts them back out
        (while a RETIRING entry some *other*, concurrent caller marked is
        still correctly counted against budget, since only this call's own
        already-decided victims are known to be leaving). Codex's
        round-1-of-round-1-of-round-1 re-review caught this: without the
        subtraction, evicting into a budget-2 bucket already at `{a, b}`
        retired both `a` and `b` instead of just the LRU one.
        """

        budget = self.media_limits.get(bucket, self.max_entries)
        victims: list[tuple[str, RuntimeEntry]] = []
        retired_this_call = 0
        while True:
            bucket_ids = [
                entry_id for entry_id, entry in self._entries.items()
                if entry.media_bucket == bucket
            ]
            if len(bucket_ids) - retired_this_call <= budget:
                break
            evictable = [
                entry_id for entry_id in bucket_ids
                if entry_id != exclude_id
                and not self._entries[entry_id].is_pinned()
                and self._entries[entry_id].state in _EVICTABLE_STATES
            ]
            if not evictable:
                break
            victim_id = evictable[0]
            victim_entry = self._entries[victim_id]
            victim_entry.state = RuntimeState.RETIRING
            retired_this_call += 1
            victims.append((victim_id, victim_entry))
        return victims

    def _finish_retirement(self, canonical_id: str, entry: RuntimeEntry) -> None:
        """Run cleanup for an already-`RETIRING` entry, then remove it.

        Caller must NOT hold `self._metadata_lock` -- cleanup can call
        arbitrary, potentially slow code (`torch.cuda.empty_cache()`, a
        pipeline's own `.to("cpu")`, ...) via `on_evict`, which the
        metadata-lock docstring at the top of this module forbids doing
        under `M`.

        PR4a Codex-review Finding 6 (round 1): metadata removal happens in a
        `finally`, not merely after cleanup returns. `_run_cleanup()` itself
        only swallows `Exception`; a `BaseException` (a custom cancellation
        signal, `KeyboardInterrupt`, ...) escaping `on_evict` used to skip
        the `del self._entries[canonical_id]` below entirely, stranding this
        entry at `RETIRING` forever -- a permanent block on every future
        `acquire_or_reserve()`/`unload()`/`unload_all()` call for this exact
        canonical id (see `_BUSY_FOR_UNLOAD_STATES`). The `finally` below
        guarantees the entry is always removed once cleanup has been
        attempted, whatever it raised; the exception (if any) still
        propagates to this method's own caller once metadata is consistent
        again.
        """

        try:
            if entry.runtime is not None:
                self._run_cleanup(canonical_id, entry.runtime)
        finally:
            with self._metadata_lock:
                current = self._entries.get(canonical_id)
                if current is entry:
                    del self._entries[canonical_id]

    def _run_cleanup(self, model_id: str, runtime_obj: Any) -> None:
        """Call `self._on_evict`, if any -- see `OnEvictCallback`'s own
        docstring for the re-entrancy contract this relies on (never called
        while holding `self._metadata_lock`; must never re-enter
        `ModelService`)."""

        if self._on_evict is None:
            return
        try:
            self._on_evict(model_id, runtime_obj)
        except Exception:  # noqa: BLE001 - a broken hook must not corrupt the cache
            logger.warning(
                "on_evict callback failed for model %r.", model_id, exc_info=True
            )

    def dispose_unpublished(self, model_id: str, runtime_obj: Any) -> None:
        """Clean up a runtime that was loaded but never made it into the cache.

        PR4a Codex-review Finding 4 (round 1): `ModelService.resolve_runtime()`
        calls this when `put()` raises `RuntimeBusyError` after
        `loader.load()` already succeeded -- the loaded `runtime_obj` was
        never published anywhere, so nothing else in the process can ever
        reach it once that call unwinds; without an explicit disposal it
        just leaks (accelerator memory included). Runs the exact same
        `on_evict` hook eviction/unload/replacement use, on this object
        only -- it never touches `self._entries`, so it is safe to call
        without holding (and does not itself acquire) `self._metadata_lock`.
        """

        self._run_cleanup(model_id, runtime_obj)


def resolve_media_cache_limits(
    env: Mapping[str, str] | None = None,
    *,
    media_types: Iterable[str] = DEFAULT_MEDIA_TYPES,
) -> dict[str, int]:
    """Read per-media runtime-cache budgets from `MAX_CACHED_MODELS_<MEDIA>`.

    For each `media_type` this checks `MAX_CACHED_MODELS_{MEDIA_TYPE.upper()}`
    (e.g. `MAX_CACHED_MODELS_TEXT`, `MAX_CACHED_MODELS_IMAGE`). A media type
    whose variable is absent, or whose value fails to parse as an integer
    >= 1, is left out of the returned mapping entirely rather than raising --
    that is what lets a caller pass the result straight to
    `ModelRuntimeCache(media_limits=...)` and get the pre-#182
    single-budget behavior for any media type nobody configured.
    """

    source = env if env is not None else os.environ
    limits: dict[str, int] = {}
    for media_type in media_types:
        env_var = f"MAX_CACHED_MODELS_{media_type.upper()}"
        raw_value = source.get(env_var)
        if raw_value is None or raw_value == "":
            continue
        try:
            parsed = int(raw_value)
        except ValueError:
            logger.warning(
                "Ignoring non-integer %s=%r; %r keeps the shared runtime-cache budget.",
                env_var,
                raw_value,
                media_type,
            )
            continue
        if parsed < 1:
            logger.warning(
                "Ignoring %s=%r (must be at least 1); %r keeps the shared "
                "runtime-cache budget.",
                env_var,
                raw_value,
                media_type,
            )
            continue
        limits[media_type] = parsed
    return limits


__all__ = [
    "DEFAULT_MEDIA_TYPES",
    "ModelRuntimeCache",
    "OnEvictCallback",
    "resolve_media_cache_limits",
]
