"""Tests for the provider-neutral image generation contract (issue #255).

Covers `generators/image/providers.py` in isolation (capability declaration,
pre-invocation rejection of unsupported parameters, protocol conformance for
both the local adapter and a fake alternative provider) and confirms
`ImageGenerator` still executes local generation successfully when routed
through that contract.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from bootstrap import create_default_model_service
from core.models.manifest import ModelManifest
from core.schemas import GenerationRequest
from generators.image.generator import ImageGenerator
from generators.image.providers import (
    ImageGenerationSpec,
    ImageProvider,
    ImageProviderCapabilities,
    ImageProviderCredential,
    ImageProviderCredentialUnavailableError,
    ImageProviderIdentity,
    ImageProviderMisconfiguredError,
    ImageProviderPreflightDisclosure,
    ImageProviderReferenceAsset,
    ImageProviderResult,
    LocalDiffusersImageProvider,
    RemoteImageProviderOptInRequiredError,
    UnsupportedImageParameterError,
    build_image_provider_preflight,
    cloud_image_provider_opt_in_granted,
    ensure_image_provider_opt_in,
    is_local_image_provider,
    redact_provider_error,
    redact_provider_metadata,
    redact_secrets,
    resolve_image_provider_credential,
    run_image_provider_request,
    summarize_prompt_for_preflight,
    validate_capabilities,
)

_SENTINEL_SECRET = "sk-sentinel-1234567890abcdef"  # never a real credential


def _spec(**overrides: object) -> ImageGenerationSpec:
    defaults: dict[str, object] = {
        "prompt": "a fox in a field",
        "negative_prompt": None,
        "width": 64,
        "height": 64,
        "seed": None,
        "batch_size": 1,
    }
    defaults.update(overrides)
    return ImageGenerationSpec(**defaults)  # type: ignore[arg-type]


class _RecordingPipelineResult:
    def __init__(self, image: Image.Image) -> None:
        self.images = [image]


class _RecordingPipeline:
    """Minimal diffusers-pipeline-shaped fake that records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: Any) -> _RecordingPipelineResult:
        self.calls.append(kwargs)
        width = int(kwargs.get("width", 64))
        height = int(kwargs.get("height", 64))
        return _RecordingPipelineResult(Image.new("RGB", (width, height), color=(1, 2, 3)))

    def to(self, device: str) -> "_RecordingPipeline":
        return self


def _fake_diffusers_load(self: object, manifest: object) -> dict[str, object]:
    return {
        "stub": False,
        "loader": self.__class__.__name__,
        "manifest_id": manifest.id,  # type: ignore[attr-defined]
        "display_name": manifest.display_name,  # type: ignore[attr-defined]
        "runtime": manifest.runtime,  # type: ignore[attr-defined]
        "provider": manifest.provider,  # type: ignore[attr-defined]
        "local_path": manifest.local_path,  # type: ignore[attr-defined]
        "remote_ref": manifest.remote_ref,  # type: ignore[attr-defined]
        "dtype": manifest.dtype,  # type: ignore[attr-defined]
        "load_dtype": "float32",
        "torch_dtype": "float32",
        "weight_dtype": "float16",
        "variant": "fp16",
        "device": "cpu",
        "default_params": dict(manifest.default_params),  # type: ignore[attr-defined]
        "path_exists": True,
        "pipeline": _RecordingPipeline(),
    }


class _FakeCloudImageProvider:
    """A structurally-different provider used only to prove `ImageProvider`
    is a real cross-backend contract and not something only the local
    adapter happens to satisfy."""

    def __init__(self, *, capabilities: ImageProviderCapabilities) -> None:
        self._capabilities = capabilities
        self.received: list[ImageGenerationSpec] = []

    @property
    def provider_id(self) -> str:
        return "fake-cloud"

    @property
    def capabilities(self) -> ImageProviderCapabilities:
        return self._capabilities

    def generate_image(
        self, spec: ImageGenerationSpec, *, request_id: str
    ) -> ImageProviderResult:
        validate_capabilities(self._capabilities, spec)
        self.received.append(spec)
        identity = ImageProviderIdentity(
            provider_id=self.provider_id, model_id="fake-model", request_id=request_id
        )
        return ImageProviderResult(
            identity=identity, image=Image.new("RGB", (spec.width, spec.height))
        )


