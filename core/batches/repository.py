"""Persistence for batch records."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from threading import RLock

from core.storage.json_files import utc_now, write_json_atomic

from .schemas import BatchRecord


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


__all__ = ["BatchRepository"]
