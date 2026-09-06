"""Pure recovery classification; no mutation, enqueue, or startup activation.

Candidates are not instructions to execute: the caller must first establish
exclusive process ownership and reconcile workflow relationships (a later PR).
"""

from __future__ import annotations

from typing import Literal

from core.schemas import GenerationStatus

from .schemas import JobRecord

RecoveryClassification = Literal[
    "requeue_candidate",
    "interrupted_failure_candidate",
    "cancelled_candidate",
    "completion_reconciliation_candidate",
    "terminal_reconciliation_candidate",
]

_CLASSIFICATIONS: dict[GenerationStatus, RecoveryClassification] = {
    "queued": "requeue_candidate",
    "preparing": "interrupted_failure_candidate",
    "running": "interrupted_failure_candidate",
    "postprocessing": "interrupted_failure_candidate",
    "cancel_requested": "cancelled_candidate",
    "succeeded": "completion_reconciliation_candidate",
    "failed": "terminal_reconciliation_candidate",
    "cancelled": "terminal_reconciliation_candidate",
}


def classify_job(job: JobRecord) -> RecoveryClassification:
    """Describe a persisted job without changing it or triggering side effects."""
    return _CLASSIFICATIONS[job.status]


__all__ = ["RecoveryClassification", "classify_job"]