class ImageProviderCapabilitiesTest(unittest.TestCase):
    """`validate_capabilities` rejects each undeclared capability on its own."""

    def test_defaults_are_conservative(self) -> None:
        capabilities = ImageProviderCapabilities()
        self.assertTrue(capabilities.supports_text_to_image)
        self.assertFalse(capabilities.supports_reference_image)
        self.assertFalse(capabilities.supports_lora)
        self.assertFalse(capabilities.supports_seed)
        self.assertEqual(capabilities.max_batch, 1)

    def test_accepts_a_request_within_declared_capabilities(self) -> None:
        capabilities = ImageProviderCapabilities(
            supports_lora=True, supports_seed=True, max_batch=4
        )
        spec = _spec(seed=7, lora_path="/models/lora/style.safetensors", batch_size=4)
        validate_capabilities(capabilities, spec)  # must not raise

    def test_rejects_reference_image_when_unsupported(self) -> None:
        capabilities = ImageProviderCapabilities(supports_reference_image=False)
        spec = _spec(reference_image_path="/tmp/ref.png")
        with self.assertRaisesRegex(UnsupportedImageParameterError, "reference_image_path"):
            validate_capabilities(capabilities, spec)

    def test_rejects_lora_when_unsupported(self) -> None:
        capabilities = ImageProviderCapabilities(supports_lora=False)
        spec = _spec(lora_path="/models/lora/style.safetensors")
        with self.assertRaisesRegex(UnsupportedImageParameterError, "lora_path"):
            validate_capabilities(capabilities, spec)

    def test_rejects_seed_when_unsupported(self) -> None:
        capabilities = ImageProviderCapabilities(supports_seed=False)
        spec = _spec(seed=42)
        with self.assertRaisesRegex(UnsupportedImageParameterError, "seed"):
            validate_capabilities(capabilities, spec)

    def test_rejects_batch_size_over_max(self) -> None:
        capabilities = ImageProviderCapabilities(max_batch=1)
        spec = _spec(batch_size=2)
        with self.assertRaisesRegex(UnsupportedImageParameterError, "batch_size"):
            validate_capabilities(capabilities, spec)

    def test_rejects_size_outside_supported_range(self) -> None:
        capabilities = ImageProviderCapabilities(min_size=64, max_size=1024)
        with self.assertRaisesRegex(UnsupportedImageParameterError, "width"):
            validate_capabilities(capabilities, _spec(width=2048, height=64))
        with self.assertRaisesRegex(UnsupportedImageParameterError, "height"):
            validate_capabilities(capabilities, _spec(width=64, height=32))

    def test_rejects_size_not_a_multiple_of_step(self) -> None:
        capabilities = ImageProviderCapabilities(size_step=8, min_size=1, max_size=4096)
        with self.assertRaisesRegex(UnsupportedImageParameterError, "multiple of 8"):
            validate_capabilities(capabilities, _spec(width=65, height=64))


class ImageProviderContractShapeTest(unittest.TestCase):
    """The contract is a real cross-provider Protocol, not local-only shape."""

    def test_local_diffusers_provider_satisfies_the_protocol(self) -> None:
        provider = LocalDiffusersImageProvider(model_id="sdxl", pipeline=_RecordingPipeline())
        self.assertIsInstance(provider, ImageProvider)

    def test_a_structurally_different_fake_provider_also_satisfies_the_protocol(
        self,
    ) -> None:
        provider = _FakeCloudImageProvider(
            capabilities=ImageProviderCapabilities(supports_reference_image=True)
        )
        self.assertIsInstance(provider, ImageProvider)

    def test_fake_provider_enforces_its_own_declared_capabilities(self) -> None:
        provider = _FakeCloudImageProvider(
            capabilities=ImageProviderCapabilities(supports_lora=False)
        )
        with self.assertRaises(UnsupportedImageParameterError):
            provider.generate_image(
                _spec(lora_path="/models/lora/style.safetensors"), request_id="req-1"
            )
        self.assertEqual(provider.received, [])


