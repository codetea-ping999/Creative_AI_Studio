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
from core.schemas import GenerationRequest

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


@router.post(
    "/image",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_image(
    request: GenerateImageRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    generation_request = GenerationRequest(
        media_type="image",
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        model_id=request.model_id,
        seed=request.seed,
        output_format=request.output_format,
        params=request.params,
    )
    bound_job = _create_project_bound_job(services, generation_request, request.project_id)
    return CreateJobResponse(job_id=bound_job.job.id, status=bound_job.job.status)


@router.post(
    "/audio",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_audio(
    request: GenerateAudioRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    generation_request = GenerationRequest(
        media_type="audio",
        prompt=request.prompt,
        model_id=request.model_id,
        seed=request.seed,
        output_format=request.output_format,
        params=request.params,
    )
    bound_job = _create_project_bound_job(services, generation_request, request.project_id)
    return CreateJobResponse(job_id=bound_job.job.id, status=bound_job.job.status)


@router.post(
    "/video",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_video(
    request: GenerateVideoRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    generation_request = GenerationRequest(
        media_type="video",
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        model_id=request.model_id,
        seed=request.seed,
        output_format=request.output_format,
        params=request.params,
    )
    bound_job = _create_project_bound_job(services, generation_request, request.project_id)
    return CreateJobResponse(job_id=bound_job.job.id, status=bound_job.job.status)


__all__ = [
    "GenerateAudioRequest",
    "GenerateImageRequest",
    "GenerateVideoRequest",
    "router",
]
