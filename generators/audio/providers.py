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

Egress opt-in is enforced (issue #235, see below):
- `cloud_audio_provider_opt_in_granted` / `ensure_cloud_audio_provider_opt_in`
  gate every cloud audio request on two independent env vars, *both*
  required: `ALLOW_CLOUD_PROVIDERS=true` globally, plus a provider-specific
  `ALLOW_CLOUD_PROVIDER_<NAME>=true`. This intentionally differs from the
  image provider's either-suffices convention
  (`generators/image/providers.py`, #256): the parent issue's own scope
  ("`ALLOW_CLOUD_PROVIDERS=true` と provider ごとの明示設定がない限り送信し
  ない", #57) requires *both*, so flipping the global switch alone opts in
  zero providers until each is also vetted and enabled by name.
- `run_cloud_audio_provider` calls `ensure_cloud_audio_provider_opt_in`
  after `ensure_capability_supported` and strictly before
  `adapter.generate()`, so a disabled/not-opted-in request never reaches a
  network call and never constructs/sends prompt or audio payload data.

Still explicitly out of scope here (see the microtask order on #57):
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
import os
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


class CloudAudioProviderOptInRequiredError(AudioProviderError, PermissionError):
    """Raised before any transport call when explicit cloud egress opt-in is missing.

    Distinct from :class:`ProviderUnavailableError`: this is not about an
    adapter's own reported state, it is about authorization to send
    anything at all to a cloud provider. Both a global switch and a
    provider-specific switch must be granted -- neither is sufficient alone
    (issue #235; see :func:`cloud_audio_provider_opt_in_granted`). Always
    raised before :meth:`CloudAudioProviderAdapter.generate` is reachable --
    see :func:`ensure_cloud_audio_provider_opt_in` and
    :func:`run_cloud_audio_provider`.
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

        Only reachable once :func:`ensure_capability_supported` and
        :func:`ensure_cloud_audio_provider_opt_in` have both passed -- see
        :func:`run_cloud_audio_provider`.
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


_GLOBAL_CLOUD_OPT_IN_ENV_VAR = "ALLOW_CLOUD_PROVIDERS"


def _provider_specific_opt_in_env_var(provider_name: str) -> str:
    """Env var name that additionally enables exactly one provider.

    E.g. ``"elevenlabs"`` -> ``"ALLOW_CLOUD_PROVIDER_ELEVENLABS"``.
    """

    suffix = "".join(
        character if character.isalnum() else "_" for character in provider_name.upper()
    )
    return f"ALLOW_CLOUD_PROVIDER_{suffix}"


def cloud_audio_provider_opt_in_granted(
    provider_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether cloud audio provider `provider_name` has explicit opt-in right now.

    Two independent switches, *both* required -- unlike the image
    provider's either-suffices convention (`generators/image/providers.py`,
    #256):

    - `ALLOW_CLOUD_PROVIDERS=true` enables cloud egress globally.
    - `ALLOW_CLOUD_PROVIDER_<NAME>=true` additionally enables this specific
      provider.

    The global switch alone is deliberately insufficient: flipping it opts
    in zero providers until each is also vetted and enabled by name, which
    is what stops one blanket switch from silently authorizing a provider
    nobody individually reviewed (issue #235 acceptance criteria). Both are
    default-closed: unset, empty, or any value other than the literal
    string ``"true"`` counts as not granted, matching
    `ALLOW_REMOTE_TEXT_ENDPOINTS` (`core/models/text_runtimes.py`) and
    `ALLOW_REMOTE_AUDIO_ENDPOINTS` (`core/models/audio_runtimes.py`).
    """

    source = env if env is not None else os.environ
    global_enabled = (
        str(source.get(_GLOBAL_CLOUD_OPT_IN_ENV_VAR, "")).strip().lower() == "true"
    )
    if not global_enabled:
        return False
    provider_env_var = _provider_specific_opt_in_env_var(provider_name)
    return str(source.get(provider_env_var, "")).strip().lower() == "true"


def ensure_cloud_audio_provider_opt_in(
    provider_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise before any transport call unless both opt-in switches are granted.

    Reading `env` only touches process state already present, so this check
    itself never performs network I/O -- safe to call unconditionally ahead
    of a transport invocation. :func:`run_cloud_audio_provider` calls this
    strictly before ``adapter.generate()`` is ever reachable, which is what
    guarantees a disabled request fails before any prompt/audio payload data
    would be sent.
    """

    if cloud_audio_provider_opt_in_granted(provider_name, env=env):
        return
    global_var = _GLOBAL_CLOUD_OPT_IN_ENV_VAR
    provider_var = _provider_specific_opt_in_env_var(provider_name)
    raise CloudAudioProviderOptInRequiredError(
        f"Cloud audio provider {provider_name!r} requires explicit opt-in: "
        f"set both {global_var}=true and {provider_var}=true. Setting only "
        f"{global_var}=true is not sufficient."
    )


def run_cloud_audio_provider(
    adapter: CloudAudioProviderAdapter,
    request: CloudAudioProviderRequest,
    *,
    env: Mapping[str, str] | None = None,
) -> CloudAudioProviderResult:
    """Validate, authorize, then run, a cloud provider request.

    The one call sequence a caller needs so ``generate()`` is unreachable
    for an unsupported, unavailable, or not-opted-in request -- callers
    should not call ``adapter.generate()`` directly.
    Capability/availability are checked first (unchanged since #234) so
    those failures keep their own specific error type; egress opt-in
    (#235) is checked immediately after, still strictly before
    ``adapter.generate()`` is reachable, so construction/sending of any
    provider request is guarded before it can happen.
    """

    ensure_capability_supported(adapter, request.capability)
    ensure_cloud_audio_provider_opt_in(adapter.provider_name, env=env)
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
    "CloudAudioProviderOptInRequiredError",
    "CloudAudioProviderRequest",
    "CloudAudioProviderResult",
    "ProviderAvailability",
    "ProviderMisconfiguredError",
    "ProviderUnavailableError",
    "UnsupportedCapabilityError",
    "cloud_audio_provider_opt_in_granted",
    "ensure_capabilities_declared",
    "ensure_capability_supported",
    "ensure_cloud_audio_provider_opt_in",
    "manifest_declared_capabilities",
    "run_cloud_audio_provider",
]
