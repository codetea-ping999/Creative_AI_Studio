"""Schemas for batch fan-out: one intent expanded into many jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

# Patch keys that append to a field instead of replacing it outright. An axis
# value using one of these can never trip a lock on the field it targets: a
# character-sheet angle axis is allowed to add "three-quarter view" to the
# prompt, but a lock still stops it from replacing the character's base
# description. `core.batches.expansion` is the only other reader of this map,
# so it stays the single source of truth for the append/replace split.
PATCH_APPEND_KEYS: dict[str, str] = {
    "prompt_suffix": "prompt",
    "prompt_fragment": "prompt",
    "negative_suffix": "negative_prompt",
    "negative_fragment": "negative_prompt",
}


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

    @model_validator(mode="after")
    def _validate_unique_labels(self) -> "Axis":
        # Two values sharing a label would make two different variations look
        # identical wherever a label stands in for the value (item labels,
        # exported filenames), so this fails at definition time rather than
        # producing indistinguishable output later.
        seen: set[str] = set()
        for value in self.values:
            if value.label in seen:
                raise ValueError(
                    f"axis {self.name!r} has duplicate value label {value.label!r}; "
                    "labels must be unique within an axis."
                )
            seen.add(value.label)
        return self


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
    # Top-level request fields (or generic param keys) an axis value patch may
    # never set outright — e.g. a character sheet locks model_id/seed/prompt/
    # negative_prompt so that varying angle, expression, or pose cannot also
    # silently swap the model, reseed a cell, or overwrite the character's base
    # description. A key in PATCH_APPEND_KEYS is exempt from its target's lock
    # since it appends rather than replaces (see core.batches.expansion).
    locked_fields: list[str] = Field(default_factory=list)

    def resolved_stages(self) -> list[Stage]:
        """Return the stages to run, defaulting to a single unnamed pass."""

        return self.stages or [Stage(name="single")]

    @model_validator(mode="after")
    def _validate_axes(self) -> "BatchSpec":
        seen_names: set[str] = set()
        for axis in self.axes:
            if axis.name in seen_names:
                raise ValueError(
                    f"duplicate axis name {axis.name!r}; axis names must be "
                    "unique within a batch, since axis_values is keyed by name "
                    "and a repeat would silently drop one axis's value."
                )
            seen_names.add(axis.name)

        if not self.locked_fields:
            return self
        locked = set(self.locked_fields)
        for axis in self.axes:
            for value in axis.values:
                for key in value.patch:
                    if key in PATCH_APPEND_KEYS or key not in locked:
                        continue
                    raise ValueError(
                        f"axis {axis.name!r} value {value.label!r} sets locked "
                        f"field {key!r}; {key!r} is in locked_fields and an axis "
                        "value cannot override it. Use one of "
                        f"{sorted(PATCH_APPEND_KEYS)} to append instead, or drop "
                        f"{key!r} from locked_fields."
                    )
        return self


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
    # Set when a stage transition itself fails (e.g. a later stage's
    # reference preflight rejects it), as opposed to an individual item
    # failing -- item failures are already reflected in `aggregate` and
    # `_derive_status()` folds them into "partial"/"failed" normally.
    # `_derive_status()` treats a non-None value here as authoritative and
    # always terminal, overriding what it would otherwise derive from
    # `items`/`aggregate`/`stage_index` alone (#201 follow-up, twelfth Codex
    # round on PR #376): without this, `_recompute_and_save()` -- called on
    # essentially every read -- recomputes `running` from a stage that
    # finished successfully with another stage still pending, silently
    # reverting a batch that was explicitly marked failed back to
    # `running` on the very next `GET`.
    advance_error: str | None = None
    # PR3: durable cancellation intent, set the instant `cancel()` is
    # called -- *before* any child job is actually told to cancel. A
    # process restart (or a crash mid-cancel) must be able to tell "this
    # batch was asked to stop" apart from "this batch just hasn't advanced
    # yet" without relying on any child having already reached
    # cancel_requested/cancelled: this flag is the source of truth for
    # "suppress new stage/child creation", independent of child state.
    cancellation_requested: bool = False
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
    "PATCH_APPEND_KEYS",
    "SEED_POLICIES",
    "Axis",
    "AxisValue",
    "BatchAggregate",
    "BatchItem",
    "BatchRecord",
    "BatchSpec",
    "Stage",
]
