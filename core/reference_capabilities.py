"""Model-agnostic contract for character/location reference-image conditioning.

Prompt fragments and seed locks (see `core/prompting/composer.py`) keep a subject's
described *attributes* consistent, but they cannot pin how a face or a location
actually looks across an angle or scene change. Reference-image conditioning
(img2img / IP-Adapter style) closes that gap by letting a generation request carry
Gallery asset IDs that steer generation toward a specific character or location
image.

This module defines that contract without implementing any runtime:

- `ReferenceImageInput` is what a request carries (asset id, role, strength,
  optional preprocessing) — the same explicit fields every caller round-trips,
  instead of ad hoc keys buried in a free-form `params` dict.
- `ReferenceCapability` is what a model manifest advertises. Support is opt-in
  data on the manifest; nothing here infers support from a model id, provider,
  or tag string, so a new model is unsupported by default until its manifest
  says otherwise.
- `validate_reference_inputs` is the one place that decides whether a request's
  references can be honored by a given model, so every caller (API route,
  generator, batch expansion) gets the same actionable failure instead of each
  re-deriving its own check.

Actually wiring this into a generator (img2img/IP-Adapter execution) is out of
scope here; see issues #199/#201 in the reference-conditioning epic (#50).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReferenceRole = Literal["character", "location"]
REFERENCE_ROLES: tuple[ReferenceRole, ...] = ("character", "location")

ReferenceConditioningMode = Literal["ip_adapter", "img2img"]
REFERENCE_CONDITIONING_MODES: tuple[ReferenceConditioningMode, ...] = (
    "ip_adapter",
    "img2img",
)

ReferencePreprocessing = Literal["none", "auto", "face_crop", "canny", "depth"]
REFERENCE_PREPROCESSING_MODES: tuple[ReferencePreprocessing, ...] = (
    "none",
    "auto",
    "face_crop",
    "canny",
    "depth",
)

MIN_REFERENCE_STRENGTH = 0.0
MAX_REFERENCE_STRENGTH = 1.0
# Strong enough to steer identity/location, weak enough not to override the
# prompt outright; callers that want a different default set strength explicitly.
DEFAULT_REFERENCE_STRENGTH = 0.6


class ReferenceImageInput(BaseModel):
    """One character/location reference image attached to a generation request."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(
        min_length=1,
        description="Gallery asset id supplying the reference image.",
    )
    role: ReferenceRole = Field(
        description="Whether this reference pins a character's identity or a location."
    )
    strength: float = Field(
        default=DEFAULT_REFERENCE_STRENGTH,
        ge=MIN_REFERENCE_STRENGTH,
        le=MAX_REFERENCE_STRENGTH,
        description=(
            "Conditioning strength: 0 leaves the base generation unaffected, "
            "1 follows the reference image most closely."
        ),
    )
    preprocessing: ReferencePreprocessing = Field(
        default="none",
        description="Optional preprocessing applied to the reference image before conditioning.",
    )


class ReferenceCapability(BaseModel):
    """What a model manifest advertises about reference-image conditioning.

    A manifest with no `reference_capability` (or one with empty
    `supported_modes`/`supported_roles`) means the model does not support
    reference conditioning at all — `enabled` is False and any request
    carrying references is rejected by `validate_reference_inputs`.
    """

    model_config = ConfigDict(extra="forbid")

    supported_modes: list[ReferenceConditioningMode] = Field(
        default_factory=list,
        description="Conditioning modes this model can honor, e.g. ip_adapter or img2img.",
    )
    supported_roles: list[ReferenceRole] = Field(
        default_factory=list,
        description="Reference roles (character/location) this model can honor.",
    )
    supported_preprocessing: list[ReferencePreprocessing] = Field(
        default_factory=lambda: ["none"],
        description="Preprocessing modes this model can honor for a reference image.",
    )
    min_strength: float = Field(default=MIN_REFERENCE_STRENGTH, ge=0.0, le=1.0)
    max_strength: float = Field(default=MAX_REFERENCE_STRENGTH, ge=0.0, le=1.0)
    max_references_per_role: int = Field(
        default=1,
        ge=0,
        description="Maximum number of reference images this model honors per role in one request.",
    )

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ReferenceCapability":
        if self.max_strength < self.min_strength:
            raise ValueError(
                "Reference capability max_strength must be >= min_strength: "
                f"{self.max_strength} < {self.min_strength}"
            )
        return self

    @property
    def enabled(self) -> bool:
        """Whether this manifest advertises any usable reference conditioning."""

        return bool(self.supported_modes) and bool(self.supported_roles)


class UnsupportedReferenceError(ValueError):
    """Raised when a request asks for reference conditioning a model cannot honor."""


def validate_reference_inputs(
    references: list[ReferenceImageInput],
    *,
    capability: ReferenceCapability | None,
    model_id: str,
) -> None:
    """Fail fast, before generation, when references cannot be honored.

    Called with the resolved model's `public_id` (or alias as given by the
    caller) so the message names the model the request actually chose, not an
    internal manifest id the caller never sees.
    """

    if not references:
        return
    if capability is None or not capability.enabled:
        raise UnsupportedReferenceError(
            f"Model {model_id!r} does not support reference-image conditioning; "
            "remove the `references` field or choose a model whose manifest "
            "advertises reference_capability."
        )

    role_counts: dict[str, int] = {}
    for reference in references:
        if reference.role not in capability.supported_roles:
            raise UnsupportedReferenceError(
                f"Model {model_id!r} does not support the {reference.role!r} reference "
                f"role; supported roles: {', '.join(capability.supported_roles) or 'none'}"
            )
        if not (capability.min_strength <= reference.strength <= capability.max_strength):
            raise UnsupportedReferenceError(
                f"Reference strength {reference.strength} for asset {reference.asset_id!r} "
                f"is outside model {model_id!r}'s supported range "
                f"[{capability.min_strength}, {capability.max_strength}]"
            )
        if reference.preprocessing not in capability.supported_preprocessing:
            raise UnsupportedReferenceError(
                f"Model {model_id!r} does not support {reference.preprocessing!r} "
                f"preprocessing for asset {reference.asset_id!r}; supported: "
                f"{', '.join(capability.supported_preprocessing) or 'none'}"
            )
        role_counts[reference.role] = role_counts.get(reference.role, 0) + 1
        if role_counts[reference.role] > capability.max_references_per_role:
            raise UnsupportedReferenceError(
                f"Model {model_id!r} supports at most {capability.max_references_per_role} "
                f"{reference.role!r} reference(s) per request; got "
                f"{role_counts[reference.role]}"
            )


__all__ = [
    "DEFAULT_REFERENCE_STRENGTH",
    "MAX_REFERENCE_STRENGTH",
    "MIN_REFERENCE_STRENGTH",
    "REFERENCE_CONDITIONING_MODES",
    "REFERENCE_PREPROCESSING_MODES",
    "REFERENCE_ROLES",
    "ReferenceCapability",
    "ReferenceConditioningMode",
    "ReferenceImageInput",
    "ReferencePreprocessing",
    "ReferenceRole",
    "UnsupportedReferenceError",
    "validate_reference_inputs",
]
