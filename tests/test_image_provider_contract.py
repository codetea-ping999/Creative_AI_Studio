"""Tests for the provider-neutral image generation contract (issue #255).

Covers `generators/image/providers.py` in isolation (capability declaration,
pre-invocation rejection of unsupported parameters, protocol conformance for
both the local adapter and a fake alternative provider) and confirms
`ImageGenerator` still executes local generation successfully when routed
through that contract.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from PIL import Image

from bootstrap import create_default_model_service
from core.schemas import GenerationRequest
from generators.image.generator import ImageGenerator
from generators.image.providers import (
    ImageGenerationSpec,
    ImageProvider,
    ImageProviderCapabilities,
    ImageProviderIdentity,
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
    run_image_provider_request,
    summarize_prompt_for_preflight,
    validate_capabilities,
)


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


if __name__ == "__main__":
    unittest.main()
