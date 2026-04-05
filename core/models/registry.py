"""Registry for loading and querying model manifests."""

from __future__ import annotations

import json
from pathlib import Path

from .manifest import ModelManifest

_DEFAULT_MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "models" / "manifests"


class ModelRegistry:
    """Load manifests from disk and expose lookup helpers."""

    def __init__(self, manifest_root: str | Path | None = None) -> None:
        self.manifest_root = Path(manifest_root or _DEFAULT_MANIFEST_ROOT)
        self._manifests: dict[str, ModelManifest] = {}
        self._loaded = False

    def load_all(self) -> None:
        manifests: dict[str, ModelManifest] = {}
        manifest_sources: dict[str, Path] = {}
        if self.manifest_root.exists():
            for path in sorted(self.manifest_root.rglob("*.json")):
                manifest = ModelManifest.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if manifest.id in manifests:
                    if self._is_duplicate_equivalent(manifests[manifest.id], manifest):
                        continue
                    original_path = manifest_sources[manifest.id]
                    raise ValueError(
                        f"Duplicate model manifest id: {manifest.id} "
                        f"({original_path} vs {path})"
                    )
                manifests[manifest.id] = manifest
                manifest_sources[manifest.id] = path

        self._manifests = manifests
        self._loaded = True

    def get(self, model_id: str) -> ModelManifest:
        self._ensure_loaded()
        try:
            return self._manifests[model_id]
        except KeyError as exc:
            raise LookupError(f"Unknown model_id: {model_id}") from exc

    def list_all(self, *, enabled_only: bool = True) -> list[ModelManifest]:
        self._ensure_loaded()
        manifests = list(self._manifests.values())
        if enabled_only:
            manifests = [manifest for manifest in manifests if manifest.enabled]
        return manifests

    def list_by_media_type(
        self,
        media_type: str,
        *,
        enabled_only: bool = True,
    ) -> list[ModelManifest]:
        return [
            manifest
            for manifest in self.list_all(enabled_only=enabled_only)
            if manifest.media_type == media_type
        ]

    def list_by_task_type(
        self,
        task_type: str,
        *,
        enabled_only: bool = True,
    ) -> list[ModelManifest]:
        return [
            manifest
            for manifest in self.list_all(enabled_only=enabled_only)
            if manifest.task_type == task_type
        ]

    def get_default(
        self,
        media_type: str,
        task_type: str | None = None,
    ) -> ModelManifest | None:
        candidates = self.list_by_media_type(media_type)
        if task_type is not None:
            candidates = [
                manifest for manifest in candidates if manifest.task_type == task_type
            ]

        for manifest in candidates:
            if manifest.is_default:
                return manifest
        return None

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_all()

    def _is_duplicate_equivalent(
        self,
        left: ModelManifest,
        right: ModelManifest,
    ) -> bool:
        return json.dumps(left.model_dump(mode="json"), sort_keys=True) == json.dumps(
            right.model_dump(mode="json"),
            sort_keys=True,
        )


__all__ = ["ModelRegistry"]
