"""Shared generation schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MediaType = Literal["image", "video", "audio"]
GenerationStatus = Literal[
    "queued",
    "preparing",
    "running",
    "postprocessing",
    "succeeded",
    "failed",
    "cancelled",
]
GenerationParams = dict[str, Any]


class GenerationRequest(BaseModel):
    """Common request payload for media generation."""

    model_config = ConfigDict(extra="forbid")

    media_type: MediaType = Field(description="Target media type to generate.")
    prompt: str = Field(description="Primary generation prompt.")
    negative_prompt: str | None = Field(
        default=None,
        description="Optional negative prompt.",
    )
    model_id: str = Field(
        description="Public model identifier from GET /models; aliases may also resolve.",
    )
    seed: int | None = Field(default=None, description="Optional deterministic seed.")
    output_format: str | None = Field(
        default=None,
        description="Requested output format such as png, mp4, or wav.",
    )
    params: GenerationParams = Field(
        default_factory=dict,
        description="Media-specific generation parameters.",
    )


class GenerationResult(BaseModel):
    """Common result payload returned by generators."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(description="Associated job identifier.")
    status: GenerationStatus = Field(description="Current or final generation status.")
    outputs: list[str] = Field(
        default_factory=list,
        description="Generated output paths or URIs.",
    )
    previews: list[str] = Field(
        default_factory=list,
        description="Preview asset paths or URIs.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional generator metadata.",
    )
    error_message: str | None = Field(
        default=None,
        description="Failure details when generation does not succeed.",
    )


__all__ = [
    "GenerationParams",
    "GenerationRequest",
    "GenerationResult",
    "GenerationStatus",
    "MediaType",
]
