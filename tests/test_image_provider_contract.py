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
import time
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
    ImageProviderAuthError,
    ImageProviderCallError,
    ImageProviderCapabilities,
    ImageProviderCostEstimate,
    ImageProviderCredential,
    ImageProviderCredentialUnavailableError,
    ImageProviderErrorCategory,
    ImageProviderIdentity,
    ImageProviderMisconfiguredError,
    ImageProviderPermanentError,
    ImageProviderPreflightDisclosure,
    ImageProviderPriceTableEntry,
    ImageProviderRateLimitError,
    ImageProviderReferenceAsset,
    ImageProviderResult,
    ImageProviderRetryPolicy,
    ImageProviderTimeoutError,
    ImageProviderTransientError,
    LocalDiffusersImageProvider,
    RemoteImageProviderOptInRequiredError,
    UnsupportedImageParameterError,
    build_flat_rate_image_cost_estimator,
    build_image_provider_preflight,
    build_image_provider_provenance,
    call_image_provider_with_retry,
    cloud_image_provider_opt_in_granted,
    ensure_image_provider_opt_in,
    hash_image_provider_request_inputs,
    is_local_image_provider,
    is_retryable_image_provider_error_category,
    redact_provider_error,
    redact_provider_metadata,
    redact_secrets,
    resolve_image_provider_credential,
    run_image_provider_request,
    summarize_prompt_for_preflight,
    unknown_image_provider_cost_estimate,
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


class ImageProviderCostEstimateTest(unittest.TestCase):
    """Construction rules for a known range vs. the explicit unknown case (issue #259)."""

    def test_known_estimate_with_a_valid_range_is_accepted(self) -> None:
        estimate = ImageProviderCostEstimate(
            currency="USD", is_known=True, low=0.01, high=0.02
        )
        self.assertTrue(estimate.is_known)
        self.assertEqual((estimate.low, estimate.high), (0.01, 0.02))

    def test_known_estimate_requires_both_low_and_high(self) -> None:
        with self.assertRaisesRegex(ValueError, "low and high"):
            ImageProviderCostEstimate(currency="USD", is_known=True, low=0.01, high=None)
        with self.assertRaisesRegex(ValueError, "low and high"):
            ImageProviderCostEstimate(currency="USD", is_known=True, low=None, high=0.02)

    def test_known_estimate_rejects_a_negative_bound(self) -> None:
        with self.assertRaisesRegex(ValueError, "not be negative"):
            ImageProviderCostEstimate(currency="USD", is_known=True, low=-0.01, high=0.02)

    def test_known_estimate_rejects_low_above_high(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            ImageProviderCostEstimate(currency="USD", is_known=True, low=0.05, high=0.01)

    def test_unknown_estimate_must_not_carry_low_or_high(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not carry low/high"):
            ImageProviderCostEstimate(currency="USD", is_known=False, low=0.01, high=0.01)

    def test_currency_must_not_be_empty(self) -> None:
        with self.assertRaisesRegex(ValueError, "currency"):
            ImageProviderCostEstimate(currency="", is_known=False)

    def test_unknown_image_provider_cost_estimate_carries_no_range(self) -> None:
        estimate = unknown_image_provider_cost_estimate()
        self.assertFalse(estimate.is_known)
        self.assertIsNone(estimate.low)
        self.assertIsNone(estimate.high)
        self.assertTrue(estimate.basis)  # explains *why* pricing is unknown

    def test_unknown_image_provider_cost_estimate_honors_currency_and_basis(self) -> None:
        estimate = unknown_image_provider_cost_estimate(
            currency="EUR", basis="vendor endpoint does not report pricing"
        )
        self.assertEqual(estimate.currency, "EUR")
        self.assertEqual(estimate.basis, "vendor endpoint does not report pricing")


class BuildFlatRateImageCostEstimatorTest(unittest.TestCase):
    """`build_flat_rate_image_cost_estimator`: request count/size/model (issue #259)."""

    def test_flat_per_image_price_scales_with_request_count(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"model-a": ImageProviderPriceTableEntry(price_per_image=0.04)}
        )
        estimate = estimator(_spec(batch_size=3), "model-a")
        self.assertTrue(estimate.is_known)
        self.assertAlmostEqual(estimate.low, 0.12)
        self.assertAlmostEqual(estimate.high, 0.12)
        self.assertEqual(estimate.currency, "USD")

    def test_per_megapixel_price_scales_with_image_size(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"model-a": ImageProviderPriceTableEntry(price_per_megapixel=0.10)}
        )
        one_megapixel_spec = _spec(width=1000, height=1000, batch_size=1)
        estimate = estimator(one_megapixel_spec, "model-a")
        self.assertAlmostEqual(estimate.low, 0.10)

    def test_price_per_image_and_per_megapixel_combine(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {
                "model-a": ImageProviderPriceTableEntry(
                    price_per_image=0.01, price_per_megapixel=0.02
                )
            }
        )
        estimate = estimator(_spec(width=1000, height=1000, batch_size=2), "model-a")
        # Per request: 0.01 + 0.02 * 1.0MP = 0.03; x2 requests = 0.06.
        self.assertAlmostEqual(estimate.low, 0.06)

    def test_a_model_absent_from_the_price_table_is_reported_unknown(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"model-a": ImageProviderPriceTableEntry(price_per_image=0.04)}
        )
        estimate = estimator(_spec(), "model-b")
        self.assertFalse(estimate.is_known)
        self.assertIn("model-b", estimate.basis)

    def test_price_table_entry_rejects_negative_prices(self) -> None:
        with self.assertRaises(ValueError):
            ImageProviderPriceTableEntry(price_per_image=-0.01)
        with self.assertRaises(ValueError):
            ImageProviderPriceTableEntry(price_per_megapixel=-0.01)


