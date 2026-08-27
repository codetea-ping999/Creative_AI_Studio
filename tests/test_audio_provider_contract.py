"""Cloud audio provider adapter and capability contract (#234, #235, #236).

Covers the acceptance criteria on all three issues directly:

#234:
- Cloud provider capability can be represented without vendor-specific route
  logic (generic dataclasses/Protocol, exercised with a fake adapter).
- Unsupported capabilities fail before network calls (a fake adapter's
  ``generate()`` is never invoked once a capability/availability check fails,
  and ``AudioGenerator.validate_request()`` rejects an unsupported cloud
  request before ``generate()`` would resolve any runtime).
- Local generation behavior remains unchanged when no cloud provider is
  selected (a `provider != "cloud"` manifest never even calls into this
  module).
- Fake adapter contract tests pass.

#235 (explicit cloud-provider egress opt-in):
- Default configuration (no env vars set) produces zero outbound provider
  calls -- a fake adapter's ``generate()`` is never touched.
- Global opt-in (`ALLOW_CLOUD_PROVIDERS=true`) alone is insufficient when
  the provider-specific `ALLOW_CLOUD_PROVIDER_<NAME>` flag is missing.
- A disabled/not-opted-in request fails before ``generate()`` -- i.e.
  before any prompt/audio payload data would be sent.
- Positive and negative opt-in cases are both covered.

#236 (resolve cloud provider secrets from the environment, redact logs):
- Credentials are resolved only from an env var named by
  `default_params.api_key_env`, never from a literal manifest field.
- A manifest holding a literal credential is rejected, both at
  `ModelManifest` construction time and again by
  `resolve_cloud_audio_provider_credential`.
- A sentinel secret value never survives into a credential's `repr()`/
  `str()`, a redacted error, or redacted provider metadata.
"""

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from core.models.manifest import ModelManifest
from core.schemas import GenerationRequest
from generators.audio.generator import AudioGenerator
from generators.audio.providers import (
    AudioProviderCapability,
    AudioProviderCredential,
    AudioProviderCredentialUnavailableError,
    CloudAudioProviderOptInRequiredError,
    CloudAudioProviderRequest,
    CloudAudioProviderResult,
    ProviderAvailability,
    ProviderMisconfiguredError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
    cloud_audio_provider_opt_in_granted,
    ensure_capabilities_declared,
    ensure_cloud_audio_provider_opt_in,
    manifest_declared_capabilities,
    redact_provider_error,
    redact_provider_metadata,
    redact_secrets,
    resolve_cloud_audio_provider_credential,
    run_cloud_audio_provider,
)

_OPT_IN_GRANTED_ENV = {
    "ALLOW_CLOUD_PROVIDERS": "true",
    "ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true",
}

_SENTINEL_SECRET = "sk-sentinel-1234567890abcdef"  # never a real credential


class _FakeCloudAudioProviderAdapter:
    """A minimal, vendor-neutral stand-in used to exercise the contract."""

    def __init__(
        self,
        *,
        provider_name: str = "fake-cloud",
        supported: frozenset[AudioProviderCapability] = frozenset(
            {AudioProviderCapability.TEXT_TO_MUSIC}
        ),
        availability: ProviderAvailability = ProviderAvailability.AVAILABLE,
    ) -> None:
        self.provider_name = provider_name
        self._supported = supported
        self._availability = availability
        self.generate_calls = 0

    def capabilities(self) -> frozenset[AudioProviderCapability]:
        return self._supported

    def availability(self) -> ProviderAvailability:
        return self._availability

    def generate(
        self, request: CloudAudioProviderRequest
    ) -> CloudAudioProviderResult:
        self.generate_calls += 1
        return CloudAudioProviderResult(
            audio_bytes=b"\x00\x00",
            sample_rate=32_000,
            channels=1,
            output_format="wav",
            provider_metadata={"destination": self.provider_name},
        )


def _request(capability: AudioProviderCapability) -> CloudAudioProviderRequest:
    return CloudAudioProviderRequest(
        prompt="a calm piano piece",
        capability=capability,
        task_type="text-to-music",
        model_id="fake-cloud-model",
    )


