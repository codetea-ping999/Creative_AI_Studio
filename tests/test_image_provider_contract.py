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
    ImageProviderResult,
    LocalDiffusersImageProvider,
    UnsupportedImageParameterError,
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


if __name__ == "__main__":
    unittest.main()
