"""Cloud audio provider adapter and capability contract (#234).

Covers the acceptance criteria on the issue directly:

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
"""

from __future__ import annotations

import pytest

from core.models.manifest import ModelManifest
from core.schemas import GenerationRequest
from generators.audio.generator import AudioGenerator
from generators.audio.providers import (
    AudioProviderCapability,
    CloudAudioProviderRequest,
    CloudAudioProviderResult,
    ProviderAvailability,
    ProviderMisconfiguredError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
    ensure_capabilities_declared,
    manifest_declared_capabilities,
    run_cloud_audio_provider,
)


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
    adapter = _FakeCloudAudioProviderAdapter()
    result = run_cloud_audio_provider(
        adapter, _request(AudioProviderCapability.TEXT_TO_MUSIC)
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


def _cloud_manifest(*, capabilities: list[str], tags: list[str] | None = None) -> ModelManifest:
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
        default_params={"duration_seconds": 8, "capabilities": capabilities},
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
