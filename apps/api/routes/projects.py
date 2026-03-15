"""Project management API endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.jobs import JobRecord
from core.projects import ProjectRepository

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectResponse(BaseModel):
    """A project representation."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    created_at: datetime
    updated_at: datetime
    job_ids: list[str] = Field(default_factory=list)


class CreateProjectRequest(BaseModel):
    """Request to create a new project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)


class ProjectJobsResponse(BaseModel):
    """Response containing project and associated jobs."""

    model_config = ConfigDict(extra="forbid")

    project: ProjectResponse
    jobs: list[JobRecord] = Field(default_factory=list)
    job_count: int = Field(ge=0)
    media_breakdown: dict[str, int] = Field(default_factory=dict)
    average_quality_score: float | None = None


class UpdateProjectRequest(BaseModel):
    """Request to update project metadata."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)


def _get_project_repo(services: ApplicationServices) -> ProjectRepository:
    """Get or create the project repository."""
    return services.project_repository


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    req: CreateProjectRequest,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    """Create a new project."""

    repo = _get_project_repo(services)
    project = repo.create(name=req.name, description=req.description)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        job_ids=project.job_ids,
    )


@router.get("", response_model=list[ProjectResponse])
def list_projects(
    services: ApplicationServices = Depends(get_services),
) -> list[ProjectResponse]:
    """List all projects."""

    repo = _get_project_repo(services)
    projects = repo.list_all()

    return [
        ProjectResponse(
            id=p.id,
            name=p.name,
            description=p.description,
            created_at=p.created_at,
            updated_at=p.updated_at,
            job_ids=p.job_ids,
        )
        for p in projects
    ]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    """Get a specific project."""

    repo = _get_project_repo(services)
    project = repo.get(project_id)

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        job_ids=project.job_ids,
    )


@router.get("/{project_id}/jobs", response_model=ProjectJobsResponse)
def get_project_jobs(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectJobsResponse:
    """Get a project with its resolved jobs."""

    repo = _get_project_repo(services)
    project = repo.get(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    jobs: list[JobRecord] = []
    media_breakdown: dict[str, int] = {}
    quality_scores: list[float] = []
    for job_id in project.job_ids:
        job = services.job_repository.get(job_id)
        if job is None:
            continue
        jobs.append(job)
        media_breakdown[job.media_type] = media_breakdown.get(job.media_type, 0) + 1
        quality_report = job.result.metadata.get("quality_report") if job.result else None
        if isinstance(quality_report, dict):
            score = quality_report.get("quality_score")
            if isinstance(score, (int, float)):
                quality_scores.append(float(score))

    return ProjectJobsResponse(
        project=ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description,
            created_at=project.created_at,
            updated_at=project.updated_at,
            job_ids=project.job_ids,
        ),
        jobs=jobs,
        job_count=len(jobs),
        media_breakdown=media_breakdown,
        average_quality_score=(
            round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else None
        ),
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    req: UpdateProjectRequest,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    """Update project metadata."""

    repo = _get_project_repo(services)
    project = repo.update(
        project_id,
        name=req.name,
        description=req.description,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        job_ids=project.job_ids,
    )


@router.post("/{project_id}/jobs/{job_id}", response_model=ProjectResponse)
def add_job_to_project(
    project_id: str,
    job_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    """Add a job to a project."""

    repo = _get_project_repo(services)
    job = services.job_repository.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    project = repo.add_job(project_id, job_id)

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    services.job_repository.update_project(job_id, project.id)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        job_ids=project.job_ids,
    )


@router.delete("/{project_id}/jobs/{job_id}", response_model=ProjectResponse)
def remove_job_from_project(
    project_id: str,
    job_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    """Remove a job from a project."""

    repo = _get_project_repo(services)
    project = repo.remove_job(project_id, job_id)

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    services.job_repository.update_project(job_id, None)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
        job_ids=project.job_ids,
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> None:
    """Delete a project."""

    repo = _get_project_repo(services)
    project = repo.get(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    for job_id in project.job_ids:
        services.job_repository.update_project(job_id, None)
    repo.delete(project_id)


__all__ = [
    "router",
    "ProjectResponse",
    "CreateProjectRequest",
    "ProjectJobsResponse",
    "UpdateProjectRequest",
]