class BuildImageProviderPreflightCostEstimateTest(unittest.TestCase):
    """`cost_estimate` on the preflight disclosure (issue #259)."""

    def test_local_provider_preflight_cost_estimate_is_always_none(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"sdxl": ImageProviderPriceTableEntry(price_per_image=1.0)}
        )
        disclosure = build_image_provider_preflight(
            _spec(),
            provider_id="local-diffusers",
            model_id="sdxl",
            cost_estimator=estimator,
        )
        self.assertIsNone(disclosure.cost_estimate)

    def test_remote_provider_with_no_estimator_gets_an_explicit_unknown(self) -> None:
        disclosure = build_image_provider_preflight(
            _spec(), provider_id="fake-cloud", model_id="fake-model"
        )
        self.assertIsNotNone(disclosure.cost_estimate)
        assert disclosure.cost_estimate is not None
        self.assertFalse(disclosure.cost_estimate.is_known)

    def test_remote_provider_with_an_estimator_gets_its_known_estimate(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"fake-model": ImageProviderPriceTableEntry(price_per_image=0.5)}
        )
        disclosure = build_image_provider_preflight(
            _spec(batch_size=2),
            provider_id="fake-cloud",
            model_id="fake-model",
            cost_estimator=estimator,
        )
        assert disclosure.cost_estimate is not None
        self.assertTrue(disclosure.cost_estimate.is_known)
        self.assertAlmostEqual(disclosure.cost_estimate.low, 1.0)


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


class ImageProviderErrorCategoryTest(unittest.TestCase):
    """The five stable categories (issue #258) and which ones are retryable."""

    def test_timeout_rate_limit_and_transient_are_retryable(self) -> None:
        for category in (
            ImageProviderErrorCategory.TIMEOUT,
            ImageProviderErrorCategory.RATE_LIMIT,
            ImageProviderErrorCategory.TRANSIENT,
        ):
            self.assertTrue(is_retryable_image_provider_error_category(category))

    def test_auth_and_permanent_are_not_retryable(self) -> None:
        for category in (
            ImageProviderErrorCategory.AUTH,
            ImageProviderErrorCategory.PERMANENT,
        ):
            self.assertFalse(is_retryable_image_provider_error_category(category))

    def test_error_subclasses_carry_the_matching_category_and_retryable_flag(self) -> None:
        cases = [
            (ImageProviderTimeoutError, ImageProviderErrorCategory.TIMEOUT, True),
            (ImageProviderRateLimitError, ImageProviderErrorCategory.RATE_LIMIT, True),
            (ImageProviderTransientError, ImageProviderErrorCategory.TRANSIENT, True),
            (ImageProviderAuthError, ImageProviderErrorCategory.AUTH, False),
            (ImageProviderPermanentError, ImageProviderErrorCategory.PERMANENT, False),
        ]
        for error_cls, expected_category, expected_retryable in cases:
            error = error_cls("boom", provider_id="fake-cloud")
            self.assertIsInstance(error, ImageProviderCallError)
            self.assertEqual(error.category, expected_category)
            self.assertEqual(error.retryable, expected_retryable)
            self.assertIsNone(error.provider_request_id)
            self.assertIsNone(error.retry_after_seconds)

    def test_error_preserves_provider_request_id_and_retry_after(self) -> None:
        error = ImageProviderRateLimitError(
            "slow down",
            provider_id="fake-cloud",
            provider_request_id="vendor-req-42",
            retry_after_seconds=2.5,
        )
        self.assertEqual(error.provider_request_id, "vendor-req-42")
        self.assertEqual(error.retry_after_seconds, 2.5)


