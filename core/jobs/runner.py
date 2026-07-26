"""Queue consumer that executes generation jobs."""

from __future__ import annotations

import logging
from threading import Event
import time
from typing import TYPE_CHECKING

from generators.registry import GeneratorRegistry

from .events import EventBus
from .service import JobService

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.storage.repositories.job_repository import JobRepository
from .schemas import JobRecord
from .statuses import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
)

logger = logging.getLogger(__name__)


class JobRunner:
    """Single-worker job runner for queued generation requests."""

    def __init__(
        self,
        job_repository: JobRepository,
        job_queue: object,
        generator_registry: GeneratorRegistry,
        event_bus: EventBus | None = None,
        asset_repository: AssetRepository | None = None,
        job_service: JobService | None = None,
    ) -> None:
        self.job_repository = job_repository
        self.job_queue = job_queue
        self.generator_registry = generator_registry
        self.event_bus = event_bus
        self.asset_repository = asset_repository
        # Terminal transitions (success/failure) are delegated to JobService so
        # the completion path lives in exactly one place. When callers do not
        # inject a shared service we build an equivalent one from our own deps.
        self.job_service = job_service or JobService(
            job_repository,
            job_queue,
            event_bus,
            asset_repository=asset_repository,
        )

    def run_once(self) -> JobRecord | None:
        job_id = self.job_queue.dequeue()
        if job_id is None:
            return None
        return self.process_job(job_id)

    def run_forever(
        self,
        *,
        poll_interval_seconds: float = 0.1,
        stop_event: Event | None = None,
    ) -> None:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            job = self.run_once()
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

        try:
            if self._update_status(job_id, JOB_STATUS_PREPARING, progress=0.0) is None:
                return self.job_repository.get(job_id)
            generator = self.generator_registry.get(job.media_type, job.request.task_type)
            if self._update_status(job_id, JOB_STATUS_RUNNING, progress=0.1) is None:
                return self.job_repository.get(job_id)
            controlled_run = getattr(generator, "run_with_control", None)
            if callable(controlled_run):
                result = controlled_run(
                    job.request,
                    progress_callback=lambda fraction, segment, segment_count: (
                        self._report_generation_progress(
                            job_id,
                            fraction,
                            segment=segment,
                            segment_count=segment_count,
                        )
                    ),
                    cancel_requested=lambda: self._is_cancelled(job_id),
                )
            else:
                # Most generators are one blocking call. Long-form audio opts
                # into the controlled path above to report/cancel at segments.
                result = generator.run(job.request)
        except Exception as exc:
            if self._is_cancelled(job_id):
                return self.job_repository.get(job_id)
            return self.job_service.mark_failed(job_id, str(exc))

        if self._is_cancelled(job_id):
            return self.job_repository.get(job_id)
        if self._update_status(job_id, JOB_STATUS_POSTPROCESSING, progress=0.9) is None:
            return self.job_repository.get(job_id)
        return self.job_service.mark_succeeded(job_id, result)

    def _is_cancelled(self, job_id: str) -> bool:
        current = self.job_repository.get(job_id)
        return current is not None and current.status == JOB_STATUS_CANCELLED

    def _report_generation_progress(
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
        status: str,
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
