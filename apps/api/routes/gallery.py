"""Gallery endpoints for browsing generated outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.jobs import JobService

router = APIRouter(prefix="/gallery", tags=["gallery"])


@dataclass(slots=True)
class GalleryItem:
    """A single generated output for display."""

    job_id: str
    project_id: str | None
    media_type: str
    prompt: str
    model_id: str
    output_path: str
    preview_path: str | None
    created_at: datetime
    quality_score: float | None = None
    quality_level: str | None = None
    success: bool = True


class GalleryItemResponse(BaseModel):
    """Gallery item as returned by the API."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    project_id: str | None = None
    media_type: str = Field(min_length=1)
    prompt: str
    model_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    preview_path: str | None = None
    created_at: datetime
    quality_score: float | None = None
    quality_level: str | None = None
    success: bool = True


class GalleryStatsResponse(BaseModel):
    """Gallery statistics."""

    model_config = ConfigDict(extra="forbid")

    total_items: int = Field(ge=0)
    total_by_media_type: dict[str, int] = Field(default_factory=dict)
    total_by_project: dict[str, int] = Field(default_factory=dict)
    average_quality_score: float | None = None


def _build_gallery_items(
    services: ApplicationServices,
    media_type: str | None = None,
    project_id: str | None = None,
    query_text: str | None = None,
    limit: int = 50,
) -> list[GalleryItem]:
    """Build gallery items from job records and output files."""

    jobs = services.job_service.list_jobs()
    normalized_query = query_text.strip().lower() if query_text else None

    items = []
    for job in jobs:
        # Filter by media type if specified
        if media_type and job.media_type != media_type:
            continue
        if project_id and job.project_id != project_id:
            continue

        # Only include succeeded jobs with output
        if job.status != "succeeded" or job.result is None:
            continue

        output_path = next((output for output in job.result.outputs if output), None)
        if not output_path:
            continue

        if normalized_query:
            search_blob = " ".join(
                [
                    job.request.prompt,
                    job.request.model_id,
                    output_path,
                    job.project_id or "",
                ]
            ).lower()
            if normalized_query not in search_blob:
                continue

        # Verify output file exists.
        output_file = Path(output_path)
        if not output_file.exists():
            continue

        quality_report = job.result.metadata.get("quality_report")
        quality_score = None
        quality_level = None
        if isinstance(quality_report, dict):
            raw_score = quality_report.get("quality_score")
            raw_level = quality_report.get("quality_level")
            if isinstance(raw_score, (int, float)):
                quality_score = float(raw_score)
            if isinstance(raw_level, str):
                quality_level = raw_level

        # Build item
        item = GalleryItem(
            job_id=job.id,
            project_id=job.project_id,
            media_type=job.media_type,
            prompt=job.request.prompt,
            model_id=job.request.model_id or "default",
            output_path=str(output_path),
            preview_path=next((preview for preview in job.result.previews if preview), None),
            created_at=job.created_at,
            quality_score=quality_score,
            quality_level=quality_level,
            success=True,
        )
        items.append(item)

    # Sort by creation date, newest first
    items.sort(key=lambda x: x.created_at, reverse=True)

    # Limit results
    return items[:limit]


@router.get("", response_model=list[GalleryItemResponse])
def list_gallery(
    media_type: str | None = Query(None, min_length=1),
    project_id: str | None = Query(None, min_length=1),
    q: str | None = Query(None, min_length=1),
    limit: int = Query(50, ge=1, le=200),
    services: ApplicationServices = Depends(get_services),
) -> list[GalleryItemResponse]:
    """List gallery items, optionally filtered by media type."""

    items = _build_gallery_items(
        services,
        media_type=media_type,
        project_id=project_id,
        query_text=q,
        limit=limit,
    )
    return [
        GalleryItemResponse(
            job_id=item.job_id,
            project_id=item.project_id,
            media_type=item.media_type,
            prompt=item.prompt,
            model_id=item.model_id,
            output_path=item.output_path,
            preview_path=item.preview_path,
            created_at=item.created_at,
            quality_score=item.quality_score,
            quality_level=item.quality_level,
            success=item.success,
        )
        for item in items
    ]


@router.get("/stats", response_model=GalleryStatsResponse)
def get_gallery_stats(
    services: ApplicationServices = Depends(get_services),
) -> GalleryStatsResponse:
    """Get gallery statistics."""

    items = _build_gallery_items(services, limit=10000)

    total_items = len(items)
    total_by_media_type: dict[str, int] = {}
    total_by_project: dict[str, int] = {}
    quality_scores: list[float] = []

    for item in items:
        # Count by media type
        total_by_media_type[item.media_type] = total_by_media_type.get(item.media_type, 0) + 1
        project_key = item.project_id or "unassigned"
        total_by_project[project_key] = total_by_project.get(project_key, 0) + 1

        # Collect quality scores
        if item.quality_score is not None:
            quality_scores.append(item.quality_score)

    average_quality = sum(quality_scores) / len(quality_scores) if quality_scores else None

    return GalleryStatsResponse(
        total_items=total_items,
        total_by_media_type=total_by_media_type,
        total_by_project=total_by_project,
        average_quality_score=average_quality,
    )


__all__ = ["router", "GalleryItemResponse", "GalleryStatsResponse"]
