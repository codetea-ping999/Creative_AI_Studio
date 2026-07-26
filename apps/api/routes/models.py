"""Manifest-backed model listing endpoints."""

from __future__ import annotations

import os
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
    runtime_status: str = Field(min_length=1)
    availability_message: str = ""


class ModelsResponse(BaseModel):
    """Wrapper response for manifest-backed model metadata."""

    model_config = ConfigDict(extra="forbid")

    models: list[ModelSummary] = Field(default_factory=list)


def _serialize_manifest(manifest: ModelManifest) -> ModelSummary:
    runtime_status, availability_message = _model_runtime_status(manifest)
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
        is_available=runtime_status in {"ready", "configured"},
        runtime_status=runtime_status,
        availability_message=availability_message,
    )


def _manifest_is_available(manifest: ModelManifest) -> bool:
    return _model_runtime_status(manifest)[0] in {"ready", "configured"}


def _model_runtime_status(manifest: ModelManifest) -> tuple[str, str]:
    if manifest.runtime == "voicevox_http":
        from core.models.audio_runtimes import audio_endpoint_origin

        configured_base_url = os.getenv("VOICEVOX_BASE_URL", "").strip()
        base_url = configured_base_url or manifest.remote_ref
        if not base_url:
            return (
                "invalid_configuration",
                "VOICEVOX endpoint URL is not configured.",
            )
        try:
            origin = audio_endpoint_origin(base_url)
        except ValueError:
            return (
                "invalid_configuration",
                "VOICEVOX endpoint configuration is invalid or disallowed.",
            )
        return (
            "configured",
            f"VOICEVOX endpoint is configured at {origin}. Availability is checked "
            "when generation starts; an engine that is not running will fail that job.",
        )

    if not manifest.local_path:
        return "missing_files", "Manifest does not define a local model path."

    local_path = _resolve_repo_path(manifest.local_path)
    if not local_path.exists():
        return "missing_files", f"Local model path is missing: {manifest.local_path}"

    if manifest.runtime == "diffusers":
        if (local_path / "model_index.json").exists():
            return "ready", "Diffusers model files are ready."
        return "missing_files", "Diffusers model_index.json is missing."
    if manifest.runtime == "transformers":
        if (local_path / "config.json").exists():
            return "ready", "Transformers model files are ready."
        return "missing_files", "Transformers config.json is missing."
    if manifest.runtime == "learned":
        if manifest.default_params.get("runtime_status") == "scaffold":
            return "scaffold", "Learned runtime adapter is still a scaffold."
        entrypoint_name = str(manifest.default_params.get("entrypoint", "runtime.py"))
        if not (local_path / entrypoint_name).exists():
            return "missing_files", f"Learned runtime entrypoint is missing: {entrypoint_name}"
        pipeline_path_value = manifest.default_params.get("pipeline_path")
        if not isinstance(pipeline_path_value, str) or not pipeline_path_value.strip():
            return "missing_files", "Learned runtime pipeline_path is not configured."
        pipeline_path = _resolve_repo_path(pipeline_path_value)
        if not (pipeline_path / "model_index.json").exists():
            return (
                "missing_files",
                f"CogVideoX model_index.json is missing: {pipeline_path_value}",
            )
        missing_components = _missing_cogvideox_components(pipeline_path)
        if missing_components:
            return (
                "missing_files",
                "CogVideoX weights are incomplete: " + ", ".join(missing_components),
            )
        return "ready", "CogVideoX adapter and local model files are ready."

    return "ready", "Local runtime files are ready."


def _resolve_repo_path(path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (_REPO_ROOT / candidate).resolve()


def _missing_cogvideox_components(pipeline_path: Path) -> list[str]:
    missing: list[str] = []
    required_configs = {
        "scheduler": "scheduler_config.json",
        "text_encoder": "config.json",
        "tokenizer": "tokenizer_config.json",
        "transformer": "config.json",
        "vae": "config.json",
    }
    for component, config_name in required_configs.items():
        if not (pipeline_path / component / config_name).exists():
            missing.append(f"{component}/{config_name}")
    for component in ("text_encoder", "transformer", "vae"):
        if not any((pipeline_path / component).glob("*.safetensors")):
            missing.append(f"{component}/*.safetensors")
    return missing


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
