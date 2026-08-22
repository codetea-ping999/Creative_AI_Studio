"""Convenience generation endpoints that enqueue jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from apps.api.routes.jobs import CreateJobResponse
from bootstrap import ApplicationServices
from core.assets import Asset
from core.jobs import JobRecord
from core.projects import ProjectRepository
from core.schemas import GenerationRequest, MediaType

router = APIRouter(prefix="/generate", tags=["generate"])


def _get_project_repo(services: ApplicationServices) -> ProjectRepository:
    return services.project_repository


@dataclass(slots=True)
class ProjectBoundJob:
    """Job plus normalized project binding state."""

    job: JobRecord
    project_id: str | None


class BaseGenerateRequest(BaseModel):
    """Fields shared by convenience generation endpoints."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    model_id: str = Field(default="")
    seed: int | None = None
    output_format: str | None = None
    project_id: str | None = None
    params: dict[str, object] = Field(default_factory=dict)

    # Endpoints that target a non-default task within their media type set this;
    # None routes to the media type's default generator.
    task_type: ClassVar[str | None] = None

    def to_generation_request(self, media_type: MediaType) -> GenerationRequest:
        """Normalize a convenience request into the shared job request schema."""

        payload = self.model_dump(exclude={"project_id"})
        return GenerationRequest(
            media_type=media_type,
            task_type=self.task_type,
            **payload,
        )


class GenerateImageRequest(BaseGenerateRequest):
    """Convenience request shape for image generation."""

    negative_prompt: str | None = None


class GenerateAudioRequest(BaseGenerateRequest):
    """Convenience request shape for audio generation."""


class GenerateSpeechRequest(BaseGenerateRequest):
    """Convenience request shape for narration synthesis."""

    task_type: ClassVar[str | None] = "text-to-speech"


class GenerateVideoRequest(BaseGenerateRequest):
    """Convenience request shape for video generation."""

    negative_prompt: str | None = None


class GenerateAssemblyRequest(BaseGenerateRequest):
    """Convenience request shape for deterministic timeline assembly."""

    task_type: ClassVar[str | None] = "assembly"
    negative_prompt: str | None = None


class GenerateTextRequest(BaseGenerateRequest):
    """Convenience request shape for story and writing tasks."""

    task_type: ClassVar[str | None] = "story"


