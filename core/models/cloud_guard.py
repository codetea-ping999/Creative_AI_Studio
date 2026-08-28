"""Egress guard for opt-in cloud model providers.

A manifest with ``provider: "cloud"`` names a model served by an external
vendor rather than something running on this machine. This studio defaults to
local-only generation (see docs/multimedia-content-generation-plan.md §7.3),
so loading such a manifest must clear two independent switches before any
network call can happen: the blanket ``ALLOW_CLOUD_PROVIDERS`` switch, and a
manifest-specific ``ALLOW_CLOUD_PROVIDER_<ID>`` switch. Losing either one
keeps the default intact, and turning on one cloud provider never silently
turns on another.
"""

from __future__ import annotations

import os


class CloudProviderDisabledError(RuntimeError):
    """Raised when a ``provider: "cloud"`` manifest is loaded without opt-in."""


def cloud_provider_env_flag(manifest_id: str) -> str:
    """Name of the manifest-specific opt-in environment variable."""

    normalized = "".join(character if character.isalnum() else "_" for character in manifest_id)
    return f"ALLOW_CLOUD_PROVIDER_{normalized.upper()}"


def ensure_cloud_provider_enabled(manifest_id: str) -> None:
    """Raise unless both the global and manifest-specific switches are set.

    Called before a cloud manifest's loader runs, so a disabled provider never
    reaches the point of making an HTTP request.
    """

    if os.getenv("ALLOW_CLOUD_PROVIDERS", "").strip().lower() != "true":
        raise CloudProviderDisabledError(
            f"Manifest {manifest_id!r} uses provider=\"cloud\" but "
            "ALLOW_CLOUD_PROVIDERS is not set to true. This studio defaults to "
            "local-only generation; set ALLOW_CLOUD_PROVIDERS=true to allow any "
            "cloud provider to run."
        )

    provider_flag = cloud_provider_env_flag(manifest_id)
    if os.getenv(provider_flag, "").strip().lower() != "true":
        raise CloudProviderDisabledError(
            f"Manifest {manifest_id!r} uses provider=\"cloud\" but {provider_flag} "
            f"is not set to true. Set {provider_flag}=true to allow this specific "
            "provider to send requests off the machine."
        )


__all__ = [
    "CloudProviderDisabledError",
    "cloud_provider_env_flag",
    "ensure_cloud_provider_enabled",
]
