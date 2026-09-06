"""PR3: startup recovery -- reconcile persisted state before the worker starts.

Runs once, synchronously, between acquiring data-directory ownership /
constructing services and starting the job runner thread (see
``apps/api/main.py``). Order matters and is deliberately fixed:

1. Row-level poison scan (tolerant of one bad row -- see
   ``JobRepository.list_tolerant()``) quarantines/repairs anything that
   cannot even be decoded.
2. Batch-persisted cancellation intent is re-applied (a crash between
   persisting ``cancellation_requested`` and actually cancelling every
   child must not leave those children running forever).
3. Every interrupted job (``preparing``/``running``/``postprocessing`` --
   the process that owned it is confirmed dead) resolves to ``failed``;
   every ``cancel_requested`` job (same reasoning) resolves to
   ``cancelled``. Never re-runs a generator.
4. Completion convergence runs for every terminal job whose
   ``completion_state`` is still ``"pending"`` -- including jobs this very
   pass just finalized in step 3, and any older ``succeeded``/``failed``/
   ``cancelled`` job whose Asset sync / Story replay / Batch reconciliation
   never completed.
5. Every remaining batch gets one reconcile pass, so a batch whose
   completion-convergence step (4) never touched it directly still reflects
   its children's now-final state.
6. Every job still (confirmed fresh) ``queued`` is re-enqueued under its
   existing id -- never a new one; ``JobQueue`` is purely in-memory and does
   not survive a restart on its own.

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
    JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
)

if TYPE_CHECKING:
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
        resumed = batch_service.resume_pending_cancellations()
        report.batches_resumed_cancelling = [record.id for record in resumed]
        # cancel_job() may have moved some of this pass's `records` from an
        # active status to cancel_requested; reread once more before step 3
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
