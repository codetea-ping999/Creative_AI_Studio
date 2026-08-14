"""Resolve requested model identifiers into manifests."""

from __future__ import annotations

from .manifest import ModelManifest
from .registry import ModelRegistry


class ModelResolver:
    """Resolve public model ids, aliases, or manifest ids on top of the registry."""

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry
        self._public_id_index: dict[str, str] | None = None

    def resolve(
        self,
        model_id: str | None,
        media_type: str,
        task_type: str | None = None,
    ) -> ModelManifest:
        manifest: ModelManifest | None
        if model_id:
            manifest = self.registry.get(self.resolve_manifest_id(model_id))
        else:
            manifest = self.registry.get_default(media_type, task_type)
            if manifest is None and task_type is not None:
                manifest = self.registry.get_default(media_type)
            if manifest is None:
                raise LookupError(
                    f"No default model for media_type={media_type!r}, task_type={task_type!r}."
                )

        if not manifest.enabled:
            raise LookupError(f"Model is disabled: {manifest.id}")
        if manifest.media_type != media_type:
            raise LookupError(
                f"Model {manifest.id!r} does not support media_type={media_type!r}."
            )
        if task_type is not None and manifest.task_type != task_type:
            raise LookupError(
                f"Model {manifest.id!r} does not support task_type={task_type!r}."
            )
        return manifest

    def resolve_manifest_id(self, model_id: str) -> str:
        """Resolve aliases and public ids before falling back to manifest ids."""

        resolved_model_id = self._public_id_index_for_registry().get(model_id)
        if resolved_model_id is not None:
            return resolved_model_id
        return model_id

    def _public_id_index_for_registry(self) -> dict[str, str]:
        if self._public_id_index is None:
            public_id_index: dict[str, str] = {}
            for manifest in self.registry.list_all(enabled_only=False):
                self._register_mapping(
                    public_id_index,
                    manifest.public_model_id,
                    manifest.id,
                )
                for alias in manifest.aliases:
                    self._register_mapping(public_id_index, alias, manifest.id)
            self._public_id_index = public_id_index
        return self._public_id_index

    def _register_mapping(
        self,
        public_id_index: dict[str, str],
        requested_id: str,
        manifest_id: str,
    ) -> None:
        existing_manifest_id = public_id_index.get(requested_id)
        if existing_manifest_id is not None and existing_manifest_id != manifest_id:
            raise ValueError(
                "Duplicate public model id or alias "
                f"{requested_id!r} for manifests {existing_manifest_id!r} and {manifest_id!r}."
            )
        public_id_index[requested_id] = manifest_id


__all__ = ["ModelResolver"]
