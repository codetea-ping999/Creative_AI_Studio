"""Operational and quality summary endpoints."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.jobs import JobRecord
from core.quality import (
    build_calibration_records,
    build_calibration_report,
    count_calibration_eligible_jobs,
    count_calibration_eligible_segments,
)
from core.schemas import MediaType
from core.jobs.statuses import (
    JOB_STATUS_FAILED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])

RUNNING_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_PREPARING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_POSTPROCESSING,
}


class MediaMetrics(BaseModel):
    """Per-media operational summary."""

    model_config = ConfigDict(extra="forbid")

    total_jobs: int = 0
    succeeded_jobs: int = 0
    failed_jobs: int = 0
    running_jobs: int = 0
    success_rate: float = 0.0
    save_success_rate: float = 0.0
    average_quality_score: float | None = None
    average_quality_score_calibrated: float | None = None
    average_business_readiness_score: float | None = None
    average_semantic_alignment_score: float | None = None
    average_semantic_alignment_score_calibrated: float | None = None
    average_creative_alignment_score: float | None = None
    average_creative_alignment_score_calibrated: float | None = None
    latest_quality_level: str | None = None
    semantic_scored_jobs: int = 0
    semantic_unavailable_jobs: int = 0
    feedback_total: int = 0
    feedback_coverage_rate: float = 0.0
    average_human_quality_rating: float | None = None
    average_human_semantic_rating: float | None = None
    average_human_creative_rating: float | None = None


class MetricsSummaryResponse(BaseModel):
    """Aggregate operational summary for the local studio."""

    model_config = ConfigDict(extra="forbid")

    total_jobs: int = 0
    succeeded_jobs: int = 0
    failed_jobs: int = 0
    running_jobs: int = 0
    success_rate: float = 0.0
    save_success_rate: float = 0.0
    average_quality_score: float | None = None
    average_quality_score_calibrated: float | None = None
    average_business_readiness_score: float | None = None
    average_semantic_alignment_score: float | None = None
    average_semantic_alignment_score_calibrated: float | None = None
    average_creative_alignment_score: float | None = None
    average_creative_alignment_score_calibrated: float | None = None
    latest_quality_level: str | None = None
    semantic_scored_jobs: int = 0
    semantic_unavailable_jobs: int = 0
    recent_window_size: int = 0
    recent_success_rate: float = 0.0
    recent_average_quality_score: float | None = None
    feedback_total: int = 0
    feedback_coverage_rate: float = 0.0
    average_human_quality_rating: float | None = None
    average_human_semantic_rating: float | None = None
    average_human_creative_rating: float | None = None
    by_media: dict[str, MediaMetrics] = Field(default_factory=dict)


class CalibrationAgreementMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paired_count: int = 0
    pearson_correlation: float | None = None
    mae: float | None = None
    mean_bias: float | None = None


class CalibrationSegmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_count: int = 0
    eligible_job_count: int = 0
    coverage_rate: float = 0.0
    minimum_sample_count: int = 0
    recommendation_status: str
    metrics: dict[str, CalibrationAgreementMetrics] = Field(default_factory=dict)


class CalibrationReportResponse(CalibrationSegmentResponse):
    segment_minimum_sample_count: int = 10
    by_media: dict[str, CalibrationSegmentResponse] = Field(default_factory=dict)
    by_model: dict[str, CalibrationSegmentResponse] = Field(default_factory=dict)
    automatic_updates_applied: bool = False


@router.get("/summary", response_model=MetricsSummaryResponse)
def get_metrics_summary(
    services: ApplicationServices = Depends(get_services),
    window_size: int = Query(default=20, ge=1, le=200),
) -> MetricsSummaryResponse:
    jobs = services.job_service.list_jobs()
    recent_jobs = jobs[:window_size]
    feedback_by_job = _group_feedback_by_job(services.feedback_repository.list_all())
    summary = _summarize_jobs(jobs, feedback_by_job)
    summary["recent_window_size"] = len(recent_jobs)
    summary["recent_success_rate"] = _ratio(
        sum(1 for job in recent_jobs if job.status == JOB_STATUS_SUCCEEDED),
        len(recent_jobs),
    )
    summary["recent_average_quality_score"] = _average(
        _quality_metric(job, "quality_score") for job in recent_jobs
    )
    return MetricsSummaryResponse.model_validate(summary)


@router.get("/calibration", response_model=CalibrationReportResponse)
def get_calibration_report(
    services: ApplicationServices = Depends(get_services),
    media_type: MediaType | None = Query(default=None),
    model_id: str | None = Query(default=None, min_length=1),
) -> CalibrationReportResponse:
    jobs = [
        job
        for job in services.job_service.list_jobs()
        if (media_type is None or job.media_type == media_type)
        and (model_id is None or job.request.model_id == model_id)
    ]
    records = build_calibration_records(
        jobs,
        services.asset_repository.list_all(),
        services.feedback_repository.list_all(),
    )
    report = build_calibration_report(
        records,
        eligible_job_count=count_calibration_eligible_jobs(jobs),
        eligible_segments=count_calibration_eligible_segments(jobs),
    )
    return CalibrationReportResponse.model_validate(report)


def _summarize_jobs(
    jobs: list[JobRecord],
    feedback_by_job: dict[str, list[object]],
) -> dict[str, object]:
    by_media_jobs: dict[str, list[JobRecord]] = defaultdict(list)
    for job in jobs:
        by_media_jobs[job.media_type].append(job)

    summary = _build_metrics(jobs, feedback_by_job)
    summary["by_media"] = {
        media_type: MediaMetrics.model_validate(_build_metrics(media_jobs, feedback_by_job))
        for media_type, media_jobs in sorted(by_media_jobs.items())
    }
    return summary


def _build_metrics(
    jobs: list[JobRecord],
    feedback_by_job: dict[str, list[object]],
) -> dict[str, object]:
    succeeded_jobs = [job for job in jobs if job.status == JOB_STATUS_SUCCEEDED]
    feedbacks = [feedback for job in jobs for feedback in feedback_by_job.get(job.id, [])]
    semantic_statuses = [_semantic_status(job) for job in jobs]
    return {
        "total_jobs": len(jobs),
        "succeeded_jobs": len(succeeded_jobs),
        "failed_jobs": sum(1 for job in jobs if job.status == JOB_STATUS_FAILED),
        "running_jobs": sum(1 for job in jobs if job.status in RUNNING_JOB_STATUSES),
        "success_rate": _ratio(len(succeeded_jobs), len(jobs)),
        "save_success_rate": _ratio(
            sum(1 for job in succeeded_jobs if _is_saved(job)),
            len(succeeded_jobs),
        ),
        "average_quality_score": _average(
            _quality_metric(job, "quality_score") for job in jobs
        ),
        "average_quality_score_calibrated": _average(
            _calibrated_quality_metric(job, feedback_by_job, "quality_score_calibrated")
            for job in jobs
        ),
        "average_business_readiness_score": _average(
            _quality_metric(job, "business_readiness_score") for job in jobs
        ),
        "average_semantic_alignment_score": _average(
            _quality_metric(job, "semantic_alignment_score") for job in jobs
        ),
        "average_semantic_alignment_score_calibrated": _average(
            _calibrated_quality_metric(
                job,
                feedback_by_job,
                "semantic_alignment_score_calibrated",
            )
            for job in jobs
        ),
        "average_creative_alignment_score": _average(
            _quality_metric(job, "creative_alignment_score") for job in jobs
        ),
        "average_creative_alignment_score_calibrated": _average(
            _calibrated_quality_metric(
                job,
                feedback_by_job,
                "creative_alignment_score_calibrated",
            )
            for job in jobs
        ),
        "latest_quality_level": next(
            (
                level
                for job in jobs
                if (level := _quality_level(job)) is not None
            ),
            None,
        ),
        "semantic_scored_jobs": sum(1 for status_value in semantic_statuses if status_value == "scored"),
        "semantic_unavailable_jobs": sum(
            1 for status_value in semantic_statuses if status_value == "unavailable"
        ),
        "feedback_total": len(feedbacks),
        "feedback_coverage_rate": _ratio(
            len({feedback.job_id for feedback in feedbacks}),
            len(jobs),
        ),
        "average_human_quality_rating": _average(
            getattr(feedback, "quality_rating", None) for feedback in feedbacks
        ),
        "average_human_semantic_rating": _average(
            getattr(feedback, "semantic_rating", None) for feedback in feedbacks
        ),
        "average_human_creative_rating": _average(
            getattr(feedback, "creative_rating", None) for feedback in feedbacks
        ),
    }


def _group_feedback_by_job(feedbacks: list[object]) -> dict[str, list[object]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for feedback in feedbacks:
        job_id = getattr(feedback, "job_id", None)
        if isinstance(job_id, str):
            grouped[job_id].append(feedback)
    return grouped


def _quality_report(job: JobRecord) -> dict[str, object] | None:
    if job.result is None:
        return None
    quality = job.result.metadata.get("quality_report")
    return quality if isinstance(quality, dict) else None


def _quality_metric(job: JobRecord, key: str) -> float | None:
    quality = _quality_report(job)
    if quality is None:
        return None
    score = quality.get(key)
    return float(score) if isinstance(score, (int, float)) else None


def _calibrated_quality_metric(
    job: JobRecord,
    feedback_by_job: dict[str, list[object]],
    key: str,
) -> float | None:
    from core.feedback import rating_to_score
    from core.quality import calibrate_quality_report

    quality = _quality_report(job)
    if quality is None:
        return None
    feedbacks = feedback_by_job.get(job.id, [])
    if not feedbacks:
        return _quality_metric(job, key.removesuffix("_calibrated"))

    human_summary = {
        "total_feedback": len(feedbacks),
        "human_quality_score": _average(
            rating_to_score(getattr(feedback, "quality_rating", None))
            for feedback in feedbacks
        ),
        "human_semantic_alignment_score": _average(
            rating_to_score(getattr(feedback, "semantic_rating", None))
            for feedback in feedbacks
        ),
        "human_creative_alignment_score": _average(
            rating_to_score(getattr(feedback, "creative_rating", None))
            for feedback in feedbacks
        ),
    }
    score = calibrate_quality_report(dict(quality), human_summary).get(key)
    return float(score) if isinstance(score, (int, float)) else None


def _quality_level(job: JobRecord) -> str | None:
    quality = _quality_report(job)
    if quality is None:
        return None
    level = quality.get("quality_level")
    return str(level) if isinstance(level, str) else None


def _semantic_status(job: JobRecord) -> str | None:
    quality = _quality_report(job)
    if quality is None:
        return None
    semantic_report = quality.get("semantic_report")
    if not isinstance(semantic_report, dict):
        return None
    status_value = semantic_report.get("status")
    return str(status_value) if isinstance(status_value, str) else None


def _is_saved(job: JobRecord) -> bool:
    if job.status != JOB_STATUS_SUCCEEDED or job.result is None or not job.result.outputs:
        return False
    return all(Path(output).exists() for output in job.result.outputs)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def _average(values: Iterable[float | int | None]) -> float | None:
    normalized = [float(value) for value in values if isinstance(value, (int, float))]
    if not normalized:
        return None
    return round(sum(normalized) / len(normalized), 1)


__all__ = [
    "CalibrationReportResponse",
    "MediaMetrics",
    "MetricsSummaryResponse",
    "router",
]
