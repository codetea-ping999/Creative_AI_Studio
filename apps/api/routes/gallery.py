"""Gallery endpoints for browsing and reusing generated outputs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from apps.api.export_paths import resolve_export_dir, sanitize_export_name
from bootstrap import ApplicationServices
from core.assets import Asset
from core.audio_conditioning import inspect_wav_reference
from core.quality import calibrate_quality_report
from core.schemas import GenerationRequest

router = APIRouter(prefix="/gallery", tags=["gallery"])


class GalleryItemResponse(BaseModel):
    """Gallery item summary used by the web UI and export flows."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    project_id: str | None = None
    project_name: str | None = None
    media_type: str = Field(min_length=1)
    prompt: str
    model_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    preview_path: str | None = None
    created_at: datetime
    updated_at: datetime
    quality_score: float | None = None
    quality_level: str | None = None
    semantic_alignment_score: float | None = None
    creative_alignment_score: float | None = None
    quality_score_calibrated: float | None = None
    semantic_alignment_score_calibrated: float | None = None
    creative_alignment_score_calibrated: float | None = None
    feedback_count: int = 0
    average_feedback_quality: float | None = None
    reuse_count: int = 0
    export_count: int = 0
    variation_index: int | None = None
    seed: int | None = None
    success: bool = True


class GalleryAssetDetailResponse(GalleryItemResponse):
    """Detailed gallery asset payload with metadata and feedback."""

    quality_report: dict[str, Any] = Field(default_factory=dict)
    request_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    feedback_summary: dict[str, Any] = Field(default_factory=dict)
    export_paths: list[str] = Field(default_factory=list)
    parent_asset_id: str | None = None
    lineage: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class GalleryStatsResponse(BaseModel):
    """Gallery statistics."""

    model_config = ConfigDict(extra="forbid")

    total_items: int = Field(ge=0)
    total_by_media_type: dict[str, int] = Field(default_factory=dict)
    total_by_project: dict[str, int] = Field(default_factory=dict)
    average_quality_score: float | None = None
    total_reuse_count: int = Field(ge=0)
    total_export_count: int = Field(ge=0)


class ReuseAssetRequest(BaseModel):
    """Reuse an existing asset as the basis for a new generation request."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["variation", "rerun", "melody"] = "rerun"
    prompt: str | None = None
    negative_prompt: str | None = None
    model_id: str | None = None
    seed: int | None = None
    output_format: str | None = None
    project_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ReuseAssetResponse(BaseModel):
    """Response returned when a reused asset spawns a new job."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    project_id: str | None = None


class ExportAssetRequest(BaseModel):
    """Request shape for exporting a gallery asset to a reusable location."""

    model_config = ConfigDict(extra="forbid")

    destination_dir: str | None = None
    destination_name: str | None = None
    include_metadata: bool = True


class ExportAssetResponse(BaseModel):
    """Export result details."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    export_path: str = Field(min_length=1)
    metadata_path: str | None = None


class BindAssetProjectRequest(BaseModel):
    """Bind an asset and its source job to a project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None


def _serialize_gallery_item(
    asset: Asset,
    services: ApplicationServices,
) -> GalleryItemResponse:
    source_job = services.job_repository.get(asset.job_id)
    if source_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source job not found")

    feedback_summary = services.feedback_repository.summarize(asset_id=asset.id)
    quality_report = _extract_quality_report(asset, feedback_summary)
    project = services.project_repository.get(asset.project_id) if asset.project_id else None

    return GalleryItemResponse(
        asset_id=asset.id,
        job_id=asset.job_id,
        project_id=asset.project_id,
        project_name=project.name if project is not None else None,
        media_type=asset.media_type,
        prompt=asset.prompt,
        model_id=asset.model_id or source_job.request.model_id or "default",
        output_path=asset.path,
        preview_path=asset.preview_path,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        quality_score=_number_or_none(quality_report.get("quality_score")),
        quality_level=_string_or_none(quality_report.get("quality_level")),
        semantic_alignment_score=_number_or_none(quality_report.get("semantic_alignment_score")),
        creative_alignment_score=_number_or_none(quality_report.get("creative_alignment_score")),
        quality_score_calibrated=_number_or_none(quality_report.get("quality_score_calibrated")),
        semantic_alignment_score_calibrated=_number_or_none(
            quality_report.get("semantic_alignment_score_calibrated")
        ),
        creative_alignment_score_calibrated=_number_or_none(
            quality_report.get("creative_alignment_score_calibrated")
        ),
        feedback_count=int(feedback_summary.get("total_feedback", 0)),
        average_feedback_quality=_number_or_none(feedback_summary.get("average_quality_rating")),
        reuse_count=int(asset.metadata.get("reuse_count", 0)),
        export_count=len(asset.export_paths),
        variation_index=_integer_or_none(asset.metadata.get("variation_index")),
        seed=_integer_or_none(asset.metadata.get("seed")),
        success=Path(asset.path).exists(),
    )