class LocalDiffusersImageProviderTest(unittest.TestCase):
    """The local adapter wraps the existing pipeline call unchanged."""

    def test_generate_image_calls_pipeline_with_the_given_kwargs_and_returns_identity(
        self,
    ) -> None:
        pipeline = _RecordingPipeline()
        provider = LocalDiffusersImageProvider(model_id="sdxl", pipeline=pipeline)
        pipeline_kwargs = {"prompt": "a fox", "width": 64, "height": 64}

        result = provider.generate_image(
            _spec(seed=1),
            request_id="img_abc_v1",
            pipeline_kwargs=pipeline_kwargs,
        )

        self.assertEqual(pipeline.calls, [pipeline_kwargs])
        self.assertEqual(result.identity.provider_id, "local-diffusers")
        self.assertEqual(result.identity.model_id, "sdxl")
        self.assertEqual(result.identity.request_id, "img_abc_v1")
        self.assertEqual(result.image.size, (64, 64))

    def test_generate_image_rejects_unsupported_parameters_before_calling_pipeline(
        self,
    ) -> None:
        pipeline = _RecordingPipeline()
        provider = LocalDiffusersImageProvider(
            model_id="sdxl",
            pipeline=pipeline,
            capabilities=ImageProviderCapabilities(supports_lora=False),
        )

        with self.assertRaises(UnsupportedImageParameterError):
            provider.generate_image(
                _spec(lora_path="/models/lora/style.safetensors"),
                request_id="img_abc_v1",
                pipeline_kwargs={"prompt": "a fox"},
            )

    def test_default_fallback_kwargs_are_used_when_pipeline_kwargs_is_omitted(
        self,
    ) -> None:
        pipeline = _RecordingPipeline()
        provider = LocalDiffusersImageProvider(model_id="sdxl", pipeline=pipeline)

        result = provider.generate_image(_spec(), request_id="img_abc_v1")

        self.assertEqual(
            pipeline.calls,
            [
                {
                    "prompt": "a fox in a field",
                    "negative_prompt": None,
                    "width": 64,
                    "height": 64,
                }
            ],
        )
        self.assertEqual(result.identity.request_id, "img_abc_v1")

    def test_default_fallback_rejects_a_seed_it_cannot_honor(self) -> None:
        pipeline = _RecordingPipeline()
        provider = LocalDiffusersImageProvider(model_id="sdxl", pipeline=pipeline)

        with self.assertRaisesRegex(UnsupportedImageParameterError, "pipeline_kwargs"):
            provider.generate_image(_spec(seed=7), request_id="img_abc_v1")
        self.assertEqual(pipeline.calls, [])

    def test_default_fallback_rejects_a_lora_it_cannot_honor(self) -> None:
        pipeline = _RecordingPipeline()
        provider = LocalDiffusersImageProvider(model_id="sdxl", pipeline=pipeline)

        with self.assertRaisesRegex(UnsupportedImageParameterError, "pipeline_kwargs"):
            provider.generate_image(
                _spec(lora_path="/models/lora/style.safetensors"),
                request_id="img_abc_v1",
            )
        self.assertEqual(pipeline.calls, [])

        self.assertEqual(pipeline.calls, [], "pipeline must not be invoked when rejected")


class ImageGeneratorRoutesThroughContractTest(unittest.TestCase):
    """Existing local image generation still executes end-to-end through the
    common contract, with no change in observable behavior."""

    def test_local_image_generation_executes_through_the_common_contract(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load,
            ):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="A lighthouse at dawn",
                        model_id="sdxl",
                        params={"steps": 2, "width": 64, "height": 64},
                    )
                )
                pipeline = service.get_runtime("sdxl", "image", "text-to-image")["pipeline"]

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(pipeline.calls), 1)
            self.assertEqual(result.metadata["image_provider_id"], "local-diffusers")
            self.assertEqual(
                [item["provider_id"] for item in result.metadata["variations"]],
                ["local-diffusers"],
            )
            self.assertEqual(
                result.metadata["variations"][0]["provider_request_id"],
                f"{result.job_id}_v1",
            )
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_unsupported_parameter_is_rejected_before_any_pipeline_call(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load,
            ):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)
                # variation_count is already bounded to [1, 4] by validate_request,
                # matching the local provider's declared max_batch, so exercise a
                # capability the local provider genuinely does not declare instead:
                # a size outside its declared [min_size, max_size] range.
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Oversized request",
                            model_id="sdxl",
                            params={"steps": 1, "width": 4096, "height": 4096},
                        )
                    )
                pipeline = service.get_runtime("sdxl", "image", "text-to-image")["pipeline"]

            self.assertEqual(
                pipeline.calls, [], "pipeline must not be invoked when size is rejected"
            )


