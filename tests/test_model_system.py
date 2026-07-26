"""Unit tests for the initial model system skeleton."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

IMPORT_ERROR: Exception | None = None

try:
    from PIL import Image

    from bootstrap import (
        create_application_services,
        create_default_audio_generator,
        create_default_image_generator,
        create_default_model_service,
    )
    from core.models import (
        ModelRegistry,
        ModelResolver,
        ModelRuntimeCache,
        create_default_loader_registry,
    )
    from core.schemas import GenerationRequest
    from generators.audio import AudioGenerator
    from generators.image import ImageGenerator
    from generators.video import VideoGenerator
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class _FakePipelineResult:
    def __init__(self, image: Image.Image) -> None:
        self.images = [image]


class _FakePipeline:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.loaded_loras: list[dict[str, object]] = []
        self.adapter_calls: list[dict[str, object]] = []
        self.unload_calls = 0

    def __call__(self, **kwargs: object) -> _FakePipelineResult:
        self.calls.append(kwargs)
        width = int(kwargs.get("width", 64))
        height = int(kwargs.get("height", 64))
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(12, 34, 56)))

    def load_lora_weights(self, source: str, **kwargs: object) -> None:
        self.loaded_loras.append({"source": source, **kwargs})

    def set_adapters(
        self,
        adapter_names: str | list[str],
        adapter_weights: float | list[float] | None = None,
    ) -> None:
        self.adapter_calls.append(
            {"adapter_names": adapter_names, "adapter_weights": adapter_weights}
        )

    def unload_lora_weights(self) -> None:
        self.unload_calls += 1

    def delete_adapters(self, adapter_names: str | list[str]) -> None:
        return None


def _fake_diffusers_load(self, manifest):
    return {
        "stub": False,
        "loader": self.__class__.__name__,
        "manifest_id": manifest.id,
        "display_name": manifest.display_name,
        "runtime": manifest.runtime,
        "provider": manifest.provider,
        "local_path": manifest.local_path,
        "remote_ref": manifest.remote_ref,
        "dtype": manifest.dtype,
        "load_dtype": "float32",
        "torch_dtype": "float32",
        "weight_dtype": "float16",
        "variant": "fp16",
        "device": "cpu",
        "default_params": dict(manifest.default_params),
        "path_exists": True,
        "pipeline": _FakePipeline(),
    }


class _FakeMusicgenProcessor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, *, text, padding, return_tensors):
        import torch

        self.calls.append(text)
        return {
            "input_ids": torch.ones((1, 4), dtype=torch.long),
            "attention_mask": torch.ones((1, 4), dtype=torch.long),
        }


class _FakeAudioEncoderConfig:
    sampling_rate = 32000
    frame_rate = 50


class _FakeMusicgenConfig:
    audio_encoder = _FakeAudioEncoderConfig()


class _FakeMusicgenModel:
    def __init__(self) -> None:
        self.config = _FakeMusicgenConfig()
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        return torch.zeros((1, 1, 32000), dtype=torch.float32)


class _RandomMusicgenModel(_FakeMusicgenModel):
    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        return torch.rand((1, 1, 32000), dtype=torch.float32)


def _fake_musicgen_load(self, manifest):
    return {
        "stub": False,
        "loader": self.__class__.__name__,
        "manifest_id": manifest.id,
        "display_name": manifest.display_name,
        "runtime": manifest.runtime,
        "provider": manifest.provider,
        "local_path": manifest.local_path,
        "remote_ref": manifest.remote_ref,
        "dtype": manifest.dtype,
        "load_dtype": "float32",
        "torch_dtype": "float32",
        "weight_dtype": "float32",
        "device": "cpu",
        "default_params": dict(manifest.default_params),
        "path_exists": True,
        "processor": _FakeMusicgenProcessor(),
        "model": _FakeMusicgenModel(),
        "sampling_rate": 32000,
        "frame_rate": 50,
    }


def _fake_random_musicgen_load(self, manifest):
    runtime = _fake_musicgen_load(self, manifest)
    runtime["model"] = _RandomMusicgenModel()
    return runtime


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ModelSystemTests(unittest.TestCase):
    def test_registry_loads_and_filters_manifests(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_manifest(
                root / "image" / "sdxl-local.json",
                {
                    "id": "sdxl-local",
                    "public_id": "sdxl",
                    "display_name": "SDXL Local",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sdxl",
                    "loader": "diffusers_image_loader",
                    "aliases": ["sdxl-local"],
                    "is_default": True,
                },
            )
            _write_manifest(
                root / "image" / "sd15-local.json",
                {
                    "id": "sd15-local",
                    "public_id": "sd15",
                    "display_name": "SD 1.5 Local",
                    "media_type": "image",
                    "task_type": "image-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sd15",
                    "loader": "diffusers_image_loader",
                    "aliases": ["sd15-local"],
                },
            )

            registry = ModelRegistry(manifest_root=root)
            registry.load_all()

            self.assertEqual(registry.get("sdxl-local").display_name, "SDXL Local")
            self.assertEqual(len(registry.list_by_media_type("image")), 2)
            self.assertEqual(len(registry.list_by_task_type("text-to-image")), 1)
            self.assertEqual(registry.get("sdxl-local").public_model_id, "sdxl")
            self.assertEqual(registry.get_default("image").id, "sdxl-local")

    def test_resolver_prefers_aliases_before_manifest_ids(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_manifest(
                root / "image" / "sdxl-local.json",
                {
                    "id": "stable-diffusion-xl",
                    "public_id": "sdxl",
                    "display_name": "SDXL Local",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sdxl",
                    "loader": "diffusers_image_loader",
                    "aliases": ["sdxl-local"],
                    "is_default": True,
                },
            )
            _write_manifest(
                root / "image" / "legacy-sdxl.json",
                {
                    "id": "sdxl",
                    "public_id": "legacy-sdxl",
                    "display_name": "Legacy SDXL",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/legacy-sdxl",
                    "loader": "diffusers_image_loader",
                },
            )

            registry = ModelRegistry(manifest_root=root)
            resolver = ModelResolver(registry)

            self.assertEqual(
                resolver.resolve("sdxl", "image", "text-to-image").id,
                "stable-diffusion-xl",
            )
            self.assertEqual(
                resolver.resolve("sdxl-local", "image", "text-to-image").id,
                "stable-diffusion-xl",
            )
            self.assertEqual(
                resolver.resolve(None, "image", "text-to-image").id,
                "stable-diffusion-xl",
            )

    def test_resolver_maps_public_id_and_manifest_id_to_sdxl_local(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_manifest(
                root / "image" / "sdxl-local.json",
                {
                    "id": "sdxl-local",
                    "public_id": "sdxl",
                    "display_name": "SDXL Local",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sdxl",
                    "loader": "diffusers_image_loader",
                    "aliases": ["sdxl-local"],
                    "is_default": True,
                },
            )

            registry = ModelRegistry(manifest_root=root)
            resolver = ModelResolver(registry)

            self.assertEqual(
                resolver.resolve("sdxl", "image", "text-to-image").id,
                "sdxl-local",
            )
            self.assertEqual(
                resolver.resolve("sdxl-local", "image", "text-to-image").id,
                "sdxl-local",
            )

    def test_registry_ignores_equivalent_duplicate_manifest_files(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = {
                "id": "learned-video-local",
                "public_id": "learned-video",
                "display_name": "Learned Video Local",
                "media_type": "video",
                "task_type": "text-to-video",
                "provider": "local",
                "runtime": "learned",
                "local_path": "./models/video/learned-runtime",
                "loader": "learned_video_loader",
                "aliases": ["learned-video-local"],
            }
            _write_manifest(root / "video" / "learned-local.json", payload)
            _write_manifest(root / "video" / "learned-local 2.json", payload)

            registry = ModelRegistry(manifest_root=root)
            registry.load_all()

            manifests = registry.list_by_media_type("video")
            self.assertEqual(len(manifests), 1)
            self.assertEqual(manifests[0].id, "learned-video-local")

    def test_runtime_cache_evicts_old_entries(self) -> None:
        cache = ModelRuntimeCache(max_entries=1)
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})

        self.assertFalse(cache.has("model-a"))
        self.assertTrue(cache.has("model-b"))
        self.assertEqual(cache.loaded_ids(), ["model-b"])

    def test_model_service_reuses_cached_runtime(self) -> None:
        with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
            service = create_default_model_service()
            first = service.get_runtime("sdxl", "image", "text-to-image")
            second = service.get_runtime("sdxl", "image", "text-to-image")

        self.assertIs(first, second)
        self.assertEqual(first["manifest_id"], "sdxl-local")
        self.assertEqual(service.runtime_cache.loaded_ids(), ["sdxl-local"])

    def test_loader_registry_contains_initial_diffusers_loader(self) -> None:
        registry = create_default_loader_registry()
        loader = registry.get("diffusers_image_loader")

        self.assertEqual(loader.__class__.__name__, "DiffusersImageLoader")

    def test_loader_registry_contains_musicgen_loader(self) -> None:
        registry = create_default_loader_registry()
        loader = registry.get("transformers_musicgen_loader")

        self.assertEqual(loader.__class__.__name__, "TransformersMusicgenLoader")

    def test_musicgen_loader_normalizes_musicgen_config_class(self) -> None:
        from transformers import MusicgenConfig, MusicgenForConditionalGeneration

        loader = create_default_loader_registry().get("transformers_musicgen_loader")
        original_config_class = MusicgenForConditionalGeneration.config_class
        try:
            MusicgenForConditionalGeneration.config_class = object
            loader._normalize_musicgen_config_class(
                MusicgenForConditionalGeneration,
                MusicgenConfig,
            )
            self.assertIs(MusicgenForConditionalGeneration.config_class, MusicgenConfig)
        finally:
            MusicgenForConditionalGeneration.config_class = original_config_class

    def test_loader_uses_float32_runtime_on_mps_for_fp16_weights(self) -> None:
        import torch

        loader = create_default_loader_registry().get("diffusers_image_loader")

        self.assertEqual(
            loader._resolve_runtime_dtype(torch.float16, "mps", torch),
            torch.float32,
        )
        self.assertEqual(
            loader._resolve_load_dtype(torch.float16, torch.float32),
            torch.float32,
        )

    def test_diffusers_loader_only_requests_an_existing_weight_variant(self) -> None:
        loader = create_default_loader_registry().get("diffusers_image_loader")
        manifest = ModelRegistry().get("sdxl-local")

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            variant_weight = root / "model.fp16.safetensors"
            variant_weight.write_bytes(b"stub")
            self.assertEqual(loader._resolve_variant(manifest, root), "fp16")

            variant_weight.unlink()
            (root / "pytorch_model.bin").write_bytes(b"stub")
            self.assertIsNone(loader._resolve_variant(manifest, root))

    def test_application_services_resolve_environment_overrides(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "custom-manifests"
            output_root = root / "custom-outputs"
            db_path = root / "custom.db"

            _write_manifest(
                manifest_root / "image" / "sdxl-local.json",
                {
                    "id": "sdxl-local",
                    "public_id": "sdxl",
                    "display_name": "SDXL Local",
                    "media_type": "image",
                    "task_type": "text-to-image",
                    "provider": "local",
                    "runtime": "diffusers",
                    "local_path": "./models/image/sdxl",
                    "loader": "diffusers_image_loader",
                    "is_default": True,
                },
            )

            with patch.dict(
                os.environ,
                {
                    "MODELS_MANIFEST_ROOT": str(manifest_root),
                    "OUTPUT_DIR": str(output_root),
                    "DB_PATH": str(db_path),
                    "MAX_CACHED_MODELS": "2",
                },
                clear=False,
            ):
                services = create_application_services()

            self.assertEqual(services.output_dir, output_root / "images")
            self.assertEqual(services.model_service.registry.manifest_root, manifest_root)
            self.assertEqual(services.model_service.runtime_cache.max_entries, 2)
            self.assertEqual(services.job_repository._db_path, db_path)

    def test_image_generator_uses_model_service_runtime(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="A cinematic city skyline at dusk",
                        model_id="",
                        params={"steps": 12},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(service.runtime_cache.loaded_ids(), ["sdxl-local"])
            self.assertEqual(result.metadata["task_type"], "text-to-image")
            self.assertEqual(result.metadata["requested_model_id"], None)
            self.assertEqual(result.metadata["model_id"], "sdxl")
            self.assertEqual(result.metadata["manifest_id"], "sdxl-local")
            self.assertEqual(result.metadata["model_runtime"], "diffusers")
            self.assertEqual(result.metadata["device"], "cpu")
            self.assertEqual(result.metadata["default_params"]["width"], 1024)
            self.assertEqual(result.metadata["params"]["num_inference_steps"], 12)
            self.assertEqual(result.metadata["stub"], False)
            self.assertIn("quality_report", result.metadata)
            self.assertIn("semantic_report", result.metadata["quality_report"])
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_image_generator_loads_lora_from_request_params(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            lora_path = Path(tmp_dir) / "mai.safetensors"
            lora_path.write_bytes(b"test")
            with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Anime portrait",
                        model_id="sdxl",
                        params={
                            "lora_path": str(lora_path),
                            "lora_scale": 0.75,
                        },
                    )
                )
                runtime_obj = service.get_runtime("sdxl", "image", "text-to-image")
                pipeline = runtime_obj["pipeline"]

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["lora_path"], str(lora_path))
            self.assertEqual(result.metadata["lora_scale"], 0.75)
            self.assertEqual(pipeline.loaded_loras[0]["weight_name"], "mai.safetensors")
            self.assertEqual(pipeline.adapter_calls[0]["adapter_weights"], 0.75)

    def test_image_generator_accepts_public_model_alias(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Studio portrait",
                        model_id="sdxl",
                        params={},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["requested_model_id"], "sdxl")
            self.assertEqual(result.metadata["model_id"], "sdxl")
            self.assertEqual(result.metadata["manifest_id"], "sdxl-local")

    def test_image_generator_accepts_canonical_manifest_id(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Studio portrait",
                        model_id="sdxl-local",
                        params={},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["requested_model_id"], "sdxl-local")
            self.assertEqual(result.metadata["model_id"], "sdxl")
            self.assertEqual(result.metadata["manifest_id"], "sdxl-local")

    def test_bootstrap_factory_composes_default_image_generator(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
                generator = create_default_image_generator(output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="An editorial portrait with dramatic rim light",
                        model_id="",
                        params={"steps": 8},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["stub"], False)
            self.assertEqual(result.metadata["model_id"], "sdxl")
            self.assertEqual(result.metadata["manifest_id"], "sdxl-local")
            self.assertEqual(result.metadata["params"]["num_inference_steps"], 8)
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_bootstrap_factory_reuses_injected_model_service(self) -> None:
        service = create_default_model_service()
        generator = create_default_image_generator(model_service=service)

        self.assertIs(generator.model_service, service)

    def test_audio_generator_uses_model_service_runtime(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch("core.models.loader.TransformersMusicgenLoader.load", new=_fake_musicgen_load):
                service = create_default_model_service()
                generator = AudioGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="dreamy synth loop",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={
                            "duration_seconds": 6,
                            "guidance_scale": 3.5,
                            "bpm": 96,
                            "mood": "dreamy",
                            "genre": "ambient",
                            "instruments": "analog synth, soft percussion",
                            "structure": "seamless loop",
                            "temperature": 0.8,
                            "top_k": 180,
                            "top_p": 0.9,
                            "source_asset_id": "asset-audio-source",
                            "reuse_action": "variation",
                        },
                    )
                )
                runtime = service.get_runtime(
                    "musicgen-small",
                    "audio",
                    "text-to-music",
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(service.runtime_cache.loaded_ids(), ["musicgen-small-local"])
            self.assertEqual(result.metadata["requested_model_id"], "musicgen-small")
            self.assertEqual(result.metadata["model_id"], "musicgen-small")
            self.assertEqual(result.metadata["manifest_id"], "musicgen-small-local")
            self.assertEqual(result.metadata["model_runtime"], "transformers")
            self.assertEqual(result.metadata["sampling_rate"], 32000)
            self.assertEqual(result.metadata["stub"], False)
            self.assertIn("quality_report", result.metadata)
            self.assertIn("semantic_report", result.metadata["quality_report"])
            self.assertEqual(
                runtime["processor"].calls[0],
                [
                    "ambient music, dreamy mood, 96 BPM, "
                    "featuring analog synth, soft percussion, "
                    "seamless loop structure, dreamy synth loop"
                ],
            )
            self.assertEqual(runtime["model"].calls[0]["temperature"], 0.8)
            self.assertEqual(runtime["model"].calls[0]["top_k"], 180)
            self.assertEqual(runtime["model"].calls[0]["top_p"], 0.9)
            self.assertNotIn("source_asset_id", runtime["model"].calls[0])
            self.assertNotIn("reuse_action", runtime["model"].calls[0])
            self.assertEqual(result.metadata["source_asset_id"], "asset-audio-source")
            self.assertEqual(result.metadata["reuse_action"], "variation")
            self.assertEqual(result.metadata["params"]["structure"], "seamless loop")
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_audio_generator_reuses_seed_without_unsupported_generator_kwarg(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with (
                patch(
                    "core.models.loader.TransformersMusicgenLoader.load",
                    new=_fake_random_musicgen_load,
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                service = create_default_model_service()
                generator = AudioGenerator(service, output_dir=output_dir)

                def generate(seed: int) -> bytes:
                    result = generator.run(
                        GenerationRequest(
                            media_type="audio",
                            prompt="deterministic synth loop",
                            model_id="musicgen-small",
                            seed=seed,
                            output_format="wav",
                            params={"duration_seconds": 2},
                        )
                    )
                    return Path(result.outputs[0]).read_bytes()

                first = generate(42)
                second = generate(42)
                different = generate(43)
                runtime = service.get_runtime(
                    "musicgen-small",
                    "audio",
                    "text-to-music",
                )

            self.assertEqual(first, second)
            self.assertNotEqual(first, different)
            self.assertNotIn("generator", runtime["model"].calls[0])
            self.assertNotIn("top_p", runtime["model"].calls[0])

    def test_audio_generator_rejects_out_of_range_params(self) -> None:
        generator = AudioGenerator(create_default_model_service())
        invalid_cases = (
            ("duration_seconds", 1, "2 and 30"),
            ("duration_seconds", 31, "2 and 30"),
            ("guidance_scale", 0.9, "1 and 10"),
            ("guidance_scale", 10.1, "1 and 10"),
            ("temperature", 0.05, "0.1 and 2"),
            ("temperature", 2.5, "0.1 and 2"),
            ("top_k", -1, "0 and 1000"),
            ("top_k", 1001, "0 and 1000"),
            ("top_p", -0.1, "0 and 1"),
            ("top_p", 1.1, "0 and 1"),
            ("bpm", 39, "40 and 240"),
            ("bpm", 241, "40 and 240"),
        )

        for name, value, expected_range in invalid_cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"Audio parameter '{name}' must be between {expected_range}",
                ):
                    generator.validate_request(
                        GenerationRequest(
                            media_type="audio",
                            prompt="minimal piano cue",
                            model_id="musicgen-small",
                            params={name: value},
                        )
                    )

        generator.validate_request(
            GenerationRequest(
                media_type="audio",
                prompt="boundary values",
                model_id="musicgen-small",
                params={
                    "duration_seconds": 30,
                    "guidance_scale": 10,
                    "temperature": 0.1,
                    "top_k": 0,
                    "top_p": 1,
                    "bpm": 240,
                },
            )
        )

    def test_bootstrap_factory_composes_default_audio_generator(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with patch("core.models.loader.TransformersMusicgenLoader.load", new=_fake_musicgen_load):
                generator = create_default_audio_generator(output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="gentle piano motif",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={"duration_seconds": 4},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["model_id"], "musicgen-small")
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_procedural_video_generator_rejects_mp4_output_format(self) -> None:
        service = create_default_model_service()
        generator = VideoGenerator(service)

        with self.assertRaisesRegex(ValueError, "supports gif output only"):
            generator.validate_request(
                GenerationRequest(
                    media_type="video",
                    prompt="procedural storyboard",
                    model_id="storyboard-video",
                    output_format="mp4",
                    params={},
                )
            )

    def test_learned_video_generator_accepts_mp4_output_format(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            runtime_root = root / "runtime" / "learned-video"
            runtime_root.mkdir(parents=True)
            (runtime_root / "runtime.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "",
                        "def load_runtime(manifest):",
                        "    output_path = Path(__file__).with_name('learned-output.mp4')",
                        "    def renderer(**kwargs):",
                        "        output_path.write_bytes(b'0' * 131072)",
                        "        return {",
                        "            'output_path': str(output_path),",
                        "            'output_format': 'mp4',",
                        "            'metadata': {'adapter_contract': 'test'},",
                        "        }",
                        "    return {'runtime_adapter': 'learned_text_to_video', 'renderer': renderer}",
                    ]
                ),
                encoding="utf-8",
            )
            _write_manifest(
                manifest_root / "video" / "learned.json",
                {
                    "id": "learned-video-local",
                    "public_id": "learned-video",
                    "display_name": "Learned Video",
                    "media_type": "video",
                    "task_type": "text-to-video",
                    "provider": "local",
                    "runtime": "learned",
                    "local_path": str(runtime_root),
                    "loader": "learned_video_loader",
                    "default_params": {"entrypoint": "runtime.py"},
                    "aliases": ["learned-video-local"],
                    "enabled": True,
                },
            )
            service = create_default_model_service(manifest_root=manifest_root)
            generator = VideoGenerator(service, output_dir=root / "outputs" / "videos")

            result = generator.run(
                GenerationRequest(
                    media_type="video",
                    prompt="learned runtime smoke",
                    model_id="learned-video",
                    output_format="mp4",
                    params={"duration_seconds": 1},
                )
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["output_format"], "mp4")
            self.assertEqual(result.metadata["runtime_adapter"], "learned_text_to_video")
            self.assertEqual(result.metadata["adapter_contract"], "test")
            self.assertTrue(Path(result.outputs[0]).exists())
            self.assertEqual(Path(result.outputs[0]).suffix, ".mp4")

    def test_video_generator_keeps_gif_output_behavior(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            service = create_default_model_service()
            generator = VideoGenerator(service, output_dir=output_dir)

            result = generator.run(
                GenerationRequest(
                    media_type="video",
                    prompt="procedural storyboard",
                    model_id="storyboard-video",
                    output_format="gif",
                    params={"duration_seconds": 2, "fps": 6},
                )
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["output_format"], "gif")
            self.assertTrue(Path(result.outputs[0]).exists())
            self.assertEqual(Path(result.outputs[0]).suffix, ".gif")


if __name__ == "__main__":
    unittest.main()