# --- Fake adapter contract tests -------------------------------------------------


def test_fake_adapter_satisfies_the_protocol_without_vendor_logic() -> None:
    from generators.audio.providers import CloudAudioProviderAdapter

    adapter = _FakeCloudAudioProviderAdapter()
    assert isinstance(adapter, CloudAudioProviderAdapter)
    assert adapter.capabilities() == frozenset({AudioProviderCapability.TEXT_TO_MUSIC})
    assert adapter.availability() == ProviderAvailability.AVAILABLE


def test_run_cloud_audio_provider_succeeds_for_a_supported_capability() -> None:
    # Egress opt-in (#235) is mandatory even for an otherwise fully valid,
    # available, capability-matching request -- both switches must be
    # granted here or run_cloud_audio_provider raises before generate().
    adapter = _FakeCloudAudioProviderAdapter()
    result = run_cloud_audio_provider(
        adapter,
        _request(AudioProviderCapability.TEXT_TO_MUSIC),
        env=_OPT_IN_GRANTED_ENV,
    )
    assert adapter.generate_calls == 1
    assert isinstance(result, CloudAudioProviderResult)
    assert result.sample_rate == 32_000
    assert result.provider_metadata["destination"] == "fake-cloud"


def test_unsupported_capability_fails_before_generate_is_called() -> None:
    adapter = _FakeCloudAudioProviderAdapter(
        supported=frozenset({AudioProviderCapability.TEXT_TO_MUSIC})
    )
    with pytest.raises(UnsupportedCapabilityError):
        run_cloud_audio_provider(
            adapter, _request(AudioProviderCapability.MELODY_CONDITIONING)
        )
    assert adapter.generate_calls == 0


@pytest.mark.parametrize(
    "availability",
    [
        ProviderAvailability.DISABLED,
        ProviderAvailability.MISCONFIGURED,
        ProviderAvailability.ERROR,
    ],
)
def test_unavailable_provider_fails_before_generate_is_called(
    availability: ProviderAvailability,
) -> None:
    adapter = _FakeCloudAudioProviderAdapter(availability=availability)
    with pytest.raises(ProviderUnavailableError):
        run_cloud_audio_provider(
            adapter, _request(AudioProviderCapability.TEXT_TO_MUSIC)
        )
    assert adapter.generate_calls == 0


def test_cloud_provider_request_and_result_reject_unknown_fields() -> None:
    """Extra fields would signal vendor-specific leakage into the contract."""

    with pytest.raises(Exception):
        CloudAudioProviderRequest(
            prompt="x",
            capability=AudioProviderCapability.TEXT_TO_MUSIC,
            task_type="text-to-music",
            model_id="m",
            vendor_specific_flag=True,  # type: ignore[call-arg]
        )


# --- Cloud provider egress opt-in (#235) ------------------------------------------


