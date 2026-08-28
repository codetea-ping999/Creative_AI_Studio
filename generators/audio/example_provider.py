"""Reference cloud audio provider adapter (issue #237, parent #57).

`ExampleCloudAudioProviderAdapter` is the one bounded example implementation
the parent issue's microtask list calls for: a concrete, structurally-typed
`providers.CloudAudioProviderAdapter` that demonstrates the seam #234/#235/
#236 already defined, plus the bounded timeout/retry and error-normalization
contract #237 adds to `providers.py`.

- `transport` is injected (never a hardcoded vendor SDK/HTTP call), so
  contract tests exercise this exact adapter with a fake transport function
  -- no live credentials or network reach CI (#237 acceptance: "Contract
  tests run without live cloud access"). A real vendor integration would
  swap in a transport that makes the actual HTTP call and raises one of
  `providers.AudioProviderCallError`'s five subclasses for a failure; this
  module intentionally does not implement one (see #57 non-goals:
  "Supporting multiple vendors").
- `generate()` runs `transport` through
  `providers.call_audio_provider_with_retry`: a bounded per-attempt timeout
  and bounded, backed-off retries of only the categories
  `providers.is_retryable_audio_provider_error_category` reports retryable
  (timeout / rate limit / transient). An auth or permanent error -- or any
  exception `transport` never translated into a
  `providers.AudioProviderCallError` -- propagates on the attempt that
  raised it and is never retried (#237 acceptance: "Permanent errors are
  not retried indefinitely").
- `providers.run_cloud_audio_provider` (unchanged from #234/#235) is still
  what a caller must use ahead of this adapter's `generate()`: it enforces
  capability support and the #235 egress opt-in guard strictly before
  `generate()` -- and therefore this adapter's `transport` -- is reachable
  (#237 acceptance: "Provider example is reachable only after the egress
  guard passes"). This module does not re-implement that guard, it only
  relies on it.
- Every error this adapter raises, and every successful result's
  `provider_metadata`, is passed through `providers.redact_provider_error` /
  `providers.redact_provider_metadata` with the resolved credential (if any)
  before it can reach a caller -- so a vendor error message or an echoed
  response field can never carry the credential value out of this adapter
  (#237 acceptance: "Error output is redacted according to #236").

Not wired into `AudioGenerator.generate()` here -- selecting/instantiating a
cloud adapter for a live request and recording egress provenance in job
metadata is #238's scope, not this one's.
"""

from __future__ import annotations

from collections.abc import Callable
import time

from .providers import (
    AudioProviderCapability,
    AudioProviderCredential,
    AudioProviderRetryPolicy,
    CloudAudioProviderRequest,
    CloudAudioProviderResult,
    ProviderAvailability,
    call_audio_provider_with_retry,
    redact_provider_error,
    redact_provider_metadata,
)

__all__ = ["ExampleCloudAudioProviderAdapter"]


class ExampleCloudAudioProviderAdapter:
    """Reference `CloudAudioProviderAdapter` implementation (issue #237).

    Satisfies `providers.CloudAudioProviderAdapter` structurally (a
    `runtime_checkable` `Protocol` -- no explicit inheritance needed): the
    `provider_name` attribute plus `capabilities()` / `availability()` /
    `generate()` methods below are exactly what that Protocol requires.
    """

    def __init__(
        self,
        *,
        transport: Callable[[CloudAudioProviderRequest], CloudAudioProviderResult],
        provider_name: str = "example-cloud",
        capabilities: frozenset[AudioProviderCapability] = frozenset(
            {AudioProviderCapability.TEXT_TO_MUSIC}
        ),
        availability: ProviderAvailability = ProviderAvailability.AVAILABLE,
        credential: AudioProviderCredential | None = None,
        retry_policy: AudioProviderRetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider_name = provider_name
        self._transport = transport
        self._capabilities = capabilities
        self._availability = availability
        self._credential = credential
        self._retry_policy = (
            retry_policy if retry_policy is not None else AudioProviderRetryPolicy()
        )
        self._sleep = sleep

    def capabilities(self) -> frozenset[AudioProviderCapability]:
        return self._capabilities

    def availability(self) -> ProviderAvailability:
        return self._availability

    def generate(self, request: CloudAudioProviderRequest) -> CloudAudioProviderResult:
        """Run `transport` under a bounded timeout/retry, then redact the outcome.

        Only reachable once a caller has gone through
        `providers.run_cloud_audio_provider` (capability support and the
        #235 egress opt-in are both checked there, strictly before this
        method is called) -- this method itself performs no such check, it
        relies on the caller having already done so.
        """

        secrets = [self._credential.value] if self._credential is not None else []
        try:
            result = call_audio_provider_with_retry(
                lambda: self._transport(request),
                provider_name=self.provider_name,
                retry_policy=self._retry_policy,
                sleep=self._sleep,
            )
        except Exception as exc:
            # Redact before the error can leave this adapter -- a vendor
            # error message may otherwise echo back the credential value
            # (issue #236/#237). `from None` intentionally drops the
            # original (still secret-bearing) exception from the traceback
            # chain rather than merely from the message.
            raise redact_provider_error(exc, secrets) from None
        return result.model_copy(
            update={
                "provider_metadata": redact_provider_metadata(
                    result.provider_metadata, secrets
                )
            }
        )