class IsLocalImageProviderTest(unittest.TestCase):
    """Local-vs-remote classification drives every other guard in this module."""

    def test_local_diffusers_provider_id_is_local(self) -> None:
        self.assertTrue(is_local_image_provider("local-diffusers"))

    def test_a_remote_looking_provider_id_is_not_local(self) -> None:
        self.assertFalse(is_local_image_provider("fake-cloud"))
        self.assertFalse(is_local_image_provider("midjourney"))


class CloudImageProviderOptInTest(unittest.TestCase):
    """`ALLOW_CLOUD_PROVIDERS` and the per-provider variant, in isolation."""

    def test_default_closed_with_no_env_grants_nothing(self) -> None:
        self.assertFalse(cloud_image_provider_opt_in_granted("fake-cloud", env={}))

    def test_global_flag_grants_any_remote_provider(self) -> None:
        env = {"ALLOW_CLOUD_PROVIDERS": "true"}
        self.assertTrue(cloud_image_provider_opt_in_granted("fake-cloud", env=env))
        self.assertTrue(cloud_image_provider_opt_in_granted("another-vendor", env=env))

    def test_provider_specific_flag_grants_only_that_provider(self) -> None:
        env = {"ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true"}
        self.assertTrue(cloud_image_provider_opt_in_granted("fake-cloud", env=env))
        self.assertFalse(cloud_image_provider_opt_in_granted("other-cloud", env=env))

    def test_non_true_values_do_not_grant(self) -> None:
        env = {"ALLOW_CLOUD_PROVIDERS": "1", "ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "yes"}
        self.assertFalse(cloud_image_provider_opt_in_granted("fake-cloud", env=env))

    def test_reads_process_environ_when_env_omitted(self) -> None:
        with patch.dict("os.environ", {"ALLOW_CLOUD_PROVIDERS": "true"}, clear=False):
            self.assertTrue(cloud_image_provider_opt_in_granted("fake-cloud"))
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(cloud_image_provider_opt_in_granted("fake-cloud"))


class EnsureImageProviderOptInTest(unittest.TestCase):
    """The guard a caller runs before any transport call is reachable."""

    def test_local_provider_is_always_allowed_with_no_env(self) -> None:
        ensure_image_provider_opt_in("local-diffusers", env={})  # must not raise

    def test_remote_provider_without_opt_in_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            RemoteImageProviderOptInRequiredError, "fake-cloud"
        ) as ctx:
            ensure_image_provider_opt_in("fake-cloud", env={})
        # Actionable: names the env vars an operator would set.
        self.assertIn("ALLOW_CLOUD_PROVIDERS", str(ctx.exception))
        self.assertIn("ALLOW_CLOUD_PROVIDER_FAKE_CLOUD", str(ctx.exception))

    def test_remote_provider_with_global_opt_in_is_allowed(self) -> None:
        ensure_image_provider_opt_in(
            "fake-cloud", env={"ALLOW_CLOUD_PROVIDERS": "true"}
        )  # must not raise

    def test_remote_provider_with_provider_specific_opt_in_is_allowed(self) -> None:
        ensure_image_provider_opt_in(
            "fake-cloud", env={"ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true"}
        )  # must not raise

    def test_provider_specific_opt_in_does_not_leak_to_other_providers(self) -> None:
        env = {"ALLOW_CLOUD_PROVIDER_FAKE_CLOUD": "true"}
        with self.assertRaises(RemoteImageProviderOptInRequiredError):
            ensure_image_provider_opt_in("other-cloud", env=env)


class SummarizePromptForPreflightTest(unittest.TestCase):
    def test_short_prompt_is_returned_unchanged_aside_from_whitespace(self) -> None:
        self.assertEqual(
            summarize_prompt_for_preflight("a  fox\nin   a field"), "a fox in a field"
        )

    def test_long_prompt_is_truncated_with_an_ellipsis(self) -> None:
        prompt = "a " * 200
        summary = summarize_prompt_for_preflight(prompt, max_chars=20)
        self.assertEqual(len(summary), 20)
        self.assertTrue(summary.endswith("…"))
        self.assertTrue(prompt.startswith(summary[:-1]))


