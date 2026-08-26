"""Queue consumer that executes generation jobs."""

from __future__ import annotations

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
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
)

# The generator's own progress fraction (0.0-1.0) is mapped into this slice of
# the job's overall progress; PREPARING/POSTPROCESSING bracket it on either
# side (see process_job).
_RUNNING_PROGRESS_START = 0.1
_RUNNING_PROGRESS_SPAN = 0.8

logger = logging.getLogger(__name__)


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
        return self.process_job(job_id)

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
            job = self.run_once(lane=lane)
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

        context = self._begin_context(job_id)
        try:
            try:
                if self._update_status(job_id, JOB_STATUS_PREPARING, progress=0.0) is None:
                    return self._finalize_cancellation(job_id)
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
            return self.job_service.mark_succeeded(job_id, result)
        finally:
            if self.cancellation_registry is not None:
                self.cancellation_registry.end(job_id)

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

    def _begin_context(self, job_id: str) -> GenerationContext | None:
        cancellation_registry = self.cancellation_registry
        if cancellation_registry is None:
            return None
        cancellation_registry.begin(job_id)
        return GenerationContext(
            is_cancelled=lambda: cancellation_registry.is_cancelled(job_id),
            on_progress=lambda fraction: self._report_generation_progress(job_id, fraction),
        )

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
