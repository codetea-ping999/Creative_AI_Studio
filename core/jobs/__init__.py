"""Job execution primitives for Creative AI Studio."""

from .cancellation import CancellationRegistry
from .context import GenerationCancelled, GenerationContext
from .events import EventBus, JobEvent
from .queue import JobQueue
from .runner import JobRunner
from .schemas import JobRecord
from .service import JobService
from .statuses import (
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

__all__ = [
    "CancellationRegistry",
    "EventBus",
    "GenerationCancelled",
    "GenerationContext",
    "JobEvent",
    "JobQueue",
    "JobRecord",
    "JobRunner",
    "JobService",
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_POSTPROCESSING",
    "JOB_STATUS_PREPARING",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_SUCCEEDED",
    "JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
]
