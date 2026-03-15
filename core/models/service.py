"""Application-facing entrypoint for model resolution and loading."""

from __future__ import annotations

from typing import Any

from .cache import ModelRuntimeCache
from .loader import LoaderRegistry
from .manifest import ModelManifest
from .registry import ModelRegistry
from .resolver import ModelResolver


class ModelService:
    """Facade combining registry, resolver, loader registry, and cache."""

    def __init__(
        self,
        registry: ModelRegistry,
        resolver: ModelResolver,
        loader_registry: LoaderRegistry,
        runtime_cache: ModelRuntimeCache,
    ) -> None:
        self.registry = registry
        self.resolver = resolver
        self.loader_registry = loader_registry
        self.runtime_cache = runtime_cache

    def list_models(
        self,
        *,
        media_type: str | None = None,
        task_type: str | None = None,
    ) -> list[ModelManifest]:
        manifests = self.registry.list_all()
        if media_type is not None:
            manifests = [
                manifest for manifest in manifests if manifest.media_type == media_type
            ]
        if task_type is not None:
            manifests = [
                manifest for manifest in manifests if manifest.task_type == task_type
            ]
        return manifests

    def get_manifest(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> ModelManifest:
        """Resolve a public model id, alias, or manifest id, or use the default."""

        return self.resolver.resolve(model_id, media_type, task_type)

    def get_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> Any:
        _, runtime_obj = self.resolve_runtime(model_id, media_type, task_type)
        return runtime_obj

    def resolve_runtime(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> tuple[ModelManifest, Any]:
        manifest = self.get_manifest(model_id, media_type, task_type)
        cached_runtime = self.runtime_cache.get(manifest.id)
        if cached_runtime is not None:
            return manifest, cached_runtime

        loader = self.loader_registry.get(manifest.loader)
        runtime_obj = loader.load(manifest)
        self.runtime_cache.put(manifest.id, runtime_obj)
        return manifest, runtime_obj

    def unload_model(self, model_id: str) -> None:
        self.runtime_cache.unload(model_id)

    def unload_all(self) -> None:
        self.runtime_cache.unload_all()


__all__ = ["ModelService"]
