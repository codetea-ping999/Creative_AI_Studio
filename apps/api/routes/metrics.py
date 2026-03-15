"""Operational and quality summary endpoints."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_job_service
from core.jobs import JobRecord, JobService
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
    average_business_readiness_score: float | None = None
    average_semantic_alignment_score: float | None = None
    latest_quality_level: str | None = None


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
    average_business_readiness_score: float | None = None
    average_semantic_alignment_score: float | None = None
    latest_quality_level: str | None = None
    recent_window_size: int = 0
    recent_success_rate: float = 0.0
    recent_average_quality_score: float | None = None
    by_media: dict[str, MediaMetrics] = Field(default_factory=dict)


@router.get("/summary", response_model=MetricsSummaryResponse)
def get_metrics_summary(
    job_service: JobService = Depends(get_job_service),
    window_size: int = Query(default=20, ge=1, le=200),
) -> MetricsSummaryResponse:
    jobs = job_service.list_jobs()
    recent_jobs = jobs[:window_size]
    summary = _summarize_jobs(jobs)
    summary["recent_window_size"] = len(recent_jobs)
    summary["recent_success_rate"] = _ratio(
        sum(1 for job in recent_jobs if job.status == "succeeded"),
        len(recent_jobs),
    )
    summary["recent_average_quality_score"] = _average(
        _quality_metric(job, "quality_score") for job in recent_jobs
    )
    return MetricsSummaryResponse.model_validate(summary)


def _summarize_jobs(jobs: list[JobRecord]) -> dict[str, object]:
    by_media_jobs: dict[str, list[JobRecord]] = defaultdict(list)
    for job in jobs:
        by_media_jobs[job.media_type].append(job)

    summary = _build_metrics(jobs)
    summary["by_media"] = {
        media_type: MediaMetrics.model_validate(_build_metrics(media_jobs))
        for media_type, media_jobs in sorted(by_media_jobs.items())
    }
    return summary


def _summarize_media(jobs: list[JobRecord]) -> dict[str, object]:
    return _build_metrics(jobs)


def _build_metrics(jobs: list[JobRecord]) -> dict[str, object]:
    succeeded_jobs = [job for job in jobs if job.status == JOB_STATUS_SUCCEEDED]
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
        "average_business_readiness_score": _average(
            _quality_metric(job, "business_readiness_score") for job in jobs
        ),
        "average_semantic_alignment_score": _average(
            _quality_metric(job, "semantic_alignment_score") for job in jobs
        ),
        "latest_quality_level": next(
            (
                level
                for job in jobs
                if (level := _quality_level(job)) is not None
            ),
            None,
        ),
    }


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


def _quality_level(job: JobRecord) -> str | None:
    quality = _quality_report(job)
    if quality is None:
        return None
    level = quality.get("quality_level")
    return str(level) if isinstance(level, str) else None


def _is_saved(job: JobRecord) -> bool:
    if job.status != JOB_STATUS_SUCCEEDED or job.result is None or not job.result.outputs:
        return False
    return all(Path(output).exists() for output in job.result.outputs)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def _average(values: Iterable[float | None]) -> float | None:
    normalized = [float(value) for value in values if isinstance(value, (int, float))]
    if not normalized:
        return None
    return round(sum(normalized) / len(normalized), 1)


__all__ = ["MediaMetrics", "MetricsSummaryResponse", "router"]
