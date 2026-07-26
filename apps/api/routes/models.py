"""Manifest-backed model listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.models import ModelManifest, evaluate_manifest_readiness
from core.schemas.generation import MediaType

router = APIRouter(prefix="/models", tags=["models"])


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
    runtime_status: str = Field(min_length=1)
    availability_message: str = ""


class ModelsResponse(BaseModel):
    """Wrapper response for manifest-backed model metadata."""

    model_config = ConfigDict(extra="forbid")

    models: list[ModelSummary] = Field(default_factory=list)


def _serialize_manifest(manifest: ModelManifest) -> ModelSummary:
    readiness = evaluate_manifest_readiness(manifest)
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
        is_available=readiness.is_ready,
        runtime_status=readiness.status,
        availability_message=readiness.message,
    )


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
