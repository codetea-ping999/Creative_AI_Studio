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

Credential resolution and log/error/metadata redaction (issue #236):
- `resolve_cloud_audio_provider_credential` resolves a cloud provider's API
  key from the environment variable named in `default_params.api_key_env` --
  never from the manifest itself -- following the same convention
  `core.models.text_runtimes.build_openai_compatible_runtime` established
  and `generators.image.providers.resolve_image_provider_credential` (#257)
  already reuses. It rejects a manifest holding a literal credential instead
  of the `*_env` indirection (via
  `core.models.manifest.reject_literal_credential_fields`, the same check
  `ModelManifest` already runs at construction time) and raises an
  actionable, non-secret-bearing error when a configured credential is
  missing.
- `redact_secrets` / `redact_provider_error` / `redact_provider_metadata`
  strip resolved credential values out of any string, exception, or
  diagnostics mapping before it could reach a log line or persisted
  job/asset/request data.

One bounded example adapter, with a timeout/retry/error-normalization
contract (issue #237):
- `AudioProviderErrorCategory` gives every provider call failure one of five
  stable, vendor-neutral categories (timeout / rate limit / transient / auth
  / permanent); `AudioProviderCallError` and its five subclasses carry that
  category plus an optional vendor `provider_request_id` for
  support/provenance. Mirrors `generators.image.providers
  .ImageProviderErrorCategory` / `ImageProviderCallError` (#258) so both
  media types normalize provider failures the same way.
- `AudioProviderRetryPolicy` bounds retry count, backoff, and per-attempt
  timeout to safe ceilings, and `call_audio_provider_with_retry` retries
  only the categories that are actually retryable (timeout, rate limit,
  transient), with backoff -- an auth or permanent error, or any exception a
  transport never translated into an `AudioProviderCallError`, propagates on
  the attempt that raised it and is never retried.
- `generators.audio.example_provider.ExampleCloudAudioProviderAdapter` is
  the concrete adapter this issue's microtask calls for: an
  injected-transport implementation of `CloudAudioProviderAdapter` that
  runs its transport through `call_audio_provider_with_retry` and redacts
  every error/result through this module's #236 redaction helpers before
  either can leave the adapter. It is only ever reachable through
  `run_cloud_audio_provider`, so the #235 egress guard still runs strictly
  before its `generate()` (and therefore its transport) is invoked.

Still explicitly out of scope here (see the microtask order on #57):
- Recording egress provenance in job metadata and surfacing it in the UI --
  #238.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import os
import threading
import time
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from core.models.manifest import reject_literal_credential_fields

_T = TypeVar("_T")


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


class AudioProviderCredentialUnavailableError(AudioProviderError):
    """Raised when a cloud audio provider is missing its required credential.

    Always actionable: names the environment variable an operator needs to
    set. Never includes a credential value -- there is none to include, the
    whole point of this error is that the value was absent. See
    :func:`resolve_cloud_audio_provider_credential`.
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


class AudioProviderErrorCategory(str, Enum):
    """Stable, vendor-neutral classification for a provider call failure.

    Every vendor SDK/HTTP error shape (status code, exception type, message
    format -- all of it vendor-specific) must be translated into exactly one
    of these five categories by whatever raises an
    :class:`AudioProviderCallError`, so a caller (retry logic, logging,
    user-facing error surfacing) never has to branch on a vendor's own error
    taxonomy. Mirrors `generators.image.providers.ImageProviderErrorCategory`
    (#258) so image and audio providers normalize failures the same way.
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    AUTH = "auth"
    PERMANENT = "permanent"


_RETRYABLE_AUDIO_PROVIDER_ERROR_CATEGORIES = frozenset(
    {
        AudioProviderErrorCategory.TIMEOUT,
        AudioProviderErrorCategory.RATE_LIMIT,
        AudioProviderErrorCategory.TRANSIENT,
    }
)


def is_retryable_audio_provider_error_category(
    category: AudioProviderErrorCategory,
) -> bool:
    """Whether `category` is safe to retry at all (before backoff/limits apply).

    Only timeout, rate limit, and transient are retryable -- auth and
    permanent are, by definition, not going to succeed on an identical
    retry, so retrying them would just repeat a doomed request (issue #237:
    "bounded retries only for documented transient failures").
    """

    return category in _RETRYABLE_AUDIO_PROVIDER_ERROR_CATEGORIES


class AudioProviderCallError(AudioProviderError):
    """Base for every categorized cloud-audio-provider call failure (#237).

    Never raise this base class directly -- raise one of the five category
    subclasses below (`AudioProviderTimeoutError`, `AudioProviderRateLimitError`,
    `AudioProviderTransientError`, `AudioProviderAuthError`,
    `AudioProviderPermanentError`) so `category` cannot drift from the type.
    `provider_request_id` carries the *vendor's* own request/trace id for a
    failed call when the vendor supplied one, for support/provenance.
    `retry_after_seconds` carries a vendor-supplied wait hint (e.g. a rate
    limit's `Retry-After`); `call_audio_provider_with_retry` honors it but
    always clamps it to `AudioProviderRetryPolicy.max_backoff_seconds`, so a
    vendor cannot force an effectively-unbounded wait.
    """

    category: AudioProviderErrorCategory

    def __init__(
        self,
        message: str,
        *,
        provider_name: str,
        provider_request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_name = provider_name
        self.provider_request_id = provider_request_id
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return is_retryable_audio_provider_error_category(self.category)


class AudioProviderTimeoutError(AudioProviderCallError):
    """The call did not complete within its bounded per-attempt deadline."""

    category = AudioProviderErrorCategory.TIMEOUT


class AudioProviderRateLimitError(AudioProviderCallError):
    """The provider rejected the call for exceeding a rate limit."""

    category = AudioProviderErrorCategory.RATE_LIMIT


class AudioProviderTransientError(AudioProviderCallError):
    """A provider-side failure that is expected to clear up on retry (e.g. a 5xx)."""

    category = AudioProviderErrorCategory.TRANSIENT


class AudioProviderAuthError(AudioProviderCallError):
    """The provider rejected the call's credential or configuration.

    Distinct from :class:`AudioProviderCredentialUnavailableError`: that one
    fires locally, before any transport call, when a credential cannot even
    be resolved from the environment. This one is raised from a transport
    implementation's own translation of a vendor's 401/403-shaped response --
    the credential *was* sent, but the vendor rejected it.
    """

    category = AudioProviderErrorCategory.AUTH


class AudioProviderPermanentError(AudioProviderCallError):
    """The request itself is invalid in a way retrying cannot fix (e.g. a 400)."""

    category = AudioProviderErrorCategory.PERMANENT


_MAX_ALLOWED_AUDIO_PROVIDER_RETRIES = 5
_MAX_ALLOWED_AUDIO_PROVIDER_TIMEOUT_SECONDS = 120.0
_MAX_ALLOWED_AUDIO_PROVIDER_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class AudioProviderRetryPolicy:
    """Bounded, explicit retry/timeout configuration for one provider call.

    Every field is validated in `__post_init__` against a hard safe-bound
    ceiling, so a caller cannot configure effectively-unbounded retries or an
    effectively-unbounded per-attempt timeout (issue #237 acceptance
    criterion: "Requests have a finite timeout and bounded retry count").
    The defaults are deliberately modest (a handful of quick retries, not an
    aggressive retry storm against a struggling provider). Mirrors
    `generators.image.providers.ImageProviderRetryPolicy` (#258).
    """

    max_retries: int = 2
    timeout_seconds: float = 30.0
    backoff_base_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 8.0

    def __post_init__(self) -> None:
        if not (0 <= self.max_retries <= _MAX_ALLOWED_AUDIO_PROVIDER_RETRIES):
            raise ValueError(
                f"max_retries={self.max_retries} must be within "
                f"[0, {_MAX_ALLOWED_AUDIO_PROVIDER_RETRIES}]."
            )
        if not (0 < self.timeout_seconds <= _MAX_ALLOWED_AUDIO_PROVIDER_TIMEOUT_SECONDS):
            raise ValueError(
                f"timeout_seconds={self.timeout_seconds} must be within "
                f"(0, {_MAX_ALLOWED_AUDIO_PROVIDER_TIMEOUT_SECONDS}]."
            )
        if self.backoff_base_seconds < 0:
            raise ValueError(
                f"backoff_base_seconds={self.backoff_base_seconds} must not be negative."
            )
        if self.backoff_multiplier < 1:
            raise ValueError(
                f"backoff_multiplier={self.backoff_multiplier} must be >= 1."
            )
        if not (0 <= self.max_backoff_seconds <= _MAX_ALLOWED_AUDIO_PROVIDER_BACKOFF_SECONDS):
            raise ValueError(
                f"max_backoff_seconds={self.max_backoff_seconds} must be within "
                f"[0, {_MAX_ALLOWED_AUDIO_PROVIDER_BACKOFF_SECONDS}]."
            )


def _audio_provider_backoff_seconds(
    error: AudioProviderCallError, policy: AudioProviderRetryPolicy, attempt: int
) -> float:
    """Seconds to wait before the retry attempt after `attempt` (0-indexed).

    Prefers a vendor-supplied `retry_after_seconds` when present (a rate
    limit response usually names exactly how long to wait), otherwise backs
    off exponentially from `backoff_base_seconds`. Either way the result is
    clamped to `max_backoff_seconds` -- a vendor hint is a hint, not a
    license to block the caller indefinitely.
    """

    if error.retry_after_seconds is not None:
        return max(0.0, min(error.retry_after_seconds, policy.max_backoff_seconds))
    exponential = policy.backoff_base_seconds * (policy.backoff_multiplier**attempt)
    return min(exponential, policy.max_backoff_seconds)


def _run_audio_provider_transport_with_timeout(
    transport: Callable[[], _T],
    *,
    provider_name: str,
    timeout_seconds: float,
) -> _T:
    """Run `transport` on a daemon thread, bounded by `timeout_seconds`.

    Python cannot safely interrupt an arbitrary synchronous call, so a
    timed-out `transport` may keep running in the background on its thread;
    this bounds *how long the caller waits*, not how long the underlying
    call itself keeps executing. That is still a real, useful guarantee: it
    is what keeps a hung remote provider from hanging its caller forever.
    """

    outcome: list[_T] = []
    failure: list[BaseException] = []

    def _target() -> None:
        try:
            outcome.append(transport())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            failure.append(exc)

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise AudioProviderTimeoutError(
            f"Cloud audio provider {provider_name!r} request exceeded its "
            f"{timeout_seconds}s timeout.",
            provider_name=provider_name,
        )
    if failure:
        raise failure[0]
    return outcome[0]


def call_audio_provider_with_retry(
    transport: Callable[[], _T],
    *,
    provider_name: str,
    retry_policy: AudioProviderRetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> _T:
    """Run `transport` under a bounded per-attempt timeout and bounded retries.

    Each attempt is individually bounded by `retry_policy.timeout_seconds`
    (see :func:`_run_audio_provider_transport_with_timeout`). Only a raised
    `AudioProviderCallError` whose category is retryable
    (:func:`is_retryable_audio_provider_error_category`) triggers another
    attempt, and only up to `retry_policy.max_retries` times, sleeping a
    bounded backoff between attempts
    (:func:`_audio_provider_backoff_seconds`). An auth- or permanent-category
    error, or any exception `transport` raises that was never translated
    into an `AudioProviderCallError` in the first place, propagates
    immediately on the attempt that raised it -- an unclassified exception
    cannot be assumed retryable (issue #237 acceptance: "Permanent errors
    are not retried indefinitely").
    """

    policy = retry_policy if retry_policy is not None else AudioProviderRetryPolicy()
    attempt = 0
    while True:
        try:
            return _run_audio_provider_transport_with_timeout(
                transport, provider_name=provider_name, timeout_seconds=policy.timeout_seconds
            )
        except AudioProviderCallError as exc:
            if not exc.retryable or attempt >= policy.max_retries:
                raise
            sleep(_audio_provider_backoff_seconds(exc, policy, attempt))
            attempt += 1


@dataclass(frozen=True)
class AudioProviderCredential:
    """A credential resolved from the environment for one provider call.

    ``repr()``/``str()`` deliberately omit `value` so an accidental
    ``logger.info("resolved %r", credential)`` or an exception message built
    from this object cannot leak the secret. Only `.value` carries it, and
    callers should place `.value` nowhere but a request's auth header/param
    -- never in `CloudAudioProviderRequest.params`,
    `CloudAudioProviderResult.provider_metadata`, or any other field that
    could reach job/asset/request persistence.
    """

    env_var: str
    value: str

    def __repr__(self) -> str:
        return f"AudioProviderCredential(env_var={self.env_var!r}, value=<redacted>)"


def resolve_cloud_audio_provider_credential(
    default_params: Mapping[str, Any],
    *,
    provider_name: str,
    manifest_label: str,
    env: Mapping[str, str] | None = None,
) -> AudioProviderCredential | None:
    """Resolve the credential `provider_name` needs, never from the manifest itself.

    Mirrors the `api_key_env` convention `openai_compatible_text_loader`
    established (`core/models/text_runtimes.py`) and
    `generators.image.providers.resolve_image_provider_credential` already
    reuses (#257): a manifest names the *environment variable* that holds a
    credential, never the credential value, so nothing committed under
    `models/manifests/` can ever carry a secret.

    Returns ``None`` when `default_params` declares no `api_key_env` at all
    -- some cloud endpoints need no separate credential (e.g. an
    already-authenticated self-hosted gateway).

    Raises :class:`ProviderMisconfiguredError` if `default_params` itself
    holds a literal credential value instead of the env-var-name indirection
    (reuses `core.models.manifest.reject_literal_credential_fields` rather
    than re-implementing the same field-name check -- every `provider:
    cloud` manifest already runs this check once at `ModelManifest`
    construction time; this is the same check re-applied at request time).
    Raises :class:`AudioProviderCredentialUnavailableError` -- an actionable
    message naming the missing environment variable, never a value -- if
    `api_key_env` is declared but that variable is unset or empty.
    """

    try:
        reject_literal_credential_fields(default_params, manifest_label=manifest_label)
    except ValueError as exc:
        raise ProviderMisconfiguredError(str(exc)) from exc

    api_key_env = default_params.get("api_key_env")
    if not api_key_env:
        return None

    source = env if env is not None else os.environ
    value = source.get(str(api_key_env), "")
    if not value:
        raise AudioProviderCredentialUnavailableError(
            f"Cloud audio provider {provider_name!r} ({manifest_label}) requires a "
            f"credential, but environment variable {api_key_env!r} is not set. Set "
            f"{api_key_env} to a valid credential before using this provider."
        )
    return AudioProviderCredential(env_var=str(api_key_env), value=value)


def redact_secrets(text: str, secrets: Iterable[str | None]) -> str:
    """Replace every occurrence of a non-empty secret in `text` with a marker.

    Used to sanitize a provider error message or diagnostics string before it
    reaches a log line or persisted job/asset/request data -- the credential
    value itself must never survive into any of those.
    """

    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def redact_provider_error(
    error: BaseException, secrets: Iterable[str | None]
) -> BaseException:
    """Return an equivalent error with every `secrets` occurrence redacted.

    An `AudioProviderCallError` (issue #237) is reconstructed with its
    `category`-defining type, `provider_name`, `retry_after_seconds`, and a
    redacted `provider_request_id` preserved -- a caller further up the
    stack (retry logic, job/asset metadata, support tooling) still needs
    those after sanitization, and this is the one place that sanitization
    happens. Any other exception type is preserved where its constructor
    accepts a single message argument (true for every stdlib exception and
    every other error class in this module); falls back to `RuntimeError`
    when reconstructing the original type fails, so a redaction failure can
    never re-raise the original (still secret-bearing) exception.
    """

    redacted_message = redact_secrets(str(error), secrets)
    if isinstance(error, AudioProviderCallError):
        redacted_provider_request_id = (
            redact_secrets(error.provider_request_id, secrets)
            if error.provider_request_id is not None
            else None
        )
        return type(error)(
            redacted_message,
            provider_name=error.provider_name,
            provider_request_id=redacted_provider_request_id,
            retry_after_seconds=error.retry_after_seconds,
        )
    try:
        return type(error)(redacted_message)
    except Exception:  # noqa: BLE001 - constructing the original type failed; never re-raise the original
        return RuntimeError(redacted_message)


def redact_provider_metadata(
    metadata: Mapping[str, Any], secrets: Iterable[str | None]
) -> dict[str, Any]:
    """Recursively redact `secrets` occurrences from a diagnostics/metadata mapping.

    Applied to anything derived from a provider call before it is attached to
    job/asset metadata or a request snapshot, so a credential a vendor SDK
    happened to echo back in a response field (or accidentally included in a
    diagnostic string) cannot persist alongside the generation record.
    """

    secret_list = [secret for secret in secrets if secret]

    def _walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {key: _walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            return redact_secrets(node, secret_list)
        return node

    return _walk(dict(metadata))


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
    "AudioProviderAuthError",
    "AudioProviderCallError",
    "AudioProviderCapability",
    "AudioProviderCredential",
    "AudioProviderCredentialUnavailableError",
    "AudioProviderError",
    "AudioProviderErrorCategory",
    "AudioProviderPermanentError",
    "AudioProviderRateLimitError",
    "AudioProviderRetryPolicy",
    "AudioProviderTimeoutError",
    "AudioProviderTransientError",
    "CloudAudioProviderAdapter",
    "CloudAudioProviderOptInRequiredError",
    "CloudAudioProviderRequest",
    "CloudAudioProviderResult",
    "ProviderAvailability",
    "ProviderMisconfiguredError",
    "ProviderUnavailableError",
    "UnsupportedCapabilityError",
    "call_audio_provider_with_retry",
    "cloud_audio_provider_opt_in_granted",
    "ensure_capabilities_declared",
    "ensure_capability_supported",
    "ensure_cloud_audio_provider_opt_in",
    "is_retryable_audio_provider_error_category",
    "manifest_declared_capabilities",
    "redact_provider_error",
    "redact_provider_metadata",
    "redact_secrets",
    "resolve_cloud_audio_provider_credential",
    "run_cloud_audio_provider",
]
