"""Model management primitives for Creative AI Studio."""

from core.model_readiness import (
    ModelReadiness,
    evaluate_manifest_payload,
    evaluate_manifest_readiness,
)

from .cache import ModelRuntimeCache
from .loader import (
    AudioCraftMusicgenLoader,
    BaseModelLoader,
    BaseSpeechLoader,
    DiffusersImageLoader,
    KokoroTtsLoader,
    LearnedVideoLoader,
    LoaderRegistry,
    TransformersMusicgenLoader,
    VoicevoxHttpLoader,
    create_default_loader_registry,
)
from .manifest import ModelManifest
from .registry import ModelRegistry
from .resolver import ModelResolver
from .service import ModelService

__all__ = [
    "AudioCraftMusicgenLoader",
    "BaseModelLoader",
    "BaseSpeechLoader",
    "DiffusersImageLoader",
    "KokoroTtsLoader",
    "LearnedVideoLoader",
    "LoaderRegistry",
    "ModelManifest",
    "ModelReadiness",
    "ModelRegistry",
    "ModelResolver",
    "ModelRuntimeCache",
    "ModelService",
    "TransformersMusicgenLoader",
    "VoicevoxHttpLoader",
    "create_default_loader_registry",
    "evaluate_manifest_payload",
    "evaluate_manifest_readiness",
]
