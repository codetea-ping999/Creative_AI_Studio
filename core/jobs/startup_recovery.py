"""PR3: startup recovery -- reconcile persisted state before the worker starts.

Runs once, synchronously, between acquiring data-directory ownership /
constructing services and starting the job runner thread (see
``apps/api/main.py``). Order matters and is deliberately fixed:

1. Row-level poison scan (tolerant of one bad row -- see
   ``JobRepository.list_tolerant()``) quarantines/repairs anything that
   cannot even be decoded.
2. Batch-persisted cancellation intent is re-applied (a crash between
   persisting ``cancellation_requested`` and actually cancelling every
   child must not leave those children running forever). Uses a tolerant
   scan that reports whether it was fully reliable -- a transient failure
   reading any batch file here could be hiding a durable cancellation
   intent, which step 6 below must not silently ignore.
2b. Every batch's current stage is resumed/materialized (a crash can leave
   a batch record persisted, or a stable child id persisted, with its Job
   row never actually created -- nothing in an ordinary reconcile pass
   would ever notice or fix that).
3. Every interrupted job (``preparing``/``running``/``postprocessing`` --
   the process that owned it is confirmed dead) resolves to ``failed``;
   every ``cancel_requested`` job (same reasoning) resolves to
   ``cancelled``. Never re-runs a generator.
4. Completion convergence runs for every terminal job whose
   ``completion_state`` is still ``"pending"`` -- including jobs this very
   pass just finalized in step 3, and any older ``succeeded``/``failed``/
   ``cancelled`` job whose Asset sync / Story replay / Batch reconciliation
   never completed.
4b. An Asset-repair pass runs for every decodable *succeeded* job,
   independent of ``completion_state`` -- step 4 alone intentionally skips
   a job already ``completion_state="done"``, so it cannot repair an Asset
   record lost or corrupted *after* that convergence already ran once.
   Never calls a generator; never touches ``completion_state``/Story/Batch
   state.
5. Every remaining batch gets one reconcile pass, so a batch whose
   completion-convergence step (4) never touched it directly still reflects
   its children's now-final state.
6. Every job still (confirmed fresh) ``queued`` is re-enqueued under its
   existing id -- never a new one; ``JobQueue`` is purely in-memory and does
   not survive a restart on its own. Skipped entirely if step 2's scan was
   not fully reliable (see step 2's note).

``classify_job()`` (``core/jobs/recovery.py``) stays pure classification;
none of its logic is duplicated or second-guessed here -- this module is the
mutation/enqueue layer classify_job's own docstring says a later PR would be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from .completion import CompletionOutcome
from .statuses import (
    JOB_STATUS_CANCEL_REQUESTED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
)

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.batches import BatchService
    from core.storage.repositories.job_repository import JobRepository

    from .completion import CompletionConverger
    from .service import JobService

logger = logging.getLogger(__name__)

_INTERRUPTED_STATUSES = (JOB_STATUS_PREPARING, JOB_STATUS_RUNNING, JOB_STATUS_POSTPROCESSING)

_PROCESS_INTERRUPTED_REASON = (
    "process_interrupted: job was still active when the process restarted; "
    "the worker that owned it is confirmed gone."
)


@dataclass
class StartupRecoveryReport:
    """What one `run_startup_recovery()` pass actually did -- for logging/tests."""

    poison_rows: dict[str, str] = field(default_factory=dict)
    interrupted_failed: list[str] = field(default_factory=list)
    cancel_requested_cancelled: list[str] = field(default_factory=list)
    completion_outcomes: dict[str, CompletionOutcome] = field(default_factory=dict)
    requeued: list[str] = field(default_factory=list)
    batches_resumed_cancelling: list[str] = field(default_factory=list)
    batches_resumed_current_stage: list[str] = field(default_factory=list)
    batch_cancellation_scan_was_fully_reliable: bool = True
    queued_enqueue_skipped_due_to_unreliable_batch_scan: bool = False
    assets_repaired: int = 0


def run_startup_recovery(
    job_repository: "JobRepository",
    job_service: "JobService",
    completion_converger: "CompletionConverger",
    *,
    batch_service: "BatchService | None" = None,
) -> StartupRecoveryReport:
    report = StartupRecoveryReport()

    # 1. Row-level poison scan -- one bad row never aborts the rest.
    records, failures = job_repository.list_tolerant()
    for job_id, exc in failures:
        report.poison_rows[job_id] = _quarantine_poison_row_safely(job_repository, job_id, exc)

    # A quarantined/repaired row's *current* state can only be known by
    # rereading it -- `records` above still reflects the pre-quarantine
    # content for any id that failed to decode the first time (and simply
    # omits it if quarantine also failed transiently). Refresh via a second
    # tolerant scan rather than trying to patch `records` in place.
    records, _second_pass_failures = job_repository.list_tolerant()

    # 2. Re-apply any batch's durable cancellation intent before touching
    # individual interrupted jobs below, so a batch's own children are
    # cancelled (not left to fail with a generic "process_interrupted").
    if batch_service is not None:
        resumed, scan_was_fully_reliable = batch_service.resume_pending_cancellations()
        report.batches_resumed_cancelling = [record.id for record in resumed]
        report.batch_cancellation_scan_was_fully_reliable = scan_was_fully_reliable

        # 2b. Resume/materialize every batch's current stage: a crash can
        # leave a batch record persisted with a child id assigned but its
        # Job row never created (or never created at all) -- nothing in an
        # ordinary reconcile pass ever notices or fixes that (PR3
        # exact-HEAD audit P1-1). Each batch's own `_enqueue_stage()` call
        # re-reads that exact batch fresh before deciding to materialize
        # anything, so this step needs no reliability gate of its own: a
        # batch this pass cannot currently read is simply skipped by
        # `list_all()`, not acted on incorrectly.
        stage_resumed = batch_service.resume_current_stage_for_all_batches()
        report.batches_resumed_current_stage = [record.id for record in stage_resumed]

        # cancel_job() may have moved some of this pass's `records` from an
        # active status to cancel_requested, and resuming a stage above may
        # have created brand-new queued rows; reread once more before step 3
        # classifies them.
        records, _third_pass_failures = job_repository.list_tolerant()

    # 3. Interrupted -> failed, cancel_requested -> cancelled. Never
    # re-runs a generator: this is a pure state transition using the exact
    # same CAS primitive the execution claim itself uses.
    for job in records:
        if job.status in _INTERRUPTED_STATUSES:
            if job_repository.transition_if_status(
                job.id,
                (job.status,),
                status=JOB_STATUS_FAILED,
                progress=1.0,
                error_message=_PROCESS_INTERRUPTED_REASON,
            ):
                report.interrupted_failed.append(job.id)
        elif job.status == JOB_STATUS_CANCEL_REQUESTED:
            if job_repository.transition_if_status(
                job.id,
                (JOB_STATUS_CANCEL_REQUESTED,),
                status=JOB_STATUS_CANCELLED,
                progress=1.0,
            ):
                report.cancel_requested_cancelled.append(job.id)

    # 4. Completion convergence for every terminal job still pending --
    # including everything step 3 just finalized, and any older terminal
    # job whose convergence never completed. list_terminal_pending_completion()
    # already filters at the SQL level and silently skips a poison row
    # (step 1 already handled those).
    for job in job_repository.list_terminal_pending_completion():
        report.completion_outcomes[job.id] = completion_converger.converge_job(job.id)

    # 4b. Asset repair, independent of completion_state (PR3 exact-HEAD
    # audit, third round, P2-1) -- see _repair_assets_for_succeeded_jobs()'s
    # own docstring for why step 4 alone cannot provide this guarantee.
    report.assets_repaired = _repair_assets_for_succeeded_jobs(
        job_repository, completion_converger.asset_repository
    )

    # 5. One reconcile pass over every batch, so a batch not directly
    # touched by step 4 (e.g. all its children were already "done" before
    # this restart, but the batch record itself was never re-read) still
    # reflects current child state.
    if batch_service is not None:
        batch_service.list_batches()

    # 6. Re-enqueue every job that is, on a fresh read right now, still
    # queued -- under its existing id, never a new one. JobQueue is
    # in-memory only, so every restart loses every pending item from it
    # regardless of how it got there (a plain create_job() or a batch's
    # stable child id). list_tolerant(), not list(): an unrelated poison
    # row elsewhere in the table (already handled by step 1, or one this
    # exact pass's step 1 quarantine attempt itself failed transiently on)
    # must never block re-enqueuing every other, perfectly healthy queued
    # job.
    #
    # Skipped entirely if step 2's batch-cancellation scan was not fully
    # reliable (PR3 exact-HEAD audit P1-6): a transient failure reading
    # some batch file there could be hiding a durable
    # `cancellation_requested=True` this pass never got the chance to
    # apply to that batch's children. Re-enqueuing a queued job right now
    # cannot distinguish "genuinely fine to run" from "belongs to a batch
    # whose cancellation intent this pass silently missed" -- a queued job
    # left un-enqueued is always safely resumable (nothing about it is
    # lost; the very next startup, or a later retry once storage recovers,
    # tries again), whereas running generation for an already-cancelled
    # batch is not undoable.
    if batch_service is not None and not report.batch_cancellation_scan_was_fully_reliable:
        report.queued_enqueue_skipped_due_to_unreliable_batch_scan = True
        return report

    queued_candidates, _requeue_scan_failures = job_repository.list_tolerant()
    for job in queued_candidates:
        if job.status != JOB_STATUS_QUEUED:
            continue
        try:
            fresh = job_repository.get(job.id)
        except Exception:
            continue
        if fresh is None or fresh.status != JOB_STATUS_QUEUED:
            continue
        job_service.enqueue_job(job.id)
        report.requeued.append(job.id)

    return report


def _repair_assets_for_succeeded_jobs(
    job_repository: "JobRepository", asset_repository: "AssetRepository"
) -> int:
    """Idempotently re-sync every decodable succeeded job's Asset records.

    Independent of `completion_state` -- PR3's regular convergence pass
    (`CompletionConverger.converge_job()`) intentionally skips a job once
    `completion_state="done"`, so it alone cannot repair an Asset record
    lost or corrupted *after* that convergence already ran once. The
    pre-PR3 startup path did exactly this for every succeeded job
    unconditionally (`asset_repository.sync_jobs(job_repository.list())`);
    this restores that guarantee without reviving the old undifferentiated
    call: `list_tolerant()`, not `list()`, so one poison row never aborts
    repair for every other succeeded job (PR3 exact-HEAD audit, third
    round, P2-1).

    Never calls a generator (`AssetRepository.sync_job()` only reads the
    already-persisted `GenerationResult`), never touches
    `completion_state`/`completion_error` (repairing an Asset is
    orthogonal to whether convergence itself is "done"), and never touches
    Story/Batch state (`sync_job()` is Asset-only). `sync_job()` is itself
    idempotent (it derives each Asset's id deterministically from the job
    id and output path), so re-running this on every single startup for
    every succeeded job ever created is safe, not just safe-once.

    Returns the count of Asset *records* (re-)synced -- not the count of
    jobs: one succeeded job can have more than one output and therefore
    more than one Asset record (see `AssetRepository.sync_job()`, which
    returns one Asset per output), and undercounting those by job would
    understate the true repair blast radius reported to an operator
    (found via adversarial review of this same round's own fix).
    """

    records, _failures = job_repository.list_tolerant()
    repaired = 0
    for job in records:
        if job.status != JOB_STATUS_SUCCEEDED:
            continue
        try:
            synced_assets = asset_repository.sync_job(job)
        except Exception:
            logger.exception(
                "Startup asset repair failed for job %s; continuing.", job.id
            )
            continue
        repaired += len(synced_assets)
    return repaired


def _quarantine_poison_row_safely(
    job_repository: "JobRepository", job_id: str, exc: Exception
) -> str:
    """Best-effort resolution for one row `list_tolerant()` could not decode.

    Never raises: a transient failure attempting the write itself is caught
    here and reported distinctly ("transient_write_failure"), not silently
    treated as resolved -- the row is left exactly as it was for a later
    retry (a runtime retry pass, or the next full restart) rather than
    aborting the rest of this scan.
    """

    reason = f"Startup recovery: row could not be reconstructed: {exc}"
    try:
        raw_status = job_repository.get_raw_status(job_id)
        if raw_status is None:
            return "missing"
        if raw_status == JOB_STATUS_CANCEL_REQUESTED:
            ok = job_repository.transition_if_status(
                job_id,
                (JOB_STATUS_CANCEL_REQUESTED,),
                status=JOB_STATUS_CANCELLED,
                progress=1.0,
                error_message=reason,
            )
            return "cancelled" if ok else "already_resolved"
        if raw_status in _INTERRUPTED_STATUSES or raw_status == JOB_STATUS_QUEUED:
            ok = job_repository.transition_if_status(
                job_id,
                (raw_status,),
                status=JOB_STATUS_FAILED,
                progress=1.0,
                error_message=reason,
            )
            return "failed" if ok else "already_resolved"
        if raw_status in TERMINAL_JOB_STATUSES:
            # Already a real terminal outcome (succeeded/failed/cancelled)
            # -- do not forcibly overwrite it just because the row cannot
            # be decoded right now (its payload/timestamp is the part that
            # is broken, not necessarily its status). Left as-is and
            # logged; it stays unreconciled for completion convergence's
            # purposes but its generation-level outcome is preserved.
            logger.warning(
                "Job %s is already terminal (%s) but its row could not be "
                "decoded during startup recovery: %s",
                job_id,
                raw_status,
                exc,
            )
            return "left_untouched_terminal"
        if raw_status not in JOB_STATUSES:
            ok = job_repository.quarantine_structurally_invalid_status(
                job_id, error_message=reason
            )
            return "quarantined_invalid_status" if ok else "already_resolved"
        return "left_untouched"  # pragma: no cover - JOB_STATUSES is exhaustive
    except Exception:
        logger.exception(
            "Transient failure while quarantining poison job %s during "
            "startup recovery; leaving it for a later retry.",
            job_id,
        )
        return "transient_write_failure"


__all__ = ["StartupRecoveryReport", "run_startup_recovery"]
