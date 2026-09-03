"""Job management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_job_service, get_services
from bootstrap import ApplicationServices
from core.jobs import JobRecord, JobService
from core.projects import ProjectRepository
from core.reference_capabilities import MissingReferenceAssetError, UnsupportedReferenceError
from core.schemas import GenerationRequest

router = APIRouter(prefix="/jobs", tags=["jobs"])


class CreateJobResponse(BaseModel):
    """Response returned when a job is created."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class RerunJobRequest(BaseModel):
    """Override fields when cloning an existing job request."""

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    negative_prompt: str | None = None
    model_id: str | None = None
    seed: int | None = None
    output_format: str | None = None
    project_id: str | None = None
    params: dict[str, object] | None = None


def _get_project_repo(services: ApplicationServices) -> ProjectRepository:
    return services.project_repository


def _resolve_project_id(
    services: ApplicationServices,
    project_id: str | None,
) -> str | None:
    if project_id is None:
        return None
    if _get_project_repo(services).get(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project_id


@router.post("", response_model=CreateJobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    request: GenerationRequest,
    job_service: JobService = Depends(get_job_service),
) -> CreateJobResponse:
    try:
        job = job_service.create_job(request)
    except (UnsupportedReferenceError, MissingReferenceAssetError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return CreateJobResponse(job_id=job.id, status=job.status)


@router.get("/{job_id}", response_model=JobRecord)
def get_job(
    job_id: str,
    job_service: JobService = Depends(get_job_service),
) -> JobRecord:
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


@router.get("", response_model=list[JobRecord])
def list_jobs(job_service: JobService = Depends(get_job_service)) -> list[JobRecord]:
    return job_service.list_jobs()


@router.post("/{job_id}/cancel", response_model=JobRecord)
def cancel_job(
    job_id: str,
    job_service: JobService = Depends(get_job_service),
) -> JobRecord:
    job = job_service.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


@router.post("/{job_id}/rerun", response_model=CreateJobResponse, status_code=status.HTTP_201_CREATED)
def rerun_job(
    job_id: str,
    request: RerunJobRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    source_job = services.job_service.get_job(job_id)
    if source_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    resolved_project_id = _resolve_project_id(
        services,
        request.project_id if request.project_id is not None else source_job.project_id,
    )
    generation_request = source_job.request.model_copy(
        update={
            "prompt": request.prompt if request.prompt is not None else source_job.request.prompt,
            "negative_prompt": (
                request.negative_prompt
                if request.negative_prompt is not None
                else source_job.request.negative_prompt
            ),
            "model_id": request.model_id if request.model_id is not None else source_job.request.model_id,
            "seed": request.seed if request.seed is not None else source_job.request.seed,
            "output_format": (
                request.output_format
                if request.output_format is not None
                else source_job.request.output_format
            ),
            "params": dict(request.params) if request.params is not None else dict(source_job.request.params),
        }
    )
    job = services.job_service.create_job(generation_request, project_id=resolved_project_id)
    if resolved_project_id is not None:
        _get_project_repo(services).add_job(resolved_project_id, job.id)
    return CreateJobResponse(job_id=job.id, status=job.status)


__all__ = ["CreateJobResponse", "RerunJobRequest", "router"]