class ImageProviderRetryPolicyTest(unittest.TestCase):
    """Retry count and timeout must be finite/configurable within safe bounds."""

    def test_default_policy_is_within_safe_bounds(self) -> None:
        policy = ImageProviderRetryPolicy()  # must not raise
        self.assertGreaterEqual(policy.max_retries, 0)
        self.assertGreater(policy.timeout_seconds, 0)

    def test_negative_max_retries_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_retries"):
            ImageProviderRetryPolicy(max_retries=-1)

    def test_max_retries_above_the_safe_ceiling_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_retries"):
            ImageProviderRetryPolicy(max_retries=10_000)

    def test_non_positive_timeout_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            ImageProviderRetryPolicy(timeout_seconds=0)

    def test_timeout_above_the_safe_ceiling_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            ImageProviderRetryPolicy(timeout_seconds=10_000)

    def test_negative_backoff_base_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "backoff_base_seconds"):
            ImageProviderRetryPolicy(backoff_base_seconds=-1)

    def test_backoff_multiplier_below_one_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "backoff_multiplier"):
            ImageProviderRetryPolicy(backoff_multiplier=0.5)

    def test_max_backoff_above_the_safe_ceiling_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_backoff_seconds"):
            ImageProviderRetryPolicy(max_backoff_seconds=10_000)


class _FlakyFakeCloudProvider:
    """Fails `failures_before_success` times with a given categorized error,
    then succeeds -- used to exercise retry-then-recover behavior without any
    real network call (issue #258's "fake-provider tests")."""

    def __init__(
        self,
        *,
        error_factory: Any,
        failures_before_success: int = 0,
        sleep_seconds: float | None = None,
    ) -> None:
        self._error_factory = error_factory
        self._failures_before_success = failures_before_success
        self._sleep_seconds = sleep_seconds
        self.call_count = 0

    def __call__(self) -> str:
        self.call_count += 1
        if self._sleep_seconds is not None:
            time.sleep(self._sleep_seconds)
        if self.call_count <= self._failures_before_success:
            raise self._error_factory()
        return f"ok-after-{self.call_count}-calls"


