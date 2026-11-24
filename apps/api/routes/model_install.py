"""Place already-downloaded model files into the manifest's model directory."""

from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.folder_picker import (
    FolderPickerFailedError,
    FolderPickerUnavailableError,
    pick_folder,
)
from core.model_install import (
    IncompleteSourceError,
    ModelInstallError,
    install_from_local_folder,
)
from core.models import RuntimeBusyError
from core.schemas.generation import MediaType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

_LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _host_name(netloc: str) -> str:
    """``netloc`` without its port such as ``[::1]:8000`` -> ``::1``."""

    value = netloc.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_local_request(request: Request) -> None:
    """Folder choosing and file copying act on the API host, so only it may ask.

    Three checks, because a loopback client address alone does not stop a web page:
    any site the user visits reaches ``127.0.0.1`` from the user's own browser.

    - the client address is loopback (``::ffff:127.0.0.1`` included);
    - the ``Host`` header is a loopback name, which defeats DNS rebinding;
    - an ``Origin`` header, when the browser sends one, is the Studio UI's own
      origin (the CORS allowlist, or the API's own origin when it serves the UI).
    """

    client = request.client
    if client is None or not _is_loopback_address(client.host):
        raise _forbidden(
            "Placing local model files is only available from the machine running the API."
        )
    host_header = request.headers.get("host", "")
    if _host_name(host_header) not in _LOOPBACK_HOST_NAMES:
        raise _forbidden("Placing local model files needs the API to be addressed as localhost.")
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = set(getattr(request.app.state, "allowed_origins", ()))
    allowed.add(f"{request.url.scheme}://{host_header}")
    if origin not in allowed:
        raise _forbidden("Placing local model files is only available from the Studio UI.")


class PickFolderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None, description="Selected folder, or null when the dialog was cancelled."
    )


class InstallModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(
        min_length=1, description="Absolute path of the downloaded model folder."
    )
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
    restart_required: bool = Field(
        default=False,
        description=(
            "True when the model was in use, so the API keeps the previously loaded "
            "runtime until it restarts."
        ),
    )


@router.post(
    "/pick-folder",
    response_model=PickFolderResponse,
    summary="Open Finder / Explorer to choose a model folder",
    dependencies=[Depends(require_local_request)],
)
def pick_model_folder() -> PickFolderResponse:
    """Show the native folder chooser on the machine running the API."""

    try:
        return PickFolderResponse(path=pick_folder())
    except FolderPickerUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"code": "picker_unavailable", "message": str(exc)},
        ) from exc
    except FolderPickerFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "picker_failed", "message": str(exc)},
        ) from exc


@router.post(
    "/{model_id}/install",
    response_model=InstallModelResponse,
    summary="Copy a downloaded model folder into the model directory",
    dependencies=[Depends(require_local_request)],
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

    restart_required = False
    try:
        # A runtime cached from the previous files must not outlive them.
        services.model_service.unload_model(manifest.public_model_id)
    except RuntimeBusyError:
        restart_required = True
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
        restart_required=restart_required,
    )
