"""Loader abstractions for model runtimes."""

from __future__ import annotations

from abc import ABC, abstractmethod
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

from core.model_readiness import (
    WEIGHT_PATTERNS,
    audiocraft_model_readiness,
    missing_diffusers_files,
    missing_transformers_files,
)

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
        variant = self._resolve_variant(manifest, Path(local_path))

        pipeline = StableDiffusionXLPipeline.from_pretrained(
            local_path,
            torch_dtype=load_dtype,
            variant=variant,
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
        missing = missing_diffusers_files(local_path)
        if missing:
            raise FileNotFoundError(
                f"Diffusers model files are missing under {local_path}: " + ", ".join(missing)
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

    def _resolve_variant(
        self,
        manifest: ModelManifest,
        local_path: Path,
    ) -> str | None:
        raw_dtype = (manifest.dtype or "").strip().lower()
        if raw_dtype in {"float16", "fp16", "half"}:
            requested_variant = "fp16"
        elif raw_dtype in {"bfloat16", "bf16"}:
            requested_variant = "bf16"
        else:
            return None
        if any(
            f".{requested_variant}." in path.name
            or f".{requested_variant}-" in path.name
            for pattern in WEIGHT_PATTERNS
            for path in local_path.rglob(pattern)
        ):
            return requested_variant
        return None


class TransformersMusicgenLoader(BaseModelLoader):
    """Load a local MusicGen runtime from manifest metadata."""

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        try:
            import torch
            from transformers import AutoProcessor, MusicgenConfig, MusicgenForConditionalGeneration
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Transformers audio runtime dependencies are missing. "
                "Install torch, transformers, accelerate, and safetensors."
            ) from exc

        # transformers can expose the decoder config class on the combined
        # MusicGen model, which breaks loading full local checkpoints.
        self._normalize_musicgen_config_class(
            MusicgenForConditionalGeneration,
            MusicgenConfig,
        )

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

    def _normalize_musicgen_config_class(
        self,
        model_cls: Any,
        config_cls: Any,
    ) -> None:
        if getattr(model_cls, "config_class", None) is config_cls:
            return
        model_cls.config_class = config_cls

    def _resolve_local_path(self, manifest: ModelManifest) -> str:
        if not manifest.local_path:
            raise ValueError(f"Manifest {manifest.id!r} is missing local_path.")

        local_path = (_REPO_ROOT / manifest.local_path).resolve()
        if not local_path.exists():
            raise FileNotFoundError(
                f"Model path does not exist for manifest {manifest.id!r}: {local_path}"
            )
        missing = missing_transformers_files(local_path)
        if missing:
            raise FileNotFoundError(
                f"Transformers model files are missing under {local_path}: " + ", ".join(missing)
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


class TransformersMusicgenMelodyLoader(TransformersMusicgenLoader):
    """Load the Transformers MusicGen Melody model and its audio processor."""

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        try:
            import torch
            from transformers import (
                MusicgenMelodyConfig,
                MusicgenMelodyForConditionalGeneration,
                MusicgenMelodyProcessor,
            )
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Transformers MusicGen Melody dependencies are missing. "
                "Install torch, torchaudio, transformers, sentencepiece, accelerate, "
                "and safetensors."
            ) from exc

        self._normalize_musicgen_config_class(
            MusicgenMelodyForConditionalGeneration,
            MusicgenMelodyConfig,
        )

        local_path = self._resolve_local_path(manifest)
        device = self._resolve_device(torch)
        requested_dtype = self._resolve_weight_dtype(manifest, torch)
        runtime_dtype = self._resolve_runtime_dtype(requested_dtype, device, torch)
        load_dtype = self._resolve_load_dtype(requested_dtype, runtime_dtype)

        processor = MusicgenMelodyProcessor.from_pretrained(
            local_path,
            local_files_only=True,
        )
        model = MusicgenMelodyForConditionalGeneration.from_pretrained(
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


class AudioCraftMusicgenLoader(BaseModelLoader):
    """Load an AudioCraft MusicGen checkpoint from an offline local export."""

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        local_path = self._resolve_local_path(manifest)
        self._install_xformers_import_shim()
        # Set these before importing AudioCraft/Transformers because their
        # offline constants are initialized during module import.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            import torch
            from audiocraft.models import MusicGen, builders, loaders
            from audiocraft.modules.conditioners import T5Conditioner
            from omegaconf import OmegaConf
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard
            raise RuntimeError(
                "AudioCraft runtime dependency is missing. Install AudioCraft in "
                "a compatible Python environment."
            ) from exc

        device = self._resolve_device(torch)

        lm_package = loaders.load_lm_model_ckpt(local_path)
        lm_config = OmegaConf.create(lm_package["xp.cfg"])
        lm_config.device = device
        lm_config.dtype = "float16" if device == "cuda" else "float32"

        # AudioCraft 1.3 imports xformers unconditionally and the published
        # checkpoint requests its memory-efficient attention backend. xformers
        # is not available on Apple Silicon, while the checkpoint remains
        # compatible with AudioCraft's built-in PyTorch attention path.
        lm_config.transformer_lm.memory_efficient = False
        loaders._delete_param(  # noqa: SLF001 - mirrors AudioCraft's public loader
            lm_config,
            "conditioners.self_wav.chroma_stem.cache_path",
        )
        loaders._delete_param(  # noqa: SLF001
            lm_config,
            "conditioners.args.merge_text_conditions_p",
        )
        loaders._delete_param(  # noqa: SLF001
            lm_config,
            "conditioners.args.drop_desc_p",
        )

        t5_path = str((Path(local_path) / "t5-base").resolve())
        t5_config = lm_config.conditioners.description.t5
        t5_config.name = t5_path
        if t5_path not in T5Conditioner.MODELS:
            T5Conditioner.MODELS.append(t5_path)
        T5Conditioner.MODELS_DIMS[t5_path] = 768

        lm = builders.get_lm_model(lm_config)
        lm.load_state_dict(lm_package["best_state"])
        lm.eval()
        lm.cfg = lm_config
        compression_model = loaders.load_compression_model(local_path, device=device)
        if "self_wav" in lm.condition_provider.conditioners:
            self_wav = lm.condition_provider.conditioners["self_wav"]
            self_wav.match_len_on_eval = True
            self_wav._use_masking = False  # noqa: SLF001 - AudioCraft setup contract
        model = MusicGen(local_path, compression_model, lm)

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
            "device": device,
            "default_params": dict(manifest.default_params),
            "path_exists": True,
            "model": model,
            "sampling_rate": int(model.sample_rate),
            "frame_rate": int(model.frame_rate),
            "max_duration": float(model.max_duration),
        }

    def _resolve_local_path(self, manifest: ModelManifest) -> str:
        if not manifest.local_path:
            raise ValueError(f"Manifest {manifest.id!r} is missing local_path.")

        local_path = (_REPO_ROOT / manifest.local_path).resolve()
        readiness = audiocraft_model_readiness(local_path)
        if not readiness.is_ready:
            raise FileNotFoundError(readiness.message)
        return str(local_path)

    def _resolve_device(self, torch: Any) -> str:
        requested_device = os.getenv("AUDIOCRAFT_DEVICE", "").strip().lower()
        if requested_device:
            return requested_device
        if torch.cuda.is_available():
            return "cuda"
        # AudioCraft 1.3 does not officially support MPS and its autocast path
        # assumes CUDA-like float16 behavior. CPU is slower but deterministic
        # and avoids publishing a model as ready only to fail during generation.
        return "cpu"

    def _install_xformers_import_shim(self) -> None:
        """Allow AudioCraft's torch-attention path to import without xformers."""

        try:
            import xformers  # type: ignore[import-not-found]  # noqa: F401
            return
        except ModuleNotFoundError:
            pass

        xformers_module = ModuleType("xformers")
        ops_module = ModuleType("xformers.ops")

        def unavailable(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "xformers attention was called even though the loader selected "
                "AudioCraft's PyTorch attention backend."
            )

        class LowerTriangularMask:
            pass

        ops_module.memory_efficient_attention = unavailable  # type: ignore[attr-defined]
        ops_module.LowerTriangularMask = LowerTriangularMask  # type: ignore[attr-defined]
        xformers_module.ops = ops_module  # type: ignore[attr-defined]
        sys.modules["xformers"] = xformers_module
        sys.modules["xformers.ops"] = ops_module


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


class LearnedVideoLoader(BaseModelLoader):
    """Load a learned text-to-video runtime through a local adapter entrypoint.

    Security note: this loader imports and executes a ``runtime.py`` / ``adapter.py``
    found inside the model directory (arbitrary code execution). Only place model
    packs from sources you trust under ``MODELS_ROOT``; never load third-party or
    untrusted model bundles.
    """

    def load(self, manifest: ModelManifest) -> dict[str, Any]:
        local_path = self._resolve_local_path(manifest)
        entrypoint = self._resolve_entrypoint(manifest, local_path)
        load_error: str | None = None
        runtime_payload: dict[str, Any] = {}

        if entrypoint is not None:
            try:
                runtime_payload = self._load_entrypoint(entrypoint, manifest)
            except Exception as exc:  # pragma: no cover - depends on local adapter implementation
                load_error = str(exc)
        else:
            load_error = (
                "No learned video adapter entrypoint found. "
                "Add runtime.py or set default_params.entrypoint in the manifest."
            )

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
            "runtime_adapter": "learned_text_to_video",
            "load_error": load_error,
            **runtime_payload,
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

    def _resolve_entrypoint(self, manifest: ModelManifest, local_path: str) -> Path | None:
        root = Path(local_path)
        configured_entrypoint = manifest.default_params.get("entrypoint")
        if isinstance(configured_entrypoint, str) and configured_entrypoint.strip():
            candidate = root / configured_entrypoint
            return candidate if candidate.exists() else None

        for candidate_name in ("runtime.py", "adapter.py"):
            candidate = root / candidate_name
            if candidate.exists():
                return candidate
        return None

    def _load_entrypoint(self, entrypoint: Path, manifest: ModelManifest) -> dict[str, Any]:
        spec = importlib.util.spec_from_file_location(
            f"creative_ai_video_runtime_{manifest.id}",
            entrypoint,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load learned video runtime entrypoint: {entrypoint}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        load_runtime = getattr(module, "load_runtime", None)
        if not callable(load_runtime):
            raise RuntimeError(
                f"Learned video runtime entrypoint must expose load_runtime(): {entrypoint}"
            )

        payload = load_runtime(manifest.model_dump(mode="json"))
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise RuntimeError("load_runtime() must return a dict payload")
        return payload


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
    registry.register(
        "transformers_musicgen_melody_loader",
        TransformersMusicgenMelodyLoader(),
    )
    registry.register("audiocraft_musicgen_loader", AudioCraftMusicgenLoader())
    registry.register("procedural_video_loader", ProceduralVideoLoader())
    registry.register("learned_video_loader", LearnedVideoLoader())
    return registry


__all__ = [
    "BaseModelLoader",
    "AudioCraftMusicgenLoader",
    "DiffusersImageLoader",
    "LearnedVideoLoader",
    "LoaderRegistry",
    "ProceduralVideoLoader",
    "TransformersMusicgenMelodyLoader",
    "TransformersMusicgenLoader",
    "create_default_loader_registry",
]
