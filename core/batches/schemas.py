"""Schemas for batch fan-out: one intent expanded into many jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.schemas import GenerationRequest
from core.schemas.generation import MediaType

BATCH_STRATEGIES: tuple[str, ...] = ("grid", "sample")
SEED_POLICIES: tuple[str, ...] = ("shared", "per_item", "sweep")

BATCH_STATUS_QUEUED = "queued"
BATCH_STATUS_RUNNING = "running"
BATCH_STATUS_PARTIAL = "partial"
BATCH_STATUS_SUCCEEDED = "succeeded"
BATCH_STATUS_FAILED = "failed"
BATCH_STATUS_CANCELLED = "cancelled"

ITEM_STATUS_PENDING = "pending"

DEFAULT_ITEM_LIMIT = 64


class AxisValue(BaseModel):
    """One option along an axis, plus the request patch it applies."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    patch: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class Axis(BaseModel):
    """A named dimension of the sweep."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    values: list[AxisValue] = Field(min_length=1)


class Stage(BaseModel):
    """One pass over the sweep.

    A two-stage spec is the normal shape: a cheap ``probe`` pass over everything,
    then a ``refine`` pass over the winners. ``keep_top_n`` is how many winners
    from *this* stage seed the next one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    keep_top_n: int | None = Field(default=None, ge=1)


class BatchSpec(BaseModel):
    """Declarative description of a fan-out run."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    media_type: MediaType = "image"
    task_type: str | None = None
    model_id: str = ""
    project_id: str | None = None
    prompt: str = ""
    negative_prompt: str | None = None
    seed: int | None = None
    output_format: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    axes: list[Axis] = Field(default_factory=list)
    bible_refs: list[str] = Field(default_factory=list)
    strategy: str = "grid"
    max_items: int | None = Field(default=None, ge=1)
    sample_seed: int | None = None
    seed_policy: str = "shared"
    stages: list[Stage] = Field(default_factory=list)
    limit: int = Field(default=DEFAULT_ITEM_LIMIT, ge=1)

    def resolved_stages(self) -> list[Stage]:
        """Return the stages to run, defaulting to a single unnamed pass."""

        return self.stages or [Stage(name="single")]


class BatchItem(BaseModel):
    """One expanded child of a batch."""

    model_config = ConfigDict(extra="forbid")

    id: str
    index: int
    label: str
    stage_name: str
    stage_index: int = 0
    axis_values: dict[str, str] = Field(default_factory=dict)
    request: GenerationRequest
    job_id: str | None = None
    status: str = ITEM_STATUS_PENDING
    score: float | None = None
    output_path: str | None = None
    preview_path: str | None = None
    error_message: str | None = None
    promoted: bool = False


class BatchAggregate(BaseModel):
    """Rollup of item states, recomputed on every reconcile."""

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    pending: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    average_score: float | None = None
    best_item_id: str | None = None


class BatchRecord(BaseModel):
    """Persisted batch state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    spec: BatchSpec
    status: str = BATCH_STATUS_QUEUED
    stage_index: int = 0
    items: list[BatchItem] = Field(default_factory=list)
    aggregate: BatchAggregate = Field(default_factory=BatchAggregate)
    created_at: datetime
    updated_at: datetime

    def items_for_stage(self, stage_index: int) -> list[BatchItem]:
        return [item for item in self.items if item.stage_index == stage_index]


__all__ = [
    "BATCH_STATUS_CANCELLED",
    "BATCH_STATUS_FAILED",
    "BATCH_STATUS_PARTIAL",
    "BATCH_STATUS_QUEUED",
    "BATCH_STATUS_RUNNING",
    "BATCH_STATUS_SUCCEEDED",
    "BATCH_STRATEGIES",
    "DEFAULT_ITEM_LIMIT",
    "ITEM_STATUS_PENDING",
    "SEED_POLICIES",
    "Axis",
    "AxisValue",
    "BatchAggregate",
    "BatchItem",
    "BatchRecord",
    "BatchSpec",
    "Stage",
]