class BuildImageProviderPreflightTest(unittest.TestCase):
    """The disclosure available before any request submission (issue #256)."""

    def test_local_provider_preflight_reports_local_destination(self) -> None:
        disclosure = build_image_provider_preflight(
            _spec(), provider_id="local-diffusers", model_id="sdxl"
        )
        self.assertIsInstance(disclosure, ImageProviderPreflightDisclosure)
        self.assertEqual(disclosure.destination, "local")
        self.assertEqual(disclosure.provider_id, "local-diffusers")
        self.assertEqual(disclosure.model_id, "sdxl")
        self.assertEqual(disclosure.reference_assets, ())
        self.assertEqual(disclosure.estimated_request_count, 1)

    def test_remote_provider_preflight_reports_remote_destination(self) -> None:
        disclosure = build_image_provider_preflight(
            _spec(), provider_id="fake-cloud", model_id="fake-model"
        )
        self.assertEqual(disclosure.destination, "remote")

    def test_preflight_carries_reference_assets_and_their_roles(self) -> None:
        reference_assets = [
            ImageProviderReferenceAsset(asset_id="asset_1", role="style-reference"),
            ImageProviderReferenceAsset(asset_id="asset_2", role="character-reference"),
        ]
        disclosure = build_image_provider_preflight(
            _spec(),
            provider_id="fake-cloud",
            model_id="fake-model",
            reference_assets=reference_assets,
        )
        self.assertEqual(disclosure.reference_assets, tuple(reference_assets))
        self.assertEqual(
            [asset.role for asset in disclosure.reference_assets],
            ["style-reference", "character-reference"],
        )

    def test_preflight_estimated_request_count_reflects_batch_size(self) -> None:
        disclosure = build_image_provider_preflight(
            _spec(batch_size=4), provider_id="fake-cloud", model_id="fake-model"
        )
        self.assertEqual(disclosure.estimated_request_count, 4)

    def test_preflight_does_not_require_opt_in_to_build(self) -> None:
        # Building the disclosure must succeed even with no opt-in granted --
        # a caller needs to be able to show a human what would be sent
        # *before* that human decides whether to opt in.
        with patch.dict("os.environ", {}, clear=True):
            disclosure = build_image_provider_preflight(
                _spec(), provider_id="fake-cloud", model_id="fake-model"
            )
        self.assertEqual(disclosure.destination, "remote")


class RunImageProviderRequestTest(unittest.TestCase):
    """Proves the opt-in guard executes before transport is ever reached."""

    def test_local_provider_reaches_transport_with_no_env_set(self) -> None:
        calls: list[str] = []
        disclosure, result = run_image_provider_request(
            _spec(),
            provider_id="local-diffusers",
            model_id="sdxl",
            transport=lambda: calls.append("called") or "ok",
            env={},
        )
        self.assertEqual(calls, ["called"])
        self.assertEqual(result, "ok")
        self.assertEqual(disclosure.destination, "local")

    def test_remote_provider_without_opt_in_never_reaches_transport(self) -> None:
        calls: list[str] = []
        with self.assertRaises(RemoteImageProviderOptInRequiredError):
            run_image_provider_request(
                _spec(),
                provider_id="fake-cloud",
                model_id="fake-model",
                transport=lambda: calls.append("called"),
                env={},
            )
        self.assertEqual(
            calls, [], "transport must not be invoked when opt-in is missing"
        )

    def test_remote_provider_with_opt_in_reaches_transport_exactly_once(self) -> None:
        calls: list[str] = []
        disclosure, result = run_image_provider_request(
            _spec(),
            provider_id="fake-cloud",
            model_id="fake-model",
            transport=lambda: calls.append("called") or "sent",
            env={"ALLOW_CLOUD_PROVIDERS": "true"},
        )
        self.assertEqual(calls, ["called"])
        self.assertEqual(result, "sent")
        self.assertEqual(disclosure.destination, "remote")
        self.assertEqual(disclosure.provider_id, "fake-cloud")


def _cloud_manifest(*, default_params: dict[str, object]) -> ModelManifest:
    return ModelManifest(
        id="cloud-image-test",
        public_id="cloud-image",
        display_name="Fake Cloud Image Provider",
        media_type="image",
        task_type="text-to-image",
        provider="cloud",
        runtime="cloud_test",
        remote_ref="https://cloud-image-provider.example/v1",
        loader="cloud_test_loader",
        default_params=default_params,
        is_default=False,
        enabled=True,
    )


