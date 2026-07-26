"""Model management primitives for Creative AI Studio."""

from .cache import ModelRuntimeCache
from .loader import (
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
    "BaseModelLoader",
    "BaseSpeechLoader",
    "DiffusersImageLoader",
    "KokoroTtsLoader",
    "LearnedVideoLoader",
    "LoaderRegistry",
    "ModelManifest",
    "ModelRegistry",
    "ModelResolver",
    "ModelRuntimeCache",
    "ModelService",
    "TransformersMusicgenLoader",
    "VoicevoxHttpLoader",
    "create_default_loader_registry",
]
