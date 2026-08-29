"""Registry for loading and querying model manifests."""

from __future__ import annotations

import json
from pathlib import Path

from .cloud_guard import cloud_provider_env_flag
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

        self._check_cloud_provider_flag_collisions(manifests)
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

    def _check_cloud_provider_flag_collisions(
        self,
        manifests: dict[str, ModelManifest],
    ) -> None:
        """Fail fast if two ``provider: "cloud"`` manifests share an opt-in flag.

        ``cloud_provider_env_flag`` normalizes a manifest id into
        ``ALLOW_CLOUD_PROVIDER_<ID>`` by upper-casing and collapsing every
        non-alphanumeric character to ``_``, so distinct ids such as
        ``vendor-tts`` and ``vendor_tts`` collide. Left unchecked, opting into
        one such manifest silently opts into the other too -- caught here,
        at load time, rather than left for an operator to discover later.
        """

        seen: dict[str, str] = {}
        for manifest in manifests.values():
            if manifest.provider != "cloud":
                continue
            flag = cloud_provider_env_flag(manifest.id)
            if flag in seen and seen[flag] != manifest.id:
                raise ValueError(
                    f"Cloud provider manifests {seen[flag]!r} and {manifest.id!r} "
                    f"both normalize to the opt-in flag {flag!r}; rename one "
                    "manifest id so each cloud provider has its own "
                    "ALLOW_CLOUD_PROVIDER_<ID> switch."
                )
            seen[flag] = manifest.id

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
