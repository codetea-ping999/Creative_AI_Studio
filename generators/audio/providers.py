"""Cloud audio provider adapter and capability contract.

This module defines the provider-neutral boundary an *optional* cloud
audio/speech backend must satisfy (issue #234, parent #57), before any
outbound implementation is added. Nothing here makes a network call; it only
describes shapes a caller checks a request against, so unsupported requests
can be rejected before a real adapter (#237) is ever reached.

Manifest convention for `provider: "cloud"` audio models (see
`docs/model-system.md`): `default_params.capabilities` is a required,
non-empty list of :class:`AudioProviderCapability` values the model
advertises. :func:`manifest_declared_capabilities` reads that field and
:func:`ensure_capabilities_declared` compares it against what a request
needs, entirely from the manifest -- no adapter instance or runtime has to be
resolved first. This is what lets a generator's ``validate_request()`` reject
an unsupported cloud request before ``generate()`` would otherwise resolve a
runtime and reach the network.

Explicitly out of scope here (see the microtask order on #57):
- Egress opt-in (`ALLOW_CLOUD_PROVIDERS`, per-provider enable flags) -- #235.
- Resolving API keys from environment variables and redacting them from logs
  -- #236, following the existing `default_params.api_key_env` convention
  used by `core/models/text_runtimes.py`'s `openai_compatible_text_loader`.
- A real vendor adapter -- #237.
- Recording egress provenance in job metadata and surfacing it in the UI --
  #238.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class AudioProviderCapability(str, Enum):
    """One generation feature a cloud audio provider may support.

    Kept as a small, explicit vocabulary rather than free-form strings so a
    manifest's declared capabilities and a request's required capabilities
    can be compared without either side inventing new spellings.
    """

    TEXT_TO_MUSIC = "text-to-music"
    TEXT_TO_SPEECH = "text-to-speech"
    MELODY_CONDITIONING = "melody-conditioning"
    LONG_FORM = "long-form"
    VOICE_CLONING = "voice-cloning"

    @classmethod
    def parse(cls, value: Any, *, context: str) -> "AudioProviderCapability":
        """Parse one capability value, naming the offending value on failure."""

        if isinstance(value, AudioProviderCapability):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:
            known = ", ".join(member.value for member in cls)
            raise ValueError(
                f"{context}: unknown audio provider capability {value!r}; "
                f"expected one of: {known}."
            ) from exc


class ProviderAvailability(str, Enum):
    """Coarse-grained state of a cloud provider adapter, checked before use."""

    AVAILABLE = "available"
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    ERROR = "error"


class AudioProviderError(RuntimeError):
    """Base class for cloud audio provider contract violations."""


class ProviderUnavailableError(AudioProviderError):
    """Raised when a live adapter cannot serve requests right now.

    Covers every :class:`ProviderAvailability` state other than
    ``AVAILABLE`` (disabled by configuration, misconfigured credentials, or a
    previously observed error) -- callers do not need to branch on which.
    """


class ProviderMisconfiguredError(AudioProviderError):
    """Raised when a `provider: cloud` manifest itself is invalid.

    Distinct from :class:`ProviderUnavailableError`: this fires while reading
    static manifest fields, before any adapter object exists.
    """


class UnsupportedCapabilityError(AudioProviderError):
    """Raised when a request needs a capability nothing advertised supporting.

    Always raised before any adapter method that could reach the network
    (:meth:`CloudAudioProviderAdapter.generate`) is invoked.
    """


class CloudAudioProviderRequest(BaseModel):
    """Normalized input to a cloud audio provider adapter.

    Mirrors the fields a generator already assembles from a
    ``GenerationRequest`` plus its resolved manifest, so an adapter never
    needs vendor-specific knowledge of either.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    capability: AudioProviderCapability
    task_type: str = Field(min_length=1)
    model_id: str = Field(min_length=1, description="Manifest public_id.")
    params: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None