class CallImageProviderWithRetryTest(unittest.TestCase):
    """`call_image_provider_with_retry`: success, timeout, rate limit,
    transient recovery, and permanent failure (issue #258)."""

    def test_a_successful_call_returns_immediately_with_no_retry(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderTransientError("boom", provider_id="fake-cloud")
        )

        result = call_image_provider_with_retry(
            provider, provider_id="fake-cloud", sleep=sleeps.append
        )

        self.assertEqual(result, "ok-after-1-calls")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_timeout_raises_when_transport_exceeds_the_per_attempt_deadline(self) -> None:
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: RuntimeError("unused"), sleep_seconds=0.3
        )
        policy = ImageProviderRetryPolicy(max_retries=0, timeout_seconds=0.05)

        with self.assertRaises(ImageProviderTimeoutError) as ctx:
            call_image_provider_with_retry(
                provider, provider_id="fake-cloud", retry_policy=policy, sleep=lambda seconds: None
            )
        self.assertIn("fake-cloud", str(ctx.exception))
        self.assertEqual(ctx.exception.category, ImageProviderErrorCategory.TIMEOUT)

    def test_rate_limit_is_retried_and_recovers(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderRateLimitError(
                "slow down", provider_id="fake-cloud", retry_after_seconds=1.0
            ),
            failures_before_success=2,
        )
        policy = ImageProviderRetryPolicy(max_retries=2)

        result = call_image_provider_with_retry(
            provider, provider_id="fake-cloud", retry_policy=policy, sleep=sleeps.append
        )

        self.assertEqual(result, "ok-after-3-calls")
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(sleeps, [1.0, 1.0])  # honors the vendor's retry_after_seconds hint

    def test_transient_error_recovers_after_one_retry(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderTransientError("hiccup", provider_id="fake-cloud"),
            failures_before_success=1,
        )

        result = call_image_provider_with_retry(
            provider, provider_id="fake-cloud", sleep=sleeps.append
        )

        self.assertEqual(result, "ok-after-2-calls")
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(sleeps, [0.5])  # default backoff_base_seconds

    def test_permanent_error_is_never_retried(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderPermanentError(
                "bad request", provider_id="fake-cloud"
            ),
            failures_before_success=999,
        )

        with self.assertRaises(ImageProviderPermanentError):
            call_image_provider_with_retry(provider, provider_id="fake-cloud", sleep=sleeps.append)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_auth_error_is_never_retried(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderAuthError("bad key", provider_id="fake-cloud"),
            failures_before_success=999,
        )

        with self.assertRaises(ImageProviderAuthError):
            call_image_provider_with_retry(provider, provider_id="fake-cloud", sleep=sleeps.append)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_an_unclassified_exception_is_never_retried(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ValueError("not a provider error"), failures_before_success=999
        )

        with self.assertRaises(ValueError):
            call_image_provider_with_retry(provider, provider_id="fake-cloud", sleep=sleeps.append)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_retries_are_exhausted_and_the_final_error_propagates(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderTransientError(
                "still down", provider_id="fake-cloud"
            ),
            failures_before_success=999,
        )
        policy = ImageProviderRetryPolicy(max_retries=2)

        with self.assertRaises(ImageProviderTransientError):
            call_image_provider_with_retry(
                provider, provider_id="fake-cloud", retry_policy=policy, sleep=sleeps.append
            )

        self.assertEqual(provider.call_count, 3)  # 1 initial + 2 retries
        self.assertEqual(len(sleeps), 2)

    def test_retry_after_seconds_is_clamped_to_max_backoff(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderRateLimitError(
                "slow down", provider_id="fake-cloud", retry_after_seconds=999.0
            ),
            failures_before_success=1,
        )
        policy = ImageProviderRetryPolicy(max_backoff_seconds=3.0)

        call_image_provider_with_retry(
            provider, provider_id="fake-cloud", retry_policy=policy, sleep=sleeps.append
        )

        self.assertEqual(sleeps, [3.0])

    def test_exponential_backoff_grows_and_is_capped(self) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderTransientError("hiccup", provider_id="fake-cloud"),
            failures_before_success=3,
        )
        policy = ImageProviderRetryPolicy(
            max_retries=3,
            backoff_base_seconds=1.0,
            backoff_multiplier=2.0,
            max_backoff_seconds=3.0,
        )

        call_image_provider_with_retry(
            provider, provider_id="fake-cloud", retry_policy=policy, sleep=sleeps.append
        )

        self.assertEqual(sleeps, [1.0, 2.0, 3.0])  # 1, 2, 4-capped-to-3


