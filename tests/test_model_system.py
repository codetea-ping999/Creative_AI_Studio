"""Unit tests for the initial model system skeleton."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np

IMPORT_ERROR: Exception | None = None

try:
    from PIL import Image

    from bootstrap import (
        create_application_services,
        create_default_audio_generator,
        create_default_image_generator,
        create_default_model_service,
    )
    from core.assets import Asset, AssetRepository
    from core.bible import BibleRepository
    from core.jobs.context import GenerationCancelled, GenerationContext
    from core.models import (
        ModelRegistry,
        ModelResolver,
        ModelRuntimeCache,
        ModelService,
        create_default_loader_registry,
        release_runtime,
    )
    from core.models.cache import resolve_media_cache_limits
    from core.prompting import PromptComposer
    from core.reference_capabilities import (
        DEFAULT_REFERENCE_STRENGTH,
        MissingReferenceAssetError,
        ReferenceImageInput,
        UnsupportedReferenceError,
    )
    from core.schemas import GenerationRequest
    from generators.audio import AudioGenerator
    from generators.image import ImageGenerator
    from generators.image.providers import UnsupportedImageParameterError
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
        self.to_calls: list[str] = []

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

    def to(self, device: str) -> "_FakePipeline":
        self.to_calls.append(device)
        return self


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


class _FakeStepAwarePipeline:
    """Fake pipeline that mimics diffusers' callback_on_step_end support."""

    def __init__(self, num_steps_actual: int | None = None) -> None:
        self.num_steps_actual = num_steps_actual
        self.steps_invoked = 0

    def __call__(
        self,
        *,
        prompt,
        negative_prompt=None,
        width=64,
        height=64,
        guidance_scale=7.5,
        num_inference_steps=30,
        callback_on_step_end=None,
        **kwargs,
    ):
        total_steps = self.num_steps_actual or num_inference_steps
        for step_index in range(total_steps):
            self.steps_invoked = step_index + 1
            if callback_on_step_end is not None:
                callback_on_step_end(self, step_index, 0, {})
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(7, 8, 9)))

    def to(self, device: str) -> "_FakeStepAwarePipeline":
        return self


class _FakeReferenceCapablePipeline:
    """Fake pipeline that mimics an img2img-style diffusers call signature.

    Declares `image`/`strength` as literal named parameters (not swallowed
    into `**kwargs`) because `ImageGenerator._pipeline_accepts_reference_image`
    (#201) probes for those exact names via `inspect.signature`.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        prompt,
        negative_prompt=None,
        width=64,
        height=64,
        guidance_scale=7.5,
        num_inference_steps=30,
        image=None,
        strength=None,
        **kwargs,
    ):
        self.calls.append({"image": image, "strength": strength})
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(7, 8, 9)))

    def to(self, device: str) -> "_FakeReferenceCapablePipeline":
        return self


class _FakeReferenceCapableStepAwarePipeline:
    """Img2img-shaped pipeline that also invokes callback_on_step_end.

    Mirrors real diffusers img2img: it only actually runs
    ``int(num_inference_steps * strength)`` denoising steps (see
    generators/image/generator.py's ``effective_inference_steps``), not the
    full requested count -- used to prove the step callback's denominator is
    derived from that reduced count, not the raw request.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.steps_invoked = 0

    def __call__(
        self,
        *,
        prompt,
        negative_prompt=None,
        width=64,
        height=64,
        guidance_scale=7.5,
        num_inference_steps=30,
        image=None,
        strength=None,
        callback_on_step_end=None,
        **kwargs,
    ):
        self.calls.append({"image": image, "strength": strength})
        actual_steps = max(1, int(num_inference_steps * (strength or 0.0)))
        for step_index in range(actual_steps):
            self.steps_invoked = step_index + 1
            if callback_on_step_end is not None:
                callback_on_step_end(self, step_index, 0, {})
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(7, 8, 9)))

    def to(self, device: str) -> "_FakeReferenceCapableStepAwarePipeline":
        return self


def _fake_diffusers_load_reference_capable_step_aware(self, manifest):
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
        "pipeline": _FakeStepAwarePipeline(),
        "img2img_pipeline": _FakeReferenceCapableStepAwarePipeline(),
    }


def _fake_diffusers_load_reference_capable(self, manifest):
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
        # Text2img-shaped: no image/strength params, matching the real
        # StableDiffusionXLPipeline this stands in for.
        "pipeline": _FakeStepAwarePipeline(),
        # Img2img-shaped: mirrors core/models/loader.py's separate
        # img2img_pipeline built from the same loaded components (#201).
        "img2img_pipeline": _FakeReferenceCapablePipeline(),
    }


def _fake_diffusers_load_reference_capability_without_img2img_runtime(self, manifest):
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
        # Manifest declares reference_capability (see the test that uses
        # this), but this runtime has no img2img_pipeline at all -- e.g. an
        # older cached runtime from before #201, or a loader that never
        # built one. The manifest-level contract cannot promise more than
        # what actually got loaded.
        "pipeline": _FakeStepAwarePipeline(),
    }


def _fake_diffusers_load_step_aware(self, manifest):
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
        "pipeline": _FakeStepAwarePipeline(),
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


class _StereoMusicgenModel(_FakeMusicgenModel):
    """Returns two channels at different, fixed levels (like a real stereo mix
    with an intentional pan), long enough to leave a flat middle region once
    the music preset's fade_in(0.05s)/fade_out(0.5s) ramps are excluded.

    A postprocessing step that flattens (channels, samples) into one
    concatenated buffer would double the frame count. A step that measures
    normalization gain per channel independently would pull both channels to
    the same target level, destroying the 8:1 ratio between them; linked gain
    preserves it. Both are checked by the test.
    """

    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        left = torch.full((64000,), 0.8, dtype=torch.float32)
        right = torch.full((64000,), 0.1, dtype=torch.float32)
        return torch.stack([left, right], dim=0).unsqueeze(0)


class _Bfloat16MusicgenModel(_FakeMusicgenModel):
    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        return torch.rand((1, 1, 16000), dtype=torch.bfloat16)


class _NearSilentMusicgenModel(_FakeMusicgenModel):
    """A constant, near-zero (but nonzero) buffer: degenerate model output.

    The RMS stage's boost cap is meant to leave content like this too quiet
    rather than amplifying it; without a linked cap on the later peak stage,
    it gets amplified to near full scale anyway.
    """

    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        return torch.full((1, 1, 16000), 2e-6, dtype=torch.float32)


class _NonFiniteMusicgenModel(_FakeMusicgenModel):
    """Contains a NaN sample: a degenerate/failed generation."""

    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        audio = torch.zeros((1, 1, 16000), dtype=torch.float32)
        audio[0, 0, 100] = float("nan")
        return audio


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


def _fake_stereo_musicgen_load(self, manifest):
    runtime = _fake_musicgen_load(self, manifest)
    runtime["model"] = _StereoMusicgenModel()
    return runtime


def _fake_bfloat16_musicgen_load(self, manifest):
    runtime = _fake_musicgen_load(self, manifest)
    runtime["model"] = _Bfloat16MusicgenModel()
    return runtime


def _fake_near_silent_musicgen_load(self, manifest):
    runtime = _fake_musicgen_load(self, manifest)
    runtime["model"] = _NearSilentMusicgenModel()
    return runtime


