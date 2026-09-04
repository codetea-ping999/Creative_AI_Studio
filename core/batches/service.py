"""Batch orchestration: create children, track them, advance stages."""

from __future__ import annotations

import logging
import os
from threading import RLock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from core.jobs.statuses import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    TERMINAL_JOB_STATUSES,
    is_terminal_status,
)
from core.reference_capabilities import MissingReferenceAssetError, UnsupportedReferenceError
from core.storage.json_files import utc_now

from .expansion import expand_items
from .repository import BatchRepository
from .schemas import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_PARTIAL,
    BATCH_STATUS_QUEUED,
    BATCH_STATUS_RUNNING,
    BATCH_STATUS_SUCCEEDED,
    DEFAULT_ITEM_LIMIT,
    ITEM_STATUS_PENDING,
    BatchAggregate,
    BatchItem,
    BatchRecord,
    BatchSpec,
)

if TYPE_CHECKING:
    from core.jobs import EventBus, JobEvent, JobService
    from core.storage.repositories.job_repository import JobRepository

logger = logging.getLogger(__name__)

_TERMINAL_EVENT_TYPES = frozenset(
    {"job_succeeded", "job_failed", "job_cancelled"}
)


def resolve_max_items_limit(configured: int | None = None) -> int:
    """Resolve the operator ceiling on batch size."""

    if configured is not None:
        return max(1, configured)
    raw_value = os.getenv("BATCH_MAX_ITEMS", "").strip()
    if not raw_value:
        return DEFAULT_ITEM_LIMIT
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(
            "Ignoring invalid BATCH_MAX_ITEMS=%r; using %d.",
            raw_value,
            DEFAULT_ITEM_LIMIT,
        )
        return DEFAULT_ITEM_LIMIT