class RunImageProviderRequestRetryIntegrationTest(unittest.TestCase):
    """`run_image_provider_request` applies bounded retry/timeout to a remote
    provider automatically, and never wraps a local provider (issue #258)."""

    def test_remote_provider_transient_failure_recovers_through_run_image_provider_request(
        self,
    ) -> None:
        sleeps: list[float] = []
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderTransientError("hiccup", provider_id="fake-cloud"),
            failures_before_success=1,
        )

        disclosure, result = run_image_provider_request(
            _spec(),
            provider_id="fake-cloud",
            model_id="fake-model",
            transport=provider,
            env={"ALLOW_CLOUD_PROVIDERS": "true"},
            sleep=sleeps.append,
        )

        self.assertEqual(result, "ok-after-2-calls")
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(disclosure.destination, "remote")

    def test_remote_provider_permanent_failure_is_not_retried(self) -> None:
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: ImageProviderPermanentError(
                "bad request", provider_id="fake-cloud"
            ),
            failures_before_success=999,
        )

        with self.assertRaises(ImageProviderPermanentError):
            run_image_provider_request(
                _spec(),
                provider_id="fake-cloud",
                model_id="fake-model",
                transport=provider,
                env={"ALLOW_CLOUD_PROVIDERS": "true"},
                sleep=lambda seconds: None,
            )

        self.assertEqual(provider.call_count, 1)

    def test_local_provider_is_never_wrapped_even_when_a_retry_policy_is_given(self) -> None:
        # A local transport can legitimately run past any bounded remote
        # deadline (e.g. slow CPU inference); a retry_policy passed in must
        # be ignored entirely for a local provider_id.
        provider = _FlakyFakeCloudProvider(
            error_factory=lambda: RuntimeError("unused"), sleep_seconds=0.2
        )
        tight_policy = ImageProviderRetryPolicy(max_retries=0, timeout_seconds=0.01)

        disclosure, result = run_image_provider_request(
            _spec(),
            provider_id="local-diffusers",
            model_id="sdxl",
            transport=provider,
            env={},
            retry_policy=tight_policy,
        )

        self.assertEqual(result, "ok-after-1-calls")
        self.assertEqual(disclosure.destination, "local")


class RunImageProviderRequestCostEstimatorIntegrationTest(unittest.TestCase):
    """`run_image_provider_request` forwards `cost_estimator` into the
    disclosure it returns (issue #259)."""

    def test_cost_estimator_is_forwarded_into_the_returned_disclosure(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"fake-model": ImageProviderPriceTableEntry(price_per_image=0.25)}
        )

        disclosure, _result = run_image_provider_request(
            _spec(),
            provider_id="fake-cloud",
            model_id="fake-model",
            transport=lambda: "sent",
            env={"ALLOW_CLOUD_PROVIDERS": "true"},
            cost_estimator=estimator,
        )

        assert disclosure.cost_estimate is not None
        self.assertTrue(disclosure.cost_estimate.is_known)
        self.assertAlmostEqual(disclosure.cost_estimate.low, 0.25)

    def test_local_provider_ignores_a_cost_estimator_and_stays_none(self) -> None:
        estimator = build_flat_rate_image_cost_estimator(
            {"sdxl": ImageProviderPriceTableEntry(price_per_image=1.0)}
        )

        disclosure, _result = run_image_provider_request(
            _spec(),
            provider_id="local-diffusers",
            model_id="sdxl",
            transport=lambda: "ok",
            env={},
            cost_estimator=estimator,
        )

        self.assertIsNone(disclosure.cost_estimate)

    def test_missing_estimator_still_discloses_an_explicit_unknown_before_transport(
        self,
    ) -> None:
        disclosure, _result = run_image_provider_request(
            _spec(),
            provider_id="fake-cloud",
            model_id="unpriced-model",
            transport=lambda: "sent",
            env={"ALLOW_CLOUD_PROVIDERS": "true"},
        )

        assert disclosure.cost_estimate is not None
        self.assertFalse(disclosure.cost_estimate.is_known)


class RedactProviderErrorPreservesCallErrorMetadataTest(unittest.TestCase):
    """Redaction must not discard category/provenance fields (issue #258)."""

    def test_redaction_preserves_category_provider_id_and_retry_after(self) -> None:
        original = ImageProviderRateLimitError(
            f"slow down, key {_SENTINEL_SECRET} is over quota",
            provider_id="fake-cloud",
            provider_request_id="vendor-req-42",
            retry_after_seconds=1.5,
        )

        redacted = redact_provider_error(original, [_SENTINEL_SECRET])

        self.assertIsInstance(redacted, ImageProviderRateLimitError)
        self.assertNotIn(_SENTINEL_SECRET, str(redacted))
        self.assertEqual(redacted.category, ImageProviderErrorCategory.RATE_LIMIT)
        self.assertEqual(redacted.provider_id, "fake-cloud")
        self.assertEqual(redacted.provider_request_id, "vendor-req-42")
        self.assertEqual(redacted.retry_after_seconds, 1.5)

    def test_redaction_also_scrubs_a_secret_embedded_in_the_provider_request_id(self) -> None:
        original = ImageProviderTransientError(
            "hiccup",
            provider_id="fake-cloud",
            provider_request_id=f"vendor-req-{_SENTINEL_SECRET}",
        )

        redacted = redact_provider_error(original, [_SENTINEL_SECRET])

        assert isinstance(redacted, ImageProviderTransientError)
        self.assertNotIn(_SENTINEL_SECRET, redacted.provider_request_id or "")


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

    def test_redacts_secrets_inside_tuples(self) -> None:
        metadata = {"reference_assets": (f"key={_SENTINEL_SECRET}", "clean")}
        sanitized = redact_provider_metadata(metadata, [_SENTINEL_SECRET])
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(sanitized))
        self.assertEqual(sanitized["reference_assets"][1], "clean")


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


