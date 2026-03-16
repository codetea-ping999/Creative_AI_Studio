"""Project management API endpoints."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.assets import Asset
from core.jobs import JobRecord
from core.projects import Project, ProjectRepository
from core.quality import calibrate_quality_report

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectResponse(BaseModel):
    """A project representation."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    status: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    pinned_asset_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    job_ids: list[str] = Field(default_factory=list)
    job_count: int = Field(default=0, ge=0)
    asset_count: int = Field(default=0, ge=0)
    cover_asset_path: str | None = None


class CreateProjectRequest(BaseModel):
    """Request to create a new project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    status: str = Field(default="active", min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateProjectRequest(BaseModel):
    """Request to update project metadata."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    pinned_asset_ids: list[str] | None = None


class ProjectAssetResponse(BaseModel):
    """Project asset summary."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    prompt: str
    output_path: str = Field(min_length=1)
    preview_path: str | None = None
    quality_score: float | None = None
    quality_score_calibrated: float | None = None
    semantic_alignment_score: float | None = None
    creative_alignment_score: float | None = None
    created_at: datetime
    updated_at: datetime


class ProjectJobsResponse(BaseModel):
    """Response containing project and associated jobs."""

    model_config = ConfigDict(extra="forbid")

    project: ProjectResponse
    jobs: list[JobRecord] = Field(default_factory=list)
    assets: list[ProjectAssetResponse] = Field(default_factory=list)
    job_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    media_breakdown: dict[str, int] = Field(default_factory=dict)
    average_quality_score: float | None = None
    average_creative_alignment_score: float | None = None


class ExportProjectRequest(BaseModel):
    """Request shape for exporting a project bundle."""

    model_config = ConfigDict(extra="forbid")

    destination_dir: str | None = None


class ExportProjectResponse(BaseModel):
    """Project export result."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    bundle_root: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)


def _sync_assets(services: ApplicationServices) -> None:
    services.asset_repository.sync_jobs(services.job_service.list_jobs())


def _get_project_repo(services: ApplicationServices) -> ProjectRepository:
    return services.project_repository


def _serialize_project(project: Project, services: ApplicationServices) -> ProjectResponse:
    assets = services.asset_repository.list_all(project_id=project.id)
    cover_asset = assets[0] if assets else None
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status,
        tags=list(project.tags),
        metadata=dict(project.metadata),
        pinned_asset_ids=list(project.pinned_asset_ids),
        created_at=project.created_at,
        updated_at=project.updated_at,
        job_ids=list(project.job_ids),
        job_count=len(project.job_ids),
        asset_count=len(assets),
        cover_asset_path=cover_asset.preview_path if cover_asset is not None else None,
    )


