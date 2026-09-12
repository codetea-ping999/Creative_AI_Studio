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


def _default_supported_preprocessing() -> list[ReferencePreprocessing]:
    # A plain `["none"]` default_factory lambda returns `list[str]`, which mypy
    # cannot narrow to the Literal-typed field below; a typed function fixes that.
    return ["none"]


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
        default_factory=_default_supported_preprocessing,
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


class MissingReferenceAssetError(ValueError):
    """Raised when a reference points at an asset that cannot supply an image.

    Distinct from `UnsupportedReferenceError`: that one means "the chosen model
    can't honor this reference type or strength"; this one means the reference
    itself is broken -- the asset id does not resolve (deleted, never existed,
    or a typo) or resolves to a non-image asset. An unknown Bible entry id
    degrades to a warning so one bad axis value cannot fail a whole batch, but
    a Bible entry that names a since-deleted reference asset must stop
    generation outright: silently dropping identity/location conditioning
    would produce a wrong-looking result without any signal that it happened.
    """


def select_effective_references(
    references: list[ReferenceImageInput],
) -> list[ReferenceImageInput]:
    """References that actually condition generation (`strength > 0`).

    `ReferenceImageInput.strength`'s own field description documents 0 as
    leaving base generation unaffected. A zero-strength reference is
    audit-only: it must remain visible in requested/provenance metadata, but
    must never consume conditioning capability -- no per-role limit, no
    total applied-image limit, no manifest capability requirement, and it is
    never selected as the primary conditioning reference.

    Callers pass this *filtered* set to `validate_reference_inputs()` and to
    every other capability/count/primary-selection decision, never the raw
    `references` list (#387 P1 hotfix: passing the raw list let a
    zero-strength entry sharing a role with a real one trip
    `max_references_per_role`, and required reference_capability even when
    every requested reference was strength=0 and therefore had no
    conditioning effect at all).

    Shared between `JobService.validate_references()`
    (`core/jobs/service.py`) and
    `ImageGenerator._resolve_references_for_conditioning()`
    (`generators/image/generator.py`) so creation-time preflight and
    execution-time runtime cannot drift on what "effective" means -- both
    call this one function rather than each keeping its own filter.
    """

    return [reference for reference in references if reference.strength > 0.0]


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

    Callers must pass an already-effective (`strength > 0`) list -- see
    `select_effective_references()`. This function itself does not filter by
    strength; a caller that passes zero-strength entries will have them
    counted toward `max_references_per_role` and will require
    `capability.enabled` even for a request with no actual conditioning
    effect.
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
    "MissingReferenceAssetError",
    "REFERENCE_CONDITIONING_MODES",
    "REFERENCE_PREPROCESSING_MODES",
    "REFERENCE_ROLES",
    "ReferenceCapability",
    "ReferenceConditioningMode",
    "ReferenceImageInput",
    "ReferencePreprocessing",
    "ReferenceRole",
    "UnsupportedReferenceError",
    "select_effective_references",
    "validate_reference_inputs",
]