class HashImageProviderRequestInputsTest(unittest.TestCase):
    """`hash_image_provider_request_inputs`: deterministic, sensitive to
    every input, and never a substitute for storing the raw payload (issue
    #259)."""

    def test_identical_inputs_hash_identically(self) -> None:
        spec = _spec(seed=7)
        first = hash_image_provider_request_inputs(spec, effective_params={"guidance_scale": 7.5})
        second = hash_image_provider_request_inputs(spec, effective_params={"guidance_scale": 7.5})
        self.assertEqual(first, second)

    def test_a_different_prompt_changes_the_hash(self) -> None:
        first = hash_image_provider_request_inputs(_spec(prompt="a fox"))
        second = hash_image_provider_request_inputs(_spec(prompt="a wolf"))
        self.assertNotEqual(first, second)

    def test_a_different_effective_params_value_changes_the_hash(self) -> None:
        spec = _spec()
        first = hash_image_provider_request_inputs(spec, effective_params={"steps": 20})
        second = hash_image_provider_request_inputs(spec, effective_params={"steps": 30})
        self.assertNotEqual(first, second)

    def test_omitted_effective_params_defaults_to_empty_and_is_stable(self) -> None:
        spec = _spec()
        without_kwarg = hash_image_provider_request_inputs(spec)
        with_empty = hash_image_provider_request_inputs(spec, effective_params={})
        self.assertEqual(without_kwarg, with_empty)

    def test_the_hash_is_a_sha256_hex_digest(self) -> None:
        digest = hash_image_provider_request_inputs(_spec())
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # must not raise -- every character is valid hex