def _serialize_asset(asset: Asset, services: ApplicationServices) -> ProjectAssetResponse:
    feedback_summary = services.feedback_repository.summarize(asset_id=asset.id)
    quality_report = asset.metadata.get("quality_report")
    calibrated = calibrate_quality_report(
        dict(quality_report) if isinstance(quality_report, dict) else {},
        feedback_summary,
    )
    return ProjectAssetResponse(
        asset_id=asset.id,
        job_id=asset.job_id,
        media_type=asset.media_type,
        prompt=asset.prompt,
        output_path=asset.path,
        preview_path=asset.preview_path,
        quality_score=_number_or_none(calibrated.get("quality_score")),
        quality_score_calibrated=_number_or_none(calibrated.get("quality_score_calibrated")),
        semantic_alignment_score=_number_or_none(calibrated.get("semantic_alignment_score")),
        creative_alignment_score=_number_or_none(calibrated.get("creative_alignment_score")),
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _project_or_404(project_id: str, services: ApplicationServices) -> Project:
    project = _get_project_repo(services).get(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    req: CreateProjectRequest,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    project = _get_project_repo(services).create(
        name=req.name,
        description=req.description,
        status=req.status,
        tags=req.tags,
        metadata=req.metadata,
    )
    _sync_assets(services)
    return _serialize_project(project, services)


@router.get("", response_model=list[ProjectResponse])
def list_projects(
    q: str | None = Query(default=None, min_length=1),
    status_filter: str | None = Query(default=None, alias="status"),
    tag: str | None = Query(default=None, min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> list[ProjectResponse]:
    _sync_assets(services)
    projects = _get_project_repo(services).list_all(
        query_text=q,
        status=status_filter,
        tag=tag,
    )
    return [_serialize_project(project, services) for project in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    _sync_assets(services)
    project = _project_or_404(project_id, services)
    return _serialize_project(project, services)


@router.get("/{project_id}/assets", response_model=list[ProjectAssetResponse])
def get_project_assets(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> list[ProjectAssetResponse]:
    _sync_assets(services)
    _project_or_404(project_id, services)
    assets = services.asset_repository.list_all(project_id=project_id)
    return [_serialize_asset(asset, services) for asset in assets]


@router.get("/{project_id}/jobs", response_model=ProjectJobsResponse)
def get_project_jobs(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectJobsResponse:
    _sync_assets(services)
    project = _project_or_404(project_id, services)

    jobs: list[JobRecord] = []
    media_breakdown: dict[str, int] = {}
    quality_scores: list[float] = []
    creative_scores: list[float] = []
    for job_id in project.job_ids:
        job = services.job_repository.get(job_id)
        if job is None:
            continue
        jobs.append(job)
        media_breakdown[job.media_type] = media_breakdown.get(job.media_type, 0) + 1
        quality_report = job.result.metadata.get("quality_report") if job.result else None
        if isinstance(quality_report, dict):
            score = quality_report.get("quality_score")
            creative_score = quality_report.get("creative_alignment_score")
            if isinstance(score, (int, float)):
                quality_scores.append(float(score))
            if isinstance(creative_score, (int, float)):
                creative_scores.append(float(creative_score))

    assets = services.asset_repository.list_all(project_id=project_id)

    return ProjectJobsResponse(
        project=_serialize_project(project, services),
        jobs=jobs,
        assets=[_serialize_asset(asset, services) for asset in assets],
        job_count=len(jobs),
        asset_count=len(assets),
        media_breakdown=media_breakdown,
        average_quality_score=(
            round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else None
        ),
        average_creative_alignment_score=(
            round(sum(creative_scores) / len(creative_scores), 1) if creative_scores else None
        ),
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    req: UpdateProjectRequest,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    _sync_assets(services)
    project = _get_project_repo(services).update(
        project_id,
        name=req.name,
        description=req.description,
        status=req.status,
        tags=req.tags,
        metadata=req.metadata,
        pinned_asset_ids=req.pinned_asset_ids,
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _serialize_project(project, services)


@router.post("/{project_id}/jobs/{job_id}", response_model=ProjectResponse)
def add_job_to_project(
    project_id: str,
    job_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    _sync_assets(services)
    repo = _get_project_repo(services)
    job = services.job_repository.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.project_id and job.project_id != project_id:
        repo.remove_job(job.project_id, job_id)
    project = repo.add_job(project_id, job_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    services.job_repository.update_project(job_id, project.id)
    services.asset_repository.bind_job_assets(job_id, project.id)
    return _serialize_project(project, services)


@router.delete("/{project_id}/jobs/{job_id}", response_model=ProjectResponse)
def remove_job_from_project(
    project_id: str,
    job_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    _sync_assets(services)
    repo = _get_project_repo(services)
    project = repo.remove_job(project_id, job_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    services.job_repository.update_project(job_id, None)
    services.asset_repository.bind_job_assets(job_id, None)
    return _serialize_project(project, services)


@router.post("/{project_id}/assets/{asset_id}", response_model=ProjectResponse)
def add_asset_to_project(
    project_id: str,
    asset_id: str,
    services: ApplicationServices = Depends(get_services),
) -> ProjectResponse:
    _sync_assets(services)
    project = _project_or_404(project_id, services)
    asset = services.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")

    if asset.project_id and asset.project_id != project_id:
        previous_project = services.project_repository.get(asset.project_id)
        if previous_project is not None:
            services.project_repository.remove_job(previous_project.id, asset.job_id)

    services.project_repository.add_job(project_id, asset.job_id)
    services.job_repository.update_project(asset.job_id, project_id)
    services.asset_repository.bind_job_assets(asset.job_id, project_id)
    services.asset_repository.bind_project(asset_id, project_id)
    updated_project = _project_or_404(project_id, services)
    return _serialize_project(updated_project, services)


@router.post("/{project_id}/export", response_model=ExportProjectResponse)
def export_project(
    project_id: str,
    req: ExportProjectRequest,
    services: ApplicationServices = Depends(get_services),
) -> ExportProjectResponse:
    _sync_assets(services)
    project = _project_or_404(project_id, services)
    assets = services.asset_repository.list_all(project_id=project_id)
    destination = (
        Path(req.destination_dir)
        if req.destination_dir
        else services.output_dir.parent / "exports" / "projects" / project.id
    )
    exported = services.asset_repository.export_project_bundle(
        project_id=project.id,
        export_root=destination,
        assets=assets,
        project_manifest=project.to_dict(),
    )
    return ExportProjectResponse(
        project_id=project.id,
        bundle_root=exported["bundle_root"],
        manifest_path=exported["manifest_path"],
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    services: ApplicationServices = Depends(get_services),
) -> None:
    _sync_assets(services)
    project = _project_or_404(project_id, services)
    for job_id in list(project.job_ids):
        services.job_repository.update_project(job_id, None)
        services.asset_repository.bind_job_assets(job_id, None)
    _get_project_repo(services).delete(project_id)


def _number_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


__all__ = [
    "CreateProjectRequest",
    "ExportProjectRequest",
    "ExportProjectResponse",
    "ProjectAssetResponse",
    "ProjectJobsResponse",
    "ProjectResponse",
    "UpdateProjectRequest",
    "router",
]