class CloudAudioProviderOptInGrantedTest(unittest.TestCase):
    """`ALLOW_CLOUD_PROVIDERS` and the per-provider variant, in isolation."""

    def test_default_closed_with_no_env_grants_nothing(self) -> None:
        assert cloud_audio_provider_opt_in_granted("fake-cloud", env={}) is False

    def test_global_flag_alone_is_insufficient(self) -> None:
        """The key #235 acceptance criterion: global opt-in alone does not

        grant a provider lacking its own provider-specific enablement --
        unlike the image provider's either-suffices convention (#256).
        """

        env = {"ALLOW_CLOUD_PROVIDERS": "true"}
        assert cloud_audio_provider_opt_in_granted("fake-cloud", env=env) is False

    def test_provider_specific_flag_alone_is_also_insufficient(self) -> None:
        env = {"ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true"}
        assert cloud_audio_provider_opt_in_granted("fake-cloud", env=env) is False

    def test_both_flags_together_grant_opt_in(self) -> None:
        assert (
            cloud_audio_provider_opt_in_granted("fake-cloud", env=_OPT_IN_GRANTED_ENV)
            is True
        )

    def test_provider_specific_flag_does_not_leak_to_other_providers(self) -> None:
        env = {
            "ALLOW_CLOUD_PROVIDERS": "true",
            "ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true",
        }
        assert cloud_audio_provider_opt_in_granted("other-cloud", env=env) is False

    def test_non_true_values_do_not_grant(self) -> None:
        env = {"ALLOW_CLOUD_PROVIDERS": "1", "ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "yes"}
        assert cloud_audio_provider_opt_in_granted("fake-cloud", env=env) is False

    def test_reads_process_environ_when_env_omitted(self) -> None:
        with patch.dict("os.environ", _OPT_IN_GRANTED_ENV, clear=False):
            assert cloud_audio_provider_opt_in_granted("fake-cloud") is True
        with patch.dict("os.environ", {}, clear=True):
            assert cloud_audio_provider_opt_in_granted("fake-cloud") is False


class EnsureCloudAudioProviderOptInTest(unittest.TestCase):
    """The guard a caller runs before any transport call is reachable."""

    def test_missing_opt_in_raises_naming_both_required_env_vars(self) -> None:
        with self.assertRaisesRegex(
            CloudAudioProviderOptInRequiredError, "fake-cloud"
        ) as ctx:
            ensure_cloud_audio_provider_opt_in("fake-cloud", env={})
        message = str(ctx.exception)
        self.assertIn("ALLOW_CLOUD_PROVIDERS", message)
        self.assertIn("ALLOW_CLOUD_PROVIDER_FAKE_CLOUD", message)

    def test_global_only_still_raises(self) -> None:
        with self.assertRaises(CloudAudioProviderOptInRequiredError):
            ensure_cloud_audio_provider_opt_in(
                "fake-cloud", env={"ALLOW_CLOUD_PROVIDERS": "true"}
            )

    def test_both_granted_does_not_raise(self) -> None:
        ensure_cloud_audio_provider_opt_in(
            "fake-cloud", env=_OPT_IN_GRANTED_ENV
        )  # must not raise


def test_run_cloud_audio_provider_never_touches_generate_under_default_settings() -> None:
    """Acceptance criterion: default configuration -> zero outbound calls.

    Exercises an otherwise fully valid, available, capability-matching
    request -- the only thing standing between it and `generate()` is the
    egress opt-in gate.
    """

    adapter = _FakeCloudAudioProviderAdapter()
    with pytest.raises(CloudAudioProviderOptInRequiredError):
        run_cloud_audio_provider(
            adapter, _request(AudioProviderCapability.TEXT_TO_MUSIC), env={}
        )
    assert adapter.generate_calls == 0


def test_run_cloud_audio_provider_rejects_global_opt_in_alone() -> None:
    """Global opt-in alone must not be sufficient to reach generate()."""

    adapter = _FakeCloudAudioProviderAdapter()
    with pytest.raises(CloudAudioProviderOptInRequiredError):
        run_cloud_audio_provider(
            adapter,
            _request(AudioProviderCapability.TEXT_TO_MUSIC),
            env={"ALLOW_CLOUD_PROVIDERS": "true"},
        )
    assert adapter.generate_calls == 0


def test_run_cloud_audio_provider_rejects_provider_specific_opt_in_alone() -> None:
    adapter = _FakeCloudAudioProviderAdapter()
    with pytest.raises(CloudAudioProviderOptInRequiredError):
        run_cloud_audio_provider(
            adapter,
            _request(AudioProviderCapability.TEXT_TO_MUSIC),
            env={"ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true"},
        )
    assert adapter.generate_calls == 0


def test_run_cloud_audio_provider_reaches_generate_exactly_once_when_both_granted() -> None:
    adapter = _FakeCloudAudioProviderAdapter()
    result = run_cloud_audio_provider(
        adapter,
        _request(AudioProviderCapability.TEXT_TO_MUSIC),
        env=_OPT_IN_GRANTED_ENV,
    )
    assert adapter.generate_calls == 1
    assert isinstance(result, CloudAudioProviderResult)


def test_unsupported_capability_still_fails_with_its_own_error_before_opt_in_check() -> None:
    """Capability/availability errors keep their specific type (unchanged

    from #234) even with no opt-in granted -- the opt-in gate does not mask
    a more specific validation failure.
    """

    adapter = _FakeCloudAudioProviderAdapter(
        supported=frozenset({AudioProviderCapability.TEXT_TO_MUSIC})
    )
    with pytest.raises(UnsupportedCapabilityError):
        run_cloud_audio_provider(
            adapter, _request(AudioProviderCapability.MELODY_CONDITIONING), env={}
        )
    assert adapter.generate_calls == 0


# --- Manifest capability declaration ---------------------------------------------


def test_manifest_declared_capabilities_parses_known_values() -> None:
    declared = manifest_declared_capabilities(
        {"capabilities": ["text-to-music", "long-form"]},
        manifest_label="Model 'x'",
    )
    assert declared == frozenset(
        {AudioProviderCapability.TEXT_TO_MUSIC, AudioProviderCapability.LONG_FORM}
    )


def test_manifest_declared_capabilities_rejects_missing_field() -> None:
    with pytest.raises(ProviderMisconfiguredError):
        manifest_declared_capabilities({}, manifest_label="Model 'x'")


def test_manifest_declared_capabilities_rejects_unknown_value() -> None:
    with pytest.raises(ProviderMisconfiguredError):
        manifest_declared_capabilities(
            {"capabilities": ["not-a-real-capability"]},
            manifest_label="Model 'x'",
        )


def test_ensure_capabilities_declared_raises_for_missing_capability() -> None:
    with pytest.raises(UnsupportedCapabilityError):
        ensure_capabilities_declared(
            frozenset({AudioProviderCapability.TEXT_TO_MUSIC}),
            frozenset({AudioProviderCapability.MELODY_CONDITIONING}),
            manifest_label="Model 'x'",
        )


def test_ensure_capabilities_declared_passes_for_a_superset() -> None:
    ensure_capabilities_declared(
        frozenset(
            {AudioProviderCapability.TEXT_TO_MUSIC, AudioProviderCapability.LONG_FORM}
        ),
        frozenset({AudioProviderCapability.TEXT_TO_MUSIC}),
        manifest_label="Model 'x'",
    )


# --- AudioGenerator.validate_request() wiring ------------------------------------


class _FakeModelService:
    """Returns one fixed manifest regardless of the requested model_id."""

    def __init__(self, manifest: ModelManifest) -> None:
        self._manifest = manifest

    def get_manifest(self, model_id, media_type, task_type=None):
        return self._manifest


def _cloud_manifest(
    *,
    capabilities: list[str],
    tags: list[str] | None = None,
    extra_default_params: dict[str, object] | None = None,
) -> ModelManifest:
    return ModelManifest(
        id="cloud-music-test",
        public_id="cloud-music",
        display_name="Fake Cloud Music Provider",
        media_type="audio",
        task_type="text-to-music",
        provider="cloud",
        runtime="cloud_test",
        remote_ref="https://cloud-provider.example/v1/audio",
        loader="cloud_test_loader",
        default_params={
            "duration_seconds": 8,
            "capabilities": capabilities,
            **(extra_default_params or {}),
        },
        tags=tags or ["audio", "music"],
        is_default=False,
        enabled=True,
    )


def _local_manifest() -> ModelManifest:
    return ModelManifest(
        id="musicgen-small-test",
        public_id="musicgen-small",
        display_name="MusicGen Small",
        media_type="audio",
        task_type="text-to-music",
        provider="test",
        runtime="transformers",
        local_path="./models/audio/musicgen-small",
        loader="transformers_musicgen_loader",
        default_params={"duration_seconds": 8},
        tags=["audio", "music"],
        is_default=True,
        enabled=True,
    )


def _generation_request(**params: object) -> GenerationRequest:
    return GenerationRequest(
        media_type="audio",
        prompt="a calm piano piece",
        model_id="cloud-music",
        params=dict(params),
    )


def test_cloud_manifest_missing_required_capability_fails_validation() -> None:
    """Melody conditioning requested, but the manifest never declared it."""

    manifest = _cloud_manifest(capabilities=["text-to-music"])
    generator = AudioGenerator(_FakeModelService(manifest))
    request = _generation_request(
        reuse_action="melody",
        source_asset_id="aud_ref",
    )
    with pytest.raises(UnsupportedCapabilityError):
        generator.validate_request(request)


def test_cloud_manifest_with_required_capability_passes_validation() -> None:
    manifest = _cloud_manifest(
        capabilities=["text-to-music", "melody-conditioning"]
    )
    generator = AudioGenerator(_FakeModelService(manifest))
    request = _generation_request(
        reuse_action="melody",
        source_asset_id="aud_ref",
    )
    generator.validate_request(request)  # must not raise


def test_cloud_manifest_missing_capabilities_field_is_misconfigured() -> None:
    manifest = _cloud_manifest(capabilities=[])
    generator = AudioGenerator(_FakeModelService(manifest))
    request = _generation_request()
    with pytest.raises(ProviderMisconfiguredError):
        generator.validate_request(request)


def test_local_provider_manifest_is_unaffected_by_cloud_validation() -> None:
    """No `default_params.capabilities` at all -- and it must not matter."""

    manifest = _local_manifest()
    assert "capabilities" not in manifest.default_params
    generator = AudioGenerator(_FakeModelService(manifest))
    request = GenerationRequest(
        media_type="audio",
        prompt="a calm piano piece",
        model_id="musicgen-small",
    )
    generator.validate_request(request)  # must not raise


# --- Credential resolution and redaction (#236) -----------------------------------


class RejectLiteralCredentialFieldsForCloudAudioManifestsTest(unittest.TestCase):
    """A `provider: cloud` audio manifest must never carry a literal credential."""

    def test_manifest_construction_rejects_a_literal_api_key(self) -> None:
        with self.assertRaisesRegex(ValidationError, "literal credential"):
            _cloud_manifest(
                capabilities=["text-to-music"],
                extra_default_params={"api_key": _SENTINEL_SECRET},
            )

    def test_manifest_construction_accepts_the_api_key_env_indirection(self) -> None:
        manifest = _cloud_manifest(
            capabilities=["text-to-music"],
            extra_default_params={"api_key_env": "ACME_AUDIO_API_KEY"},
        )
        self.assertEqual(manifest.default_params["api_key_env"], "ACME_AUDIO_API_KEY")

    def test_manifest_error_never_contains_the_secret_value(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            _cloud_manifest(
                capabilities=["text-to-music"],
                extra_default_params={"api_key": _SENTINEL_SECRET},
            )
        self.assertNotIn(_SENTINEL_SECRET, str(ctx.exception))


class ResolveCloudAudioProviderCredentialTest(unittest.TestCase):
    """`resolve_cloud_audio_provider_credential` (issue #236)."""

    def test_no_api_key_env_declared_needs_no_credential(self) -> None:
        credential = resolve_cloud_audio_provider_credential(
            {"capabilities": ["text-to-music"]},
            provider_name="fake-cloud",
            manifest_label="cloud-music-test",
            env={"ACME_AUDIO_API_KEY": _SENTINEL_SECRET},
        )
        self.assertIsNone(credential)

    def test_resolves_the_credential_from_the_named_env_var(self) -> None:
        credential = resolve_cloud_audio_provider_credential(
            {"api_key_env": "ACME_AUDIO_API_KEY"},
            provider_name="fake-cloud",
            manifest_label="cloud-music-test",
            env={"ACME_AUDIO_API_KEY": _SENTINEL_SECRET},
        )
        assert credential is not None
        self.assertEqual(credential.env_var, "ACME_AUDIO_API_KEY")
        self.assertEqual(credential.value, _SENTINEL_SECRET)

    def test_raises_actionable_error_naming_the_env_var_when_unset(self) -> None:
        with self.assertRaises(AudioProviderCredentialUnavailableError) as ctx:
            resolve_cloud_audio_provider_credential(
                {"api_key_env": "ACME_AUDIO_API_KEY"},
                provider_name="fake-cloud",
                manifest_label="cloud-music-test",
                env={},
            )
        message = str(ctx.exception)
        self.assertIn("ACME_AUDIO_API_KEY", message)
        self.assertIn("fake-cloud", message)

    def test_raises_when_the_env_var_is_set_but_empty(self) -> None:
        with self.assertRaises(AudioProviderCredentialUnavailableError):
            resolve_cloud_audio_provider_credential(
                {"api_key_env": "ACME_AUDIO_API_KEY"},
                provider_name="fake-cloud",
                manifest_label="cloud-music-test",
                env={"ACME_AUDIO_API_KEY": ""},
            )

    def test_literal_credential_in_default_params_is_rejected_before_env_lookup(self) -> None:
        with self.assertRaises(ProviderMisconfiguredError):
            resolve_cloud_audio_provider_credential(
                {"api_key": _SENTINEL_SECRET},
                provider_name="fake-cloud",
                manifest_label="cloud-music-test",
                env={},
            )

    def test_reads_process_environ_when_env_omitted(self) -> None:
        with patch.dict("os.environ", {"ACME_AUDIO_API_KEY": _SENTINEL_SECRET}, clear=False):
            credential = resolve_cloud_audio_provider_credential(
                {"api_key_env": "ACME_AUDIO_API_KEY"},
                provider_name="fake-cloud",
                manifest_label="cloud-music-test",
            )
        assert credential is not None
        self.assertEqual(credential.value, _SENTINEL_SECRET)


class AudioProviderCredentialReprTest(unittest.TestCase):
    """The credential's own repr/str must never leak `.value` (issue #236)."""

    def test_repr_omits_the_value(self) -> None:
        credential = AudioProviderCredential(env_var="ACME_AUDIO_API_KEY", value=_SENTINEL_SECRET)
        self.assertNotIn(_SENTINEL_SECRET, repr(credential))
        self.assertIn("ACME_AUDIO_API_KEY", repr(credential))

    def test_logging_the_credential_object_does_not_leak_the_value(self) -> None:
        credential = AudioProviderCredential(env_var="ACME_AUDIO_API_KEY", value=_SENTINEL_SECRET)
        with self.assertLogs("test.audio.providers", level="INFO") as ctx:
            logging.getLogger("test.audio.providers").info("resolved %r", credential)
        self.assertNotIn(_SENTINEL_SECRET, "\n".join(ctx.output))


class RedactSecretsTest(unittest.TestCase):
    def test_redacts_every_occurrence(self) -> None:
        text = f"Authorization: Bearer {_SENTINEL_SECRET} (retry with {_SENTINEL_SECRET})"
        redacted = redact_secrets(text, [_SENTINEL_SECRET])
        self.assertNotIn(_SENTINEL_SECRET, redacted)
        self.assertEqual(redacted.count("<redacted>"), 2)

    def test_ignores_none_and_empty_secrets(self) -> None:
        text = "no secrets here"
        self.assertEqual(redact_secrets(text, [None, ""]), text)

    def test_leaves_unrelated_text_untouched(self) -> None:
        text = "a calm piano piece"
        self.assertEqual(redact_secrets(text, [_SENTINEL_SECRET]), text)


class RedactProviderErrorTest(unittest.TestCase):
    """A caught provider error must never propagate a secret (issue #236)."""

    def test_redacts_the_secret_from_the_error_message(self) -> None:
        original = RuntimeError(f"upstream rejected key {_SENTINEL_SECRET}")
        self.assertIn(_SENTINEL_SECRET, str(original))  # the test is not vacuous

        redacted = redact_provider_error(original, [_SENTINEL_SECRET])

        self.assertNotIn(_SENTINEL_SECRET, str(redacted))
        self.assertIsInstance(redacted, RuntimeError)

    def test_preserves_a_custom_exception_type_with_a_single_message_constructor(self) -> None:
        original = AudioProviderCredentialUnavailableError(f"leaked {_SENTINEL_SECRET}")
        redacted = redact_provider_error(original, [_SENTINEL_SECRET])
        self.assertIsInstance(redacted, AudioProviderCredentialUnavailableError)
        self.assertNotIn(_SENTINEL_SECRET, str(redacted))

    def test_falls_back_to_runtime_error_when_the_type_cannot_be_reconstructed(self) -> None:
        class _WeirdError(Exception):
            def __init__(self, code: int, message: str) -> None:  # two required args
                super().__init__(message)
                self.code = code

        original = _WeirdError(42, f"failed with {_SENTINEL_SECRET}")
        redacted = redact_provider_error(original, [_SENTINEL_SECRET])
        self.assertIsInstance(redacted, RuntimeError)
        self.assertNotIn(_SENTINEL_SECRET, str(redacted))


class RedactProviderMetadataTest(unittest.TestCase):
    """Diagnostics/metadata dicts must never carry a secret through (issue #236)."""

    def test_redacts_a_top_level_string_value(self) -> None:
        sanitized = redact_provider_metadata(
            {"note": f"used key {_SENTINEL_SECRET}"}, [_SENTINEL_SECRET]
        )
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(sanitized))

    def test_redacts_nested_dicts_and_lists(self) -> None:
        metadata = {
            "request": {"headers": {"Authorization": f"Bearer {_SENTINEL_SECRET}"}},
            "warnings": [f"retrying with {_SENTINEL_SECRET}", "no secret here"],
        }
        sanitized = redact_provider_metadata(metadata, [_SENTINEL_SECRET])
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(sanitized))
        self.assertEqual(sanitized["warnings"][1], "no secret here")

    def test_non_string_values_pass_through_unchanged(self) -> None:
        sanitized = redact_provider_metadata({"request_count": 3, "ok": True}, [_SENTINEL_SECRET])
        self.assertEqual(sanitized, {"request_count": 3, "ok": True})