class BuildImageProviderProvenanceTest(unittest.TestCase):
    """`build_image_provider_provenance`: the auditable record attached to
    job/asset metadata after a call (issue #259)."""

    def _result(
        self, *, provider_id: str = "fake-cloud", provider_request_id: str | None = "vendor-req-1"
    ) -> ImageProviderResult:
        identity = ImageProviderIdentity(
            provider_id=provider_id, model_id="fake-model", request_id="req-1"
        )
        return ImageProviderResult(
            identity=identity, image=None, provider_request_id=provider_request_id
        )

    def test_identity_fields_come_from_the_result(self) -> None:
        provenance = build_image_provider_provenance(
            _spec(), self._result(), capabilities=ImageProviderCapabilities()
        )
        self.assertEqual(provenance.provider_id, "fake-cloud")
        self.assertEqual(provenance.model_id, "fake-model")
        self.assertEqual(provenance.request_id, "req-1")
        self.assertEqual(provenance.provider_request_id, "vendor-req-1")

    def test_destination_is_derived_from_the_result_provider_id_not_trusted_input(self) -> None:
        remote_provenance = build_image_provider_provenance(
            _spec(),
            self._result(provider_id="fake-cloud"),
            capabilities=ImageProviderCapabilities(),
        )
        local_provenance = build_image_provider_provenance(
            _spec(),
            self._result(provider_id="local-diffusers", provider_request_id=None),
            capabilities=ImageProviderCapabilities(),
        )
        self.assertEqual(remote_provenance.destination, "remote")
        self.assertEqual(local_provenance.destination, "local")

    def test_local_result_naturally_carries_no_vendor_request_id(self) -> None:
        provenance = build_image_provider_provenance(
            _spec(),
            self._result(provider_id="local-diffusers", provider_request_id=None),
            capabilities=ImageProviderCapabilities(),
        )
        self.assertIsNone(provenance.provider_request_id)

    def test_effective_capabilities_matches_the_declared_capabilities(self) -> None:
        capabilities = ImageProviderCapabilities(supports_lora=True, max_batch=4)
        provenance = build_image_provider_provenance(
            _spec(), self._result(), capabilities=capabilities
        )
        self.assertEqual(provenance.effective_capabilities, asdict(capabilities))

    def test_effective_params_and_reference_assets_and_cost_estimate_are_carried_through(
        self,
    ) -> None:
        reference_assets = [ImageProviderReferenceAsset(asset_id="asset_1", role="style-reference")]
        cost_estimate = ImageProviderCostEstimate(currency="USD", is_known=True, low=0.1, high=0.1)
        provenance = build_image_provider_provenance(
            _spec(),
            self._result(),
            capabilities=ImageProviderCapabilities(),
            effective_params={"guidance_scale": 7.5},
            reference_assets=reference_assets,
            cost_estimate=cost_estimate,
        )
        self.assertEqual(provenance.effective_params, {"guidance_scale": 7.5})
        self.assertEqual(provenance.reference_assets, tuple(reference_assets))
        self.assertEqual(provenance.cost_estimate, cost_estimate)

    def test_omitted_effective_params_and_reference_assets_default_empty(self) -> None:
        provenance = build_image_provider_provenance(
            _spec(), self._result(), capabilities=ImageProviderCapabilities()
        )
        self.assertEqual(provenance.effective_params, {})
        self.assertEqual(provenance.reference_assets, ())
        self.assertIsNone(provenance.cost_estimate)

    def test_input_summary_hash_matches_hash_image_provider_request_inputs(self) -> None:
        spec = _spec()
        effective_params = {"guidance_scale": 7.5}
        provenance = build_image_provider_provenance(
            spec,
            self._result(),
            capabilities=ImageProviderCapabilities(),
            effective_params=effective_params,
        )
        expected = hash_image_provider_request_inputs(spec, effective_params=effective_params)
        self.assertEqual(provenance.input_summary_hash, expected)

    def test_provenance_is_json_serializable_via_asdict(self) -> None:
        provenance = build_image_provider_provenance(
            _spec(),
            self._result(),
            capabilities=ImageProviderCapabilities(),
            effective_params={"guidance_scale": 7.5},
            reference_assets=[
                ImageProviderReferenceAsset(asset_id="asset_1", role="style-reference")
            ],
            cost_estimate=ImageProviderCostEstimate(
                currency="USD", is_known=True, low=0.1, high=0.1
            ),
        )
        # Must not raise -- this is the shape a caller merges into job/asset
        # metadata, which is itself JSON-persisted.
        serialized = json.dumps(asdict(provenance))
        self.assertIn("fake-cloud", serialized)


class ProvenanceSentinelSecretPersistenceTest(unittest.TestCase):
    """The provenance record must never carry a credential through, even when
    a vendor SDK response happened to echo one back (issue #259, extending
    the issue #257 guarantee to the new provenance shape)."""

    def test_provenance_built_from_safely_resolved_inputs_never_carries_the_sentinel(
        self,
    ) -> None:
        resolve_image_provider_credential(
            {"api_key_env": "ACME_IMAGE_API_KEY"},
            provider_id="fake-cloud",
            manifest_label="cloud-image-test",
            env={"ACME_IMAGE_API_KEY": _SENTINEL_SECRET},
        )
        identity = ImageProviderIdentity(
            provider_id="fake-cloud", model_id="fake-model", request_id="req-1"
        )
        result = ImageProviderResult(
            identity=identity, image=None, provider_request_id="vendor-req-1"
        )
        provenance = build_image_provider_provenance(
            _spec(),
            result,
            capabilities=ImageProviderCapabilities(),
            effective_params={"guidance_scale": 7.5},
        )
        self.assertNotIn(_SENTINEL_SECRET, json.dumps(asdict(provenance)))

    def test_input_summary_hash_is_not_the_secret_itself(self) -> None:
        # Not a cryptographic claim -- just proves the hash is a digest, not
        # an accidental passthrough of a raw secret-bearing field.
        spec = _spec()
        digest = hash_image_provider_request_inputs(
            spec, effective_params={"api_key": _SENTINEL_SECRET}
        )
        self.assertNotIn(_SENTINEL_SECRET, digest)


if __name__ == "__main__":
    unittest.main()
