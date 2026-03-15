"""Canonical job status values."""

from __future__ import annotations

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PREPARING = "preparing"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_POSTPROCESSING = "postprocessing"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

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
    "TERMINAL_JOB_STATUSES",
    "is_terminal_status",
]
