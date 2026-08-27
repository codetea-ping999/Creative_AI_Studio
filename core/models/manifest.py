"""Schema for declarative model definitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.reference_capabilities import ReferenceCapability
from core.schemas.generation import MediaType

# Field names that must never hold a literal credential value in
# `default_params` (issue #257 / #236's manifest-side counterpart). A
# manifest is committed to the repository, so a literal secret in one of
# these fields is a leaked secret the moment the commit lands. The approved
# indirection -- naming the *environment variable* that holds the value, e.g.
# `api_key_env` -- already established by
# `core.models.text_runtimes.build_openai_compatible_runtime` is exempt: any
# key ending in `_env` names a variable, not a secret.
_LITERAL_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "secretkey",
        "access_key",
        "access_token",
        "auth_token",
        "bearer_token",
        "token",
        "password",
        "credential",
        "credentials",
        "client_secret",
    }
)


def reject_literal_credential_fields(
    default_params: Mapping[str, Any], *, manifest_label: str
) -> None:
    """Raise ``ValueError`` if `default_params` holds a literal credential.

    Checked recursively because `default_params` can nest provider-specific
    config under its own keys. Reused (not re-implemented) by
    `generators/image/providers.py` so manifest-time and request-time
    checks share one field-name vocabulary.
    """

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                key_path = f"{path}.{key}" if path else str(key)
                normalized_key = str(key).strip().lower()
                if (
                    normalized_key in _LITERAL_CREDENTIAL_FIELD_NAMES
                    and isinstance(value, str)
                    and value
                ):
                    raise ValueError(
                        f"{manifest_label}: default_params field {key_path!r} holds a "
                        "literal credential value. Manifests must never contain secrets "
                        f"-- name the environment variable instead (e.g. {key}_env) and "
                        "resolve it at runtime."
                    )
                _walk(value, key_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")

    _walk(default_params, "default_params")


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
        reject_literal_credential_fields(self.default_params, manifest_label=self.id)
        return self

    @property
    def public_model_id(self) -> str:
        return self.public_id or self.id


__all__ = ["ModelManifest", "reject_literal_credential_fields"]
