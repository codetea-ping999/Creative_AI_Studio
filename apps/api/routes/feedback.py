"""Feedback API endpoints for user evaluation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.feedback import FeedbackRepository

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackResponse(BaseModel):
    """Feedback representation."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    asset_id: str | None = None
    project_id: str | None = None
    quality_rating: int = Field(ge=1, le=5)
    semantic_rating: int | None = Field(default=None, ge=1, le=5)
    creative_rating: int | None = Field(default=None, ge=1, le=5)
    reuse_intent: bool | None = None
    export_ready: bool | None = None
    issue_tags: list[str] = Field(default_factory=list)
    comments: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CreateFeedbackRequest(BaseModel):
    """Request to create feedback."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    asset_id: str | None = None
    project_id: str | None = None
    quality_rating: int = Field(ge=1, le=5)
    semantic_rating: int | None = Field(default=None, ge=1, le=5)
    creative_rating: int | None = Field(default=None, ge=1, le=5)
    reuse_intent: bool | None = None
    export_ready: bool | None = None
    issue_tags: list[str] = Field(default_factory=list)
    comments: str = Field(default="", max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackSummaryResponse(BaseModel):
    """Aggregate feedback statistics."""

    model_config = ConfigDict(extra="forbid")

    total_feedback: int = Field(ge=0)
    average_quality_rating: float | None = None
    average_semantic_rating: float | None = None
    average_creative_rating: float | None = None
    comment_count: int = Field(ge=0)
    export_ready_rate: float | None = None
    reuse_intent_rate: float | None = None
    issue_tag_counts: dict[str, int] = Field(default_factory=dict)
    human_quality_score: float | None = None
    human_semantic_alignment_score: float | None = None
    human_creative_alignment_score: float | None = None
    latest_feedback_at: datetime | None = None


def _get_feedback_repo(services: ApplicationServices) -> FeedbackRepository:
    return services.feedback_repository


def _serialize_feedback(feedback) -> FeedbackResponse:
    return FeedbackResponse(
        id=feedback.id,
        job_id=feedback.job_id,
        asset_id=feedback.asset_id,
        project_id=feedback.project_id,
        quality_rating=feedback.quality_rating,
        semantic_rating=feedback.semantic_rating,
        creative_rating=feedback.creative_rating,
        reuse_intent=feedback.reuse_intent,
        export_ready=feedback.export_ready,
        issue_tags=list(feedback.issue_tags),
        comments=feedback.comments,
        metadata=dict(feedback.metadata),
        created_at=feedback.created_at,
    )


@router.post("", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
def submit_feedback(
    req: CreateFeedbackRequest,
    services: ApplicationServices = Depends(get_services),
) -> FeedbackResponse:
    if services.job_repository.get(req.job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if req.asset_id is not None and services.asset_repository.get(req.asset_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if req.project_id is not None and services.project_repository.get(req.project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    repo = _get_feedback_repo(services)
    feedback = repo.create(
        job_id=req.job_id,
        asset_id=req.asset_id,
        project_id=req.project_id,
        quality_rating=req.quality_rating,
        semantic_rating=req.semantic_rating,
        creative_rating=req.creative_rating,
        reuse_intent=req.reuse_intent,
        export_ready=req.export_ready,
        issue_tags=req.issue_tags,
        comments=req.comments,
        metadata=req.metadata,
    )

    return _serialize_feedback(feedback)


@router.get("/job/{job_id}", response_model=list[FeedbackResponse])
def get_job_feedback(
    job_id: str = PathParam(min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> list[FeedbackResponse]:
    repo = _get_feedback_repo(services)
    return [_serialize_feedback(item) for item in repo.list_by_job(job_id)]


@router.get("/asset/{asset_id}", response_model=list[FeedbackResponse])
def get_asset_feedback(
    asset_id: str = PathParam(min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> list[FeedbackResponse]:
    if services.asset_repository.get(asset_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    repo = _get_feedback_repo(services)
    return [_serialize_feedback(item) for item in repo.list_by_asset(asset_id)]


@router.get("", response_model=list[FeedbackResponse])
def list_all_feedback(
    asset_id: str | None = Query(default=None, min_length=1),
    project_id: str | None = Query(default=None, min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> list[FeedbackResponse]:
    repo = _get_feedback_repo(services)
    if asset_id is not None:
        return [_serialize_feedback(item) for item in repo.list_by_asset(asset_id)]
    if project_id is not None:
        return [_serialize_feedback(item) for item in repo.list_by_project(project_id)]
    return [_serialize_feedback(item) for item in repo.list_all()]


@router.get("/summary", response_model=FeedbackSummaryResponse)
def get_feedback_summary(
    job_id: str | None = Query(default=None),
    asset_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    services: ApplicationServices = Depends(get_services),
) -> FeedbackSummaryResponse:
    if job_id is not None and services.job_repository.get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if asset_id is not None and services.asset_repository.get(asset_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if project_id is not None and services.project_repository.get(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    repo = _get_feedback_repo(services)
    return FeedbackSummaryResponse.model_validate(
        repo.summarize(job_id=job_id, asset_id=asset_id, project_id=project_id)
    )


@router.delete("/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feedback(
    feedback_id: str = PathParam(min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> None:
    repo = _get_feedback_repo(services)
    if not repo.delete(feedback_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")


__all__ = [
    "CreateFeedbackRequest",
    "FeedbackResponse",
    "FeedbackSummaryResponse",
    "router",
]
