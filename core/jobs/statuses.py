"""Canonical job status values."""

from __future__ import annotations

from core.schemas import GenerationStatus

JOB_STATUS_QUEUED: GenerationStatus = "queued"
JOB_STATUS_PREPARING: GenerationStatus = "preparing"
JOB_STATUS_RUNNING: GenerationStatus = "running"
JOB_STATUS_POSTPROCESSING: GenerationStatus = "postprocessing"
JOB_STATUS_SUCCEEDED: GenerationStatus = "succeeded"
JOB_STATUS_FAILED: GenerationStatus = "failed"
JOB_STATUS_CANCELLED: GenerationStatus = "cancelled"

JOB_STATUSES = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_PREPARING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)

TERMINAL_JOB_STATUSES = (
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)

ACTIVE_JOB_STATUSES = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_PREPARING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_POSTPROCESSING,
)


def is_terminal_status(status: str) -> bool:
    """Return True when the job status is final."""

    return status in TERMINAL_JOB_STATUSES


__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_POSTPROCESSING",
    "JOB_STATUS_PREPARING",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_SUCCEEDED",
    "JOB_STATUSES",
    "ACTIVE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "is_terminal_status",
]
