"""Convenience generation endpoints that enqueue jobs."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from apps.api.routes.jobs import CreateJobResponse
from bootstrap import ApplicationServices
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

    def to_generation_request(self, media_type: MediaType) -> GenerationRequest:
        """Normalize a convenience request into the shared job request schema."""

        payload = self.model_dump(exclude={"project_id"})
        return GenerationRequest(media_type=media_type, **payload)


class GenerateImageRequest(BaseGenerateRequest):
    """Convenience request shape for image generation."""

    negative_prompt: str | None = None


class GenerateAudioRequest(BaseGenerateRequest):
    """Convenience request shape for audio generation."""


class GenerateVideoRequest(BaseGenerateRequest):
    """Convenience request shape for video generation."""

    negative_prompt: str | None = None


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


__all__ = [
    "GenerateAudioRequest",
    "GenerateImageRequest",
    "GenerateVideoRequest",
    "router",
]