def _serialize_gallery_detail(
    asset: Asset,
    services: ApplicationServices,
) -> GalleryAssetDetailResponse:
    source_job = services.job_repository.get(asset.job_id)
    if source_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source job not found")

    feedback_summary = services.feedback_repository.summarize(asset_id=asset.id)
    quality_report = _extract_quality_report(asset, feedback_summary)
    summary = _serialize_gallery_item(asset, services)
    request_snapshot = _request_from_asset(asset, source_job)
    return GalleryAssetDetailResponse(
        **summary.model_dump(),
        quality_report=quality_report,
        request_snapshot=request_snapshot.model_dump(mode="json"),
        metadata=dict(asset.metadata),
        feedback_summary=feedback_summary,
        export_paths=list(asset.export_paths),
        parent_asset_id=asset.parent_asset_id,
        lineage=list(asset.lineage),
        tags=list(asset.tags),
    )


def _extract_quality_report(
    asset: Asset,
    feedback_summary: dict[str, Any],
) -> dict[str, Any]:
    quality_report = asset.metadata.get("quality_report")
    if not isinstance(quality_report, dict):
        quality_report = {}
    return calibrate_quality_report(dict(quality_report), feedback_summary)


def _rebind_source_job_project(
    services: ApplicationServices,
    *,
    job_id: str,
    project_id: str | None,
) -> None:
    source_job = services.job_repository.get(job_id)
    if source_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source job not found")

    current_project_id = source_job.project_id
    if current_project_id and current_project_id != project_id:
        services.project_repository.remove_job(current_project_id, job_id)
    if project_id and project_id != current_project_id:
        if services.project_repository.get(project_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        services.project_repository.add_job(project_id, job_id)
    services.job_repository.update_project(job_id, project_id)
    services.asset_repository.bind_job_assets(job_id, project_id)


def _build_reuse_request(
    source_asset: Asset,
    services: ApplicationServices,
    req: ReuseAssetRequest,
) -> tuple[GenerationRequest, str | None]:
    source_job = services.job_repository.get(source_asset.job_id)
    if source_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source job not found")

    project_id = req.project_id if req.project_id is not None else source_asset.project_id
    if project_id is not None and services.project_repository.get(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    source_request = _request_from_asset(source_asset, source_job)
    selected_model_id = (
        req.model_id if req.model_id is not None else source_request.model_id
    )
    if req.action == "melody":
        _validate_melody_reuse(
            source_asset,
            services,
            model_id=selected_model_id,
        )

    next_params = {
        **dict(source_request.params),
        **dict(req.params),
        "source_asset_id": source_asset.id,
        "source_job_id": source_asset.job_id,
        "reference_asset_path": source_asset.path,
        "reuse_action": req.action,
    }
    generation_request = source_request.model_copy(
        update={
            "prompt": req.prompt if req.prompt is not None else source_request.prompt,
            "negative_prompt": (
                req.negative_prompt
                if req.negative_prompt is not None
                else source_request.negative_prompt
            ),
            "model_id": selected_model_id,
            "seed": (
                req.seed
                if req.seed is not None
                else secrets.randbits(63)
                if req.action == "rerun"
                else source_request.seed
            ),
            "output_format": (
                req.output_format
                if req.output_format is not None
                else source_request.output_format
            ),
            "params": next_params,
        }
    )
    return generation_request, project_id


def _request_from_asset(
    source_asset: Asset,
    source_job,
) -> GenerationRequest:
    snapshot = source_asset.metadata.get("request_snapshot")
    if isinstance(snapshot, dict):
        try:
            return GenerationRequest.model_validate(snapshot)
        except (TypeError, ValueError):
            pass
    return source_job.request


def _validate_melody_reuse(
    source_asset: Asset,
    services: ApplicationServices,
    *,
    model_id: str | None,
) -> None:
    """Reject invalid melody requests before a queued job or reuse mark exists."""

    if source_asset.media_type != "audio":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Melody conditioning requires an audio Gallery asset.",
        )
    try:
        manifest = services.model_service.get_manifest(
            model_id,
            media_type="audio",
            task_type="text-to-music",
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if "melody-conditioning" not in manifest.tags:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Model {manifest.public_model_id!r} does not support melody conditioning."
            ),
        )

    reference_minimum = manifest.default_params.get("min_reference_duration_seconds")
    reference_limit = manifest.default_params.get("max_reference_duration_seconds")
    try:
        min_reference_duration_seconds = float(reference_minimum)
        max_reference_duration_seconds = float(reference_limit)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Melody model does not define a valid reference duration limit.",
        ) from exc
    try:
        inspect_wav_reference(
            source_asset.path,
            min_duration_seconds=min_reference_duration_seconds,
            max_duration_seconds=max_reference_duration_seconds,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("", response_model=list[GalleryItemResponse])
def list_gallery(
    media_type: str | None = Query(None, min_length=1),
    project_id: str | None = Query(None, min_length=1),
    q: str | None = Query(None, min_length=1),
    limit: int = Query(50, ge=1, le=200),
    services: ApplicationServices = Depends(get_services),
) -> list[GalleryItemResponse]:
    items = services.asset_repository.list_all(
        media_type=media_type,
        project_id=project_id,
        query_text=q,
        limit=limit,
    )
    return [_serialize_gallery_item(item, services) for item in items]


@router.get("/job/{job_id}", response_model=GalleryAssetDetailResponse)
def get_gallery_item_by_job(
    job_id: str,
    services: ApplicationServices = Depends(get_services),
) -> GalleryAssetDetailResponse:
    asset = services.asset_repository.get_primary_by_job(job_id)
    if asset is None:
        source_job = services.job_repository.get(job_id)
        if source_job is not None and source_job.status == "succeeded":
            # A job status and its JSON asset record live in different stores.
            # Reconcile only this requested job if the status became visible
            # just before JobService finished writing the gallery record.
            services.asset_repository.sync_job(source_job)
            asset = services.asset_repository.get_primary_by_job(job_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gallery asset not found")
    return _serialize_gallery_detail(asset, services)


@router.get("/stats", response_model=GalleryStatsResponse)
def get_gallery_stats(
    services: ApplicationServices = Depends(get_services),
) -> GalleryStatsResponse:
    items = services.asset_repository.list_all()

    total_by_media_type: dict[str, int] = {}
    total_by_project: dict[str, int] = {}
    quality_scores: list[float] = []
    total_reuse_count = 0
    total_export_count = 0

    for asset in items:
        total_by_media_type[asset.media_type] = total_by_media_type.get(asset.media_type, 0) + 1
        project_key = asset.project_id or "unassigned"
        total_by_project[project_key] = total_by_project.get(project_key, 0) + 1
        quality_report = asset.metadata.get("quality_report")
        if isinstance(quality_report, dict):
            score = quality_report.get("quality_score")
            if isinstance(score, (int, float)):
                quality_scores.append(float(score))
        total_reuse_count += int(asset.metadata.get("reuse_count", 0))
        total_export_count += len(asset.export_paths)

    average_quality = round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else None
    return GalleryStatsResponse(
        total_items=len(items),
        total_by_media_type=total_by_media_type,
        total_by_project=total_by_project,
        average_quality_score=average_quality,
        total_reuse_count=total_reuse_count,
        total_export_count=total_export_count,
    )


@router.get("/{asset_id}", response_model=GalleryAssetDetailResponse)
def get_gallery_asset(
    asset_id: str,
    services: ApplicationServices = Depends(get_services),
) -> GalleryAssetDetailResponse:
    asset = services.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gallery asset not found")
    return _serialize_gallery_detail(asset, services)


@router.post(
    "/{asset_id}/reuse",
    response_model=ReuseAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
def reuse_gallery_asset(
    asset_id: str,
    req: ReuseAssetRequest,
    services: ApplicationServices = Depends(get_services),
) -> ReuseAssetResponse:
    source_asset = services.asset_repository.get(asset_id)
    if source_asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gallery asset not found")

    generation_request, project_id = _build_reuse_request(source_asset, services, req)
    job = services.job_service.create_job(generation_request, project_id=project_id)
    if project_id is not None:
        services.project_repository.add_job(project_id, job.id)
    services.asset_repository.mark_reused(
        asset_id,
        action=req.action,
        derived_job_id=job.id,
    )
    return ReuseAssetResponse(
        asset_id=asset_id,
        job_id=job.id,
        status=job.status,
        project_id=project_id,
    )


@router.post("/{asset_id}/export", response_model=ExportAssetResponse)
def export_gallery_asset(
    asset_id: str,
    req: ExportAssetRequest,
    services: ApplicationServices = Depends(get_services),
) -> ExportAssetResponse:
    asset = services.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gallery asset not found")

    destination_dir = resolve_export_dir(
        services,
        req.destination_dir,
        default_subpath=asset.media_type,
    )
    destination_name = sanitize_export_name(req.destination_name)
    exported = services.asset_repository.export_asset(
        asset_id,
        export_root=destination_dir,
        destination_name=destination_name,
        include_metadata=req.include_metadata,
    )
    return ExportAssetResponse(
        asset_id=asset_id,
        export_path=exported["export_path"],
        metadata_path=exported["metadata_path"] or None,
    )


@router.patch("/{asset_id}/project", response_model=GalleryAssetDetailResponse)
def bind_gallery_asset_to_project(
    asset_id: str,
    req: BindAssetProjectRequest,
    services: ApplicationServices = Depends(get_services),
) -> GalleryAssetDetailResponse:
    asset = services.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gallery asset not found")

    _rebind_source_job_project(services, job_id=asset.job_id, project_id=req.project_id)
    rebound_asset = services.asset_repository.bind_project(asset_id, req.project_id)
    if rebound_asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gallery asset not found")
    return _serialize_gallery_detail(rebound_asset, services)


def _number_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _integer_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _string_or_none(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None


__all__ = [
    "BindAssetProjectRequest",
    "ExportAssetRequest",
    "ExportAssetResponse",
    "GalleryAssetDetailResponse",
    "GalleryItemResponse",
    "GalleryStatsResponse",
    "ReuseAssetRequest",
    "ReuseAssetResponse",
    "router",
]
