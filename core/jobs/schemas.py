"""Shared schemas for persisted generation jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.schemas import GenerationRequest, GenerationResult
from core.schemas.generation import GenerationStatus, MediaType

# Whether a terminal Job's post-completion side effects (Asset sync, Story
# replay, Batch reconciliation) have been durably applied yet. Deliberately
# separate from `status` (PR3, "succeeded Job != completion fully applied"):
# a Job can be `status="succeeded"` (the generator finished) while its
# convergence work is still `completion_state="pending"` (a crash, an Asset
# sync failure, an unresolved Story replay, ...) -- retrying that
# convergence must never re-run the generator or touch `status`/`result`.
# "blocked" or similar additional states are deliberately not introduced
# here; add one only with concrete evidence pending/done cannot express.
CompletionState = Literal["pending", "done"]


class JobRecord(BaseModel):
    """Persisted job state tracked by the execution pipeline."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    project_id: str | None = None
    media_type: MediaType
    status: GenerationStatus
    request: GenerationRequest
    result: GenerationResult | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error_message: str | None = None
    # Defaults to "pending" for every row, new or pre-migration: a job is
    # created active (not yet terminal, so this is moot) and, once it
    # becomes terminal, is by construction still sitting at "pending" until
    # completion convergence explicitly marks it "done" -- no separate
    # "set completion_state=pending" write is needed at the terminal
    # transition itself. For a legacy row from before this field existed,
    # "pending" is the safe default: convergence is idempotent/safe to
    # (re)run, so a one-time reconciliation pass is harmless, while
    # defaulting to "done" could silently skip a legitimately-unapplied
    # completion.
    completion_state: CompletionState = "pending"
    # Set when convergence attempts and fails (Asset sync raised, Story
    # replay was retryable/ambiguous, ...); cleared once completion_state
    # becomes "done". Never touches `error_message` (that is the
    # generation-level failure reason set by `mark_failed()`).
    completion_error: str | None = None
    created_at: datetime
    updated_at: datetime


__all__ = ["CompletionState", "JobRecord"]