class RejectLiteralCredentialFieldsTest(unittest.TestCase):
    """Manifests must never carry a literal credential (issue #257)."""

    def test_manifest_construction_rejects_a_literal_api_key(self) -> None:
        with self.assertRaisesRegex(ValidationError, "literal credential"):
            _cloud_manifest(default_params={"api_key": _SENTINEL_SECRET})

    def test_manifest_construction_rejects_a_nested_literal_secret(self) -> None:
        with self.assertRaisesRegex(ValidationError, "literal credential"):
            _cloud_manifest(
                default_params={"auth": {"token": _SENTINEL_SECRET}}
            )

    def test_manifest_construction_accepts_the_api_key_env_indirection(self) -> None:
        manifest = _cloud_manifest(default_params={"api_key_env": "ACME_IMAGE_API_KEY"})
        self.assertEqual(manifest.default_params["api_key_env"], "ACME_IMAGE_API_KEY")

    def test_manifest_error_never_contains_the_secret_value(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            _cloud_manifest(default_params={"api_key": _SENTINEL_SECRET})
        self.assertNotIn(_SENTINEL_SECRET, str(ctx.exception))


class ResolveImageProviderCredentialTest(unittest.TestCase):
    """`resolve_image_provider_credential` (issue #257)."""

    def test_local_provider_never_needs_a_credential_even_if_env_is_set(self) -> None:
        credential = resolve_image_provider_credential(
            {"api_key_env": "ACME_IMAGE_API_KEY"},
            provider_id="local-diffusers",
            manifest_label="sdxl-local",
            env={"ACME_IMAGE_API_KEY": _SENTINEL_SECRET},
        )
        self.assertIsNone(credential)

    def test_remote_provider_with_no_api_key_env_needs_no_credential(self) -> None:
        credential = resolve_image_provider_credential(
            {},
            provider_id="fake-cloud",
            manifest_label="cloud-image-test",
            env={},
        )
        self.assertIsNone(credential)

    def test_remote_provider_resolves_the_credential_from_the_named_env_var(self) -> None:
        credential = resolve_image_provider_credential(
            {"api_key_env": "ACME_IMAGE_API_KEY"},
            provider_id="fake-cloud",
            manifest_label="cloud-image-test",
            env={"ACME_IMAGE_API_KEY": _SENTINEL_SECRET},
        )
        assert credential is not None
        self.assertEqual(credential.env_var, "ACME_IMAGE_API_KEY")
        self.assertEqual(credential.value, _SENTINEL_SECRET)

    def test_remote_provider_missing_the_named_env_var_raises_actionable_error(self) -> None:
        with self.assertRaises(ImageProviderCredentialUnavailableError) as ctx:
            resolve_image_provider_credential(
                {"api_key_env": "ACME_IMAGE_API_KEY"},
                provider_id="fake-cloud",
                manifest_label="cloud-image-test",
                env={},
            )
        message = str(ctx.exception)
        self.assertIn("ACME_IMAGE_API_KEY", message)
        self.assertIn("fake-cloud", message)

    def test_remote_provider_empty_env_var_is_treated_as_missing(self) -> None:
        with self.assertRaises(ImageProviderCredentialUnavailableError):
            resolve_image_provider_credential(
                {"api_key_env": "ACME_IMAGE_API_KEY"},
                provider_id="fake-cloud",
                manifest_label="cloud-image-test",
                env={"ACME_IMAGE_API_KEY": ""},
            )

    def test_literal_credential_in_default_params_is_rejected_before_env_lookup(self) -> None:
        with self.assertRaises(ImageProviderMisconfiguredError):
            resolve_image_provider_credential(
                {"api_key": _SENTINEL_SECRET},
                provider_id="fake-cloud",
                manifest_label="cloud-image-test",
                env={},
            )

    def test_reads_process_environ_when_env_omitted(self) -> None:
        with patch.dict(
            "os.environ", {"ACME_IMAGE_API_KEY": _SENTINEL_SECRET}, clear=False
        ):
            credential = resolve_image_provider_credential(
                {"api_key_env": "ACME_IMAGE_API_KEY"},
                provider_id="fake-cloud",
                manifest_label="cloud-image-test",
            )
        assert credential is not None
        self.assertEqual(credential.value, _SENTINEL_SECRET)


class ImageProviderCredentialReprTest(unittest.TestCase):
    """The credential's own repr/str must never leak `.value` (issue #257)."""

    def test_repr_omits_the_value(self) -> None:
        credential = ImageProviderCredential(env_var="ACME_IMAGE_API_KEY", value=_SENTINEL_SECRET)
        self.assertNotIn(_SENTINEL_SECRET, repr(credential))
        self.assertIn("ACME_IMAGE_API_KEY", repr(credential))

    def test_logging_the_credential_object_does_not_leak_the_value(self) -> None:
        credential = ImageProviderCredential(env_var="ACME_IMAGE_API_KEY", value=_SENTINEL_SECRET)
        with self.assertLogs("test.image.providers", level="INFO") as ctx:
            logging.getLogger("test.image.providers").info("resolved %r", credential)
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
        text = "a fox in a field"
        self.assertEqual(redact_secrets(text, [_SENTINEL_SECRET]), text)


class RedactProviderErrorTest(unittest.TestCase):
    """A caught provider error must never propagate a secret (issue #257)."""

    def test_redacts_the_secret_from_the_error_message(self) -> None:
        original = RuntimeError(f"upstream rejected key {_SENTINEL_SECRET}")
        self.assertIn(_SENTINEL_SECRET, str(original))  # the test is not vacuous

        redacted = redact_provider_error(original, [_SENTINEL_SECRET])

        self.assertNotIn(_SENTINEL_SECRET, str(redacted))
        self.assertIsInstance(redacted, RuntimeError)

    def test_preserves_a_custom_exception_type_with_a_single_message_constructor(self) -> None:
        original = ImageProviderCredentialUnavailableError(f"leaked {_SENTINEL_SECRET}")
        redacted = redact_provider_error(original, [_SENTINEL_SECRET])
        self.assertIsInstance(redacted, ImageProviderCredentialUnavailableError)
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
    """Diagnostics/metadata dicts must never carry a secret through (issue #257)."""

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
    """The sentinel must never survive into request/job/asset-shaped data
    (issue #257 acceptance criterion: "Provider credentials never appear in
    persisted job/asset/request data")."""

    def test_credential_resolution_never_touches_the_generation_spec(self) -> None:
        resolve_image_provider_credential(
            {"api_key_env": "ACME_IMAGE_API_KEY"},
            provider_id="fake-cloud",
            manifest_label="cloud-image-test",
            env={"ACME_IMAGE_API_KEY": _SENTINEL_SECRET},
        )
        spec = _spec()
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(asdict(spec)))

    def test_preflight_disclosure_never_carries_the_credential(self) -> None:
        # The preflight disclosure (job/request-facing) is built from the
        # spec/provider/model id alone -- a resolved credential is never an
        # input to it, so it structurally cannot leak into what a caller
        # would attach to job metadata or a request snapshot.
        resolve_image_provider_credential(
            {"api_key_env": "ACME_IMAGE_API_KEY"},
            provider_id="fake-cloud",
            manifest_label="cloud-image-test",
            env={"ACME_IMAGE_API_KEY": _SENTINEL_SECRET},
        )
        disclosure = build_image_provider_preflight(
            _spec(), provider_id="fake-cloud", model_id="fake-model"
        )
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(asdict(disclosure)))

    def test_provider_result_metadata_sanitized_before_it_would_reach_job_metadata(
        self,
    ) -> None:
        # Simulates a vendor SDK echoing the credential back in a response
        # field -- a caller must run the result's metadata through
        # `redact_provider_metadata` before attaching it to job/asset
        # metadata (this is the "job metadata" / "asset metadata" leg of the
        # acceptance criterion).
        identity = ImageProviderIdentity(
            provider_id="fake-cloud", model_id="fake-model", request_id="req-1"
        )
        result = ImageProviderResult(
            identity=identity,
            image=None,
            metadata={"debug_request_headers": {"Authorization": f"Bearer {_SENTINEL_SECRET}"}},
        )
        sanitized_metadata = redact_provider_metadata(result.metadata, [_SENTINEL_SECRET])
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(sanitized_metadata))

    def test_logging_a_redacted_provider_error_never_emits_the_secret(self) -> None:
        original = RuntimeError(f"upstream rejected key {_SENTINEL_SECRET}")
        redacted = redact_provider_error(original, [_SENTINEL_SECRET])

        with self.assertLogs("test.image.providers", level="ERROR") as ctx:
            logging.getLogger("test.image.providers").error("provider call failed: %s", redacted)

        self.assertNotIn(_SENTINEL_SECRET, "\n".join(ctx.output))


if __name__ == "__main__":
    unittest.main()
