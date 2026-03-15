"""Loader abstractions for model runtimes."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
from pathlib import Path
from typing import Any

from .manifest import ModelManifest

_REPO_ROOT = Path(__file__).resolve().parents[2]


class BaseModelLoader(ABC):
    """Contract for turning a manifest into a runtime object."""

    @abstractmethod
    def load(self, manifest: ModelManifest) -> Any:
        """Instantiate a runtime object for the given manifest."""


class DiffusersImageLoader(BaseModelLoader):
    """Load a local diffusers image pipeline from manifest metadata."""

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        try:
            import torch
            from diffusers import StableDiffusionXLPipeline
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Diffusers runtime dependencies are missing. "
                "Install torch, diffusers, transformers, accelerate, and safetensors."
            ) from exc

        local_path = self._resolve_local_path(manifest)
        device = self._resolve_device(torch)
        requested_dtype = self._resolve_weight_dtype(manifest, torch)
        runtime_dtype = self._resolve_runtime_dtype(requested_dtype, device, torch)
        load_dtype = self._resolve_load_dtype(requested_dtype, runtime_dtype)
        variant = self._resolve_variant(manifest)

        pipeline = StableDiffusionXLPipeline.from_pretrained(
            local_path,
            torch_dtype=load_dtype,
            variant=variant,
            use_safetensors=True,
            local_files_only=True,
        )
        pipeline.set_progress_bar_config(disable=True)
        pipeline.to(device=device, dtype=runtime_dtype)
        pipeline.enable_attention_slicing()
        if hasattr(pipeline, "vae") and hasattr(pipeline.vae, "enable_slicing"):
            pipeline.vae.enable_slicing()
        elif hasattr(pipeline, "enable_vae_slicing"):
            pipeline.enable_vae_slicing()

        return {
            "stub": False,
            "loader": self.__class__.__name__,
            "manifest_id": manifest.id,
            "display_name": manifest.display_name,
            "runtime": manifest.runtime,
            "provider": manifest.provider,
            "local_path": local_path,
            "remote_ref": manifest.remote_ref,
            "dtype": manifest.dtype,
            "load_dtype": str(load_dtype).split(".")[-1],
            "torch_dtype": str(runtime_dtype).split(".")[-1],
            "weight_dtype": str(requested_dtype).split(".")[-1],
            "variant": variant,
            "device": device,
            "default_params": dict(manifest.default_params),
            "path_exists": True,
            "pipeline": pipeline,
        }

    def _resolve_local_path(self, manifest: ModelManifest) -> str:
        if not manifest.local_path:
            raise ValueError(f"Manifest {manifest.id!r} is missing local_path.")

        local_path = (_REPO_ROOT / manifest.local_path).resolve()
        if not local_path.exists():
            raise FileNotFoundError(
                f"Model path does not exist for manifest {manifest.id!r}: {local_path}"
            )
        if not (local_path / "model_index.json").exists():
            raise FileNotFoundError(
                f"Diffusers model_index.json was not found under: {local_path}"
            )
        return str(local_path)

    def _resolve_device(self, torch: Any) -> str:
        requested_device = os.getenv("DEVICE", "auto").strip().lower()
        if requested_device and requested_device != "auto":
            return requested_device
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _resolve_weight_dtype(self, manifest: ModelManifest, torch: Any) -> Any:
        raw_dtype = (manifest.dtype or "").strip().lower()
        if raw_dtype in {"float16", "fp16", "half"}:
            return torch.float16
        if raw_dtype in {"bfloat16", "bf16"}:
            return torch.bfloat16
        if raw_dtype in {"float32", "fp32"}:
            return torch.float32
        return torch.float16

    def _resolve_runtime_dtype(
        self,
        requested_dtype: Any,
        device: str,
        torch: Any,
    ) -> Any:
        if device in {"cpu", "mps"} and requested_dtype == torch.float16:
            return torch.float32
        return requested_dtype

    def _resolve_load_dtype(self, requested_dtype: Any, runtime_dtype: Any) -> Any:
        if runtime_dtype != requested_dtype:
            return runtime_dtype
        return requested_dtype

    def _resolve_variant(self, manifest: ModelManifest) -> str | None:
        raw_dtype = (manifest.dtype or "").strip().lower()
        if raw_dtype in {"float16", "fp16", "half"}:
            return "fp16"
        if raw_dtype in {"bfloat16", "bf16"}:
            return "bf16"
        return None


class TransformersMusicgenLoader(BaseModelLoader):
    """Load a local MusicGen runtime from manifest metadata."""

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        try:
            import torch
            from transformers import AutoProcessor, MusicgenForConditionalGeneration
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Transformers audio runtime dependencies are missing. "
                "Install torch, transformers, accelerate, and safetensors."
            ) from exc

        local_path = self._resolve_local_path(manifest)
        device = self._resolve_device(torch)
        requested_dtype = self._resolve_weight_dtype(manifest, torch)
        runtime_dtype = self._resolve_runtime_dtype(requested_dtype, device, torch)
        load_dtype = self._resolve_load_dtype(requested_dtype, runtime_dtype)

        processor = AutoProcessor.from_pretrained(
            local_path,
            local_files_only=True,
        )
        model = MusicgenForConditionalGeneration.from_pretrained(
            local_path,
            torch_dtype=load_dtype,
            local_files_only=True,
        )
        model.to(device=device, dtype=runtime_dtype)
        model.eval()

        return {
            "stub": False,
            "loader": self.__class__.__name__,
            "manifest_id": manifest.id,
            "display_name": manifest.display_name,
            "runtime": manifest.runtime,
            "provider": manifest.provider,
            "local_path": local_path,
            "remote_ref": manifest.remote_ref,
            "dtype": manifest.dtype,
            "load_dtype": str(load_dtype).split(".")[-1],
            "torch_dtype": str(runtime_dtype).split(".")[-1],
            "weight_dtype": str(requested_dtype).split(".")[-1],
            "device": device,
            "default_params": dict(manifest.default_params),
            "path_exists": True,
            "processor": processor,
            "model": model,
            "sampling_rate": int(model.config.audio_encoder.sampling_rate),
            "frame_rate": int(model.config.audio_encoder.frame_rate),
        }

    def _resolve_local_path(self, manifest: ModelManifest) -> str:
        if not manifest.local_path:
            raise ValueError(f"Manifest {manifest.id!r} is missing local_path.")

        local_path = (_REPO_ROOT / manifest.local_path).resolve()
        if not local_path.exists():
            raise FileNotFoundError(
                f"Model path does not exist for manifest {manifest.id!r}: {local_path}"
            )
        if not (local_path / "config.json").exists():
            raise FileNotFoundError(
                f"Transformers config.json was not found under: {local_path}"
            )
        return str(local_path)

    def _resolve_device(self, torch: Any) -> str:
        requested_device = os.getenv("DEVICE", "auto").strip().lower()
        if requested_device and requested_device != "auto":
            return requested_device
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def _resolve_weight_dtype(self, manifest: ModelManifest, torch: Any) -> Any:
        raw_dtype = (manifest.dtype or "").strip().lower()
        if raw_dtype in {"float16", "fp16", "half"}:
            return torch.float16
        if raw_dtype in {"bfloat16", "bf16"}:
            return torch.bfloat16
        if raw_dtype in {"float32", "fp32"}:
            return torch.float32
        return torch.float32

    def _resolve_runtime_dtype(
        self,
        requested_dtype: Any,
        device: str,
        torch: Any,
    ) -> Any:
        if device == "cpu" and requested_dtype != torch.float32:
            return torch.float32
        return requested_dtype

    def _resolve_load_dtype(self, requested_dtype: Any, runtime_dtype: Any) -> Any:
        if runtime_dtype != requested_dtype:
            return runtime_dtype
        return requested_dtype


class ProceduralVideoLoader(BaseModelLoader):
    """Expose a lightweight local runtime for storyboard-style video output."""

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        local_path = self._resolve_local_path(manifest)
        return {
            "stub": False,
            "loader": self.__class__.__name__,
            "manifest_id": manifest.id,
            "display_name": manifest.display_name,
            "runtime": manifest.runtime,
            "provider": manifest.provider,
            "local_path": local_path,
            "remote_ref": manifest.remote_ref,
            "dtype": manifest.dtype,
            "device": "cpu",
            "default_params": dict(manifest.default_params),
            "path_exists": True,
            "palette": [
                "#111827",
                "#1d4ed8",
                "#f59e0b",
                "#fef3c7",
                "#0f766e",
            ],
        }

    def _resolve_local_path(self, manifest: ModelManifest) -> str:
        if not manifest.local_path:
            raise ValueError(f"Manifest {manifest.id!r} is missing local_path.")

        local_path = (_REPO_ROOT / manifest.local_path).resolve()
        if not local_path.exists():
            raise FileNotFoundError(
                f"Model path does not exist for manifest {manifest.id!r}: {local_path}"
            )
        return str(local_path)


class LoaderRegistry:
    """Lookup table for named loader instances."""

    def __init__(self) -> None:
        self._loaders: dict[str, BaseModelLoader] = {}

    def register(self, name: str, loader: BaseModelLoader) -> None:
        self._loaders[name] = loader

    def get(self, name: str) -> BaseModelLoader:
        try:
            return self._loaders[name]
        except KeyError as exc:
            raise LookupError(f"Unknown loader: {name}") from exc


def create_default_loader_registry() -> LoaderRegistry:
    """Create the minimal loader registry for local image and audio scope."""

    registry = LoaderRegistry()
    registry.register("diffusers_image_loader", DiffusersImageLoader())
    registry.register("transformers_musicgen_loader", TransformersMusicgenLoader())
    registry.register("procedural_video_loader", ProceduralVideoLoader())
    return registry


__all__ = [
    "BaseModelLoader",
    "DiffusersImageLoader",
    "LoaderRegistry",
    "ProceduralVideoLoader",
    "TransformersMusicgenLoader",
    "create_default_loader_registry",
]