class CloudAudioProviderResult(BaseModel):
    """Normalized output from a cloud audio provider adapter."""

    model_config = ConfigDict(extra="forbid")

    audio_bytes: bytes
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    output_format: str = Field(min_length=1)
    # A redacted, non-secret summary of what was sent/received (destination,
    # request id, timing). See #238 for how this reaches job metadata. This
    # is never a place to carry credentials.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class CloudAudioProviderAdapter(Protocol):
    """Boundary an optional cloud audio backend must implement.

    No method here is vendor-specific: a caller checks ``availability()``/
    ``capabilities()`` to fail fast, then calls ``generate()`` -- it never
    branches on which vendor is behind the adapter.
    """

    provider_name: str

    def capabilities(self) -> frozenset[AudioProviderCapability]:
        """Capabilities this adapter can currently serve."""
        ...

    def availability(self) -> ProviderAvailability:
        """Whether this adapter can serve a request right now."""
        ...

    def generate(
        self, request: CloudAudioProviderRequest
    ) -> CloudAudioProviderResult:
        """Perform the (network) generation call.

        Only reachable once :func:`ensure_capability_supported` has passed.
        """
        ...


def ensure_capability_supported(
    adapter: CloudAudioProviderAdapter,
    capability: AudioProviderCapability,
) -> None:
    """Raise before ``adapter.generate()`` if the request cannot be served.

    Both checks read only local adapter state (``availability()``,
    ``capabilities()``); neither is expected to make a network call, which is
    what lets :func:`run_cloud_audio_provider` guarantee an unsupported
    request fails before any egress is attempted.
    """

    availability = adapter.availability()
    if availability != ProviderAvailability.AVAILABLE:
        raise ProviderUnavailableError(
            f"Cloud audio provider {adapter.provider_name!r} is not available "
            f"({availability.value})."
        )
    supported = adapter.capabilities()
    if capability not in supported:
        known = ", ".join(sorted(member.value for member in supported)) or "none"
        raise UnsupportedCapabilityError(
            f"Cloud audio provider {adapter.provider_name!r} does not support "
            f"capability {capability.value!r}; supports: {known}."
        )


def run_cloud_audio_provider(
    adapter: CloudAudioProviderAdapter,
    request: CloudAudioProviderRequest,
) -> CloudAudioProviderResult:
    """Validate, then run, a cloud provider request.

    The one call sequence a caller needs so ``generate()`` is unreachable for
    an unsupported request -- callers should not call ``adapter.generate()``
    directly.
    """

    ensure_capability_supported(adapter, request.capability)
    return adapter.generate(request)


def manifest_declared_capabilities(
    default_params: Mapping[str, Any],
    *,
    manifest_label: str,
) -> frozenset[AudioProviderCapability]:
    """Read the ``default_params.capabilities`` convention for `provider: cloud`.

    A cloud manifest declares what it supports statically, so a request that
    needs a capability the manifest never advertised can be rejected during
    ``validate_request()`` -- before any runtime/adapter for it is resolved.
    """

    raw_values = default_params.get("capabilities")
    if not isinstance(raw_values, list) or not raw_values:
        raise ProviderMisconfiguredError(
            f"{manifest_label}: provider 'cloud' manifests must declare a "
            "non-empty default_params.capabilities list."
        )
    try:
        return frozenset(
            AudioProviderCapability.parse(value, context=manifest_label)
            for value in raw_values
        )
    except ValueError as exc:
        raise ProviderMisconfiguredError(str(exc)) from exc


def ensure_capabilities_declared(
    declared: frozenset[AudioProviderCapability],
    required: frozenset[AudioProviderCapability],
    *,
    manifest_label: str,
) -> None:
    """Raise if ``required`` is not a subset of what a manifest declared.

    The static counterpart of :func:`ensure_capability_supported`: it never
    touches a live adapter, so it can run during request validation, ahead of
    resolving any runtime.
    """

    missing = required - declared
    if missing:
        missing_names = ", ".join(sorted(capability.value for capability in missing))
        raise UnsupportedCapabilityError(
            f"{manifest_label} does not advertise required capabilities: "
            f"{missing_names}."
        )


__all__ = [
    "AudioProviderCapability",
    "AudioProviderError",
    "CloudAudioProviderAdapter",
    "CloudAudioProviderRequest",
    "CloudAudioProviderResult",
    "ProviderAvailability",
    "ProviderMisconfiguredError",
    "ProviderUnavailableError",
    "UnsupportedCapabilityError",
    "ensure_capabilities_declared",
    "ensure_capability_supported",
    "manifest_declared_capabilities",
    "run_cloud_audio_provider",
]
