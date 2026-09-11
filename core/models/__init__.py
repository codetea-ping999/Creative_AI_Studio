"""Model management primitives for Creative AI Studio."""

from core.model_readiness import (
    ModelReadiness,
    evaluate_manifest_payload,
    evaluate_manifest_readiness,
)

from .cache import ModelRuntimeCache, resolve_media_cache_limits
from .cleanup import release_runtime
from .cloud_guard import CloudProviderDisabledError, ensure_cloud_provider_enabled
from .loader import (
    AudioCraftMusicgenLoader,
    BaseModelLoader,
    BaseSpeechLoader,
    CloudHttpSpeechLoader,
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
from .runtime_lease import RuntimeBusyError, RuntimeEntry, RuntimeState
from .service import ModelService, RuntimeHandle

__all__ = [
    "AudioCraftMusicgenLoader",
    "BaseModelLoader",
    "BaseSpeechLoader",
    "CloudHttpSpeechLoader",
    "CloudProviderDisabledError",
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
    "RuntimeBusyError",
    "RuntimeEntry",
    "RuntimeHandle",
    "RuntimeState",
    "TransformersMusicgenLoader",
    "VoicevoxHttpLoader",
    "create_default_loader_registry",
    "ensure_cloud_provider_enabled",
    "evaluate_manifest_payload",
    "evaluate_manifest_readiness",
    "release_runtime",
    "resolve_media_cache_limits",
]
