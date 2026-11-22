"""FLUX.1-dev runtime support: manifest, pipeline class, dtype/variant, negative prompt (#21)."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from core.model_readiness import evaluate_manifest_readiness
from core.models import ModelManifest, ModelRegistry, create_default_loader_registry
from core.models.image_pipelines import resolve_image_pipeline_family

_FLUX_INDEX = {
    "_class_name": "FluxPipeline",
    "scheduler": ["diffusers", "FlowMatchEulerDiscreteScheduler"],
    "text_encoder": ["transformers", "CLIPTextModel"],
    "text_encoder_2": ["transformers", "T5EncoderModel"],
    "tokenizer": ["transformers", "CLIPTokenizer"],
    "tokenizer_2": ["transformers", "T5TokenizerFast"],
    "transformer": ["diffusers", "FluxTransformer2DModel"],
    "vae": ["diffusers", "AutoencoderKL"],
}


def _image_manifest(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "test-image-local",
        "public_id": "test-image",
        "display_name": "Test Image",
        "media_type": "image",
        "task_type": "text-to-image",
        "provider": "local",
        "runtime": "diffusers",
        "local_path": "./models/image/test",
        "loader": "diffusers_image_loader",
    }
    payload.update(overrides)
    return payload


class _FakeDtype:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return f"torch.{self.name}"


def _fake_torch(*, cuda: bool = False, mps: bool = False) -> ModuleType:
    torch = ModuleType("torch")
    torch.float16 = _FakeDtype("float16")  # type: ignore[attr-defined]
    torch.bfloat16 = _FakeDtype("bfloat16")  # type: ignore[attr-defined]
    torch.float32 = _FakeDtype("float32")  # type: ignore[attr-defined]
    torch.cuda = SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
    torch.backends = SimpleNamespace(  # type: ignore[attr-defined]
        mps=SimpleNamespace(is_available=lambda: mps)
    )
    return torch


class _FakePipeline:
    def __init__(self, **components: Any) -> None:
        self.components = components or {"unet": object()}
        self.from_pretrained_kwargs: dict[str, Any] = {}

    @classmethod
    def from_pretrained(cls, path: str, **kwargs: Any) -> "_FakePipeline":
        instance = cls()
        instance.from_pretrained_kwargs = {"path": path, **kwargs}
        return instance

    def set_progress_bar_config(self, **_: Any) -> None:
        return None

    def to(self, **_: Any) -> "_FakePipeline":
        return self


class FakeSDXL(_FakePipeline):
    def enable_attention_slicing(self) -> None:
        return None


class FakeSDXLImg2Img(_FakePipeline):
    pass


class FakeFlux(_FakePipeline):
    pass


def _fake_diffusers() -> ModuleType:
    diffusers = ModuleType("diffusers")
    diffusers.StableDiffusionXLPipeline = FakeSDXL  # type: ignore[attr-defined]
    diffusers.StableDiffusionXLImg2ImgPipeline = FakeSDXLImg2Img  # type: ignore[attr-defined]
    diffusers.FluxPipeline = FakeFlux  # type: ignore[attr-defined]
    return diffusers


class FluxManifestTests(unittest.TestCase):
    def test_flux_manifest_is_discoverable_with_explicit_pipeline_identity(self) -> None:
        manifest = ModelRegistry().get("flux-1-dev-local")

        self.assertEqual(manifest.public_model_id, "flux-1-dev")
        self.assertEqual(manifest.pipeline_class, "FluxPipeline")
        self.assertEqual(manifest.dtype, "bfloat16")
        self.assertEqual(manifest.media_type, "image")
        self.assertFalse(manifest.is_default)

    def test_flux_manifest_reports_missing_files_without_weights(self) -> None:
        manifest = ModelRegistry().get("flux-1-dev-local")
        with TemporaryDirectory() as tmp_dir:
            missing = manifest.model_copy(update={"local_path": str(Path(tmp_dir) / "absent")})
            readiness = evaluate_manifest_readiness(missing)

        self.assertFalse(readiness.is_ready)

    def test_unknown_pipeline_class_is_rejected_with_supported_list(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            ModelManifest.model_validate(_image_manifest(pipeline_class="MysteryPipeline"))
        message = str(caught.exception)
        self.assertIn("MysteryPipeline", message)
        self.assertIn("FluxPipeline", message)
        self.assertIn("StableDiffusionXLPipeline", message)

    def test_pipeline_class_is_only_valid_on_image_manifests(self) -> None:
        with self.assertRaises(ValidationError):
            ModelManifest.model_validate(
                _image_manifest(
                    media_type="audio",
                    task_type="text-to-music",
                    pipeline_class="FluxPipeline",
                )
            )

    def test_family_lookup_defaults_to_sdxl(self) -> None:
        self.assertEqual(
            resolve_image_pipeline_family(None).pipeline_class, "StableDiffusionXLPipeline"
        )
        self.assertTrue(resolve_image_pipeline_family(None).supports_negative_prompt)
        flux = resolve_image_pipeline_family("FluxPipeline")
        self.assertFalse(flux.supports_negative_prompt)
        self.assertIsNone(flux.img2img_pipeline_class)
        with self.assertRaises(ValueError):
            resolve_image_pipeline_family("MysteryPipeline")


class DiffusersLoaderPipelineSelectionTests(unittest.TestCase):
    def _load(self, manifest: ModelManifest, *, mps: bool = False) -> dict[str, Any]:
        loader = create_default_loader_registry().get("diffusers_image_loader")
        with TemporaryDirectory() as tmp_dir, patch.dict(
            "sys.modules",
            {"torch": _fake_torch(mps=mps), "diffusers": _fake_diffusers()},
        ), patch.object(type(loader), "_resolve_local_path", return_value=tmp_dir), patch.dict(
            "os.environ", {"DEVICE": "auto"}
        ):
            return loader.load(manifest)

    def test_existing_sdxl_manifest_loads_through_the_sdxl_pipeline(self) -> None:
        runtime = self._load(ModelRegistry().get("sdxl-local"))

        self.assertIsInstance(runtime["pipeline"], FakeSDXL)
        self.assertIsInstance(runtime["img2img_pipeline"], FakeSDXLImg2Img)
        self.assertEqual(runtime["pipeline_class"], "StableDiffusionXLPipeline")
        self.assertTrue(runtime["supports_negative_prompt"])

    def test_flux_manifest_loads_through_flux_pipeline_without_img2img(self) -> None:
        runtime = self._load(ModelRegistry().get("flux-1-dev-local"), mps=True)

        self.assertIsInstance(runtime["pipeline"], FakeFlux)
        self.assertIsNone(runtime["img2img_pipeline"])
        self.assertEqual(runtime["pipeline_class"], "FluxPipeline")
        self.assertFalse(runtime["supports_negative_prompt"])
        self.assertEqual(runtime["device"], "mps")
        # bfloat16 stays bfloat16 on MPS (#141); no variant files -> no variant requested.
        self.assertEqual(runtime["torch_dtype"], "bfloat16")
        self.assertIsNone(runtime["pipeline"].from_pretrained_kwargs["variant"])

    def test_missing_pipeline_class_in_installed_diffusers_is_actionable(self) -> None:
        loader = create_default_loader_registry().get("diffusers_image_loader")
        old_diffusers = ModuleType("diffusers")
        with patch.dict(
            "sys.modules", {"torch": _fake_torch(), "diffusers": old_diffusers}
        ), self.assertRaisesRegex(RuntimeError, "has no FluxPipeline.*Upgrade diffusers"):
            loader.load(ModelRegistry().get("flux-1-dev-local"))


class DiffusersLoaderDtypeAndVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = create_default_loader_registry().get("diffusers_image_loader")
        self.torch = _fake_torch()

    def test_mps_dtype_policy(self) -> None:
        torch = self.torch
        resolve = self.loader._resolve_runtime_dtype
        self.assertIs(resolve(torch.float16, "mps", torch), torch.float32)
        self.assertIs(resolve(torch.float16, "cpu", torch), torch.float32)
        self.assertIs(resolve(torch.float16, "cuda", torch), torch.float16)
        self.assertIs(resolve(torch.bfloat16, "mps", torch), torch.bfloat16)
        self.assertIs(resolve(torch.bfloat16, "cuda", torch), torch.bfloat16)
        self.assertIs(resolve(torch.float32, "mps", torch), torch.float32)

    def test_unsupported_dtype_fails_with_an_actionable_message(self) -> None:
        manifest = ModelManifest.model_validate(_image_manifest(dtype="int4"))
        with self.assertRaisesRegex(ValueError, "unsupported diffusers dtype 'int4'.*bfloat16"):
            self.loader._resolve_weight_dtype(manifest, self.torch)

    def test_variantless_flux_weights_request_no_variant(self) -> None:
        manifest = ModelRegistry().get("flux-1-dev-local")
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "model_index.json").write_text(json.dumps(_FLUX_INDEX), encoding="utf-8")
            transformer = root / "transformer"
            transformer.mkdir()
            shard = "diffusion_pytorch_model-00001-of-00003.safetensors"
            (transformer / shard).write_bytes(b"stub")
            self.assertIsNone(self.loader._resolve_variant(manifest, root))

            (transformer / "diffusion_pytorch_model.bf16.safetensors").write_bytes(b"stub")
            self.assertEqual(self.loader._resolve_variant(manifest, root), "bf16")

    def test_sdxl_fp16_variant_still_resolves(self) -> None:
        manifest = ModelRegistry().get("sdxl-local")
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "model.fp16.safetensors").write_bytes(b"stub")
            self.assertEqual(self.loader._resolve_variant(manifest, root), "fp16")


class _RecordingResult:
    def __init__(self, width: int, height: int) -> None:
        from PIL import Image

        self.images = [Image.new("RGB", (width, height), color=(10, 20, 30))]


class _StrictFluxPipeline:
    """Fails like FluxPipeline does when called with a negative_prompt."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _RecordingResult:
        if "negative_prompt" in kwargs:
            raise TypeError("__call__() got an unexpected keyword argument 'negative_prompt'")
        self.calls.append(kwargs)
        return _RecordingResult(int(kwargs.get("width", 64)), int(kwargs.get("height", 64)))


