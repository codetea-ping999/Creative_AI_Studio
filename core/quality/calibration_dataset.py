"""Deterministic dataset and correlation reports for human quality feedback."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
import math
from typing import Any

from core.assets import Asset
from core.feedback import Feedback, rating_to_score
from core.jobs import JobRecord

from .calibration import calibrate_quality_report

SCORE_DIMENSIONS = {
    "quality": ("quality_score", "quality_score_calibrated", "quality_rating"),
    "semantic": (
        "semantic_alignment_score",
        "semantic_alignment_score_calibrated",
        "semantic_rating",
    ),
    "creative": (
        "creative_alignment_score",
        "creative_alignment_score_calibrated",
        "creative_rating",
    ),
    "creative_vs_quality": (
        "quality_score",
        "quality_score_calibrated",
        "creative_rating",
    ),
    "creative_vs_semantic": (
        "semantic_alignment_score",
        "semantic_alignment_score_calibrated",
        "creative_rating",
    ),
}


def build_calibration_records(
    jobs: list[JobRecord],
    assets: list[Asset],
    feedbacks: list[Feedback],
) -> list[dict[str, Any]]:
    """Join quality reports, assets, and feedback into one record per rated job."""

    assets_by_job: dict[str, list[Asset]] = defaultdict(list)
    for asset in assets:
        assets_by_job[asset.job_id].append(asset)
    feedback_by_job: dict[str, list[Feedback]] = defaultdict(list)
    for feedback in feedbacks:
        feedback_by_job[feedback.job_id].append(feedback)

    records: list[dict[str, Any]] = []
    for job in sorted(jobs, key=lambda item: (item.created_at.isoformat(), item.id)):
        quality_report = _quality_report(job)
        job_feedback = sorted(
            feedback_by_job.get(job.id, []),
            key=lambda item: (item.created_at.isoformat(), item.id),
        )
        if quality_report is None or not job_feedback:
            continue
        job_assets = sorted(assets_by_job.get(job.id, []), key=lambda item: item.id)
        human_summary = _human_summary(job_feedback)
        calibrated = calibrate_quality_report(quality_report, human_summary)
        records.append(
            {
                "job_id": job.id,
                "asset_ids": [asset.id for asset in job_assets],
                "project_id": job.project_id,
                "media_type": job.media_type,
                "model_id": job.request.model_id,
                "created_at": job.created_at.isoformat(),
                "feedback_ids": [feedback.id for feedback in job_feedback],
                "feedback_count": len(job_feedback),
                "issue_tags": sorted(
                    {tag for feedback in job_feedback for tag in feedback.issue_tags}
                ),
                "reuse_intent_rate": _boolean_rate(
                    feedback.reuse_intent for feedback in job_feedback
                ),
                "export_ready_rate": _boolean_rate(
                    feedback.export_ready for feedback in job_feedback
                ),
                "reuse_count": sum(
                    int(asset.metadata.get("reuse_count", 0)) for asset in job_assets
                ),
                "export_count": sum(len(asset.export_paths) for asset in job_assets),
                "quality_score": _number(quality_report.get("quality_score")),
                "semantic_alignment_score": _number(
                    quality_report.get("semantic_alignment_score")
                ),
                "creative_alignment_score": _number(
                    quality_report.get("creative_alignment_score")
                ),
                "quality_score_calibrated": _number(
                    calibrated.get("quality_score_calibrated")
                ),
                "semantic_alignment_score_calibrated": _number(
                    calibrated.get("semantic_alignment_score_calibrated")
                ),
                "creative_alignment_score_calibrated": _number(
                    calibrated.get("creative_alignment_score_calibrated")
                ),
                "human_quality_score": human_summary["human_quality_score"],
                "human_semantic_alignment_score": human_summary[
                    "human_semantic_alignment_score"
                ],
                "human_creative_alignment_score": human_summary[
                    "human_creative_alignment_score"
                ],
            }
        )
    return records


def count_calibration_eligible_jobs(jobs: list[JobRecord]) -> int:
    return sum(1 for job in jobs if _quality_report(job) is not None)


def count_calibration_eligible_segments(jobs: list[JobRecord]) -> dict[str, dict[str, int]]:
    by_media: dict[str, int] = defaultdict(int)
    by_model: dict[str, int] = defaultdict(int)
    for job in jobs:
        if _quality_report(job) is None:
            continue
        by_media[job.media_type] += 1
        by_model[job.request.model_id] += 1
    return {
        "by_media": dict(sorted(by_media.items())),
        "by_model": dict(sorted(by_model.items())),
    }


def build_calibration_report(
    records: list[dict[str, Any]],
    *,
    eligible_job_count: int,
    eligible_segments: dict[str, dict[str, int]] | None = None,
    minimum_sample_count: int = 20,
    segment_minimum_sample_count: int = 10,
) -> dict[str, Any]:
    """Summarize agreement without applying any automatic score changes."""

    return {
        **_summarize_records(
            records,
            eligible_job_count=eligible_job_count,
            minimum_sample_count=minimum_sample_count,
        ),
        "segment_minimum_sample_count": segment_minimum_sample_count,
        "by_media": _segment_reports(
            records,
            "media_type",
            segment_minimum_sample_count,
            (eligible_segments or {}).get("by_media", {}),
        ),
        "by_model": _segment_reports(
            records,
            "model_id",
            segment_minimum_sample_count,
            (eligible_segments or {}).get("by_model", {}),
        ),
        "automatic_updates_applied": False,
    }


def _segment_reports(
    records: list[dict[str, Any]],
    key: str,
    minimum_sample_count: int,
    eligible_counts: dict[str, int],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        value = record.get(key)
        if isinstance(value, str) and value:
            grouped[value].append(record)
    all_names = sorted(set(grouped) | set(eligible_counts))
    return {
        name: _summarize_records(
            grouped.get(name, []),
            eligible_job_count=eligible_counts.get(name, len(grouped.get(name, []))),
            minimum_sample_count=minimum_sample_count,
        )
        for name in all_names
    }


def _summarize_records(
    records: list[dict[str, Any]],
    *,
    eligible_job_count: int,
    minimum_sample_count: int,
) -> dict[str, Any]:
    sample_count = len(records)
    return {
        "sample_count": sample_count,
        "eligible_job_count": eligible_job_count,
        "coverage_rate": _percentage(sample_count, eligible_job_count),
        "minimum_sample_count": minimum_sample_count,
        "recommendation_status": (
            "review_recommended" if sample_count >= minimum_sample_count else "insufficient_data"
        ),
        "metrics": {
            dimension: _agreement_metrics(records, auto_key, human_key)
            for dimension, (auto_key, _calibrated_key, rating_key) in SCORE_DIMENSIONS.items()
            for human_key in [_human_score_key(rating_key)]
        },
    }


def _agreement_metrics(
    records: list[dict[str, Any]],
    auto_key: str,
    human_key: str,
) -> dict[str, Any]:
    pairs = [
        (float(auto), float(human))
        for record in records
        if (auto := record.get(auto_key)) is not None
        and (human := record.get(human_key)) is not None
        and isinstance(auto, (int, float))
        and isinstance(human, (int, float))
    ]
    if not pairs:
        return {"paired_count": 0, "pearson_correlation": None, "mae": None, "mean_bias": None}
    differences = [auto - human for auto, human in pairs]
    return {
        "paired_count": len(pairs),
        "pearson_correlation": _pearson(pairs),
        "mae": round(sum(abs(value) for value in differences) / len(differences), 3),
        "mean_bias": round(sum(differences) / len(differences), 3),
    }


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left_mean = sum(left for left, _right in pairs) / len(pairs)
    right_mean = sum(right for _left, right in pairs) / len(pairs)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_variance = sum((left - left_mean) ** 2 for left, _right in pairs)
    right_variance = sum((right - right_mean) ** 2 for _left, right in pairs)
    denominator = math.sqrt(left_variance * right_variance)
    return None if denominator == 0 else round(numerator / denominator, 4)


def _quality_report(job: JobRecord) -> dict[str, Any] | None:
    if job.result is None:
        return None
    report = job.result.metadata.get("quality_report")
    return dict(report) if isinstance(report, dict) else None


def _human_summary(feedbacks: list[Feedback]) -> dict[str, Any]:
    return {
        "total_feedback": len(feedbacks),
        "human_quality_score": _average(
            rating_to_score(feedback.quality_rating) for feedback in feedbacks
        ),
        "human_semantic_alignment_score": _average(
            rating_to_score(feedback.semantic_rating) for feedback in feedbacks
        ),
        "human_creative_alignment_score": _average(
            rating_to_score(feedback.creative_rating) for feedback in feedbacks
        ),
    }


def _human_score_key(rating_key: str) -> str:
    return {
        "quality_rating": "human_quality_score",
        "semantic_rating": "human_semantic_alignment_score",
        "creative_rating": "human_creative_alignment_score",
    }[rating_key]


def _number(value: object) -> float | None:
    return round(float(value), 3) if isinstance(value, (int, float)) else None


def _average(values: Iterable[float | None]) -> float | None:
    normalized = [float(value) for value in values if isinstance(value, (int, float))]
    return None if not normalized else round(sum(normalized) / len(normalized), 3)


def _boolean_rate(values: Iterable[bool | None]) -> float | None:
    normalized = [value for value in values if isinstance(value, bool)]
    return None if not normalized else _percentage(sum(normalized), len(normalized))


def _percentage(numerator: int, denominator: int) -> float:
    return 0.0 if denominator <= 0 else round((numerator / denominator) * 100.0, 1)


__all__ = [
    "build_calibration_records",
    "build_calibration_report",
    "count_calibration_eligible_jobs",
    "count_calibration_eligible_segments",
]