class SentinelSecretPersistenceTest(unittest.TestCase):
    """The sentinel must never survive into request/job/asset-shaped data.

    Issue #236 acceptance criterion: "Sentinel secret values do not appear
    in logs, errors, or job metadata."
    """

    def test_credential_resolution_never_touches_the_provider_request(self) -> None:
        resolve_cloud_audio_provider_credential(
            {"api_key_env": "ACME_AUDIO_API_KEY"},
            provider_name="fake-cloud",
            manifest_label="cloud-music-test",
            env={"ACME_AUDIO_API_KEY": _SENTINEL_SECRET},
        )
        request = _request(AudioProviderCapability.TEXT_TO_MUSIC)
        self.assertNotIn(_SENTINEL_SECRET, request.model_dump_json())

    def test_provider_result_metadata_sanitized_before_it_would_reach_job_metadata(
        self,
    ) -> None:
        # Simulates a vendor SDK echoing the credential back in a response
        # field -- a caller must run the result's provider_metadata through
        # `redact_provider_metadata` before attaching it to job/asset
        # metadata (the "job metadata" leg of the acceptance criterion).
        result = CloudAudioProviderResult(
            audio_bytes=b"\x00\x00",
            sample_rate=32_000,
            channels=1,
            output_format="wav",
            provider_metadata={
                "debug_request_headers": {"Authorization": f"Bearer {_SENTINEL_SECRET}"}
            },
        )
        sanitized_metadata = redact_provider_metadata(
            result.provider_metadata, [_SENTINEL_SECRET]
        )
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(sanitized_metadata))

    def test_logging_a_redacted_provider_error_never_emits_the_secret(self) -> None:
        original = RuntimeError(f"upstream rejected key {_SENTINEL_SECRET}")
        redacted = redact_provider_error(original, [_SENTINEL_SECRET])

        with self.assertLogs("test.audio.providers", level="ERROR") as ctx:
            logging.getLogger("test.audio.providers").error(
                "provider call failed: %s", redacted
            )

        self.assertNotIn(_SENTINEL_SECRET, "\n".join(ctx.output))
