"""Manifest-backed model listing endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.models import ModelManifest
from core.schemas.generation import MediaType

router = APIRouter(prefix="/models", tags=["models"])
_REPO_ROOT = Path(__file__).resolve().parents[3]


class ModelSummary(BaseModel):
    """UI-safe model metadata derived from manifests."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    internal_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    media_type: MediaType
    task_type: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    default_params: dict[str, object] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    is_default: bool = False
    is_available: bool = False


class ModelsResponse(BaseModel):
    """Wrapper response for manifest-backed model metadata."""

    model_config = ConfigDict(extra="forbid")

    models: list[ModelSummary] = Field(default_factory=list)


def _serialize_manifest(manifest: ModelManifest) -> ModelSummary:
    return ModelSummary(
        id=manifest.public_model_id,
        internal_id=manifest.id,
        display_name=manifest.display_name,
        media_type=manifest.media_type,
        task_type=manifest.task_type,
        provider=manifest.provider,
        default_params=dict(manifest.default_params),
        tags=list(manifest.tags),
        is_default=manifest.is_default,
        is_available=_manifest_is_available(manifest),
    )


def _manifest_is_available(manifest: ModelManifest) -> bool:
    if not manifest.local_path:
        return False

    local_path = (_REPO_ROOT / manifest.local_path).resolve()
    if not local_path.exists():
        return False

    if manifest.runtime == "diffusers":
        return (local_path / "model_index.json").exists()
    if manifest.runtime == "transformers":
        return (local_path / "config.json").exists()
    if manifest.runtime == "learned":
        if manifest.default_params.get("runtime_status") == "scaffold":
            return False
        return any(
            (local_path / candidate).exists()
            for candidate in ("runtime.py", "adapter.py", "model_index.json", "config.json")
        )

    return True


@router.get(
    "",
    response_model=ModelsResponse,
    summary="List enabled public models",
)
def list_models(
    media_type: MediaType | None = Query(default=None),
    services: ApplicationServices = Depends(get_services),
) -> ModelsResponse:
    """Return enabled model metadata from the configured service graph."""

    manifests = services.model_service.list_models(media_type=media_type)

    return ModelsResponse(
        models=[_serialize_manifest(manifest) for manifest in manifests]
    )
