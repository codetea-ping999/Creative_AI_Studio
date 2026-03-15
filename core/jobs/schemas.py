"""Shared schemas for persisted generation jobs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.schemas import GenerationRequest, GenerationResult
from core.schemas.generation import GenerationStatus, MediaType


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
    created_at: datetime
    updated_at: datetime


__all__ = ["JobRecord"]