def _fake_non_finite_musicgen_load(self, manifest):
    runtime = _fake_musicgen_load(self, manifest)
    runtime["model"] = _NonFiniteMusicgenModel()
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

    def test_runtime_cache_calls_on_evict_exactly_once_per_removal(self) -> None:
        evicted: list[str] = []
        cache = ModelRuntimeCache(
            max_entries=1,
            on_evict=lambda model_id, runtime_obj: evicted.append(model_id),
        )
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})  # evicts model-a
        cache.put("model-c", {"id": "model-c"})  # evicts model-b
        cache.unload("model-c")
        cache.unload("missing-model")  # no-op, must not call on_evict

        self.assertEqual(evicted, ["model-a", "model-b", "model-c"])

    def test_runtime_cache_put_evicts_the_runtime_it_replaces(self) -> None:
        # Regression (#373): replacing an existing model_id via put() must run
        # the same on_evict cleanup as unload()/unload_all(), not just drop
        # the old runtime object -- that cleanup is what actually returns
        # GPU/MPS memory, which plain Python GC does not reliably do.
        evicted: list[tuple[str, dict]] = []
        cache = ModelRuntimeCache(
            max_entries=2,  # large enough that this never triggers overflow eviction
            on_evict=lambda model_id, runtime_obj: evicted.append((model_id, runtime_obj)),
        )
        first = {"id": "model-a", "generation": 1}
        second = {"id": "model-a", "generation": 2}

        cache.put("model-a", first)
        self.assertEqual(evicted, [])  # nothing to evict on first insert

        cache.put("model-a", second)  # replaces the same model_id

        self.assertEqual(evicted, [("model-a", first)])
        self.assertIs(cache.get("model-a"), second)

    def test_runtime_cache_put_skips_eviction_when_reinserting_the_same_object(
        self,
    ) -> None:
        # Regression (Codex review on PR #374, P2): reinserting the *same*
        # runtime instance under its own model_id -- e.g. to refresh its LRU
        # position or change its media bucket -- is not a replacement. The
        # #373 fix above must not run on_evict cleanup (which strips
        # pipeline/model/processor, see core/models/cleanup.py) on an object
        # that is being kept, not discarded.
        evicted: list[str] = []

        def _on_evict(model_id: str, runtime_obj: dict) -> None:
            # Mirrors core/models/cleanup.py's release_runtime(): a real
            # on_evict hook destructively strips the runtime, which is
            # exactly what must not happen to an object being reinserted.
            evicted.append(model_id)
            runtime_obj.pop("pipeline", None)

        cache = ModelRuntimeCache(max_entries=2, on_evict=_on_evict)
        runtime = {"id": "model-a", "pipeline": object()}

        cache.put("model-a", runtime)
        cache.put("model-a", runtime, media_type="image")  # reinsert same object

        self.assertEqual(evicted, [])
        self.assertIs(cache.get("model-a"), runtime)
        self.assertIn("pipeline", runtime)  # not stripped by a spurious evict

    def test_resolve_runtime_serializes_concurrent_loads_of_the_same_model(self) -> None:
        # Regression (#373 follow-up, Codex review on PR #374): two callers
        # racing to resolve the same uncached model_id must not both load
        # and put() their own runtime -- the second put() would evict (and
        # run on_evict cleanup on) the runtime the first caller already
        # received and may still be using mid-generation. ModelService now
        # serializes load+put per model_id via ModelRuntimeCache.lock_for().
        load_started = Event()
        release_load = Event()

        class _FakeManifest:
            id = "model-a"
            loader = "fake"
            provider = "local"
            public_model_id = "model-a"

        class _FakeResolver:
            def resolve(self, model_id, media_type, task_type):
                return _FakeManifest()

        class _SlowLoader:
            def __init__(self) -> None:
                self.load_calls = 0

            def load(self, manifest):
                self.load_calls += 1
                load_started.set()
                # Blocks here to hold the "in progress" window open long
                # enough for a second concurrent resolve_runtime() call to
                # reach (and be forced to wait on) the same model's lock.
                release_load.wait(timeout=5)
                return {"id": manifest.id, "token": object()}

        class _FakeLoaderRegistry:
            def __init__(self, loader) -> None:
                self._loader = loader

            def get(self, name):
                return self._loader

        loader = _SlowLoader()
        service = ModelService(
            registry=None,
            resolver=_FakeResolver(),
            loader_registry=_FakeLoaderRegistry(loader),
            runtime_cache=ModelRuntimeCache(max_entries=2),
        )

        results: list[tuple[object, object]] = []

        def _resolve() -> None:
            results.append(service.resolve_runtime(None, "image", "text-to-image"))

        first_caller = Thread(target=_resolve)
        first_caller.start()
        self.assertTrue(load_started.wait(timeout=5))  # first caller is inside load(), blocked

        second_caller = Thread(target=_resolve)
        second_caller.start()
        time.sleep(0.05)  # give the second caller a chance to reach the lock

        # The second caller must be blocked waiting for the first caller's
        # lock, not independently calling load() a second time.
        self.assertEqual(loader.load_calls, 1)

        release_load.set()
        first_caller.join(timeout=5)
        second_caller.join(timeout=5)

        self.assertEqual(loader.load_calls, 1)
        self.assertEqual(len(results), 2)
        # Both callers received the exact same runtime object -- the second
        # one reused the cache instead of loading (and evicting) its own.
        self.assertIs(results[0][1], results[1][1])

    def test_runtime_cache_unload_all_calls_on_evict_for_every_entry(self) -> None:
        evicted: list[str] = []
        cache = ModelRuntimeCache(
            max_entries=2,
            on_evict=lambda model_id, runtime_obj: evicted.append(model_id),
        )
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})
        cache.unload_all()

        self.assertEqual(sorted(evicted), ["model-a", "model-b"])
        self.assertEqual(cache.loaded_ids(), [])

    def test_runtime_cache_survives_failing_on_evict_hook(self) -> None:
        def _broken_hook(model_id: str, runtime_obj: object) -> None:
            raise RuntimeError("boom")

        cache = ModelRuntimeCache(max_entries=1, on_evict=_broken_hook)
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})  # triggers the broken hook

        self.assertEqual(cache.loaded_ids(), ["model-b"])
        cache.unload("model-b")  # must not raise
        self.assertEqual(cache.loaded_ids(), [])

    def test_runtime_cache_rejects_non_positive_media_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "media_limits\\['text'\\]"):
            ModelRuntimeCache(media_limits={"text": 0})

    def test_runtime_cache_media_limits_stay_resident_independently(self) -> None:
        # Issue #182 acceptance criterion: "Text and image runtimes can
        # remain resident independently when configured."
        cache = ModelRuntimeCache(max_entries=1, media_limits={"text": 1, "image": 1})

        cache.put("text-a", {"id": "text-a"}, media_type="text")
        cache.put("image-a", {"id": "image-a"}, media_type="image")

        self.assertTrue(cache.has("text-a"))
        self.assertTrue(cache.has("image-a"))
        self.assertEqual(set(cache.loaded_ids()), {"text-a", "image-a"})

    def test_runtime_cache_evicts_within_a_media_budget_deterministically(self) -> None:
        evicted: list[str] = []
        cache = ModelRuntimeCache(
            max_entries=1,
            media_limits={"text": 1},
            on_evict=lambda model_id, runtime_obj: evicted.append(model_id),
        )
        cache.put("text-a", {"id": "text-a"}, media_type="text")
        cache.put("text-b", {"id": "text-b"}, media_type="text")  # evicts text-a

        self.assertEqual(evicted, ["text-a"])
        self.assertEqual(cache.loaded_ids(), ["text-b"])

    def test_runtime_cache_media_type_absent_from_limits_uses_default_bucket(self) -> None:
        # A media type with no configured budget falls back to the shared
        # `max_entries` budget, alongside entries that never pass
        # `media_type` at all -- this is the "missing per-media settings
        # retain backward-compatible behavior" acceptance criterion.
        cache = ModelRuntimeCache(max_entries=1, media_limits={"text": 5})

        cache.put("audio-a", {"id": "audio-a"}, media_type="audio")
        cache.put("no-media-b", {"id": "no-media-b"})  # evicts audio-a

        self.assertFalse(cache.has("audio-a"))
        self.assertTrue(cache.has("no-media-b"))

    def test_runtime_cache_put_without_media_type_matches_pre_182_behavior(self) -> None:
        # Callers that never pass `media_type` (e.g. code written before
        # #182) must see byte-for-byte the same eviction behavior as before.
        cache = ModelRuntimeCache(max_entries=1, media_limits={"text": 5, "image": 5})
        cache.put("model-a", {"id": "model-a"})
        cache.put("model-b", {"id": "model-b"})

        self.assertFalse(cache.has("model-a"))
        self.assertTrue(cache.has("model-b"))

    def test_resolve_media_cache_limits_reads_per_media_env_vars(self) -> None:
        env = {"MAX_CACHED_MODELS_TEXT": "3", "MAX_CACHED_MODELS_IMAGE": "2"}
        self.assertEqual(
            resolve_media_cache_limits(env),
            {"text": 3, "image": 2},
        )

    def test_resolve_media_cache_limits_omits_unset_media_types(self) -> None:
        self.assertEqual(resolve_media_cache_limits({"MAX_CACHED_MODELS_TEXT": "3"}), {"text": 3})

    def test_resolve_media_cache_limits_ignores_invalid_values(self) -> None:
        env = {
            "MAX_CACHED_MODELS_TEXT": "not-a-number",
            "MAX_CACHED_MODELS_IMAGE": "0",
            "MAX_CACHED_MODELS_AUDIO": "-1",
        }
        self.assertEqual(resolve_media_cache_limits(env), {})

    def test_resolve_media_cache_limits_reads_process_environ_when_env_omitted(self) -> None:
        with patch.dict(os.environ, {"MAX_CACHED_MODELS_VIDEO": "4"}, clear=True):
            self.assertEqual(resolve_media_cache_limits(), {"video": 4})

    def test_release_runtime_resets_lora_moves_pipeline_and_drops_references(self) -> None:
        pipeline = _FakePipeline()
        runtime_obj = {
            "manifest_id": "sdxl-local",
            "pipeline": pipeline,
            "active_lora_path": "/models/loras/example.safetensors",
            "active_lora_adapter": "lora_abc123",
            "active_lora_scale": 0.8,
        }

        release_runtime("sdxl-local", runtime_obj)

        self.assertEqual(pipeline.unload_calls, 1)
        self.assertEqual(pipeline.to_calls, ["cpu"])
        self.assertNotIn("pipeline", runtime_obj)
        self.assertNotIn("active_lora_path", runtime_obj)
        self.assertNotIn("active_lora_adapter", runtime_obj)
        self.assertNotIn("active_lora_scale", runtime_obj)

    def test_release_runtime_ignores_non_dict_and_missing_pipeline(self) -> None:
        release_runtime("opaque-runtime", object())  # must not raise
        release_runtime("bare-runtime", {"manifest_id": "storyboard-local"})  # must not raise

    def test_release_runtime_survives_pipeline_to_failure(self) -> None:
        class _BrokenPipeline:
            def to(self, device: str) -> None:
                raise RuntimeError("device move failed")

        runtime_obj = {"pipeline": _BrokenPipeline()}
        release_runtime("broken-model", runtime_obj)  # must not raise

        self.assertNotIn("pipeline", runtime_obj)

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

    def test_application_services_wires_per_media_cache_limits_from_env(self) -> None:
        # #182: resolve_media_cache_limits() is fully implemented and unit
        # tested on its own, but create_default_model_service() never called
        # it -- MAX_CACHED_MODELS_IMAGE/_TEXT had zero effect in the running
        # app. This drives the real factory, not ModelRuntimeCache directly.
        with TemporaryDirectory() as tmp_dir:
            with patch.dict(
                os.environ,
                {
                    "MODELS_MANIFEST_ROOT": str(Path(tmp_dir) / "manifests"),
                    "OUTPUT_DIR": str(Path(tmp_dir) / "outputs"),
                    "DB_PATH": str(Path(tmp_dir) / "jobs.db"),
                    "MAX_CACHED_MODELS": "1",
                    "MAX_CACHED_MODELS_IMAGE": "3",
                    "MAX_CACHED_MODELS_TEXT": "2",
                },
                clear=False,
            ):
                services = create_application_services()

            self.assertEqual(
                services.model_service.runtime_cache.media_limits,
                {"image": 3, "text": 2},
            )

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

    def test_image_generator_rejects_invalid_variation_count(self) -> None:
        generator = ImageGenerator(create_default_model_service())

        for value in (0, 5, 1.5, "2", True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "variation_count.*(?:integer|between 1 and 4)",
                ):
                    generator.validate_request(
                        GenerationRequest(
                            media_type="image",
                            prompt="Variation validation",
                            model_id="sdxl",
                            params={"variation_count": value},
                        )
                    )

    def test_image_generator_creates_reproducible_variations(self) -> None:
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
                        prompt="Three reproducible variations",
                        model_id="sdxl",
                        seed=100,
                        params={
                            "steps": 2,
                            "width": 64,
                            "height": 64,
                            "variation_count": 3,
                        },
                    )
                )
                pipeline = service.get_runtime(
                    "sdxl",
                    "image",
                    "text-to-image",
                )["pipeline"]

            self.assertEqual(len(result.outputs), 3)
            self.assertEqual(len(pipeline.calls), 3)
            self.assertEqual(
                [call["generator"].initial_seed() for call in pipeline.calls],
                [100, 101, 102],
            )
            self.assertEqual(result.metadata["base_seed"], 100)
            self.assertEqual(result.metadata["variation_count"], 3)
            self.assertEqual(
                [
                    (item["variation_index"], item["seed"])
                    for item in result.metadata["variations"]
                ],
                [(0, 100), (1, 101), (2, 102)],
            )
            self.assertTrue(all(Path(path).exists() for path in result.outputs))

    def test_image_generator_persists_random_base_seed(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with (
                patch(
                    "core.models.loader.DiffusersImageLoader.load",
                    new=_fake_diffusers_load,
                ),
                patch("generators.image.generator.secrets.randbits", return_value=900),
            ):
                service = create_default_model_service()
                result = ImageGenerator(service, output_dir=output_dir).run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Random seed becomes reproducible",
                        model_id="sdxl",
                        params={
                            "steps": 1,
                            "width": 64,
                            "height": 64,
                            "variation_count": 2,
                        },
                    )
                )

            self.assertEqual(result.metadata["base_seed"], 900)
            self.assertEqual(
                [item["seed"] for item in result.metadata["variations"]],
                [900, 901],
            )

    def test_image_generator_reports_diffusers_step_progress(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            reported_progress: list[float] = []
            context = GenerationContext(
                is_cancelled=lambda: False,
                on_progress=reported_progress.append,
                min_interval_seconds=0.0,
                min_progress_delta=0.0,
            )
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_step_aware,
            ):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="A four-step progress check",
                        model_id="sdxl",
                        params={"steps": 4, "width": 64, "height": 64},
                    ),
                    context,
                )
                pipeline = service.get_runtime(
                    "sdxl",
                    "image",
                    "text-to-image",
                )["pipeline"]

            self.assertEqual(reported_progress, [0.25, 0.5, 0.75, 1.0])
            self.assertEqual(pipeline.steps_invoked, 4)
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_image_generator_reports_progress_across_all_variations(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            reported_progress: list[float] = []
            context = GenerationContext(
                is_cancelled=lambda: False,
                on_progress=reported_progress.append,
            )
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_step_aware,
            ):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)
                generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Two variations with aggregate progress",
                        model_id="sdxl",
                        seed=21,
                        params={
                            "steps": 2,
                            "width": 64,
                            "height": 64,
                            "variation_count": 2,
                        },
                    ),
                    context,
                )

            self.assertEqual(reported_progress, [0.25, 0.5, 0.75, 1.0])

    def test_image_generator_stops_diffusers_pipeline_when_cancelled(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            cancellation_state = {"requested": False}

            def _record_progress(fraction: float) -> None:
                if fraction >= 0.5:
                    cancellation_state["requested"] = True

            context = GenerationContext(
                is_cancelled=lambda: cancellation_state["requested"],
                on_progress=_record_progress,
                min_interval_seconds=0.0,
                min_progress_delta=0.0,
            )
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_step_aware,
            ):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                with self.assertRaises(GenerationCancelled):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Cancel after the second step",
                            model_id="sdxl",
                            params={"steps": 4, "width": 64, "height": 64},
                        ),
                        context,
                    )
                pipeline = service.get_runtime(
                    "sdxl",
                    "image",
                    "text-to-image",
                )["pipeline"]

            self.assertEqual(pipeline.steps_invoked, 2)
            self.assertEqual(list(output_dir.glob("*")), [])

    def test_image_generator_removes_completed_variations_when_later_one_is_cancelled(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            cancellation_state = {"requested": False}

            def _record_progress(fraction: float) -> None:
                if fraction >= 0.75:
                    cancellation_state["requested"] = True

            context = GenerationContext(
                is_cancelled=lambda: cancellation_state["requested"],
                on_progress=_record_progress,
                min_interval_seconds=0.0,
                min_progress_delta=0.0,
            )
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_step_aware,
            ):
                service = create_default_model_service()
                generator = ImageGenerator(service, output_dir=output_dir)

                with self.assertRaises(GenerationCancelled):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Cancel during the second variation",
                            model_id="sdxl",
                            seed=300,
                            params={
                                "steps": 2,
                                "width": 64,
                                "height": 64,
                                "variation_count": 2,
                            },
                        ),
                        context,
                    )

            self.assertEqual(list(output_dir.glob("*")), [])

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

    def _prepare_character_reference(
        self, root: Path
    ) -> tuple[PromptComposer, str]:
        """Real Bible/Asset repositories with one character reference (#199/#201).

        Returns the composer and the Bible entry id -- request params then
        set `bible_refs: [entry_id]` to pull the reference into
        `resolved_prompt.resolved_references`, exactly as production does.
        """

        bible_repository = BibleRepository(root / "bible")
        asset_repository = AssetRepository(root / "assets")
        reference_image_path = root / "reference.png"
        Image.new("RGB", (32, 32), color=(200, 50, 50)).save(reference_image_path)
        asset_repository.create_or_update(
            Asset(
                id="asset_char_1",
                job_id="job_fixture",
                project_id=None,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path=str(reference_image_path),
            )
        )
        character = bible_repository.create(
            kind="character", name="Mina", reference_asset_ids=["asset_char_1"]
        )
        composer = PromptComposer(bible_repository, asset_repository)
        return composer, character.id

    def _write_reference_capable_manifest(
        self, manifest_root: Path, *, reference_capability: dict[str, object] | None = None
    ) -> str:
        """A custom SDXL-shaped manifest that advertises reference_capability.

        The real "sdxl" default manifest deliberately does not (#201 follow-up:
        Bible-derived references must be checked against
        `manifest.reference_capability` just like `request.references` already
        is), so tests that need a model advertising support use this instead.
        `reference_capability` overrides the default img2img-capable one, for
        tests that need a manifest advertising something narrower (e.g. only
        ip_adapter, or no face_crop preprocessing). Returns the manifest's
        public_id.
        """

        _write_manifest(
            manifest_root / "image" / "sdxl-reference-test.json",
            {
                "id": "sdxl-reference-test",
                "public_id": "sdxl-reference-test",
                "display_name": "SDXL Reference Test",
                "media_type": "image",
                "task_type": "text-to-image",
                "provider": "local",
                "runtime": "diffusers",
                "local_path": "./models/image/sdxl",
                "loader": "diffusers_image_loader",
                "reference_capability": reference_capability
                or {
                    "supported_modes": ["img2img"],
                    "supported_roles": ["character", "location"],
                    "min_strength": 0.0,
                    "max_strength": 1.0,
                    "max_references_per_role": 1,
                },
                "is_default": True,
            },
        )
        return "sdxl-reference-test"

    def test_image_generator_applies_resolved_reference_when_pipeline_supports_it(
        self,
    ) -> None:
        # Regression (#201, follow-up after Codex review on PR #376): a
        # Bible reference the composer resolved must actually reach the
        # *img2img* runtime as image/strength conditioning when a manifest
        # advertises support and that runtime can honor it -- not just sit
        # in metadata, and not the plain text2img "pipeline" (which real
        # StableDiffusionXLPipeline never accepts image/strength on).
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Mina on the rooftop",
                        model_id=model_id,
                        params={
                            # >1 so the strength floor (derived from
                            # num_inference_steps, see generate()) stays well
                            # below DEFAULT_REFERENCE_STRENGTH's expected
                            # inverted value and does not affect this
                            # assertion; 1 step would force strength to 1.0
                            # regardless of the requested lock strength.
                            "steps": 10,
                            "width": 64,
                            "height": 64,
                            "bible_refs": [character_id],
                        },
                    )
                )
                runtime = service.get_runtime(model_id, "image", "text-to-image")
                text2img_pipeline = runtime["pipeline"]
                img2img_pipeline = runtime["img2img_pipeline"]

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(text2img_pipeline.steps_invoked, 0)  # never called
            self.assertEqual(len(img2img_pipeline.calls), 1)
            # DEFAULT_REFERENCE_STRENGTH in the public 0=no-effect/
            # 1=follow-closely contract must arrive at diffusers img2img
            # (0=closest to source/1=ignores it) inverted.
            self.assertAlmostEqual(
                img2img_pipeline.calls[0]["strength"], 1.0 - DEFAULT_REFERENCE_STRENGTH
            )
            applied_image = img2img_pipeline.calls[0]["image"]
            self.assertIsInstance(applied_image, Image.Image)
            # Resized from the fixture's native 32x32 to the requested
            # 64x64 output size (#201 follow-up), not left at 32x32.
            self.assertEqual(applied_image.size, (64, 64))
            self.assertTrue(result.metadata["reference_conditioning_applied"])
            self.assertEqual(result.metadata["reference_applied_asset_id"], "asset_char_1")
            self.assertEqual(result.metadata["pipeline_class"], "_FakeReferenceCapablePipeline")

    def test_image_generator_treats_zero_strength_reference_as_unconditioned(
        self,
    ) -> None:
        # Regression (#201 follow-up, fourth Codex round on PR #376):
        # strength=0 in the public contract means "no effect", but the
        # img2img path routed every resolved reference through img2img
        # regardless of strength, computing diffusers strength=1.0 for a
        # strength=0 request. img2img still VAE-encodes the reference and
        # consumes the seeded generator's random draws to do it, so even
        # diffusers strength=1.0 is not guaranteed to reproduce what a plain
        # text2img call would have produced -- a zero-strength reference
        # must bypass img2img entirely, not just get the "weakest" img2img
        # setting.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, _character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Mina on the rooftop",
                        model_id=model_id,
                        references=[
                            ReferenceImageInput(
                                asset_id="asset_char_1", role="character", strength=0.0
                            )
                        ],
                        params={"steps": 10, "width": 64, "height": 64},
                    )
                )
                runtime = service.get_runtime(model_id, "image", "text-to-image")
                text2img_pipeline = runtime["pipeline"]
                img2img_pipeline = runtime["img2img_pipeline"]

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(img2img_pipeline.calls), 0)  # img2img never called
            self.assertEqual(text2img_pipeline.steps_invoked, 10)
            self.assertFalse(result.metadata["reference_conditioning_applied"])
            self.assertIsNone(result.metadata["reference_applied_asset_id"])
            # Still reported as considered, for audit purposes, even though
            # it was correctly never applied.
            self.assertEqual(len(result.metadata["considered_references"]), 1)
            self.assertEqual(
                result.metadata["considered_references"][0]["asset_id"], "asset_char_1"
            )

    def test_image_generator_rejects_combined_top_level_and_bible_references(
        self,
    ) -> None:
        # Regression (#201 follow-up, third Codex round on PR #376): a
        # non-empty `request.references` used to replace
        # `resolved_prompt.resolved_references` outright, so a request
        # carrying both a top-level reference and a Bible axis that resolves
        # its own reference silently dropped the Bible one from pixel
        # conditioning -- while it still showed up in prompt composition and
        # `resolved_references` metadata as if it had been honored, and
        # bypassed the "exactly one reference" limit below entirely. Both
        # sources are now merged before that limit is enforced.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            second_reference_path = root / "second_reference.png"
            Image.new("RGB", (16, 16), color=(10, 20, 30)).save(second_reference_path)
            composer.asset_repository.create_or_update(
                Asset(
                    id="asset_location_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="second reference fixture",
                    prompt="a second reference image",
                    model_id="sdxl",
                    path=str(second_reference_path),
                )
            )
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            # Distinct role from the Bible entry's "character"
                            # so validate_reference_inputs' per-role count
                            # limit does not itself reject this first --
                            # this test isolates the "exactly one reference
                            # total" check in _resolve_references_for_conditioning.
                            references=[
                                ReferenceImageInput(
                                    asset_id="asset_location_1",
                                    role="location",
                                    strength=0.5,
                                )
                            ],
                            params={
                                "steps": 10,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_excludes_a_zero_strength_reference_from_the_combined_limit(
        self,
    ) -> None:
        # Regression (#201 follow-up, fourteenth Codex round on PR #376,
        # confirmed product decision): strength=0 means "no effect" in the
        # public contract, so it must not count toward the "exactly one
        # reference total" limit the test above exercises, and must not be
        # selected as the primary conditioning reference -- unlike that
        # test (both references nonzero), this combination is one this
        # conditioning path can actually honor: the zero-strength "location"
        # reference never reaches img2img, only the nonzero Bible-derived
        # "character" one does.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            second_reference_path = root / "second_reference.png"
            Image.new("RGB", (16, 16), color=(10, 20, 30)).save(second_reference_path)
            composer.asset_repository.create_or_update(
                Asset(
                    id="asset_location_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="second reference fixture",
                    prompt="a second reference image",
                    model_id="sdxl",
                    path=str(second_reference_path),
                )
            )
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Mina on the rooftop",
                        model_id=model_id,
                        references=[
                            ReferenceImageInput(
                                asset_id="asset_location_1",
                                role="location",
                                strength=0.0,
                            )
                        ],
                        params={
                            "steps": 10,
                            "width": 64,
                            "height": 64,
                            "bible_refs": [character_id],
                        },
                    )
                )
                runtime = service.get_runtime(model_id, "image", "text-to-image")
                img2img_pipeline = runtime["img2img_pipeline"]

            self.assertEqual(result.status, "succeeded")
            # Only the nonzero (Bible-derived) reference is applied.
            self.assertEqual(len(img2img_pipeline.calls), 1)
            self.assertTrue(result.metadata["reference_conditioning_applied"])
            self.assertEqual(result.metadata["reference_applied_asset_id"], "asset_char_1")
            # The zero-strength reference is still reported for audit, not
            # silently dropped, and did not block the request as a second
            # conditioning image.
            considered_asset_ids = {
                reference["asset_id"]
                for reference in result.metadata["considered_references"]
            }
            self.assertEqual(considered_asset_ids, {"asset_char_1", "asset_location_1"})

    def test_image_generator_rejects_a_reference_asset_outside_the_context_project(
        self,
    ) -> None:
        # Regression (#201 follow-up, seventh Codex round on PR #376, P1):
        # JobService.create_job()'s project-boundary check resolves a Bible
        # entry's reference_asset_ids only once, at job creation. If
        # PATCH /bible/{entry_id} changes those ids to point at a different
        # project's asset before the queued job executes, the generator
        # would load that new asset without ever re-checking the boundary --
        # JobRunner threads the job's project_id through GenerationContext
        # precisely so the generator can re-check here, at the point the
        # asset is actually resolved and used, regardless of what changed
        # between creation and execution.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            bible_repository = BibleRepository(root / "bible")
            asset_repository = AssetRepository(root / "assets")
            reference_image_path = root / "reference.png"
            Image.new("RGB", (16, 16), color=(9, 9, 9)).save(reference_image_path)
            asset_repository.create_or_update(
                Asset(
                    id="asset_char_1",
                    job_id="job_fixture",
                    project_id="project-a",
                    media_type="image",
                    kind="output",
                    title="reference fixture",
                    prompt="a reference image",
                    model_id="sdxl",
                    path=str(reference_image_path),
                )
            )
            character = bible_repository.create(
                kind="character", name="Mina", reference_asset_ids=["asset_char_1"]
            )
            composer = PromptComposer(bible_repository, asset_repository)
            output_dir = root / "outputs"
            # Simulates JobRunner's context for a job that was created
            # (and had its Bible reference validated) against project-b --
            # the asset above now belongs to project-a instead.
            context = GenerationContext(is_cancelled=lambda: False, project_id="project-b")

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(MissingReferenceAssetError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 10,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character.id],
                            },
                        ),
                        context,
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_reports_progress_using_the_effective_img2img_step_count(
        self,
    ) -> None:
        # Regression (#201 follow-up, third Codex round on PR #376): real
        # diffusers img2img only ever runs int(num_inference_steps *
        # strength) denoising steps, not the full requested count. The step
        # callback's progress fraction used the raw requested count as its
        # denominator, so it topped out around `strength` (here 0.4, well
        # under 1.0) instead of reaching 1.0 by the last step diffusers
        # actually runs.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"
            reported_progress: list[float] = []
            context = GenerationContext(
                is_cancelled=lambda: False,
                on_progress=reported_progress.append,
                min_interval_seconds=0.0,
                min_progress_delta=0.0,
            )

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable_step_aware,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Mina on the rooftop",
                        model_id=model_id,
                        params={
                            "steps": 10,
                            "width": 64,
                            "height": 64,
                            "bible_refs": [character_id],
                        },
                    ),
                    context,
                )
                img2img_pipeline = service.get_runtime(
                    model_id, "image", "text-to-image"
                )["img2img_pipeline"]

            # DEFAULT_REFERENCE_STRENGTH=0.6 inverts to diffusers strength
            # 0.4; with 10 requested steps that is int(10 * 0.4) == 4 actual
            # steps -- the fake pipeline (mirroring real diffusers) only
            # invokes the callback that many times.
            self.assertEqual(img2img_pipeline.steps_invoked, 4)
            self.assertTrue(reported_progress)
            self.assertAlmostEqual(reported_progress[-1], 1.0)

    def test_image_generator_rejects_a_lock_strength_too_strong_for_the_step_count(
        self,
    ) -> None:
        # Regression (#201 follow-up, second AND seventh Codex rounds on PR
        # #376): the second round fixed a fixed 0.01 diffusers-strength
        # floor computing int(30 * 0.01) == 0 -- zero denoising steps -- by
        # deriving a floor from num_inference_steps instead. The seventh
        # round found that floor itself was wrong: silently substituting a
        # much weaker diffusers strength (up to a fully-noised 1.0) than
        # what a strong public lock actually requested, while still
        # reporting the lock as applied. The public contract's strength=1.0
        # ("follows the reference image most closely") inverts to diffusers
        # strength 0.0, which can never leave a surviving denoising step at
        # any step count (int(steps * 0) == 0 always) -- so this combination
        # must now be rejected outright rather than silently weakened.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            asset_repository = AssetRepository(root / "assets")
            reference_image_path = root / "reference.png"
            Image.new("RGB", (16, 16), color=(9, 9, 9)).save(reference_image_path)
            asset_repository.create_or_update(
                Asset(
                    id="asset_char_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="reference fixture",
                    prompt="a reference image",
                    model_id="sdxl",
                    path=str(reference_image_path),
                )
            )
            composer = PromptComposer(BibleRepository(root / "bible"), asset_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                num_inference_steps = 30
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            references=[
                                ReferenceImageInput(
                                    asset_id="asset_char_1", role="character", strength=1.0
                                )
                            ],
                            params={
                                "steps": num_inference_steps,
                                "width": 64,
                                "height": 64,
                            },
                        )
                    )
                img2img_pipeline = service.get_runtime(model_id, "image", "text-to-image")[
                    "img2img_pipeline"
                ]

            # Rejected before the pipeline is ever called -- not silently
            # weakened to some other diffusers strength.
            self.assertEqual(img2img_pipeline.calls, [])

    def test_image_generator_honors_a_strong_lock_the_step_count_can_represent(
        self,
    ) -> None:
        # Companion to the rejection test above: a strong (but not literally
        # maximal) lock that a given step count *can* represent must still
        # succeed and actually reach the requested strength, proving the new
        # reject-when-infeasible check isn't rejecting strong locks broadly
        # -- only combinations that are genuinely impossible to honor.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            asset_repository = AssetRepository(root / "assets")
            reference_image_path = root / "reference.png"
            Image.new("RGB", (16, 16), color=(9, 9, 9)).save(reference_image_path)
            asset_repository.create_or_update(
                Asset(
                    id="asset_char_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="reference fixture",
                    prompt="a reference image",
                    model_id="sdxl",
                    path=str(reference_image_path),
                )
            )
            composer = PromptComposer(BibleRepository(root / "bible"), asset_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                num_inference_steps = 30
                # natural_strength = 1.0 - 0.95 = 0.05; int(30 * 0.05) == 1,
                # so this is right at the edge of what 30 steps can honor.
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Mina on the rooftop",
                        model_id=model_id,
                        references=[
                            ReferenceImageInput(
                                asset_id="asset_char_1", role="character", strength=0.95
                            )
                        ],
                        params={
                            "steps": num_inference_steps,
                            "width": 64,
                            "height": 64,
                        },
                    )
                )
                img2img_pipeline = service.get_runtime(model_id, "image", "text-to-image")[
                    "img2img_pipeline"
                ]

            self.assertEqual(result.status, "succeeded")
            self.assertAlmostEqual(img2img_pipeline.calls[0]["strength"], 0.05)

    def test_image_generator_skips_conditioning_kwargs_without_a_reference(self) -> None:
        # Same reference-capable manifest/runtime, but no reference
        # requested: the img2img pipeline must never even be invoked, and
        # the existing text2img path must stay unchanged.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            output_dir = root / "outputs"
            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(service, output_dir=output_dir)
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="No reference here",
                        model_id=model_id,
                        params={"steps": 1, "width": 64, "height": 64},
                    )
                )
                runtime = service.get_runtime(model_id, "image", "text-to-image")

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(runtime["img2img_pipeline"].calls, [])
            self.assertEqual(runtime["pipeline"].steps_invoked, 1)
            self.assertFalse(result.metadata["reference_conditioning_applied"])
            self.assertIsNone(result.metadata["reference_applied_asset_id"])

    def test_image_generator_rejects_a_reference_when_the_manifest_lacks_capability(
        self,
    ) -> None:
        # Regression (#201 follow-up, P2): the real "ssd-1b" manifest
        # declares no reference_capability at all ("sdxl" now does -- #201
        # second follow-up -- so this uses the shipped manifest that still
        # doesn't). Before this fix, a Bible-derived reference against it was
        # checked only by physically probing the pipeline signature --
        # JobService's validate_reference_inputs() never ran for the Bible
        # path. Now the same manifest-level contract applies to both paths.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service()  # real "ssd-1b": no reference_capability
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedReferenceError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id="ssd-1b",
                            params={
                                "steps": 1,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_a_reference_the_runtime_cannot_honor(self) -> None:
        # Regression (#201 acceptance criterion): "Unsupported runtime
        # capabilities fail before an invalid model call" -- a manifest can
        # advertise reference_capability while the *loaded runtime* still
        # has no working img2img path (e.g. a loader that has not been
        # updated); that mismatch must raise, not silently drop the
        # reference or call the wrong pipeline.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capability_without_img2img_runtime,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 1,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_prefers_request_references_over_bible_references(
        self,
    ) -> None:
        # Regression (#201 follow-up, P2): GenerationRequest.references is
        # the documented top-level field JobService already validates
        # against manifest.reference_capability before a job is created.
        # Before this fix, ImageGenerator only ever looked at Bible-derived
        # resolved_references, so a caller using the documented field got
        # silent no-op conditioning despite passing validation upstream.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            asset_repository = AssetRepository(root / "assets")
            reference_image_path = root / "reference.png"
            Image.new("RGB", (16, 16), color=(10, 20, 30)).save(reference_image_path)
            asset_repository.create_or_update(
                Asset(
                    id="asset_direct_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="direct reference fixture",
                    prompt="a reference image",
                    model_id="sdxl",
                    path=str(reference_image_path),
                )
            )
            # A composer configured with only an asset repository (no Bible
            # entries at all) proves the top-level path does not depend on
            # Bible resolution to supply the asset lookup.
            composer = PromptComposer(BibleRepository(root / "bible"), asset_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                result = generator.run(
                    GenerationRequest(
                        media_type="image",
                        prompt="Direct reference, no Bible entry",
                        model_id=model_id,
                        references=[
                            ReferenceImageInput(
                                asset_id="asset_direct_1", role="character", strength=0.5
                            )
                        ],
                        # >1 so natural_strength=0.5 stays representable
                        # (int(steps * 0.5) >= 1, see the reject-when-
                        # infeasible check in generate()) -- 1 step would
                        # make this combination itself get rejected, which
                        # is not what this test is checking.
                        params={"steps": 10, "width": 64, "height": 64},
                    )
                )
                img2img_pipeline = service.get_runtime(model_id, "image", "text-to-image")[
                    "img2img_pipeline"
                ]

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(img2img_pipeline.calls), 1)
            self.assertEqual(result.metadata["reference_applied_asset_id"], "asset_direct_1")

    def test_image_generator_rejects_more_than_one_considered_reference(self) -> None:
        # Regression (#201 follow-up, P2): resolving both a character and a
        # location reference must not silently apply only the first while
        # reporting every resolved reference as though it were honored.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            bible_repository = BibleRepository(root / "bible")
            asset_repository = AssetRepository(root / "assets")
            for asset_id in ("asset_char_1", "asset_loc_1"):
                image_path = root / f"{asset_id}.png"
                Image.new("RGB", (16, 16), color=(1, 2, 3)).save(image_path)
                asset_repository.create_or_update(
                    Asset(
                        id=asset_id,
                        job_id="job_fixture",
                        project_id=None,
                        media_type="image",
                        kind="output",
                        title="reference fixture",
                        prompt="a reference image",
                        model_id="sdxl",
                        path=str(image_path),
                    )
                )
            character = bible_repository.create(
                kind="character", name="Mina", reference_asset_ids=["asset_char_1"]
            )
            location = bible_repository.create(
                kind="location", name="Rooftop", reference_asset_ids=["asset_loc_1"]
            )
            composer = PromptComposer(bible_repository, asset_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 1,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character.id, location.id],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_fails_when_a_resolved_reference_asset_is_gone(self) -> None:
        # Regression (#201 follow-up, P2): a Bible reference that resolved
        # to an asset id which then can't be looked up (deleted between
        # composition and this lookup, or no asset repository configured at
        # all) must raise rather than silently generating without
        # conditioning as though nothing had been requested.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            bible_repository = BibleRepository(root / "bible")
            character = bible_repository.create(
                kind="character", name="Mina", reference_asset_ids=["asset_missing"]
            )
            # No AssetRepository at all: resolved_references is still
            # populated (PromptComposer's own documented behavior when it
            # has no asset store to validate against), but nothing can
            # resolve the asset id to a path.
            composer = PromptComposer(bible_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(MissingReferenceAssetError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 1,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character.id],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_a_reference_when_manifest_lacks_img2img_mode(
        self,
    ) -> None:
        # Regression (#201 follow-up, second Codex round on PR #376):
        # validate_reference_inputs() only checks that *some* mode is
        # declared (capability.enabled), not that it is specifically
        # img2img -- the only mode this conditioning path implements. A
        # manifest advertising e.g. only ip_adapter must not be routed
        # through the img2img pipeline anyway.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(
                manifest_root,
                reference_capability={
                    "supported_modes": ["ip_adapter"],
                    "supported_roles": ["character", "location"],
                    "min_strength": 0.0,
                    "max_strength": 1.0,
                    "max_references_per_role": 1,
                },
            )
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 1,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_unimplemented_reference_preprocessing(self) -> None:
        # Regression (#201 follow-up, second Codex round on PR #376): a
        # manifest can advertise support for face_crop/canny/depth
        # preprocessing, and validate_reference_inputs() accepts a request
        # for it, but this path only ever performs a plain resize --
        # silently ignoring the requested transform would report
        # conditioning as applied while quietly skipping part of what was
        # asked for.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(
                manifest_root,
                reference_capability={
                    "supported_modes": ["img2img"],
                    "supported_roles": ["character", "location"],
                    "supported_preprocessing": ["none", "auto", "face_crop"],
                    "min_strength": 0.0,
                    "max_strength": 1.0,
                    "max_references_per_role": 1,
                },
            )
            bible_repository = BibleRepository(root / "bible")
            asset_repository = AssetRepository(root / "assets")
            reference_image_path = root / "reference.png"
            Image.new("RGB", (16, 16), color=(1, 2, 3)).save(reference_image_path)
            asset_repository.create_or_update(
                Asset(
                    id="asset_char_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="image",
                    kind="output",
                    title="reference fixture",
                    prompt="a reference image",
                    model_id="sdxl",
                    path=str(reference_image_path),
                )
            )
            # No Bible entry needed -- this test conditions via
            # request.references directly, not a bible_refs axis.
            composer = PromptComposer(bible_repository, asset_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                request = GenerationRequest(
                    media_type="image",
                    prompt="Mina on the rooftop",
                    model_id=model_id,
                    references=[
                        ReferenceImageInput(
                            asset_id="asset_char_1", role="character", preprocessing="face_crop"
                        )
                    ],
                    params={"steps": 1, "width": 64, "height": 64},
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(request)

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_denoising_start_for_a_reference_job(self) -> None:
        # Regression (#201 follow-up, fifth Codex round on PR #376, P2): in
        # the required diffusers 0.37.x img2img pipeline, get_timesteps()
        # ignores the computed `strength` entirely whenever `denoising_start`
        # is also set -- so a reference's public lock strength would be
        # silently discarded rather than honored, letting requests with
        # different reference strengths follow the identical conditioning
        # path. This parameter must be rejected outright for a reference
        # job rather than silently forwarded alongside strength.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 10,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                                "denoising_start": 0.3,
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_denoising_end_for_a_reference_job(self) -> None:
        # Regression (#201 follow-up, sixth Codex round on PR #376, P2):
        # denoising_end applies its own cutoff *after* diffusers has already
        # selected the timestep range from the computed `strength`, which can
        # leave zero denoising steps or run fewer steps than the progress
        # callback's denominator expects -- a noisy/incorrect result and
        # incomplete progress reporting, exactly like denoising_start's
        # conflict with strength above.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 10,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                                "denoising_end": 0.7,
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_custom_timesteps_for_a_reference_job(self) -> None:
        # Regression (#201 follow-up, seventh Codex round on PR #376, P2):
        # a custom `timesteps` (or `sigmas`) schedule makes diffusers replace
        # num_inference_steps with the schedule's own length before applying
        # strength at all -- so the floor/progress denominator computed from
        # the *requested* num_inference_steps would no longer match what
        # diffusers actually runs, exactly like denoising_start/denoising_end
        # above but one step further removed from the requested step count.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 10,
                                "width": 64,
                                "height": 64,
                                "bible_refs": [character_id],
                                "timesteps": [999, 500, 1],
                            },
                        )
                    )

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_a_non_image_direct_reference_asset(self) -> None:
        # Regression (#201 follow-up, second Codex round on PR #376):
        # PromptComposer._resolve_reference_asset() rejects a non-image
        # asset for Bible-derived references, but request.references skips
        # the composer entirely -- without the same check here, a
        # mislabeled audio/video asset id could be conditioned on directly.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            asset_repository = AssetRepository(root / "assets")
            asset_repository.create_or_update(
                Asset(
                    id="asset_audio_1",
                    job_id="job_fixture",
                    project_id=None,
                    media_type="audio",
                    kind="output",
                    title="not an image",
                    prompt="a narration clip",
                    model_id="kokoro",
                    path=str(root / "narration.wav"),
                )
            )
            composer = PromptComposer(BibleRepository(root / "bible"), asset_repository)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                request = GenerationRequest(
                    media_type="image",
                    prompt="Mina on the rooftop",
                    model_id=model_id,
                    references=[
                        ReferenceImageInput(asset_id="asset_audio_1", role="character")
                    ],
                    params={"steps": 1, "width": 64, "height": 64},
                )
                with self.assertRaises(MissingReferenceAssetError):
                    generator.run(request)

            self.assertEqual(list(output_dir.glob("**/*")), [])

    def test_image_generator_rejects_oversized_dimensions_before_resizing_reference(
        self,
    ) -> None:
        # Regression (#201 follow-up, second Codex round on PR #376): the
        # reference image used to be opened and resized before
        # validate_capabilities() ever ran (that only happened later,
        # inside generate_image() in the per-variation loop) -- an absurd
        # width/height could make Pillow attempt a huge allocation before
        # the provider's own size bounds got a chance to reject it. The
        # error alone isn't proof of *when* it happened (generate_image()
        # would eventually raise the same error after paying for the
        # resize), so this also asserts on wall-clock time: resizing to
        # 100000x100000 first measurably takes tens of seconds.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_id = self._write_reference_capable_manifest(manifest_root)
            composer, character_id = self._prepare_character_reference(root)
            output_dir = root / "outputs"

            with patch(
                "core.models.loader.DiffusersImageLoader.load",
                new=_fake_diffusers_load_reference_capable,
            ):
                service = create_default_model_service(manifest_root=manifest_root)
                generator = ImageGenerator(
                    service, output_dir=output_dir, prompt_composer=composer
                )
                started_at = time.monotonic()
                with self.assertRaises(UnsupportedImageParameterError):
                    generator.run(
                        GenerationRequest(
                            media_type="image",
                            prompt="Mina on the rooftop",
                            model_id=model_id,
                            params={
                                "steps": 1,
                                # Local diffusers capabilities cap at 2048px
                                # (generators/image/providers.py); this must
                                # be rejected, not attempted.
                                "width": 100000,
                                "height": 100000,
                                "bible_refs": [character_id],
                            },
                        )
                    )
                elapsed = time.monotonic() - started_at

            self.assertEqual(list(output_dir.glob("**/*")), [])
            self.assertLess(elapsed, 5.0)

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
            self.assertEqual(result.metadata["params"]["postprocess"], True)
            self.assertEqual(result.metadata["audio_postprocess"]["preset"], "music")
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_audio_generator_applies_shared_music_postprocessing_by_default(self) -> None:
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

                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="warm pad swell",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={"duration_seconds": 2},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            audio_postprocess = result.metadata["audio_postprocess"]
            self.assertEqual(audio_postprocess["preset"], "music")
            self.assertTrue(audio_postprocess["enabled"])
            self.assertEqual(
                audio_postprocess["chain"],
                ["normalize_rms", "apply_fades", "normalize_peak"],
            )
            self.assertEqual(result.metadata["params"]["postprocess"], True)

    def test_audio_generator_can_disable_music_postprocessing(self) -> None:
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

                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="warm pad swell",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={"duration_seconds": 2, "postprocess": False},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            audio_postprocess = result.metadata["audio_postprocess"]
            self.assertEqual(audio_postprocess["preset"], "music")
            self.assertFalse(audio_postprocess["enabled"])
            self.assertEqual(audio_postprocess["chain"], [])
            self.assertEqual(result.metadata["params"]["postprocess"], False)
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_audio_generator_rejects_non_boolean_postprocess(self) -> None:
        generator = AudioGenerator(create_default_model_service())

        with self.assertRaisesRegex(ValueError, "postprocess"):
            generator.validate_request(
                GenerationRequest(
                    media_type="audio",
                    prompt="minimal piano cue",
                    model_id="musicgen-small",
                    params={"postprocess": "false"},
                )
            )

    def test_audio_generator_rejects_null_postprocess_at_validation_time(self) -> None:
        # An explicit JSON null is a present key with value None, distinct from
        # the key being absent; it must fail validate_request (→ 422) rather
        # than surviving the effective-params merge into a job that fails
        # later during generation.
        generator = AudioGenerator(create_default_model_service())

        with self.assertRaisesRegex(ValueError, "postprocess"):
            generator.validate_request(
                GenerationRequest(
                    media_type="audio",
                    prompt="minimal piano cue",
                    model_id="musicgen-small",
                    params={"postprocess": None},
                )
            )

    def test_audio_generator_preserves_stereo_channels_during_postprocessing(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with (
                patch(
                    "core.models.loader.TransformersMusicgenLoader.load",
                    new=_fake_stereo_musicgen_load,
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                service = create_default_model_service()
                generator = AudioGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="stereo pad",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={"duration_seconds": 2},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["channels"], 2)
            output_path = Path(result.outputs[0])
            with wave.open(str(output_path), "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 2)
                frame_count = wav_file.getnframes()
                frames = wav_file.readframes(frame_count)

            # A channel-flattening bug would report 128000 frames of mono
            # audio (the two channels concatenated) instead of 64000 stereo
            # frames.
            self.assertEqual(frame_count, 64000)
            samples = np.frombuffer(frames, dtype="<i2").reshape(-1, 2)
            # Sample a frame from the middle, well clear of both the
            # fade_in(0.05s) and fade_out(0.5s) ramps, where levels are flat.
            middle = samples[frame_count // 2]
            self.assertGreater(int(middle[0]), 0)
            self.assertGreater(int(middle[1]), 0)
            # Independent per-channel gain would pull both channels to the
            # same peak target, collapsing this to ~1:1; linked gain keeps
            # the original 0.8:0.1 = 8:1 ratio between the channels.
            ratio = middle[0] / middle[1]
            self.assertGreater(ratio, 6.0)
            self.assertLess(ratio, 10.0)

    def test_audio_generator_postprocesses_bfloat16_output(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with (
                patch(
                    "core.models.loader.TransformersMusicgenLoader.load",
                    new=_fake_bfloat16_musicgen_load,
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                service = create_default_model_service()
                generator = AudioGenerator(service, output_dir=output_dir)

                # bfloat16 has no NumPy equivalent; this must not raise
                # TypeError: Got unsupported ScalarType BFloat16.
                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="bfloat16 pad",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={"duration_seconds": 2},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            self.assertTrue(result.metadata["audio_postprocess"]["enabled"])
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_audio_generator_does_not_amplify_near_silent_output_to_full_scale(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with (
                patch(
                    "core.models.loader.TransformersMusicgenLoader.load",
                    new=_fake_near_silent_musicgen_load,
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                service = create_default_model_service()
                generator = AudioGenerator(service, output_dir=output_dir)

                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="near silent output",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={"duration_seconds": 2},
                    )
                )

            self.assertEqual(result.status, "succeeded")
            rms_step, _fade_step, peak_step = result.metadata["audio_postprocess"][
                "steps"
            ]
            self.assertTrue(rms_step["gain_capped"])
            # Without the fix, the peak stage would unconditionally scale this
            # up to the -1 dB target, turning 2e-6 model noise into
            # near-full-scale audio.
            self.assertFalse(peak_step["applied"])

            output_path = Path(result.outputs[0])
            with wave.open(str(output_path), "rb") as wav_file:
                frames = wav_file.readframes(wav_file.getnframes())
            samples = np.frombuffer(frames, dtype="<i2")
            self.assertLess(int(np.max(np.abs(samples))), 100)

    def test_audio_generator_rejects_non_finite_model_output(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            with (
                patch(
                    "core.models.loader.TransformersMusicgenLoader.load",
                    new=_fake_non_finite_musicgen_load,
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                service = create_default_model_service()
                generator = AudioGenerator(service, output_dir=output_dir)

                with self.assertRaisesRegex(RuntimeError, "non-finite"):
                    generator.run(
                        GenerationRequest(
                            media_type="audio",
                            prompt="corrupt output",
                            model_id="musicgen-small",
                            output_format="wav",
                            params={"duration_seconds": 2},
                        )
                    )

            # No WAV should have been written for the rejected generation.
            self.assertEqual(list(output_dir.glob("*.wav")), [])

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

    def test_procedural_video_generator_honors_cancellation_before_rendering(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "outputs"
            service = create_default_model_service()
            generator = VideoGenerator(service, output_dir=output_dir)
            context = GenerationContext(is_cancelled=lambda: True)

            with self.assertRaises(GenerationCancelled):
                generator.run(
                    GenerationRequest(
                        media_type="video",
                        prompt="procedural storyboard",
                        model_id="storyboard-video",
                        output_format="gif",
                        params={"duration_seconds": 2, "fps": 6},
                    ),
                    context,
                )

            self.assertEqual(list(output_dir.glob("**/*.gif")), [])

    def test_learned_video_generator_honors_cancellation_before_inference(self) -> None:
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
            context = GenerationContext(is_cancelled=lambda: True)

            with self.assertRaises(GenerationCancelled):
                generator.run(
                    GenerationRequest(
                        media_type="video",
                        prompt="learned runtime smoke",
                        model_id="learned-video",
                        output_format="mp4",
                        params={"duration_seconds": 1},
                    ),
                    context,
                )

            self.assertFalse((runtime_root / "learned-output.mp4").exists())

    def _write_step_aware_learned_video_manifest(
        self, manifest_root: Path, runtime_root: Path
    ) -> tuple[Path, Path]:
        """A fake CogVideoX-shaped adapter that simulates 5 denoising steps.

        Mirrors the real adapter's contract (models/video/learned-runtime/
        runtime.py): it pops `raise_if_cancelled` from kwargs and calls it
        once per step, exactly like the real adapter's callback_on_step_end
        does -- so this proves the generator -> adapter wiring (#209) without
        needing torch/diffusers/real weights.
        """

        runtime_root.mkdir(parents=True)
        output_path = runtime_root / "learned-output.mp4"
        progress_path = runtime_root / "steps-completed.txt"
        (runtime_root / "runtime.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "",
                    "def load_runtime(manifest):",
                    "    output_path = Path(__file__).with_name('learned-output.mp4')",
                    "    progress_path = Path(__file__).with_name('steps-completed.txt')",
                    "    def renderer(**kwargs):",
                    "        raise_if_cancelled = kwargs.pop('raise_if_cancelled', None)",
                    "        for step_index in range(5):",
                    "            if raise_if_cancelled is not None:",
                    "                raise_if_cancelled()",
                    "            progress_path.write_text(str(step_index + 1))",
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
        return output_path, progress_path

    def test_learned_video_generator_stops_mid_inference_when_cancelled_via_step_callback(
        self,
    ) -> None:
        # Regression (#209): cancellation must reach the adapter's own
        # per-step callback, not just the boundary check before/after the
        # whole render call -- a cancel request mid-run should stop a
        # multi-step CogVideoX-shaped generation before its final step.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            runtime_root = root / "runtime" / "learned-video"
            output_path, progress_path = self._write_step_aware_learned_video_manifest(
                manifest_root, runtime_root
            )
            service = create_default_model_service(manifest_root=manifest_root)
            generator = VideoGenerator(service, output_dir=root / "outputs" / "videos")

            call_count = {"n": 0}

            def is_cancelled() -> bool:
                call_count["n"] += 1
                # Two boundary checks (VideoGenerator.generate() and
                # LearnedVideoRuntime.render()) run before the adapter's own
                # step loop starts, so this must clear those first before
                # letting a couple of in-loop step checks pass too.
                return call_count["n"] > 4

            context = GenerationContext(is_cancelled=is_cancelled)

            with self.assertRaises(GenerationCancelled):
                generator.run(
                    GenerationRequest(
                        media_type="video",
                        prompt="learned runtime smoke",
                        model_id="learned-video",
                        output_format="mp4",
                        params={"duration_seconds": 1},
                    ),
                    context,
                )

            self.assertTrue(progress_path.exists())
            self.assertLess(int(progress_path.read_text()), 5)
            self.assertFalse(output_path.exists())  # never reached the final write

    def test_learned_video_generator_completes_all_steps_when_not_cancelled(self) -> None:
        # Same step-aware adapter, but with cancellation never requested:
        # normal generation must run every step and succeed unchanged.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            runtime_root = root / "runtime" / "learned-video"
            output_path, progress_path = self._write_step_aware_learned_video_manifest(
                manifest_root, runtime_root
            )
            service = create_default_model_service(manifest_root=manifest_root)
            generator = VideoGenerator(service, output_dir=root / "outputs" / "videos")
            context = GenerationContext(is_cancelled=lambda: False)

            result = generator.run(
                GenerationRequest(
                    media_type="video",
                    prompt="learned runtime smoke",
                    model_id="learned-video",
                    output_format="mp4",
                    params={"duration_seconds": 1},
                ),
                context,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(progress_path.read_text(), "5")
            self.assertTrue(output_path.exists())

    def test_learned_video_generator_tolerates_a_fixed_signature_renderer(self) -> None:
        # Regression (#209 follow-up, Codex review on PR #375): a renderer
        # with a fixed keyword-only signature (no **kwargs, no
        # raise_if_cancelled parameter) must not be handed that opt-in
        # kwarg -- doing so raises TypeError: unexpected keyword argument
        # for any adapter that was never updated to accept it. The
        # cancellation boundary check must still run either way.
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
                        "    def renderer(",
                        "        *, prompt, negative_prompt, seed, output_dir,",
                        "        output_format, entrypoint=None,",
                        "    ):",
                        "        out_dir = Path(output_dir)",
                        "        out_dir.mkdir(parents=True, exist_ok=True)",
                        "        output_path = out_dir / 'learned-output.mp4'",
                        "        output_path.write_bytes(b'0' * 131072)",
                        "        return {",
                        "            'output_path': str(output_path),",
                        "            'output_format': 'mp4',",
                        "            'metadata': {'adapter_contract': 'fixed-signature'},",
                        "        }",
                        "    return {",
                        "        'runtime_adapter': 'learned_text_to_video',",
                        "        'renderer': renderer,",
                        "    }",
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
            # A non-null context with is_cancelled always False: this proves
            # the fixed-signature renderer is tolerated when cancellation is
            # in play at all, not just when context is None entirely.
            context = GenerationContext(is_cancelled=lambda: False)

            result = generator.run(
                GenerationRequest(
                    media_type="video",
                    prompt="fixed-signature adapter smoke",
                    model_id="learned-video",
                    output_format="mp4",
                ),
                context,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.metadata["adapter_contract"], "fixed-signature")


if __name__ == "__main__":
    unittest.main()