class _RecordingSDXLPipeline(_StrictFluxPipeline):
    def __call__(self, **kwargs: Any) -> _RecordingResult:
        self.calls.append(kwargs)
        return _RecordingResult(int(kwargs.get("width", 64)), int(kwargs.get("height", 64)))


def _fake_load_with(pipeline_cls: type, *, supports_negative_prompt: bool):
    def _load(self: object, manifest: Any) -> dict[str, object]:
        return {
            "stub": False,
            "loader": "DiffusersImageLoader",
            "manifest_id": manifest.id,
            "display_name": manifest.display_name,
            "runtime": manifest.runtime,
            "provider": manifest.provider,
            "local_path": manifest.local_path,
            "remote_ref": manifest.remote_ref,
            "dtype": manifest.dtype,
            "load_dtype": "bfloat16",
            "torch_dtype": "bfloat16",
            "weight_dtype": "bfloat16",
            "variant": None,
            "device": "cpu",
            "default_params": dict(manifest.default_params),
            "path_exists": True,
            "pipeline": pipeline_cls(),
            "pipeline_class": (
                "StableDiffusionXLPipeline" if supports_negative_prompt else "FluxPipeline"
            ),
            "supports_negative_prompt": supports_negative_prompt,
            "img2img_pipeline": None,
        }

    return _load


