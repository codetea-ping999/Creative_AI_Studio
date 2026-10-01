"""Place already-downloaded model files into the manifest's model directory."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.folder_picker import FolderPickerUnavailableError, pick_folder
from core.model_install import (
    IncompleteSourceError,
    ModelInstallError,
    install_from_local_folder,
)
from core.models import RuntimeBusyError
from core.schemas.generation import MediaType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

_LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_local_client(request: Request) -> None:
    """Folder choosing and file copying act on the API host, so only it may ask."""

    client = request.client
    if client is None or client.host not in _LOOPBACK_CLIENTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Placing local model files is only available from the machine running the API.",
        )


class PickFolderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None, description="Selected folder, or null when the dialog was cancelled."
    )


class InstallModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1, description="Absolute path of the downloaded model folder.")
    media_type: MediaType
    replace: bool = Field(
        default=False,
        description="Move files already in the install location aside before placing the model.",
    )


class InstallModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    destination: str
    source: str
    replaced_to: str | None = None
    copied_bytes: int
    is_available: bool
    runtime_status: str
    availability_message: str


@router.post(
    "/pick-folder",
    response_model=PickFolderResponse,
    summary="Open Finder / Explorer to choose a model folder",
    dependencies=[Depends(require_local_client)],
)
def pick_model_folder() -> PickFolderResponse:
    """Show the native folder chooser on the machine running the API."""

    try:
        return PickFolderResponse(path=pick_folder())
    except FolderPickerUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc


@router.post(
    "/{model_id}/install",
    response_model=InstallModelResponse,
    summary="Copy a downloaded model folder into the model directory",
    dependencies=[Depends(require_local_client)],
)
def install_model_from_folder(
    model_id: str,
    payload: InstallModelRequest,
    services: ApplicationServices = Depends(get_services),
) -> InstallModelResponse:
    """Validate ``source_path`` against the manifest and copy it into place."""

    try:
        manifest = services.model_service.get_manifest(model_id, payload.media_type)
    except Exception as exc:  # resolver raises its own not-found errors
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        result = install_from_local_folder(
            source_path=payload.source_path,
            runtime=manifest.runtime,
            local_path=manifest.local_path,
            remote_ref=manifest.remote_ref,
            default_params=manifest.default_params,
            manifest_id=manifest.id,
            replace=payload.replace,
        )
    except ModelInstallError as exc:
        detail: dict[str, object] = {"code": exc.code, "message": str(exc)}
        if isinstance(exc, IncompleteSourceError):
            detail["missing"] = list(exc.missing)
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except OSError as exc:
        logger.exception("Model import failed for %s", manifest.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "copy_failed", "message": f"Copying the model failed: {exc}"},
        ) from exc

    try:
        # A runtime cached from the previous files must not outlive them.
        services.model_service.unload_model(manifest.public_model_id)
    except RuntimeBusyError:
        logger.warning("Model %s is busy; restart the API to pick up replaced files", manifest.id)

    return InstallModelResponse(
        model_id=manifest.public_model_id,
        destination=str(result.destination),
        source=str(result.source),
        replaced_to=str(result.replaced_to) if result.replaced_to else None,
        copied_bytes=result.copied_bytes,
        is_available=result.readiness.is_ready,
        runtime_status=result.readiness.status,
        availability_message=result.readiness.message,
    )
