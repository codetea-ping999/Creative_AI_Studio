"""Queue consumer that executes generation jobs."""

from __future__ import annotations

from enum import Enum
import logging
from threading import Event
import time
from typing import TYPE_CHECKING

from generators.registry import GeneratorRegistry

from core.schemas import GenerationStatus

from .cancellation import CancellationRegistry
from .context import GenerationCancelled, GenerationContext
from .events import EventBus
from .service import JobService

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.storage.repositories.job_repository import JobRepository
    from .queue import JobQueue
from .schemas import JobRecord
from .statuses import (
    JOB_STATUS_CANCEL_REQUESTED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    is_terminal_status,
)

# The generator's own progress fraction (0.0-1.0) is mapped into this slice of
# the job's overall progress; PREPARING/POSTPROCESSING bracket it on either
# side (see process_job).
_RUNNING_PROGRESS_START = 0.1
_RUNNING_PROGRESS_SPAN = 0.8

logger = logging.getLogger(__name__)


class _QuarantineOutcome(Enum):
    """Result of a `JobRunner._quarantine_poison_job()` attempt.

    A plain bool would conflate two different reasons a caller must NOT
    requeue (already settled, one way or another) with the one reason it
    MUST (the quarantine write itself failed) -- see `run_once()`.
    """

    QUARANTINED = "quarantined"
    ALREADY_RESOLVED = "already_resolved"
    WRITE_FAILED = "write_failed"