class ImageGeneratorNegativePromptTests(unittest.TestCase):
    def _run(self, model_id: str, pipeline_cls: type, *, supports_negative_prompt: bool):
        from bootstrap import create_default_model_service
        from core.schemas.generation import GenerationRequest
        from generators.image.generator import ImageGenerator

        with TemporaryDirectory() as tmp_dir, patch(
            "core.models.loader.DiffusersImageLoader.load",
            new=_fake_load_with(pipeline_cls, supports_negative_prompt=supports_negative_prompt),
        ):
            service = create_default_model_service()
            generator = ImageGenerator(service, output_dir=Path(tmp_dir) / "outputs")
            result = generator.run(
                GenerationRequest(
                    media_type="image",
                    prompt="A lighthouse at dawn",
                    negative_prompt="text, watermark",
                    model_id=model_id,
                    params={"steps": 2, "width": 64, "height": 64},
                )
            )
            pipeline = service.get_runtime(model_id, "image", "text-to-image")["pipeline"]
        return result, pipeline

    def test_flux_receives_no_negative_prompt_and_metadata_says_so(self) -> None:
        result, pipeline = self._run(
            "flux-1-dev", _StrictFluxPipeline, supports_negative_prompt=False
        )

        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(len(pipeline.calls), 1)
        self.assertNotIn("negative_prompt", pipeline.calls[0])
        self.assertEqual(pipeline.calls[0]["num_inference_steps"], 2)
        self.assertEqual(pipeline.calls[0]["width"], 64)
        self.assertIn("generator", pipeline.calls[0])
        self.assertEqual(result.metadata["negative_prompt"], "text, watermark")
        self.assertFalse(result.metadata["negative_prompt_applied"])
        self.assertFalse(result.metadata["negative_prompt_supported"])
        self.assertEqual(result.metadata["torch_dtype"], "bfloat16")

    def test_sdxl_still_receives_the_negative_prompt(self) -> None:
        result, pipeline = self._run("sdxl", _RecordingSDXLPipeline, supports_negative_prompt=True)

        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(pipeline.calls[0]["negative_prompt"], "text, watermark")
        self.assertTrue(result.metadata["negative_prompt_applied"])
        self.assertTrue(result.metadata["negative_prompt_supported"])


if __name__ == "__main__":
    unittest.main()
