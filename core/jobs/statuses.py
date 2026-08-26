"""Canonical job status values and the status-transition contract.

This module is the single source of truth for which job status transitions
are valid. It intentionally only defines the *contract* (status groupings and
``is_valid_transition``); enforcing it against live job records is the
responsibility of callers such as ``core/jobs/service.py`` and
``core/jobs/runner.py``.

Cooperative cancellation (``core/jobs/cancellation.py`` /
`#18 <https://github.com>`_) introduces ``cancel_requested`` as an explicit,
non-terminal intermediate state:

- A ``queued`` job has no in-flight work to interrupt, so cancelling it moves
  it straight to the terminal ``cancelled`` state.
- A job already being worked on (``preparing`` / ``running`` /
  ``postprocessing``) cannot be cancelled synchronously — the runtime has to
  notice and unwind first — so cancelling it moves it to ``cancel_requested``
  instead. This is what keeps "cancelled a queued job" distinguishable from
  "asked a running job to stop".
- ``cancel_requested`` only ever resolves to a terminal state (``cancelled``
  on the happy path, ``failed`` if the cooperative shutdown itself errors) —
  never back to an active status and never to ``succeeded``, so a completion
  that races a cancel request cannot be reported as a success.
- Requesting cancellation again while already ``cancel_requested`` (or on a
  job that already reached a terminal state) is idempotent: it is a no-op,
  not a new transition, and never an error.
"""

from __future__ import annotations

from core.schemas import GenerationStatus

JOB_STATUS_QUEUED: GenerationStatus = "queued"
JOB_STATUS_PREPARING: GenerationStatus = "preparing"
JOB_STATUS_RUNNING: GenerationStatus = "running"
JOB_STATUS_POSTPROCESSING: GenerationStatus = "postprocessing"
JOB_STATUS_CANCEL_REQUESTED: GenerationStatus = "cancel_requested"
JOB_STATUS_SUCCEEDED: GenerationStatus = "succeeded"
JOB_STATUS_FAILED: GenerationStatus = "failed"
JOB_STATUS_CANCELLED: GenerationStatus = "cancelled"

JOB_STATUSES = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_PREPARING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_CANCEL_REQUESTED,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)

TERMINAL_JOB_STATUSES = (
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)

# Non-terminal statuses, i.e. JOB_STATUSES minus TERMINAL_JOB_STATUSES.
# ``cancel_requested`` belongs here: it has not reached a final outcome yet,
# even though it can no longer make forward progress toward "succeeded".
ACTIVE_JOB_STATUSES = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_PREPARING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_CANCEL_REQUESTED,
)

# The documented status-transition contract: for each status, the set of
# statuses it may move to next. Identity (a status "transitioning" to itself)
# is deliberately left out of this table and handled uniformly by
# ``is_valid_transition`` instead, since every status allows it (repeated
# cancel requests and re-observing a terminal status must both be no-ops
# rather than errors).
ALLOWED_TRANSITIONS: dict[GenerationStatus, tuple[GenerationStatus, ...]] = {
    JOB_STATUS_QUEUED: (
        JOB_STATUS_PREPARING,
        JOB_STATUS_CANCELLED,
        JOB_STATUS_FAILED,
    ),
    JOB_STATUS_PREPARING: (
        JOB_STATUS_RUNNING,
        JOB_STATUS_CANCEL_REQUESTED,
        JOB_STATUS_FAILED,
    ),
    JOB_STATUS_RUNNING: (
        JOB_STATUS_POSTPROCESSING,
        JOB_STATUS_CANCEL_REQUESTED,
        JOB_STATUS_FAILED,
    ),
    JOB_STATUS_POSTPROCESSING: (
        JOB_STATUS_SUCCEEDED,
        JOB_STATUS_CANCEL_REQUESTED,
        JOB_STATUS_FAILED,
    ),
    JOB_STATUS_CANCEL_REQUESTED: (
        JOB_STATUS_CANCELLED,
        JOB_STATUS_FAILED,
    ),
    JOB_STATUS_SUCCEEDED: (),
    JOB_STATUS_FAILED: (),
    JOB_STATUS_CANCELLED: (),
}


def is_terminal_status(status: str) -> bool:
    """Return True when the job status is final."""

    return status in TERMINAL_JOB_STATUSES


def is_valid_transition(current: GenerationStatus, target: GenerationStatus) -> bool:
    """Return True when moving a job from ``current`` to ``target`` is allowed.

    Requesting the status a job is already in is always allowed, even though
    it is not listed as an explicit edge in ``ALLOWED_TRANSITIONS``: this
    makes repeated cancellation of an already ``cancel_requested`` job, and a
    repeated cancel call against a job that already reached a terminal
    status, idempotent no-ops rather than invalid transitions. Terminal
    statuses have no other outgoing edges, so this also guarantees a terminal
    job can never be moved back into an active status.
    """

    if target == current:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, ())


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "ALLOWED_TRANSITIONS",
    "JOB_STATUS_CANCEL_REQUESTED",
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
    "is_valid_transition",
]