class JobRunner:
    """Single-worker job runner for queued generation requests."""

    def __init__(
        self,
        job_repository: JobRepository,
        job_queue: "JobQueue",
        generator_registry: GeneratorRegistry,
        event_bus: EventBus | None = None,
        asset_repository: AssetRepository | None = None,
        job_service: JobService | None = None,
        cancellation_registry: CancellationRegistry | None = None,
    ) -> None:
        self.job_repository = job_repository
        self.job_queue = job_queue
        self.generator_registry = generator_registry
        self.event_bus = event_bus
        self.asset_repository = asset_repository
        self.cancellation_registry = cancellation_registry
        # Terminal transitions (success/failure) are delegated to JobService so
        # the completion path lives in exactly one place. When callers do not
        # inject a shared service we build an equivalent one from our own deps.
        self.job_service = job_service or JobService(
            job_repository,
            job_queue,
            event_bus,
            asset_repository=asset_repository,
        )

    def run_once(self, lane: str | None = None) -> JobRecord | None:
        # #180: `lane=None` dequeues from the queue's implicit single lane,
        # exactly as before lane routing existed -- required for every
        # existing single-lane caller (bootstrap/factories.py, tests) to keep
        # working unchanged. Pulling from a *specific* lane (for independent
        # per-lane workers) is #181's concern, not this one; this parameter
        # only exposes the queue's own lane addressing to a caller that wants
        # it, it does not add any concurrency or fairness policy here.
        job_id = self.job_queue.dequeue(lane=lane)
        if job_id is None:
            return None
        try:
            return self.process_job(job_id)
        except Exception as exc:
            # process_job() only ever re-raises here for a failure that
            # happened *before* this worker won the execution claim -- its
            # own except clause resolves every failure after that claim to a
            # terminal/observable job state via _resolve_claim_failure and
            # never re-raises (see the `claimed` guard there). A pre-claim
            # failure never touched ownership, but dequeue() above has
            # already popped this job id out of the in-memory queue; without
            # putting it back here, a perfectly healthy `queued` row would
            # have nothing left to ever redeliver it (post-#395 audit, P2).
            if self._is_permanent_pre_claim_failure(exc):
                # Codex exact-HEAD review on the P2 fix above: a *permanent*
                # failure (the persisted row itself can never be
                # deserialized/validated) would retry forever if requeued --
                # the exact same bytes fail identically every time, so this
                # would starve every other job behind it in this lane and
                # log without bound. Quarantine instead of requeuing.
                outcome = self._quarantine_poison_job(job_id, exc)
                if outcome is _QuarantineOutcome.WRITE_FAILED:
                    # Second Codex round: the quarantine *write* itself can
                    # fail transiently (e.g. a SQLite lock/busy/I-O error --
                    # unrelated to the row's content). Swallowing that would
                    # recreate exactly the "durable queued row with no queue
                    # entry" condition this whole fix exists to prevent, so
                    # fall through to the same transient requeue path used
                    # below for every other pre-claim failure.
                    self.job_queue.enqueue(job_id, lane=lane)
                    logger.error(
                        "Job %s is permanently unprocessable (%s) but its "
                        "quarantine write failed transiently; requeued so "
                        "it is not lost.",
                        job_id,
                        type(exc).__name__,
                    )
                    raise
                # QUARANTINED (queued -> failed committed) or
                # ALREADY_RESOLVED (something else moved the row off
                # `queued` before the quarantine CAS ran) both mean this job
                # id must not be requeued -- it is settled, one way or
                # another.
                logger.error(
                    "Job %s has a permanent pre-claim failure (%s) and will "
                    "not be retried (%s).",
                    job_id,
                    type(exc).__name__,
                    outcome.value,
                )
                raise
            # Transient (a SQLite operational/locking/I-O error carries no
            # information about the row's content, unlike a deserialization
            # or validation failure -- see _is_permanent_pre_claim_failure):
            # requeue into the exact lane it came from -- not the default
            # lane -- so this holds for a future multi-lane configuration
            # too, not just today's single implicit lane.
            self.job_queue.enqueue(job_id, lane=lane)
            logger.exception(
                "Job %s failed before execution claim; requeued for redelivery.",
                job_id,
            )
            raise

    def _is_permanent_pre_claim_failure(self, exc: BaseException) -> bool:
        """Classify a pre-claim failure as permanent (never retry) vs. transient.

        The only two realistic sources of a pre-claim exception are
        `JobRepository.get()` and `transition_if_status()`'s raw SQL
        execution. `get()` normalizes every failure that can only be caused
        by *this row's own persisted content*, at any stage of
        reconstruction -- malformed JSON, a payload schema the current
        models no longer accept, JSON nested deep enough to overflow the
        decoder's C stack, a malformed timestamp, or the row as a whole
        failing `JobRecord`'s own validation (an invalid persisted
        status/media_type, an out-of-range progress, etc.) -- into one
        `JobRecordDecodeError` (see `JobRepository._row_to_record`); the
        exact same bytes will fail identically on every future read, so
        requeuing is pure worker starvation with no chance of ever
        succeeding. Classifying on that one boundary type, rather than on
        `ValueError` directly, is deliberate: `RecursionError` (not a
        `ValueError`) is one of the content-caused failures `get()` already
        normalizes, and this must never accidentally widen to catch an
        unrelated `ValueError`/`RuntimeError` a future change might raise
        for a completely different, non-content reason.
        `transition_if_status()` never touches row content at all (a plain
        parameterized `UPDATE`), so a failure there is an execution-level
        problem -- typically `sqlite3.Error` (locked, busy, disk I/O) --
        with no bearing on the row's content, and is exactly the case
        retrying is *for*.
        """

        # Deferred import: core/jobs/* deliberately avoids a top-level
        # runtime import of core.storage.repositories.job_repository
        # elsewhere in this module and in service.py (see their
        # TYPE_CHECKING-only imports of JobRepository) to keep this
        # package's import graph one-directional; this is only needed at
        # call time, well after both modules have finished loading.
        from core.storage.repositories.job_repository import JobRecordDecodeError

        return isinstance(exc, JobRecordDecodeError)

    def _quarantine_poison_job(
        self, job_id: str, exc: BaseException
    ) -> _QuarantineOutcome:
        """Best-effort terminal resolution for a job that can never be retried.

        Returns the outcome instead of swallowing it, so the caller can
        tell a genuinely-settled poison job (quarantined, or already
        resolved by something else) apart from a quarantine *write* that
        itself failed -- the latter must still be requeued by the caller,
        or this best-effort step would recreate the exact "durable queued
        row with no queue entry" condition this whole fix exists to prevent
        (Codex exact-HEAD review: the prior version of this method
        unconditionally swallowed that exception here).

        Uses `transition_if_status()` -- the CAS primitive that deliberately
        never rereads the row after its UPDATE commits -- rather than
        `JobService.mark_failed()`, since the very failure being quarantined
        can be a persisted payload that makes any read-back of this same row
        (including `mark_failed()`'s own trailing reread) raise identically.
        A job only ever reaches here still `queued` (see `process_job`'s own
        pre-claim status checks), so `queued -> failed` is the only expected
        source transition.
        """

        try:
            quarantined = self.job_repository.transition_if_status(
                job_id,
                (JOB_STATUS_QUEUED,),
                status=JOB_STATUS_FAILED,
                progress=1.0,
            )
        except Exception:
            logger.exception(
                "Could not quarantine poison job %s; its quarantine write "
                "itself failed.",
                job_id,
            )
            return _QuarantineOutcome.WRITE_FAILED
        if not quarantined:
            return _QuarantineOutcome.ALREADY_RESOLVED
        self._publish(
            "job_failed",
            {
                "job_id": job_id,
                "status": JOB_STATUS_FAILED,
                "progress": 1.0,
                "error_message": str(exc),
            },
        )
        return _QuarantineOutcome.QUARANTINED

    def run_forever(
        self,
        *,
        poll_interval_seconds: float = 0.1,
        stop_event: Event | None = None,
        lane: str | None = None,
    ) -> None:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                job = self.run_once(lane=lane)
            except Exception:
                # One job's post-completion side effect must not kill the
                # consumer or cause this already-dequeued job to be retried.
                logger.exception("Job runner iteration failed; continuing.")
                if stop_event is None:
                    time.sleep(max(0.1, poll_interval_seconds))
                else:
                    stop_event.wait(max(0.1, poll_interval_seconds))
                continue
            if job is None:
                if stop_event is None:
                    time.sleep(poll_interval_seconds)
                else:
                    stop_event.wait(poll_interval_seconds)

    def process_job(self, job_id: str) -> JobRecord | None:
        job = self.job_repository.get(job_id)
        if job is None:
            logger.warning("Skipping missing job %s.", job_id)
            return None
        if job.status == JOB_STATUS_CANCELLED:
            return job
        if job.status != JOB_STATUS_QUEUED:
            logger.warning(
                "Skipping job %s because status %s is not runnable.",
                job.id,
                job.status,
            )
            return job

        cancellation_token = None
        claimed = False
        try:
            # This compare-and-set is the execution claim. Do not put an
            # in-memory cancellation entry in the registry before owning it.
            if not self._claim_job(job_id):
                return self.job_repository.get(job_id)
            # This is the execution-ownership boundary.  Do it before the
            # fallible reread below: SQLite has already committed the CAS.
            claimed = True
            claimed_job = self.job_repository.get(job_id)
            if claimed_job is None:
                raise RuntimeError(f"Claimed job {job_id!r} disappeared.")
            self._publish(
                self._event_name_for_status(JOB_STATUS_PREPARING),
                {
                    "job_id": claimed_job.id,
                    "status": JOB_STATUS_PREPARING,
                    "progress": 0.0,
                },
            )
            if self.cancellation_registry is not None:
                cancellation_token = self.cancellation_registry.begin(job_id)
            context = self._begin_context(
                job_id,
                project_id=job.project_id,
                cancellation_token=cancellation_token,
            )
            # A cancel can land between claim and registration. The database
            # remains authoritative, so observe it before doing more work.
            if self._is_cancelled(job_id):
                return self._finalize_cancellation(job_id)
            try:
                generator = self.generator_registry.get(job.media_type, job.request.task_type)
                if self._update_status(job_id, JOB_STATUS_RUNNING, progress=0.1) is None:
                    return self._finalize_cancellation(job_id)
                controlled_run = getattr(generator, "run_with_control", None)
                if callable(controlled_run):
                    # Long-form audio opts into segment-level progress/cancel
                    # instead of the generic context below.
                    result = controlled_run(
                        job.request,
                        progress_callback=lambda fraction, segment, segment_count: (
                            self._report_segment_progress(
                                job_id,
                                fraction,
                                segment=segment,
                                segment_count=segment_count,
                            )
                        ),
                        cancel_requested=lambda: self._is_cancelled(job_id),
                    )
                else:
                    # Generators that accept ``context`` can report step-level
                    # progress and observe cancellation mid-flight by raising
                    # GenerationCancelled; generators that ignore it fall back to
                    # cancellation being honored only at the boundary below.
                    result = generator.run(job.request, context)
            except GenerationCancelled:
                # A generator raises this after observing cancellation via
                # `context.raise_if_cancelled()` / `cancel_requested()`; the
                # job is `cancel_requested` at this point (#207), so resolve
                # it to the terminal `cancelled` state.
                return self._finalize_cancellation(job_id)
            except Exception as exc:
                if self._is_cancelled(job_id):
                    return self._finalize_cancellation(job_id)
                return self.job_service.mark_failed(job_id, str(exc))

            if self._is_cancelled(job_id):
                return self._finalize_cancellation(job_id)
            if self._update_status(job_id, JOB_STATUS_POSTPROCESSING, progress=0.9) is None:
                return self._finalize_cancellation(job_id)
            completion = self.job_service.mark_succeeded(job_id, result)
            if completion is not None and completion.status == JOB_STATUS_CANCEL_REQUESTED:
                # mark_succeeded()'s CAS only ever commits from postprocessing
                # (see _SUCCEEDABLE_JOB_STATUSES), so losing it while this
                # worker still holds execution ownership means a cancel won
                # the race in the instant between the postprocessing
                # transition above and this call -- the only other edge out
                # of postprocessing (statuses.py's ALLOWED_TRANSITIONS). That
                # cancellation is authoritative, but nothing else will ever
                # resolve cancel_requested to a terminal state once this
                # worker's `finally` below tears down its cancellation
                # registration and gives up ownership. As the owner of
                # record for this race, this worker -- not
                # JobService.mark_succeeded()'s general contract, which stays
                # a no-op for every other CAS-miss reason -- is responsible
                # for finalizing it here, exactly like every other
                # cancellation checkpoint in this method.
                return self._finalize_cancellation(job_id)
            return completion
        except Exception as exc:
            if not claimed:
                raise
            return self._resolve_claim_failure(job_id, exc)
        finally:
            if self.cancellation_registry is not None and cancellation_token is not None:
                self.cancellation_registry.end(job_id, cancellation_token)

    def _claim_job(
        self,
        job_id: str,
    ) -> bool:
        """Return whether this runner won the persisted execution claim."""

        return self.job_repository.transition_if_status(
            job_id,
            (JOB_STATUS_QUEUED,),
            status=JOB_STATUS_PREPARING,
            progress=0.0,
        )

    def _resolve_claim_failure(self, job_id: str, exc: Exception) -> JobRecord | None:
        """Resolve every post-claim setup failure without killing the loop."""

        logger.exception("Job %s failed after execution claim.", job_id)
        current: JobRecord | None = None
        try:
            # This preserves the cancellation race contract and is a no-op for
            # a terminal job.  A cancel request always wins over ordinary
            # failure resolution.
            current = self._finalize_cancellation(job_id)
        except Exception:
            logger.exception("Could not resolve cancellation for job %s.", job_id)

        if current is not None and is_terminal_status(current.status):
            return current

        try:
            failed = self.job_service.mark_failed(job_id, str(exc))
        except Exception:
            # Recording failure can itself fail (for example, a transient
            # SQLite error).  Keep the consumer alive and make the condition
            # observable; a later reconciliation pass can classify the job.
            logger.exception("Could not record failure for job %s.", job_id)
            try:
                return self.job_repository.get(job_id)
            except Exception:
                logger.exception("Could not reread failed job %s.", job_id)
                return None
        return failed

    def _is_cancelled(self, job_id: str) -> bool:
        # `cancel_requested` (in-flight job, cooperative shutdown pending) and
        # `cancelled` (queued job, or shutdown already finalized) both mean
        # "stop doing forward-progress work on this job" from the runner's
        # and a generator's point of view (#207); only `_finalize_cancellation`
        # distinguishes between them for persistence purposes.
        current = self.job_repository.get(job_id)
        return current is not None and current.status in (
            JOB_STATUS_CANCEL_REQUESTED,
            JOB_STATUS_CANCELLED,
        )

    def _finalize_cancellation(self, job_id: str) -> JobRecord | None:
        """Resolve a possibly-`cancel_requested` job to `cancelled` and return it.

        Safe to call at any "this might be a cancellation" bail-out point in
        `process_job`: a `cancel_requested` job is moved to the terminal
        `cancelled` state; any other status (already `cancelled`, or -- in the
        single-worker case -- unrelated) is simply re-read and returned
        unchanged, matching the pre-#207 fallback of reading the current
        record.
        """

        return self.job_service.finalize_cancellation(job_id)

    def _begin_context(
        self,
        job_id: str,
        project_id: str | None = None,
        cancellation_token: Event | None = None,
    ) -> GenerationContext:
        # Always builds a context now (#201 follow-up, eighth Codex round on
        # PR #376): this used to return None outright when no
        # CancellationRegistry was configured, which meant project_id never
        # reached the generator either -- a project-bound reference could
        # pass JobService.create_job()'s same-project validation but then
        # fail execution-time re-validation because it compared the asset's
        # project against None instead of the job's real project_id.
        # Progress reporting and project_id tracking never actually depended
        # on cancellation support; only "is_cancelled" genuinely does, and
        # with no registry to ever record a cancellation request in, "never
        # cancelled" is the correct (not a regressed) answer.
        cancellation_registry = self.cancellation_registry
        if cancellation_registry is not None:
            if cancellation_token is None:
                cancellation_token = cancellation_registry.begin(job_id)

            def is_cancelled() -> bool:
                return (
                    cancellation_registry.is_cancelled(job_id)
                    or self._is_cancelled(job_id)
                )
        else:

            def is_cancelled() -> bool:
                return False

        context = GenerationContext(
            is_cancelled=is_cancelled,
            on_progress=lambda fraction: self._report_generation_progress(job_id, fraction),
            project_id=project_id,
        )
        return context

    def _report_generation_progress(self, job_id: str, fraction: float) -> None:
        mapped_progress = (
            _RUNNING_PROGRESS_START + max(0.0, min(1.0, fraction)) * _RUNNING_PROGRESS_SPAN
        )
        job = self.job_repository.update_if_status(
            job_id,
            (JOB_STATUS_RUNNING,),
            progress=mapped_progress,
        )
        if job is not None:
            self._publish(
                "job_progress",
                {"job_id": job.id, "status": job.status, "progress": job.progress},
            )

    def _report_segment_progress(
        self,
        job_id: str,
        fraction: float,
        *,
        segment: int,
        segment_count: int,
    ) -> None:
        if self._is_cancelled(job_id):
            return
        # Reserve 0.0–0.1 for preparation and 0.9–1.0 for postprocessing.
        progress = min(0.89, 0.1 + max(0.0, min(1.0, fraction)) * 0.79)
        job = self.job_repository.update_if_status(
            job_id,
            (JOB_STATUS_RUNNING,),
            status=JOB_STATUS_RUNNING,
            progress=progress,
        )
        if job is not None:
            self._publish(
                "job_segment_progress",
                {
                    "job_id": job.id,
                    "status": job.status,
                    "progress": job.progress,
                    "segment": segment,
                    "segment_count": segment_count,
                },
            )

    def _update_status(
        self,
        job_id: str,
        status: GenerationStatus,
        *,
        progress: float | None = None,
    ) -> JobRecord | None:
        expected_statuses = {
            JOB_STATUS_PREPARING: (JOB_STATUS_QUEUED,),
            JOB_STATUS_RUNNING: (JOB_STATUS_PREPARING,),
            JOB_STATUS_POSTPROCESSING: (JOB_STATUS_RUNNING,),
        }.get(status)
        if expected_statuses is None:
            job = self.job_repository.update_status(job_id, status, progress=progress)
        else:
            job = self.job_repository.update_if_status(
                job_id,
                expected_statuses,
                status=status,
                progress=progress,
            )
        if job is not None:
            self._publish(
                self._event_name_for_status(status),
                {
                    "job_id": job.id,
                    "status": job.status,
                    "progress": job.progress,
                },
            )
        return job

    def _publish(self, event_type: str, payload: dict[str, object]) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(event_type, payload)

    def _event_name_for_status(self, status: str) -> str:
        # Only the in-progress transitions flow through here; terminal
        # success/failure/cancellation events are emitted by JobService.
        if status == JOB_STATUS_PREPARING:
            return "job_preparing"
        if status == JOB_STATUS_RUNNING:
            return "job_started"
        if status == JOB_STATUS_POSTPROCESSING:
            return "job_postprocessing"
        return "job_status_updated"


__all__ = ["JobRunner"]
