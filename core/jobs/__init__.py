"""Job execution primitives for Creative AI Studio."""

from .cancellation import CancellationRegistry
from .context import GenerationCancelled, GenerationContext
from .events import EventBus, JobEvent
from .lanes import (
    DEFAULT_JOB_LANES,
    LANE_HEAVY,
    LANE_LIGHT,
    LaneConfig,
    assign_lane,
    parse_job_lanes,
    resolve_lane,
)
from .queue import JobQueue
from .runner import JobRunner
from .schemas import JobRecord
from .service import JobService
from .statuses import (
    ACTIVE_JOB_STATUSES,
    ALLOWED_TRANSITIONS,
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
    is_terminal_status,
    is_valid_transition,
)
from .worker_pool import WorkerPool

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "ALLOWED_TRANSITIONS",
    "CancellationRegistry",
    "DEFAULT_JOB_LANES",
    "EventBus",
    "GenerationCancelled",
    "GenerationContext",
    "JobEvent",
    "JobQueue",
    "JobRecord",
    "JobRunner",
    "JobService",
    "JOB_STATUS_CANCEL_REQUESTED",
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_POSTPROCESSING",
    "JOB_STATUS_PREPARING",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_SUCCEEDED",
    "JOB_STATUSES",
    "LANE_HEAVY",
    "LANE_LIGHT",
    "LaneConfig",
    "TERMINAL_JOB_STATUSES",
    "WorkerPool",
    "assign_lane",
    "is_terminal_status",
    "is_valid_transition",
    "parse_job_lanes",
    "resolve_lane",
]