def _resolve_project_id(
    services: ApplicationServices,
    project_id: str | None,
) -> str | None:
    if project_id is None:
        return None
    project_repo = _get_project_repo(services)
    if project_repo.get(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project_id


def _create_project_bound_job(
    services: ApplicationServices,
    generation_request: GenerationRequest,
    project_id: str | None,
) -> ProjectBoundJob:
    resolved_project_id = _resolve_project_id(services, project_id)
    job = services.job_service.create_job(generation_request, project_id=resolved_project_id)
    if resolved_project_id is not None:
        _get_project_repo(services).add_job(resolved_project_id, job.id)
    return ProjectBoundJob(job=job, project_id=resolved_project_id)


def _enqueue_generation(
    services: ApplicationServices,
    media_type: MediaType,
    request: BaseGenerateRequest,
) -> CreateJobResponse:
    bound_job = _create_project_bound_job(
        services,
        request.to_generation_request(media_type),
        request.project_id,
    )
    return CreateJobResponse(job_id=bound_job.job.id, status=bound_job.job.status)


_ASSEMBLY_ASSET_TRACKS = ("visual", "narration", "music")


def _strip_timeline_paths(value: object) -> object:
    """Copy a timeline payload without trusting any caller-provided file path."""

    if isinstance(value, dict):
        return {
            key: _strip_timeline_paths(item)
            for key, item in value.items()
            if key != "path"
        }
    if isinstance(value, list):
        return [_strip_timeline_paths(item) for item in value]
    return value


def resolve_timeline_assets(
    services: ApplicationServices,
    timeline: Any,
    project_id: str | None,
) -> Any:
    """Return a copy of ``timeline`` whose assets all live in ``project_id``.

    Shared by every route that queues an assembly job, so the project boundary is
    enforced in one place: a timeline built server-side from a story and one
    posted by a client are checked by the same code.
    """

    sanitized_timeline = _strip_timeline_paths(timeline)
    if not isinstance(sanitized_timeline, dict):
        return sanitized_timeline

    tracks = sanitized_timeline.get("tracks")
    resolved_assets: dict[str, Asset] = {}
    if isinstance(tracks, dict):
        for track_name in _ASSEMBLY_ASSET_TRACKS:
            entries = tracks.get(track_name)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                raw_asset_id = entry.get("asset_id")
                asset_id = (
                    raw_asset_id.strip()
                    if isinstance(raw_asset_id, str)
                    else ""
                )
                if not asset_id:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Timeline {track_name} entry #{index} requires an "
                            "asset_id; direct paths are not accepted."
                        ),
                    )

                asset = resolved_assets.get(asset_id)
                if asset is None:
                    asset = services.asset_repository.get(asset_id)
                    if asset is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Timeline asset not found: {asset_id}",
                        )
                    resolved_assets[asset_id] = asset

                # Project membership is an exact boundary. In particular, an
                # unassigned asset is accepted only for an unassigned assembly,
                # so moving a story into a project after its media was generated
                # leaves that media behind — the message names the way back.
                if asset.project_id != project_id:
                    # Name a recovery that actually exists. Attaching an asset to
                    # a project is a route; detaching it is not, so when the
                    # assembly targets no project the only way back is to move the
                    # assembly into the asset's project instead.
                    if project_id is None:
                        recovery = (
                            f"Bind the assembly to project {asset.project_id} "
                            "instead (PATCH /stories/{story_id} with project_id), "
                            "or assemble from assets that belong to no project."
                        )
                    else:
                        recovery = (
                            "Re-parent the asset with "
                            f"POST /projects/{project_id}/assets/{asset_id} "
                            "before assembling."
                        )
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=(
                            "Timeline asset not found in target project: "
                            f"{asset_id} belongs to "
                            f"{asset.project_id or 'no project'} while this "
                            f"assembly targets {project_id or 'no project'}. "
                            + recovery
                        ),
                    )
                entry["asset_id"] = asset_id

    return sanitized_timeline


def _prepare_assembly_request(
    services: ApplicationServices,
    request: GenerateAssemblyRequest,
) -> GenerateAssemblyRequest:
    """Resolve every timeline asset inside the assembly's project boundary."""

    # Resolve the target first so an invalid project cannot be used to probe
    # whether an asset exists.
    project_id = _resolve_project_id(services, request.project_id)
    return request.model_copy(
        update={
            "params": {
                **request.params,
                "timeline": resolve_timeline_assets(
                    services, request.params.get("timeline"), project_id
                ),
            }
        }
    )


@router.post(
    "/image",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_image(
    request: GenerateImageRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    return _enqueue_generation(services, "image", request)


@router.post(
    "/audio",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_audio(
    request: GenerateAudioRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    generation_request = request.to_generation_request("audio")
    try:
        services.generator_registry.get("audio").validate_request(generation_request)
    except (LookupError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _enqueue_generation(services, "audio", request)


@router.post(
    "/speech",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_speech(
    request: GenerateSpeechRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    generation_request = request.to_generation_request("audio")
    try:
        services.generator_registry.get("audio", "text-to-speech").validate_request(
            generation_request
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _enqueue_generation(services, "audio", request)


@router.post(
    "/video",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_video(
    request: GenerateVideoRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    return _enqueue_generation(services, "video", request)


@router.post(
    "/assembly",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_assembly(
    request: GenerateAssemblyRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    return _enqueue_generation(
        services,
        "video",
        _prepare_assembly_request(services, request),
    )


@router.post(
    "/text",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_text(
    request: GenerateTextRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    return _enqueue_generation(services, "text", request)


__all__ = [
    "GenerateAssemblyRequest",
    "GenerateAudioRequest",
    "GenerateImageRequest",
    "GenerateSpeechRequest",
    "GenerateTextRequest",
    "GenerateVideoRequest",
    "resolve_timeline_assets",
    "router",
]
