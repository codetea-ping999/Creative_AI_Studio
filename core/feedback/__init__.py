"""Human feedback collection for quality score refinement and reuse analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any
import uuid

from core.storage.json_files import ensure_utc, utc_now, write_json_atomic


def _normalize_score(value: float | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, min(100.0, float(value))), 1)


def rating_to_score(rating: float | None) -> float | None:
    if rating is None:
        return None
    return _normalize_score((float(rating) / 5.0) * 100.0)


@dataclass(slots=True)
class Feedback:
    """Human feedback on a generated output."""

    id: str
    job_id: str
    asset_id: str | None = None
    project_id: str | None = None
    quality_rating: int = 3
    semantic_rating: int | None = None
    creative_rating: int | None = None
    reuse_intent: bool | None = None
    export_ready: bool | None = None
    issue_tags: list[str] | None = None
    comments: str = ""
    created_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = utc_now()
        if self.issue_tags is None:
            self.issue_tags = []
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "asset_id": self.asset_id,
            "project_id": self.project_id,
            "quality_rating": self.quality_rating,
            "semantic_rating": self.semantic_rating,
            "creative_rating": self.creative_rating,
            "reuse_intent": self.reuse_intent,
            "export_ready": self.export_ready,
            "issue_tags": list(self.issue_tags),
            "comments": self.comments,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }


class FeedbackRepository:
    """Persist and retrieve feedback."""

    def __init__(self, feedback_dir: str | Path = "data/feedback") -> None:
        self.feedback_dir = Path(feedback_dir)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        job_id: str,
        quality_rating: int,
        *,
        asset_id: str | None = None,
        project_id: str | None = None,
        semantic_rating: int | None = None,
        creative_rating: int | None = None,
        reuse_intent: bool | None = None,
        export_ready: bool | None = None,
        issue_tags: list[str] | None = None,
        comments: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Feedback:
        self._validate_rating(quality_rating, "quality_rating")
        self._validate_rating(semantic_rating, "semantic_rating")
        self._validate_rating(creative_rating, "creative_rating")

        feedback = Feedback(
            id=str(uuid.uuid4()),
            job_id=job_id,
            asset_id=asset_id,
            project_id=project_id,
            quality_rating=quality_rating,
            semantic_rating=semantic_rating,
            creative_rating=creative_rating,
            reuse_intent=reuse_intent,
            export_ready=export_ready,
            issue_tags=list(issue_tags or []),
            comments=comments,
            metadata=dict(metadata or {}),
            created_at=utc_now(),
        )
        self._save_feedback(feedback)
        return feedback

    def get(self, feedback_id: str) -> Feedback | None:
        feedback_file = self.feedback_dir / f"{feedback_id}.json"
        if not feedback_file.exists():
            return None
        return self._try_load_feedback(feedback_file)

    def list_by_job(self, job_id: str) -> list[Feedback]:
        return [
            feedback
            for feedback in self.list_all()
            if feedback.job_id == job_id
        ]

    def list_by_asset(self, asset_id: str) -> list[Feedback]:
        return [
            feedback
            for feedback in self.list_all()
            if feedback.asset_id == asset_id
        ]

    def list_by_project(self, project_id: str) -> list[Feedback]:
        return [
            feedback
            for feedback in self.list_all()
            if feedback.project_id == project_id
        ]

    def list_all(self) -> list[Feedback]:
        feedbacks = [
            feedback
            for feedback_file in sorted(self.feedback_dir.glob("*.json"))
            if (feedback := self._try_load_feedback(feedback_file)) is not None
        ]
        feedbacks.sort(key=lambda item: item.created_at, reverse=True)
        return feedbacks

    def summarize(
        self,
        *,
        job_id: str | None = None,
        asset_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if job_id is not None:
            feedbacks = self.list_by_job(job_id)
        elif asset_id is not None:
            feedbacks = self.list_by_asset(asset_id)
        elif project_id is not None:
            feedbacks = self.list_by_project(project_id)
        else:
            feedbacks = self.list_all()

        quality_ratings = [feedback.quality_rating for feedback in feedbacks]
        semantic_ratings = [
            feedback.semantic_rating
            for feedback in feedbacks
            if feedback.semantic_rating is not None
        ]
        creative_ratings = [
            feedback.creative_rating
            for feedback in feedbacks
            if feedback.creative_rating is not None
        ]
        comment_count = sum(1 for feedback in feedbacks if feedback.comments.strip())
        export_ready_total = sum(1 for feedback in feedbacks if feedback.export_ready is True)
        reuse_intent_total = sum(1 for feedback in feedbacks if feedback.reuse_intent is True)
        issue_tag_counts: dict[str, int] = {}
        for feedback in feedbacks:
            for tag in feedback.issue_tags:
                issue_tag_counts[tag] = issue_tag_counts.get(tag, 0) + 1

        average_quality_rating = _average_rating(quality_ratings)
        average_semantic_rating = _average_rating(semantic_ratings)
        average_creative_rating = _average_rating(creative_ratings)

        return {
            "total_feedback": len(feedbacks),
            "average_quality_rating": average_quality_rating,
            "average_semantic_rating": average_semantic_rating,
            "average_creative_rating": average_creative_rating,
            "comment_count": comment_count,
            "export_ready_rate": _average_boolean(export_ready_total, len(feedbacks)),
            "reuse_intent_rate": _average_boolean(reuse_intent_total, len(feedbacks)),
            "issue_tag_counts": issue_tag_counts,
            "latest_feedback_at": feedbacks[0].created_at if feedbacks else None,
            "human_quality_score": rating_to_score(average_quality_rating),
            "human_semantic_alignment_score": rating_to_score(average_semantic_rating),
            "human_creative_alignment_score": rating_to_score(average_creative_rating),
        }

    def delete(self, feedback_id: str) -> bool:
        feedback_file = self.feedback_dir / f"{feedback_id}.json"
        if feedback_file.exists():
            feedback_file.unlink()
            return True
        return False

    def _load_feedback(self, feedback_file: Path) -> Feedback:
        data = json.loads(feedback_file.read_text(encoding="utf-8"))
        return Feedback(
            id=data["id"],
            job_id=data["job_id"],
            asset_id=data.get("asset_id"),
            project_id=data.get("project_id"),
            quality_rating=int(data["quality_rating"]),
            semantic_rating=data.get("semantic_rating"),
            creative_rating=data.get("creative_rating"),
            reuse_intent=data.get("reuse_intent"),
            export_ready=data.get("export_ready"),
            issue_tags=list(data.get("issue_tags", [])),
            comments=data.get("comments", ""),
            metadata=dict(data.get("metadata", {})),
            created_at=ensure_utc(datetime.fromisoformat(data["created_at"])),
        )

    def _try_load_feedback(self, feedback_file: Path) -> Feedback | None:
        try:
            return self._load_feedback(feedback_file)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_feedback(self, feedback: Feedback) -> None:
        feedback_file = self.feedback_dir / f"{feedback.id}.json"
        write_json_atomic(feedback_file, feedback.to_dict())

    def _validate_rating(self, rating: int | None, field_name: str) -> None:
        if rating is None:
            return
        if not 1 <= rating <= 5:
            raise ValueError(f"{field_name} must be between 1 and 5")


def _average_rating(values: list[int | None]) -> float | None:
    normalized = [int(value) for value in values if isinstance(value, int)]
    if not normalized:
        return None
    return round(sum(normalized) / len(normalized), 2)


def _average_boolean(total_true: int, count: int) -> float | None:
    if count <= 0:
        return None
    return round((total_true / count) * 100.0, 1)


__all__ = ["Feedback", "FeedbackRepository", "rating_to_score"]
