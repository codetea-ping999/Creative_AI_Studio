"""Queue consumer that executes generation jobs."""

from __future__ import annotations

import logging
from threading import Event
import time
from typing import TYPE_CHECKING

from generators.registry import GeneratorRegistry

from .events import EventBus

if TYPE_CHECKING:
    from core.storage.repositories.job_repository import JobRepository
from .schemas import JobRecord
from .statuses import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
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
    ) -> None:
        self.job_repository = job_repository
        self.job_queue = job_queue
        self.generator_registry = generator_registry
        self.event_bus = event_bus

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
            self._update_status(job_id, JOB_STATUS_PREPARING, progress=0.0)
            generator = self.generator_registry.get(job.media_type)
            self._update_status(job_id, JOB_STATUS_RUNNING, progress=0.1)
            result = generator.run(job.request)
            normalized_result = result.model_copy(
                update={
                    "job_id": job_id,
                    "status": JOB_STATUS_SUCCEEDED,
                    "error_message": None,
                }
            )
            self._update_status(job_id, JOB_STATUS_POSTPROCESSING, progress=0.9)
            self.job_repository.update_result(job_id, normalized_result)
            final_job = self.job_repository.update(
                job_id,
                status=JOB_STATUS_SUCCEEDED,
                progress=1.0,
                error_message=None,
            )
            if final_job is not None:
                self._publish(
                    "job_succeeded",
                    {
                        "job_id": final_job.id,
                        "status": final_job.status,
                        "progress": final_job.progress,
                        "outputs": final_job.result.outputs if final_job.result else [],
                    },
                )
            return final_job
        except Exception as exc:
            failed_job = self.job_repository.update(
                job_id,
                status=JOB_STATUS_FAILED,
                progress=1.0,
                error_message=str(exc),
            )
            if failed_job is not None:
                self._publish(
                    "job_failed",
                    {
                        "job_id": failed_job.id,
                        "status": failed_job.status,
                        "progress": failed_job.progress,
                        "error_message": failed_job.error_message,
                    },
                )
            return failed_job

    def _update_status(
        self,
        job_id: str,
        status: str,
        *,
        progress: float | None = None,
    ) -> JobRecord | None:
        job = self.job_repository.update_status(job_id, status, progress=progress)
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
        if status == JOB_STATUS_PREPARING:
            return "job_preparing"
        if status == JOB_STATUS_RUNNING:
            return "job_started"
        if status == JOB_STATUS_POSTPROCESSING:
            return "job_postprocessing"
        if status == JOB_STATUS_CANCELLED:
            return "job_cancelled"
        if status == JOB_STATUS_FAILED:
            return "job_failed"
        return "job_status_updated"


__all__ = ["JobRunner"]
