"""Feedback API endpoints for user evaluation."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, status
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
    quality_rating: int = Field(ge=1, le=5)
    semantic_rating: int | None = Field(default=None, ge=1, le=5)
    comments: str = ""
    created_at: datetime


class CreateFeedbackRequest(BaseModel):
    """Request to create feedback."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    quality_rating: int = Field(ge=1, le=5)
    semantic_rating: int | None = Field(default=None, ge=1, le=5)
    comments: str = Field(default="", max_length=1000)


class FeedbackSummaryResponse(BaseModel):
    """Aggregate feedback statistics."""

    model_config = ConfigDict(extra="forbid")

    total_feedback: int = Field(ge=0)
    average_quality_rating: float | None = None
    average_semantic_rating: float | None = None
    comment_count: int = Field(ge=0)
    latest_feedback_at: datetime | None = None


def _get_feedback_repo(services: ApplicationServices) -> FeedbackRepository:
    """Get or create the feedback repository."""
    return services.feedback_repository


@router.post("", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
def submit_feedback(
    req: CreateFeedbackRequest,
    services: ApplicationServices = Depends(get_services),
) -> FeedbackResponse:
    """Submit feedback for a job."""

    if services.job_repository.get(req.job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    repo = _get_feedback_repo(services)
    feedback = repo.create(
        job_id=req.job_id,
        quality_rating=req.quality_rating,
        semantic_rating=req.semantic_rating,
        comments=req.comments,
    )

    return FeedbackResponse(
        id=feedback.id,
        job_id=feedback.job_id,
        quality_rating=feedback.quality_rating,
        semantic_rating=feedback.semantic_rating,
        comments=feedback.comments,
        created_at=feedback.created_at,
    )


@router.get("/job/{job_id}", response_model=list[FeedbackResponse])
def get_job_feedback(
    job_id: str = PathParam(min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> list[FeedbackResponse]:
    """Get all feedback for a specific job."""

    repo = _get_feedback_repo(services)
    feedbacks = repo.list_by_job(job_id)

    return [
        FeedbackResponse(
            id=f.id,
            job_id=f.job_id,
            quality_rating=f.quality_rating,
            semantic_rating=f.semantic_rating,
            comments=f.comments,
            created_at=f.created_at,
        )
        for f in feedbacks
    ]


@router.get("", response_model=list[FeedbackResponse])
def list_all_feedback(
    services: ApplicationServices = Depends(get_services),
) -> list[FeedbackResponse]:
    """List all feedback (for analytics)."""

    repo = _get_feedback_repo(services)
    feedbacks = repo.list_all()

    return [
        FeedbackResponse(
            id=f.id,
            job_id=f.job_id,
            quality_rating=f.quality_rating,
            semantic_rating=f.semantic_rating,
            comments=f.comments,
            created_at=f.created_at,
        )
        for f in feedbacks
    ]


@router.get("/summary", response_model=FeedbackSummaryResponse)
def get_feedback_summary(
    job_id: str | None = None,
    services: ApplicationServices = Depends(get_services),
) -> FeedbackSummaryResponse:
    """Summarize feedback, optionally for a specific job."""

    if job_id is not None and services.job_repository.get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    repo = _get_feedback_repo(services)
    return FeedbackSummaryResponse.model_validate(repo.summarize(job_id=job_id))


@router.delete("/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feedback(
    feedback_id: str = PathParam(min_length=1),
    services: ApplicationServices = Depends(get_services),
) -> None:
    """Delete feedback."""

    repo = _get_feedback_repo(services)
    if not repo.delete(feedback_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")


__all__ = [
    "router",
    "FeedbackResponse",
    "CreateFeedbackRequest",
    "FeedbackSummaryResponse",
]
