"""Batch orchestration: create children, track them, advance stages."""

from __future__ import annotations

from enum import Enum
import logging
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from core.jobs.statuses import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_SUCCEEDED,
    TERMINAL_JOB_STATUSES,
    is_terminal_status,
)
from core.reference_capabilities import MissingReferenceAssetError, UnsupportedReferenceError
from core.storage.json_files import utc_now
from core.storage.repositories.job_repository import JobRecordDecodeError

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


class BatchReconciliationOutcome(Enum):
    """Result of one `BatchService.reconcile_child_job()` attempt.

    A caller reconciling a Job's completion (`core.jobs.completion.
    CompletionConverger`) must not conflate "no parent Batch, nothing to
    do" with "a parent Batch might exist but a transient storage failure
    prevented finding/reconciling it" -- collapsing both to the same
    signal (as a bare `BatchRecord | None` return does) let a transient
    Batch-file read failure be silently treated as "reconciliation
    succeeded," permanently excluding the job from every future
    completion retry (Codex exact-HEAD review).
    """

    # This job does not belong to any Batch -- completion may proceed.
    NO_PARENT = "no_parent"
    # A parent Batch was found and this reconciliation step itself ran
    # (its own further effects, e.g. advancing a stage, may still be
    # pending independently -- that is tracked by the Batch's own state,
    # not by this outcome).
    RECONCILED = "reconciled"
    # The owning Batch, if any, could not be read right now (a transient
    # storage failure, not a confirmed absence) -- completion must stay
    # pending and retry later.
    RETRYABLE_FAILURE = "retryable_failure"


