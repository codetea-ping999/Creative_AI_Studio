"""Persistence for batch records."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from threading import RLock
from typing import TypeVar

from core.storage.json_files import utc_now, write_json_atomic

from .schemas import BatchRecord

_T = TypeVar("_T")


class BatchRepository:
    """Persist and manage batch records on disk.

    A batch document can be written by several independent callers in the
    same process -- API routes (create/cancel/promote/advance) and
    ``BatchService.handle_job_event`` reacting to a child job finishing --
    each potentially doing its own read-modify-write. Without a shared
    boundary, a read taken *before* a lock is acquired can go stale by the
    time the mutated record is saved, silently discarding whatever another
    writer committed in between (PR3 exact-HEAD audit: this is exactly what
    ``handle_job_event``'s old lock-free ``find_by_job_id`` did). ``mutate()``
    and ``mutate_by_job_id()`` are that boundary: both hold ``_lock`` across
    the read, the caller's mutation, and the save, so every writer that goes
    through them is serialized against every other one -- mirroring
    ``StoryRepository.mutate()``.
    """

    def __init__(self, batch_dir: str | Path = "data/batches") -> None:
        self.batch_dir = Path(batch_dir)
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        # Reentrant: advancing a stage creates jobs, which publish events,
        # which can call back into BatchService.handle_job_event -> mutate()
        # on the same thread (see BatchService's own docstring). A plain
        # Lock would deadlock on that nested call.
        self._lock = RLock()

    def create(self, record: BatchRecord) -> BatchRecord:
        with self._lock:
            self._save(record)
            return record

    def get(self, batch_id: str) -> BatchRecord | None:
        batch_file = self.batch_dir / f"{batch_id}.json"
        if not batch_file.exists():
            return None
        return self._try_load(batch_file)

    def get_or_diagnose(self, batch_id: str) -> tuple[BatchRecord | None, bool]:
        """Like `get()`, but tells confirmed absence apart from unreadable.

        Returns `(record, uncertain)`. `uncertain=True` means the file
        exists but could not be read right now (a transient `OSError`) --
        `record is None` in that case must not be read as "this batch was
        deleted." A single-file counterpart to `list_all_tolerant()` /
        `find_by_job_id_or_diagnose()`, for a caller (`BatchService.
        reconcile_child_job()`) that already knows the exact id it cares
        about and does not need a full directory scan to find it (PR3
        exact-HEAD audit, second round, P1-2).
        """

        batch_file = self.batch_dir / f"{batch_id}.json"
        if not batch_file.exists():
            return None, False
        return self._try_load_diagnosed(batch_file)

    def run_exclusive(self, fn: Callable[[], _T]) -> _T:
        """Run `fn()` while holding this repository's own lock.

        For a caller that must linearize a multi-step decision -- reading
        this repository's state, deciding based on it, then taking an
        action *outside* this repository (e.g. exposing a Job to a worker
        queue) -- against every write that already goes through
        `mutate()`/`mutate_by_job_id()`/`mutate_by_job_id_diagnosed()` on
        this same instance. `fn` takes no arguments; its return value is
        passed straight through.

        This is a narrow, general-purpose exclusive-execution primitive --
        not a new distributed-locking mechanism, not a runtime lease or
        WorkerPool coordination point, and not itself aware of
        cancellation or Job queues. It exists because a plain `get()`
        (lock-free) followed by a separate action, with nothing
        serializing them against a concurrent `mutate()`, is exactly the
        race the exact-HEAD audit (third round) identified between
        persisting `cancellation_requested` and exposing a not-yet-queued
        child Job to a worker: `mutate()`'s own critical section can
        complete in the gap between an ordinary `get()` and whatever the
        caller does with its result. Running both the read and the action
        under this same lock closes that gap for any caller that needs it
        (see `BatchService._authorize_and_expose()`), without every future
        caller having to reimplement it.

        Do not call back into this repository's own `mutate()`/
        `mutate_by_job_id()`/`mutate_by_job_id_diagnosed()` from within
        `fn` unless you are certain of the reentrancy implications: the
        lock is an `RLock`, so same-thread reentry will not deadlock, but
        can still produce surprising nested-mutation ordering. Prefer a
        plain `get()`/`get_or_diagnose()` read inside `fn` instead.
        """

        with self._lock:
            return fn()

    def save(self, record: BatchRecord) -> BatchRecord:
        with self._lock:
            updated = record.model_copy(update={"updated_at": utc_now()})
            self._save(updated)
            return updated

    def mutate(
        self,
        batch_id: str,
        fn: Callable[[BatchRecord | None], BatchRecord | None],
    ) -> BatchRecord | None:
        """Read, apply ``fn``, and atomically save one batch as a single step.

        ``fn`` is called with the current record (or ``None`` if it does
        not exist). Its return value controls what happens next, exactly
        like ``StoryRepository.mutate()``:

        - ``None`` -- decline the mutation; nothing is saved and
          ``mutate()`` returns ``None``.
        - a record equal to a snapshot of what ``fn`` was given -- nothing
          is saved (no pointless ``updated_at`` bump), and ``mutate()``
          returns the (possibly in-place-mutated) record as-is. Comparing
          against a *snapshot* taken before calling ``fn`` -- not against
          the live ``current`` reference -- matters here specifically
          because, unlike ``StoryRepository``'s callers, ``BatchService``'s
          existing code mutates ``BatchItem``/``BatchRecord`` fields in
          place and returns the same object; comparing `updated is current`
          would then trivially "match" even when real fields changed.
        - any other record -- saved atomically, and the saved (persisted)
          record is returned.

        The read, the call to ``fn``, and the save all happen while holding
        the repository's lock, so this -- not a caller's own ``get()`` +
        ``save()`` -- is the boundary every batch writer should go through.
        Do not put job creation, generation, or other slow/side-effecting
        work inside ``fn`` itself; a caller that needs to create child jobs
        as a *result* of a mutation should do that after ``mutate()``
        returns, using the ids ``fn`` persisted onto the record (see
        ``BatchService._enqueue_stage``'s two-phase persist-id-then-create
        split) -- never inside the critical section.

        If ``fn`` raises, the exception propagates and nothing is saved.
        """

        with self._lock:
            current = self.get(batch_id)
            snapshot = current.model_copy(deep=True) if current is not None else None
            updated = fn(current)
            if updated is None:
                return None
            if updated == snapshot:
                return updated
            return self.save(updated)

    def mutate_by_job_id(
        self,
        job_id: str,
        fn: Callable[[BatchRecord], BatchRecord | None],
    ) -> BatchRecord | None:
        """Resolve the batch owning ``job_id`` and ``mutate()`` it, atomically.

        The lookup (``find_by_job_id``) happens *inside* the same lock as
        the mutation and save -- the fix for the exact-HEAD audit finding
        this repository exists to close: doing that lookup before acquiring
        the lock let the resolved record go stale by the time a later
        ``save()`` committed it, clobbering a concurrent writer's change.
        ``fn`` is only called when a batch is actually found; return
        ``None`` from it to decline, exactly like ``mutate()``.
        """

        with self._lock:
            current = self.find_by_job_id(job_id)
            if current is None:
                return None
            snapshot = current.model_copy(deep=True)
            updated = fn(current)
            if updated is None:
                return None
            if updated == snapshot:
                return updated
            return self.save(updated)

    def mutate_by_job_id_diagnosed(
        self,
        job_id: str,
        fn: Callable[[BatchRecord], BatchRecord | None],
    ) -> tuple[BatchRecord | None, bool]:
        """Like `mutate_by_job_id()`, but tells "no parent Batch" apart from
        "a scan failure means a parent might exist but wasn't found".

        Returns `(result, uncertain)`. `uncertain=True` means at least one
        batch file could not be read due to a transient `OSError` during
        the lookup -- the file `job_id` actually belongs to, if any, could
        be exactly that unreadable one. A caller (completion convergence)
        must treat `(None, True)` as "retry later," never as "genuinely no
        parent Batch, proceed" -- seeing `(None, False)` -- otherwise a job
        whose owning Batch is merely having a transient read hiccup gets
        marked completion-done and is excluded from every future retry.

        The lookup, the call to `fn`, and the save all happen under one
        critical section, exactly like `mutate_by_job_id()`.
        """

        with self._lock:
            current, uncertain = self.find_by_job_id_or_diagnose(job_id)
            if current is None:
                return None, uncertain
            snapshot = current.model_copy(deep=True)
            updated = fn(current)
            if updated is None:
                return None, False
            if updated == snapshot:
                return updated, False
            return self.save(updated), False

    def list_all(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[BatchRecord]:
        records: list[BatchRecord] = []
        for batch_file in sorted(self.batch_dir.glob("*.json")):
            record = self._try_load(batch_file)
            if record is None:
                continue
            if project_id and record.spec.project_id != project_id:
                continue
            records.append(record)

        records.sort(key=lambda entry: entry.updated_at, reverse=True)
        if limit is not None:
            return records[:limit]
        return records

    def find_by_job_id(self, job_id: str) -> BatchRecord | None:
        """Return the batch owning a job, or None.

        Scans the directory because a batch is small and there are few of them;
        an index would add a second thing to keep consistent for no measured win.
        """

        for record in self.list_all():
            if any(item.job_id == job_id for item in record.items):
                return record
        return None

    def list_all_tolerant(
        self,
        *,
        project_id: str | None = None,
    ) -> tuple[list[BatchRecord], list[str], bool]:
        """Like `list_all()`, but reports *why* any file failed to load.

        Returns `(records, malformed_ids, scan_was_fully_reliable)``.
        `malformed_ids` lists the batch id (filename stem) of every file
        whose content is deterministically invalid (bad JSON, or content
        that no longer matches `BatchRecord`'s schema) -- the same content
        will fail to load identically on every future attempt, exactly
        like `list_all()` already silently tolerates for ordinary listing.

        `scan_was_fully_reliable` is `False` if *any* file instead hit a
        transient `OSError` while being read -- meaning some batch (one
        this scan cannot even identify by id, since reading it is what
        failed) was not observed by this pass at all. A caller about to
        treat "not currently seen as cancelling" as "safe to proceed" (see
        `BatchService.resume_pending_cancellations`) must check this flag
        first: a transient read failure must never be silently treated as
        "there is nothing left to resume."
        """

        records: list[BatchRecord] = []
        malformed_ids: list[str] = []
        scan_was_fully_reliable = True
        for batch_file in sorted(self.batch_dir.glob("*.json")):
            record, transient_failure = self._try_load_diagnosed(batch_file)
            if record is None:
                if transient_failure:
                    scan_was_fully_reliable = False
                else:
                    malformed_ids.append(batch_file.stem)
                continue
            if project_id and record.spec.project_id != project_id:
                continue
            records.append(record)

        records.sort(key=lambda entry: entry.updated_at, reverse=True)
        return records, malformed_ids, scan_was_fully_reliable

    def find_by_job_id_or_diagnose(self, job_id: str) -> tuple[BatchRecord | None, bool]:
        """Like `find_by_job_id()`, but tells "no owner" apart from "unsure".

        Returns `(batch_or_none, uncertain)`. `uncertain=True` means at
        least one batch file could not be read (a transient `OSError`)
        during this scan -- the true owner of `job_id`, if any, could be
        exactly that unreadable file, so `None` here must not be read as
        "confirmed: no parent Batch."
        """

        records, _malformed_ids, scan_was_fully_reliable = self.list_all_tolerant()
        for record in records:
            if any(item.job_id == job_id for item in record.items):
                return record, False
        return None, not scan_was_fully_reliable

    def delete(self, batch_id: str) -> bool:
        # Shares _lock with mutate()/mutate_by_job_id()/save() so a
        # concurrent read-modify-write can never race this delete into
        # resurrecting the batch it just removed.
        with self._lock:
            batch_file = self.batch_dir / f"{batch_id}.json"
            if batch_file.exists():
                batch_file.unlink()
                return True
            return False

    def _save(self, record: BatchRecord) -> None:
        write_json_atomic(
            self.batch_dir / f"{record.id}.json",
            record.model_dump(mode="json"),
        )

    def _try_load(self, batch_file: Path) -> BatchRecord | None:
        try:
            return BatchRecord.model_validate_json(
                batch_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _try_load_diagnosed(self, batch_file: Path) -> tuple[BatchRecord | None, bool]:
        """Load one batch file, distinguishing malformed content from a
        transient read failure -- see `list_all_tolerant()`.

        Returns `(record, transient_failure)`. `record` is `None` on
        either kind of failure; `transient_failure=True` only for an
        `OSError` (the file exists but could not be read right now --
        permissions, a concurrent replace, disk I/O), never for malformed
        JSON or schema content, which fails the same deterministic way on
        every future attempt too.
        """

        try:
            return (
                BatchRecord.model_validate_json(batch_file.read_text(encoding="utf-8")),
                False,
            )
        except OSError:
            return None, True
        except (json.JSONDecodeError, ValueError):
            return None, False


__all__ = ["BatchRepository"]