class BatchService:
    """Create and track batches of generation jobs.

    The job repository is the source of truth for item state, and batch state is
    re-derived from it on read. That is what keeps a batch correct after a process
    restart, when no in-memory event history survives.
    """

    def __init__(
        self,
        batch_repository: BatchRepository,
        job_service: JobService,
        job_repository: JobRepository,
        *,
        event_bus: EventBus | None = None,
        max_items_limit: int | None = None,
    ) -> None:
        self.batch_repository = batch_repository
        self.job_service = job_service
        self.job_repository = job_repository
        self.event_bus = event_bus
        self.max_items_limit = resolve_max_items_limit(max_items_limit)
        # Advancing a stage creates jobs, which publish events, which can call back
        # into this service on the same thread. A reentrant lock keeps that safe
        # without deadlocking on the nested call.
        self._lock = RLock()

    # ---------------------------------------------------------------- creation

    def create_batch(self, spec: BatchSpec) -> BatchRecord:
        effective_spec = spec
        if spec.limit > self.max_items_limit:
            # An operator ceiling overrides an over-ambitious request rather than
            # rejecting it, so a preset written for a bigger machine still runs.
            effective_spec = spec.model_copy(update={"limit": self.max_items_limit})

        stages = effective_spec.resolved_stages()
        batch_id = f"batch_{uuid4().hex}"
        items = expand_items(
            effective_spec,
            stage=stages[0],
            stage_index=0,
            id_prefix=f"{batch_id}_item",
        )
        # #201 follow-up (Codex P2, tenth round): preflight every item's
        # references before anything is persisted. _enqueue_stage() below
        # calls JobService.create_job() (which re-validates references
        # itself) one item at a time -- if an early item's job was already
        # created and enqueued by the time a later item's reference turns
        # out invalid, the raised exception still aborts this call with a
        # 4xx, but the earlier item's job (and this batch's own record) were
        # already persisted: an invisible queued job the client was never
        # told about, and a batch id it never received either. Validating
        # every item up front keeps a reference failure atomic with "nothing
        # was created," matching the oversized-sweep check expand_items()
        # already enforces before any of this runs.
        for item in items:
            self.job_service.validate_references(item.request, effective_spec.project_id)

        now = utc_now()
        record = BatchRecord(
            id=batch_id,
            spec=effective_spec,
            status=BATCH_STATUS_QUEUED,
            stage_index=0,
            items=items,
            aggregate=BatchAggregate(total=len(items), pending=len(items)),
            created_at=now,
            updated_at=now,
        )
        record = self.batch_repository.create(record)
        return self._enqueue_stage(record, stage_index=0)

    def _enqueue_stage(self, record: BatchRecord, *, stage_index: int) -> BatchRecord:
        for item in record.items:
            if item.stage_index != stage_index or item.job_id is not None:
                continue
            job = self.job_service.create_job(
                item.request,
                project_id=record.spec.project_id,
            )
            item.job_id = job.id
            item.status = job.status
        record.status = BATCH_STATUS_RUNNING
        return self._recompute_and_save(record)

    # ------------------------------------------------------------------- reads

    def get_batch(self, batch_id: str) -> BatchRecord | None:
        return self.reconcile(batch_id)

    def list_batches(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[BatchRecord]:
        records = self.batch_repository.list_all(project_id=project_id, limit=limit)
        return [self.reconcile(record.id) or record for record in records]

    # --------------------------------------------------------- reconciliation

    def reconcile(self, batch_id: str) -> BatchRecord | None:
        with self._lock:
            record = self.batch_repository.get(batch_id)
            if record is None:
                return None
            return self._recompute_and_save(record)

    def _recompute_and_save(self, record: BatchRecord) -> BatchRecord:
        for item in record.items:
            if item.job_id is None:
                continue
            job = self.job_repository.get(item.job_id)
            if job is None:
                continue
            item.status = job.status
            item.error_message = job.error_message
            if job.result is not None:
                item.output_path = next(
                    (output for output in job.result.outputs if output), None
                )
                item.preview_path = (
                    next((preview for preview in job.result.previews if preview), None)
                    or item.output_path
                )
                item.score = _extract_score(job.result.metadata)

        record.aggregate = _build_aggregate(record.items)
        record.status = _derive_status(record)
        return self.batch_repository.save(record)

    # ----------------------------------------------------------- stage control

    def advance(self, batch_id: str) -> BatchRecord | None:
        with self._lock:
            record = self.batch_repository.get(batch_id)
            if record is None:
                return None
            record = self._recompute_and_save(record)
            return self._advance_locked(record)

    def _advance_locked(self, record: BatchRecord) -> BatchRecord:
        stages = record.spec.resolved_stages()
        next_stage_index = record.stage_index + 1
        if next_stage_index >= len(stages):
            return record

        current_items = record.items_for_stage(record.stage_index)
        if not all(is_terminal_status(item.status) for item in current_items):
            return record

        current_stage = stages[record.stage_index]
        winners = _rank_winners(current_items, keep_top_n=current_stage.keep_top_n)
        if not winners:
            return record

        next_stage = stages[next_stage_index]
        new_items = expand_items(
            record.spec,
            stage=next_stage,
            stage_index=next_stage_index,
            seed_items=winners,
            id_prefix=f"{record.id}_item",
        )
        # #201 follow-up (Codex P2, tenth round): same preflight as
        # create_batch() above, for the same reason -- this stage's items
        # are about to be persisted onto the batch record and enqueued one
        # at a time, so a later item's reference failure must not leave an
        # earlier one already queued behind an exception the caller has no
        # way to partially undo.
        for new_item in new_items:
            self.job_service.validate_references(new_item.request, record.spec.project_id)
        # #201 follow-up (Codex P2, thirteenth round): the preflight above
        # passed, so this is either the first attempt or a retry after an
        # operator fixed whatever made an earlier attempt's preflight raise
        # (see handle_job_event()). Clear any stale advance_error from that
        # earlier attempt now -- otherwise _derive_status() would keep
        # forcing this batch to "failed" forever even as the code below
        # creates and enqueues real, live jobs for it.
        record.advance_error = None

        # Carry each winner's label forward so the refined output is traceable to
        # the probe that earned it. Match on axis values rather than position:
        # expand_items sorts by model_id to protect the runtime cache, so a spec
        # whose axis patches model_id yields an order different from the score
        # ranking, and zipping the two labels a refine item with another winner's
        # combination.
        winners_by_axis = {_axis_key(winner.axis_values): winner for winner in winners}
        for new_item in new_items:
            winner = winners_by_axis.get(_axis_key(new_item.axis_values))
            if winner is not None:
                new_item.label = f"{winner.label}__{next_stage.name}"
            else:  # pragma: no cover - expansion mirrors the winners it was given
                new_item.label = f"{new_item.label}__{next_stage.name}"

        record.items.extend(new_items)
        record.stage_index = next_stage_index
        record = self.batch_repository.save(record)
        return self._enqueue_stage(record, stage_index=next_stage_index)

    def cancel(self, batch_id: str) -> BatchRecord | None:
        with self._lock:
            record = self.batch_repository.get(batch_id)
            if record is None:
                return None
            for item in record.items:
                if item.job_id is None:
                    item.status = JOB_STATUS_CANCELLED
                    continue
                if not is_terminal_status(item.status):
                    self.job_service.cancel_job(item.job_id)
            return self._recompute_and_save(record)

    def promote(self, batch_id: str, item_id: str) -> BatchRecord | None:
        with self._lock:
            record = self.batch_repository.get(batch_id)
            if record is None:
                return None
            item = next((entry for entry in record.items if entry.id == item_id), None)
            if item is None:
                raise LookupError(f"Unknown batch item: {item_id}")
            item.promoted = True
            return self._recompute_and_save(record)

    # ---------------------------------------------------------------- events

    def attach_to_event_bus(self) -> None:
        """Subscribe to terminal job events so stages advance on their own."""

        if self.event_bus is None:
            return
        self.event_bus.subscribe(self.handle_job_event)

    def handle_job_event(self, event: JobEvent) -> None:
        """React to a job finishing. Runs on the job runner thread."""

        if event.type not in _TERMINAL_EVENT_TYPES:
            return
        job_id = event.payload.get("job_id")
        if not isinstance(job_id, str):
            return
        try:
            record = self.batch_repository.find_by_job_id(job_id)
            if record is None:
                return
            with self._lock:
                refreshed = self._recompute_and_save(record)
                try:
                    self._advance_locked(refreshed)
                except (UnsupportedReferenceError, MissingReferenceAssetError) as exc:
                    # #201 follow-up (Codex P2, eleventh round): the
                    # stage-advance reference preflight (added this same
                    # round, in _advance_locked) can raise here, on the job
                    # runner thread with no HTTP caller to hand a 4xx to.
                    # Left to the broad except below, that was logged and
                    # swallowed, leaving the batch stuck in "running"
                    # forever: its current stage's items all terminal, but
                    # the next stage never created and no further job event
                    # will ever retrigger this path. Persist it as failed
                    # instead so the batch reaches an observable terminal
                    # state.
                    #
                    # #201 follow-up (Codex P2, twelfth round): setting only
                    # `status` here was not durable -- the next read calls
                    # _recompute_and_save(), whose _derive_status() has no
                    # concept of "the stage transition itself failed" and
                    # recomputes "running" from a successful current stage
                    # plus a pending next one, silently reverting this.
                    # advance_error is what _derive_status() actually checks.
                    refreshed.advance_error = str(exc)
                    refreshed.status = BATCH_STATUS_FAILED
                    self.batch_repository.save(refreshed)
                    logger.warning(
                        "Batch %s failed to advance past stage %d: %s",
                        refreshed.id,
                        refreshed.stage_index,
                        exc,
                    )
        except Exception:  # pragma: no cover - never break the runner
            logger.exception("Failed to update batch state for job %s.", job_id)


def _extract_score(metadata: dict[str, Any]) -> float | None:
    quality_report = metadata.get("quality_report")
    if not isinstance(quality_report, dict):
        return None
    score = quality_report.get("quality_score")
    return float(score) if isinstance(score, (int, float)) else None


def _build_aggregate(items: list[BatchItem]) -> BatchAggregate:
    scores = [item.score for item in items if item.score is not None]
    best_item = max(
        (item for item in items if item.score is not None),
        key=lambda item: (item.score, -item.index),
        default=None,
    )
    return BatchAggregate(
        total=len(items),
        pending=sum(1 for item in items if item.status == ITEM_STATUS_PENDING),
        running=sum(
            1
            for item in items
            if item.status not in TERMINAL_JOB_STATUSES
            and item.status != ITEM_STATUS_PENDING
        ),
        succeeded=sum(1 for item in items if item.status == JOB_STATUS_SUCCEEDED),
        failed=sum(1 for item in items if item.status == JOB_STATUS_FAILED),
        cancelled=sum(1 for item in items if item.status == JOB_STATUS_CANCELLED),
        average_score=round(sum(scores) / len(scores), 2) if scores else None,
        best_item_id=best_item.id if best_item is not None else None,
    )


def _derive_status(record: BatchRecord) -> str:
    if record.advance_error is not None:
        # A stage transition itself failed (see BatchRecord.advance_error) --
        # authoritative and terminal, regardless of what the items/aggregate
        # below would otherwise derive (a successful current stage with
        # another stage still pending would normally compute "running").
        return BATCH_STATUS_FAILED
    items = record.items
    if not items:
        return BATCH_STATUS_QUEUED

    aggregate = record.aggregate
    all_terminal = all(is_terminal_status(item.status) for item in items)
    if not all_terminal:
        if aggregate.pending == aggregate.total:
            return BATCH_STATUS_QUEUED
        return BATCH_STATUS_RUNNING

    stages = record.spec.resolved_stages()
    has_further_stage = record.stage_index + 1 < len(stages)
    if has_further_stage and aggregate.succeeded:
        # The current stage is done but the run is not: keep it "running" so the
        # UI does not claim completion before the refine pass exists.
        return BATCH_STATUS_RUNNING

    if aggregate.succeeded == aggregate.total:
        return BATCH_STATUS_SUCCEEDED
    if aggregate.succeeded:
        return BATCH_STATUS_PARTIAL
    if aggregate.cancelled:
        return BATCH_STATUS_CANCELLED
    return BATCH_STATUS_FAILED


def _axis_key(axis_values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Order-independent identity for one cell of the sweep."""

    return tuple(sorted(axis_values.items()))


def _rank_winners(
    items: list[BatchItem],
    *,
    keep_top_n: int | None,
) -> list[BatchItem]:
    """Rank succeeded items by score, breaking ties by index for determinism."""

    succeeded = [item for item in items if item.status == JOB_STATUS_SUCCEEDED]
    ranked = sorted(
        succeeded,
        key=lambda item: (-(item.score if item.score is not None else -1.0), item.index),
    )
    if keep_top_n is None:
        return ranked
    return ranked[:keep_top_n]


__all__ = ["BatchService", "resolve_max_items_limit"]
