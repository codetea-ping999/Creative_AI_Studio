"""Schema for declarative model definitions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.reference_capabilities import ReferenceCapability
from core.schemas.generation import MediaType


class ModelManifest(BaseModel):
    """Declarative metadata for a loadable model."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Internal manifest identifier.")
    public_id: str | None = Field(
        default=None,
        min_length=1,
        description="Public model identifier returned by GET /models.",
    )
    display_name: str = Field(min_length=1)
    media_type: MediaType
    task_type: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    local_path: str | None = None
    remote_ref: str | None = None
    loader: str = Field(min_length=1)
    dtype: str | None = None
    revision: str | None = None
    default_params: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    is_default: bool = False
    enabled: bool = True
    reference_capability: ReferenceCapability | None = Field(
        default=None,
        description=(
            "Reference-image conditioning (character/location) this model supports, "
            "if any. Absent means unsupported — never inferred from id/provider/tags."
        ),
    )

    @model_validator(mode="after")
    def validate_manifest(self) -> "ModelManifest":
        if not self.local_path and not self.remote_ref:
            raise ValueError("Model manifest requires local_path or remote_ref.")
        seen_aliases: set[str] = set()
        for alias in self.aliases:
            if alias in seen_aliases:
                raise ValueError(f"Duplicate model alias in manifest {self.id!r}: {alias}")
            seen_aliases.add(alias)
        if self.public_model_id in seen_aliases:
            raise ValueError(
                f"Public model id must not be duplicated in aliases: {self.public_model_id}"
            )
        return self

    @property
    def public_model_id(self) -> str:
        return self.public_id or self.id


__all__ = ["ModelManifest"]
