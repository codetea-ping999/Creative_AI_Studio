"""Job orchestration service used by the API layer."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from typing import TYPE_CHECKING

from core.schemas import GenerationRequest, GenerationResult

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
    TERMINAL_JOB_STATUSES,
)

_STATUS_TO_EVENT = {
    JOB_STATUS_QUEUED: "job_queued",
    JOB_STATUS_PREPARING: "job_preparing",
    JOB_STATUS_RUNNING: "job_started",
    JOB_STATUS_POSTPROCESSING: "job_postprocessing",
    JOB_STATUS_SUCCEEDED: "job_succeeded",
    JOB_STATUS_FAILED: "job_failed",
    JOB_STATUS_CANCELLED: "job_cancelled",
}


class JobService:
    """Create, queue, and update jobs without executing generators directly."""

    def __init__(
        self,
        job_repository: JobRepository,
        job_queue: object,
        event_bus: EventBus | None = None,
    ) -> None:
        self.job_repository = job_repository
        self.job_queue = job_queue
        self.event_bus = event_bus

    def create_job(
        self,
        request: GenerationRequest,
        project_id: str | None = None,
    ) -> JobRecord:
        now = datetime.now(timezone.utc)
        job = JobRecord(
            id=f"job_{uuid4().hex}",
            project_id=project_id,
            media_type=request.media_type,
            status=JOB_STATUS_QUEUED,
            request=request,
            progress=0.0,
            created_at=now,
            updated_at=now,
        )
        created_job = self.job_repository.create(job)
        self._publish(
            "job_created",
            {
                "job_id": created_job.id,
                "status": created_job.status,
                "media_type": created_job.media_type,
            },
        )
        self.enqueue_job(created_job.id)
        return created_job

    def enqueue_job(self, job_id: str) -> JobRecord | None:
        job = self.get_job(job_id)
        if job is None or job.status == JOB_STATUS_CANCELLED:
            return job

        self.job_queue.enqueue(job_id)
        self._publish(
            "job_queued",
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
            },
        )
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.job_repository.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        return self.job_repository.list()

    def update_status(
        self,
        job_id: str,
        status: str,
        progress: float | None = None,
    ) -> JobRecord | None:
        job = self.job_repository.update_status(job_id, status, progress=progress)
        if job is None:
            return None
        self._publish(
            _STATUS_TO_EVENT.get(status, "job_status_updated"),
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
            },
        )
        return job

    def mark_failed(self, job_id: str, message: str) -> JobRecord | None:
        job = self.job_repository.update(
            job_id,
            status=JOB_STATUS_FAILED,
            progress=1.0,
            error_message=message,
        )
        if job is not None:
            self._publish(
                "job_failed",
                {
                    "job_id": job.id,
                    "status": job.status,
                    "progress": job.progress,
                    "error_message": job.error_message,
                },
            )
        return job

    def mark_succeeded(
        self,
        job_id: str,
        result: GenerationResult,
    ) -> JobRecord | None:
        normalized_result = result.model_copy(
            update={
                "job_id": job_id,
                "status": JOB_STATUS_SUCCEEDED,
                "error_message": None,
            }
        )
        job = self.job_repository.update(
            job_id,
            status=JOB_STATUS_SUCCEEDED,
            progress=1.0,
            result=normalized_result,
            error_message=None,
        )
        if job is not None:
            self._publish(
                "job_succeeded",
                {
                    "job_id": job.id,
                    "status": job.status,
                    "progress": job.progress,
                    "outputs": job.result.outputs if job.result else [],
                },
            )
        return job

    def cancel_job(self, job_id: str) -> JobRecord | None:
        job = self.get_job(job_id)
        if job is None or job.status in TERMINAL_JOB_STATUSES:
            return job
        return self.update_status(job_id, JOB_STATUS_CANCELLED)

    def _publish(self, event_type: str, payload: dict[str, object]) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(event_type, payload)


__all__ = ["JobService"]