class BatchStageMaterializationError(RuntimeError):
    """A stage advance persisted, but materializing its children could not
    be confirmed right now (a transient storage failure) -- retry the same
    call once resolved.

    Distinct from `UnsupportedReferenceError`/`MissingReferenceAssetError`
    (a permanent reference-preflight failure a retry can never fix on its
    own) and from `advance()` returning `None` (the batch itself is
    confirmed gone): this means neither -- the batch still exists, the
    stage transition itself succeeded and was persisted, but its new
    stage's Job rows are not yet confirmed to exist (PR3 exact-HEAD audit,
    third round, P1-6). Reporting a normal-looking successful result here
    would silently leave the batch stuck with no Job to ever finish it and
    no runtime retry scheduled, since the triggering job may already be
    `completion_state="done"`.
    """


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

    Every mutation goes through ``BatchRepository.mutate()``/``mutate_by_job_id()``
    (PR3 exact-HEAD audit): those hold the repository's own lock across a fresh
    read, the mutation, and the save, so no caller here does its own
    read-outside-the-lock followed by a possibly-stale save. Child job
    creation happens *outside* those critical sections (see
    ``_enqueue_stage``'s two-phase persist-id-then-create split): a child's id
    is durably assigned to its item first, then the job row is created
    (or reused) against that exact id, so a crash between the two steps is
    resumable by simply re-running ``_enqueue_stage`` rather than minting a
    new id and creating a duplicate job.
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
        # references before anything is persisted -- see _try_advance_in_place
        # for the identical reasoning applied to a later stage's items.
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
        enqueued = self._enqueue_stage(record.id, stage_index=0)
        # The batch was just created above; nothing else has had a chance
        # to delete it before this line runs.
        assert enqueued is not None
        return enqueued

    def _enqueue_stage(self, batch_id: str, *, stage_index: int) -> BatchRecord | None:
        """Idempotently create/reuse this stage's children and enqueue them.

        Two phases, deliberately not one: (1) assign a durable job id to
        every not-yet-assigned item in this stage, under the repository
        lock; (2) create-or-reuse the actual job row for each assigned id,
        outside the lock (job creation publishes events that can reenter
        this service, and must never happen while a mutate() critical
        section is open). Re-running this after a crash at any point in
        either phase resumes correctly: phase 1 only touches items still
        missing an id, and phase 2's create-or-reuse never creates a
        second job for an id that already has one (see
        ``JobService.create_or_reuse_job``).
        """

        def _assign_ids(record: BatchRecord | None) -> BatchRecord | None:
            if record is None or record.cancellation_requested:
                return record
            for item in record.items:
                if item.stage_index != stage_index or item.job_id is not None:
                    continue
                if is_terminal_status(item.status):
                    # A pre-PR3 ("legacy") batch record has no
                    # `cancellation_requested` field on disk -- pydantic
                    # defaults it to `False` on load, exactly like a batch
                    # that was never cancelled at all. The old `cancel()`
                    # implementation, which predates the durable-intent
                    # flag, persisted a cancelled item as `job_id=None`,
                    # `status="cancelled"` directly, with nothing else
                    # recording that intent. Without this check, resuming
                    # such a batch (`resume_current_stage_for_all_batches()`
                    # at startup, or any other `_enqueue_stage()` call) would
                    # see "no job_id" and mint a *brand new* id for an item
                    # that was deliberately terminalized, silently
                    # resurrecting a generation the operator already
                    # cancelled (PR3 exact-HEAD audit, second round, P1-3).
                    # A `job_id is None` item reaching a terminal status is
                    # otherwise impossible going forward (this class's own
                    # `cancel()`/`_recompute()` never do it), so this only
                    # ever fires for genuinely legacy data.
                    continue
                item.job_id = f"job_{uuid4().hex}"
            return record

        record = self.batch_repository.mutate(batch_id, _assign_ids)
        if record is None or record.cancellation_requested:
            return record

        for item in record.items_for_stage(stage_index):
            if item.job_id is None:
                continue
            # Cheap, lock-free pre-check -- purely an optimization to skip
            # unnecessary Job-row materialization for a batch already known
            # to be cancelling. This is *not* the safety boundary (see
            # `_authorize_and_expose()` below, which is): a stale or
            # momentarily-unreadable read here just means this item's
            # materialization is skipped one pass early, which is always
            # safe to retry later.
            current = self.batch_repository.get(batch_id)
            if current is None or current.cancellation_requested:
                break
            # Materialize the row *without* enqueuing it yet (PR3 exact-HEAD
            # audit, second round, P1-1): nothing below can be observed by
            # `JobRunner` until `enqueue_job()` is actually called inside
            # `_authorize_and_expose()`, so that call's own fresh
            # cancellation check runs against a row that is, by
            # construction, still invisible to every worker.
            try:
                created, _was_created = self.job_service.create_or_reuse_job_without_enqueue(
                    item.job_id, item.request, project_id=record.spec.project_id
                )
            except JobRecordDecodeError:
                # This item's stable id already has a Job row on disk, but
                # it cannot currently be decoded (PR3 exact-HEAD audit,
                # third round, P1-5) -- PR #397's own quarantine
                # primitives (via job-level startup recovery) are what
                # eventually resolve this; this loop must not re-derive
                # that logic, nor guess at (or overwrite) the row's true
                # content by trying to recreate it. Skip just this one
                # item and continue with the rest of the stage -- one
                # poisoned child must never abort materialization for
                # every other item, in this batch or any other.
                logger.warning(
                    "Batch %s: child job %s's row could not be decoded "
                    "while materializing stage %d; leaving it for a "
                    "later retry.",
                    batch_id,
                    item.job_id,
                    stage_index,
                )
                continue
            # The authoritative gate (PR3 exact-HEAD audit, third round):
            # decide whether to expose this job to a worker under the exact
            # same lock `cancel()`'s own durable-intent mutation uses, so no
            # cancellation can land in the gap between checking and
            # enqueuing -- closing the race a lock-free "check, then
            # enqueue" sequence cannot. Checked by job *status*, not by
            # whether this call is what created the row: a reused row that
            # is still genuinely `queued` (e.g. a prior `enqueue_job()` call
            # raised after materialization but before this exact attempt)
            # is re-attempted here too, not just a freshly-created one
            # (P1-1) -- `JobQueue.enqueue()` is itself idempotent for an id
            # already pending in the same lane, so this can never duplicate
            # delivery, and the CAS-guarded execution claim (`JobRepository
            # .transition_if_status()`) already makes a duplicate *delivery*
            # safe against duplicate *execution* regardless.
            decision = self._authorize_and_expose(batch_id, created.id, created.status)
            if decision in ("cancelled", "absent"):
                # A confirmed cancellation or a confirmed-deleted batch --
                # either way this row can never run; terminalize it now
                # rather than leave it queued-but-unexposed forever.
                if not is_terminal_status(created.status):
                    self.job_service.cancel_job(created.id)
                break
            if decision == "unreadable":
                # The batch's cancellation state could not be confirmed
                # right now -- must never be treated the same as "not
                # cancelled" (PR3 exact-HEAD audit, third round, P1-3).
                # Leave the row exactly as-is (materialized, unexposed) for
                # a later retry once storage recovers; do not guess either
                # way by cancelling or enqueuing it.
                break
            # decision in ("enqueued", "not_queued") -- proceed to the next
            # item in this stage.

        return self.reconcile(batch_id)

    def _authorize_and_expose(self, batch_id: str, job_id: str, job_status: str) -> str:
        """Atomically decide whether `job_id` may become worker-visible
        right now, and expose it if so.

        Runs under `BatchRepository.run_exclusive()` -- the exact same lock
        `cancel()`'s own durable-intent mutation uses -- so this decision
        and any concurrent `cancel()` call are strictly linearized: either
        `cancel()` fully commits `cancellation_requested=True` before this
        call's own read (which then correctly declines), or this call's
        `enqueue_job()` fully completes before `cancel()` even starts
        (which is legitimate -- that job started before any cancellation
        intent existed, and `cancel()`'s own cooperative `cancel_job()`
        loop is what reaches it from there). No lock-free "check, then act"
        window exists in between (PR3 exact-HEAD audit, third round: the
        prior round's separate pre/post checks still had exactly this gap).

        Returns one of:
        - ``"enqueued"`` -- exposed to the worker queue just now.
        - ``"not_queued"`` -- `job_status` was not `queued` (already active
          or terminal); nothing to do.
        - ``"cancelled"`` -- the batch's cancellation intent is durably
          set; never enqueued.
        - ``"absent"`` -- the batch record is confirmed gone.
        - ``"unreadable"`` -- the batch could not be read right now (a
          transient failure); the caller must not enqueue and must not
          assume either "cancelled" or "not cancelled".

        `enqueue_job()` is safe to call from inside this lock:
        `JobQueue.enqueue()` is itself idempotent for an id already pending
        in the same lane, and the ``"job_queued"`` event it publishes is
        not subscribed to by anything that calls back into this repository
        (`BatchService.handle_job_event`/`CompletionConverger.
        handle_job_event` both filter to terminal event types only), so
        there is no reentrancy or deadlock risk.
        """

        def _decide() -> str:
            record, uncertain = self.batch_repository.get_or_diagnose(batch_id)
            if record is None:
                return "unreadable" if uncertain else "absent"
            if record.cancellation_requested:
                return "cancelled"
            if job_status != JOB_STATUS_QUEUED:
                return "not_queued"
            self.job_service.enqueue_job(job_id)
            return "enqueued"

        return self.batch_repository.run_exclusive(_decide)

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
        return self.batch_repository.mutate(batch_id, self._recompute)

    def reconcile_child_job(
        self, job_id: str
    ) -> tuple[BatchRecord | None, BatchReconciliationOutcome]:
        """Reconcile the batch owning ``job_id`` from that job's own state.

        The same operation ``handle_job_event()`` performs for a terminal
        event, exposed directly for a caller with no event to react to --
        completion convergence reconciling from a job's persisted status,
        not from the event bus (see ``core.jobs.completion``) -- so a batch
        whose event was lost (a crash before the event bus delivered it, no
        subscriber attached yet, ...) still advances. Idempotent: recompute
        and stage-advance are themselves no-ops once nothing has changed,
        so calling this for an event that *was* already handled is safe.

        Returns ``(record_or_none, outcome)`` -- see
        ``BatchReconciliationOutcome`` for what each outcome means and why
        a caller must not collapse them to a single ``None``/not-``None``
        check (PR3 exact-HEAD audit P1-5): a transient failure reading the
        owning Batch must be reported as ``RETRYABLE_FAILURE``, never
        conflated with the genuine ``NO_PARENT`` case.

        The stage-advance mutation above can succeed (a new stage is
        persisted) while the *following* ``_enqueue_stage()`` call --
        materializing that new stage's children -- fails on its own
        transient read (PR3 exact-HEAD audit, second round, P1-2): without
        this final check, ``RECONCILED`` was returned unconditionally
        whenever the advance itself persisted, even if ``_enqueue_stage()``
        came back empty-handed. ``CompletionConverger`` treats
        ``RECONCILED`` as "nothing left to retry" and marks the *job's*
        completion done -- permanently excluding it from every future
        retry despite the batch's new stage never having been created.
        """

        refreshed, uncertain = self.batch_repository.mutate_by_job_id_diagnosed(
            job_id, self._recompute_and_advance_capturing
        )
        if refreshed is None:
            if uncertain:
                return None, BatchReconciliationOutcome.RETRYABLE_FAILURE
            return None, BatchReconciliationOutcome.NO_PARENT
        enqueued = self._enqueue_stage(refreshed.id, stage_index=refreshed.stage_index)
        if enqueued is None:
            # _enqueue_stage() failed to confirm/materialize the (possibly
            # just-advanced) stage. Positively confirm the batch is
            # actually gone -- not merely unreadable this instant -- before
            # ever reporting the non-retryable NO_PARENT outcome; anything
            # else (still exists, or currently unreadable) must stay
            # retryable, since the next stage may not have been
            # materialized.
            still_exists, still_uncertain = self.batch_repository.get_or_diagnose(
                refreshed.id
            )
            if still_exists is None and not still_uncertain:
                return None, BatchReconciliationOutcome.NO_PARENT
            return None, BatchReconciliationOutcome.RETRYABLE_FAILURE
        return enqueued, BatchReconciliationOutcome.RECONCILED

    def _recompute(self, record: BatchRecord | None) -> BatchRecord | None:
        if record is None:
            return None
        for item in record.items:
            if item.job_id is None:
                continue
            try:
                job = self.job_repository.get(item.job_id)
            except JobRecordDecodeError:
                # A poison row (PR #397's own quarantine primitives are
                # what eventually resolve this, via job-level startup
                # recovery / the runtime retry loop -- this method must
                # not re-derive that logic, only avoid raising because of
                # it). Leave this item's last-known state exactly as it
                # was: recomputing the batch's aggregate/status from a row
                # that cannot currently be decoded is exactly as safe as
                # recomputing it before this job's row was ever touched,
                # and never guesses at (or overwrites) its true past
                # outcome. Without this, one poisoned child could abort
                # reconciliation for every other batch in the same sweep
                # (PR3 exact-HEAD audit, third round, P1-5).
                logger.warning(
                    "Batch %s: child job %s could not be decoded during "
                    "reconciliation; leaving its last-known state as-is.",
                    record.id,
                    item.job_id,
                )
                continue
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
        return record

    # ----------------------------------------------------------- stage control

    def advance(self, batch_id: str) -> BatchRecord | None:
        record = self.batch_repository.mutate(batch_id, self._recompute_and_advance_raising)
        if record is None:
            return None
        enqueued = self._enqueue_stage(record.id, stage_index=record.stage_index)
        if enqueued is None:
            # The stage-advance mutation above already persisted -- this
            # batch is not simply "not found" -- but materializing the new
            # stage's children could not be confirmed (PR3 exact-HEAD
            # audit, third round, P1-6). Unlike `reconcile_child_job()`,
            # there is no runtime retry scheduled to pick this back up on
            # its own if the triggering condition was itself a completed
            # job: the caller here is a synchronous manual API call, so it
            # must learn about the failure now rather than receive a
            # normal-looking (but incompletely-materialized) result.
            still_exists, uncertain = self.batch_repository.get_or_diagnose(record.id)
            if still_exists is None and not uncertain:
                return None
            raise BatchStageMaterializationError(
                f"Batch {record.id!r} advanced to stage {record.stage_index}, "
                "but materializing its Job rows could not be confirmed "
                "right now (a transient storage failure); retry."
            )
        return self.reconcile(batch_id)

    def _recompute_and_advance_raising(self, record: BatchRecord | None) -> BatchRecord | None:
        """recompute(), then attempt to advance a stage, raising a reference
        preflight failure through to the caller -- the manual ``advance()``
        API's contract (the API route re-raises this as a 422); it must
        keep raising, unlike the event-driven path below.
        """

        if record is None:
            return None
        record = self._recompute(record)
        assert record is not None  # _recompute() only returns None for a None input
        return self._try_advance_in_place(record)

    def _recompute_and_advance_capturing(self, record: BatchRecord | None) -> BatchRecord | None:
        """Same attempt, but a reference preflight failure is encoded into
        ``advance_error``/``status`` on the record instead of raising.

        Used by the event-driven auto-advance path (``handle_job_event``/
        ``reconcile_child_job``), which runs on the job runner thread with
        no HTTP caller to hand a 4xx to (#201 follow-up, eleventh Codex
        round on PR #376) -- left uncaught, the batch would otherwise be
        stuck "running" forever: its current stage's items all terminal,
        but the next stage never created and no further job event ever
        retriggers this path.
        """

        if record is None:
            return None
        record = self._recompute(record)
        assert record is not None  # _recompute() only returns None for a None input
        try:
            return self._try_advance_in_place(record)
        except (UnsupportedReferenceError, MissingReferenceAssetError) as exc:
            # Setting only `status` here would not be durable -- the next
            # read calls _recompute(), whose _derive_status() has no
            # concept of "the stage transition itself failed" and would
            # recompute "running" from a successful current stage plus a
            # pending next one, silently reverting this. advance_error is
            # what _derive_status() actually checks.
            record.advance_error = str(exc)
            record.status = BATCH_STATUS_FAILED
            logger.warning(
                "Batch %s failed to advance past stage %d: %s",
                record.id,
                record.stage_index,
                exc,
            )
            return record

    def _try_advance_in_place(self, record: BatchRecord) -> BatchRecord:
        """Pure attempt to advance one stage in place; may raise a reference
        preflight failure (`UnsupportedReferenceError`/
        `MissingReferenceAssetError`) -- callers decide whether to let that
        propagate or catch it (see the two wrappers above).
        """

        stages = record.spec.resolved_stages()
        next_stage_index = record.stage_index + 1
        if next_stage_index >= len(stages):
            return record
        if record.cancellation_requested:
            # Durable cancellation intent suppresses creating a new stage,
            # even if every current-stage item just finished terminally.
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
        # are about to be persisted onto the batch record, so a later
        # item's reference failure must not leave an earlier one already
        # assigned a job id behind an exception the caller has no way to
        # partially undo.
        for new_item in new_items:
            self.job_service.validate_references(new_item.request, record.spec.project_id)
        # #201 follow-up (Codex P2, thirteenth round): the preflight above
        # passed, so this is either the first attempt or a retry after an
        # operator fixed whatever made an earlier attempt's preflight
        # raise. Clear any stale advance_error from that earlier attempt
        # now -- otherwise _derive_status() would keep forcing this batch
        # to "failed" forever even as real, live jobs get created for it.
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
        return record

    def cancel(self, batch_id: str) -> BatchRecord | None:
        def _apply_cancellation_intent(record: BatchRecord | None) -> BatchRecord | None:
            if record is None:
                return None
            # Durable intent first, in the same atomic save as marking any
            # item that will now never get a job id -- or never get its Job
            # row created -- as terminally cancelled; both must be
            # persisted before any child job is actually told to cancel
            # below, so a crash right after this save still leaves an
            # observable, resumable "cancellation was requested" record
            # (see resume_pending_cancellations()).
            record.cancellation_requested = True
            for item in record.items:
                if item.status == JOB_STATUS_CANCELLED:
                    continue
                if item.job_id is None:
                    item.status = JOB_STATUS_CANCELLED
                    continue
                # A stable child id can be durably persisted (see
                # _enqueue_stage()'s two-phase id-then-row split) before its
                # Job row is ever created -- a crash, or this exact
                # cancellation racing that creation, can leave it that way
                # permanently (PR3 exact-HEAD audit P1-3). Such an item can
                # never run: _assign_ids() only assigns an id to an item
                # that does not already have one, and _enqueue_stage()'s own
                # cancellation recheck (see above) now durably refuses to
                # create a row for it once this intent is set. cancel_job()
                # itself returns None for a missing row, so nothing would
                # ever terminalize it without this check -- it would stay
                # "pending" forever, and so would the batch.
                try:
                    row_exists = self.job_repository.get(item.job_id) is not None
                except JobRecordDecodeError:
                    # This item's row exists but cannot currently be
                    # decoded (found via adversarial review of the third
                    # round's own poison-tolerance fixes elsewhere in this
                    # method): must not be treated the same as "no row"
                    # (which would wrongly mark it cancelled based on a
                    # guess about undecodable content), and must not be
                    # allowed to abort this entire cancellation pass --
                    # `BatchRepository.mutate()` propagates any exception
                    # from this closure uncaught, which would otherwise
                    # durably lose the cancellation intent for every other
                    # item in the batch, and (via
                    # `resume_pending_cancellations()` ->
                    # `run_startup_recovery()`) crash the whole
                    # application's startup on every future restart until
                    # the row is fixed by hand. Leave this item's status
                    # exactly as-is; PR #397's own quarantine primitives
                    # (via job-level startup recovery) are what eventually
                    # resolve a poisoned row.
                    logger.warning(
                        "Batch %s: child job %s's row could not be "
                        "decoded while applying cancellation intent; "
                        "leaving its status as-is.",
                        record.id,
                        item.job_id,
                    )
                    continue
                if not row_exists:
                    item.status = JOB_STATUS_CANCELLED
            return record

        record = self.batch_repository.mutate(batch_id, _apply_cancellation_intent)
        if record is None:
            return None

        for item in record.items:
            if item.job_id is None or is_terminal_status(item.status):
                continue
            try:
                self.job_service.cancel_job(item.job_id)
            except JobRecordDecodeError:
                # JobService.cancel_job() itself reads the row first
                # (`get_job()`) before ever mutating anything -- a poisoned
                # row raises here before any state is touched, so simply
                # skipping it is safe (found via adversarial review of
                # this same round's poison-tolerance fixes: the closure
                # above already tolerates this exact row, but this second,
                # outside-the-lock loop calls a completely different
                # method that performs its own unguarded read and was
                # still able to abort cancellation of every other item).
                logger.warning(
                    "Batch %s: child job %s's row could not be decoded "
                    "while issuing its cancel signal; leaving it for a "
                    "later retry.",
                    record.id,
                    item.job_id,
                )
                continue

        return self.reconcile(batch_id)

    def resume_pending_cancellations(self) -> tuple[list[BatchRecord], bool]:
        """Re-apply cancellation to every batch whose durable intent is set.

        For startup recovery: a crash between persisting
        ``cancellation_requested`` and actually telling every child job to
        cancel must not leave those children running forever. Safe to call
        any number of times -- ``cancel()`` re-marking an
        already-``cancellation_requested`` batch, and
        ``JobService.cancel_job()`` re-cancelling an already-cancelled or
        already-``cancel_requested`` job, are both themselves idempotent
        no-ops.

        Returns ``(resumed, scan_was_fully_reliable)``. Uses the tolerant
        scan (``BatchRepository.list_all_tolerant()``), not ``list_all()``:
        the latter silently *skips* a batch file that hits a transient
        ``OSError`` while being read, which could hide one with a durable
        ``cancellation_requested=True`` -- startup recovery would then
        proceed as if there were nothing left to resume and re-enqueue that
        batch's still-queued children, running generation despite a
        persisted cancel intent it never got the chance to see (PR3
        exact-HEAD audit P1-6). ``scan_was_fully_reliable=False`` tells the
        caller exactly that: a transient read failure occurred, so "no
        cancellation intent found" cannot be trusted this pass.
        """

        records, _malformed_ids, scan_was_fully_reliable = (
            self.batch_repository.list_all_tolerant()
        )
        resumed: list[BatchRecord] = []
        fully_reliable = scan_was_fully_reliable
        for record in records:
            if not record.cancellation_requested:
                continue
            refreshed = self.cancel(record.id)
            if refreshed is not None:
                resumed.append(refreshed)
                continue
            # `cancel()` failed to confirm re-application of a durable
            # cancellation intent this exact tolerant scan already
            # discovered (PR3 exact-HEAD audit, third round, P1-4): either
            # the batch was genuinely deleted in the interim (fine --
            # nothing left to cancel), or `cancel()`'s own fresh
            # read/mutate hit a transient failure (not fine -- the
            # cancellation this record already told us about was never
            # actually reapplied to its children this pass). Only a
            # *confirmed* deletion may be treated as "no problem" here;
            # anything else must downgrade the overall result, since a
            # caller trusting `scan_was_fully_reliable=True` (see
            # `run_startup_recovery()`'s queued-job sweep) would otherwise
            # re-enqueue this exact batch's still-queued children despite
            # its cancellation intent never having reached them.
            still_exists, uncertain = self.batch_repository.get_or_diagnose(record.id)
            if still_exists is not None or uncertain:
                fully_reliable = False
        return resumed, fully_reliable

    def resume_current_stage_for_all_batches(self) -> list[BatchRecord]:
        """Resume every batch's current stage -- for startup recovery.

        A crash can leave a Batch record persisted (``create_batch()``'s
        ``BatchRepository.create()`` call committed) with no child Job rows
        created yet at all, or with a stable child id persisted
        (``_enqueue_stage()``'s phase 1) but the Job row for it never
        created (phase 2 never ran, or crashed partway through) (PR3
        exact-HEAD audit P1-1). Nothing in an ordinary
        ``list_batches()``/``reconcile()`` pass will ever notice or fix
        this: ``_recompute()`` only reads *existing* job rows, it never
        creates one, and the in-memory ``JobQueue`` a ``queued`` row would
        otherwise be re-enqueued onto does not survive a restart anyway.
        Without this, such a batch stays permanently stuck pending.

        Safe to call unconditionally, for every batch, on every startup:
        ``_enqueue_stage()`` is itself idempotent (phase 1 only assigns an
        id to an item that does not already have one; phase 2's
        create-or-reuse never creates a second job for an id that already
        has one), and it already refuses to materialize anything for a
        batch whose durable ``cancellation_requested`` is set.
        """

        resumed: list[BatchRecord] = []
        for record in self.batch_repository.list_all():
            refreshed = self._enqueue_stage(record.id, stage_index=record.stage_index)
            if refreshed is not None:
                resumed.append(refreshed)
        return resumed

    def promote(self, batch_id: str, item_id: str) -> BatchRecord | None:
        def _mark_promoted_and_recompute(record: BatchRecord | None) -> BatchRecord | None:
            if record is None:
                return None
            item = next((entry for entry in record.items if entry.id == item_id), None)
            if item is None:
                raise LookupError(f"Unknown batch item: {item_id}")
            item.promoted = True
            return self._recompute(record)

        return self.batch_repository.mutate(batch_id, _mark_promoted_and_recompute)

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
            self.reconcile_child_job(job_id)
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
    if has_further_stage and aggregate.succeeded and not record.cancellation_requested:
        # The current stage is done but the run is not: keep it "running" so the
        # UI does not claim completion before the refine pass exists. Not when
        # cancellation was requested, though -- that next stage will never be
        # created (see _try_advance_in_place), so waiting for it would leave
        # the batch "running" forever instead of reaching a terminal status.
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


__all__ = [
    "BatchReconciliationOutcome",
    "BatchService",
    "BatchStageMaterializationError",
    "resolve_max_items_limit",
]
