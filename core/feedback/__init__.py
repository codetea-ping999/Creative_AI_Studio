"""Human feedback collection for quality score refinement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import uuid


@dataclass(slots=True)
class Feedback:
    """Human feedback on a generated output."""

    id: str
    job_id: str
    quality_rating: int  # 1-5 stars
    semantic_rating: int | None = None  # 1-5 stars for prompt alignment
    comments: str = ""
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "quality_rating": self.quality_rating,
            "semantic_rating": self.semantic_rating,
            "comments": self.comments,
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
        semantic_rating: int | None = None,
        comments: str = "",
    ) -> Feedback:
        """Create and persist feedback."""
        if not 1 <= quality_rating <= 5:
            raise ValueError("quality_rating must be between 1 and 5")
        if semantic_rating is not None and not 1 <= semantic_rating <= 5:
            raise ValueError("semantic_rating must be between 1 and 5")

        feedback = Feedback(
            id=str(uuid.uuid4()),
            job_id=job_id,
            quality_rating=quality_rating,
            semantic_rating=semantic_rating,
            comments=comments,
            created_at=datetime.now(),
        )
        self._save_feedback(feedback)
        return feedback

    def get(self, feedback_id: str) -> Feedback | None:
        """Get feedback by ID."""
        feedback_file = self.feedback_dir / f"{feedback_id}.json"
        if not feedback_file.exists():
            return None

        data = json.loads(feedback_file.read_text(encoding="utf-8"))
        return Feedback(
            id=data["id"],
            job_id=data["job_id"],
            quality_rating=data["quality_rating"],
            semantic_rating=data.get("semantic_rating"),
            comments=data.get("comments", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def list_by_job(self, job_id: str) -> list[Feedback]:
        """Get all feedback for a specific job."""
        feedbacks = []
        for feedback_file in self.feedback_dir.glob("*.json"):
            data = json.loads(feedback_file.read_text(encoding="utf-8"))
            if data["job_id"] == job_id:
                feedback = Feedback(
                    id=data["id"],
                    job_id=data["job_id"],
                    quality_rating=data["quality_rating"],
                    semantic_rating=data.get("semantic_rating"),
                    comments=data.get("comments", ""),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                feedbacks.append(feedback)

        feedbacks.sort(key=lambda f: f.created_at, reverse=True)
        return feedbacks

    def list_all(self) -> list[Feedback]:
        """Get all feedback."""
        feedbacks = []
        for feedback_file in sorted(self.feedback_dir.glob("*.json")):
            data = json.loads(feedback_file.read_text(encoding="utf-8"))
            feedback = Feedback(
                id=data["id"],
                job_id=data["job_id"],
                quality_rating=data["quality_rating"],
                semantic_rating=data.get("semantic_rating"),
                comments=data.get("comments", ""),
                created_at=datetime.fromisoformat(data["created_at"]),
            )
            feedbacks.append(feedback)

        feedbacks.sort(key=lambda f: f.created_at, reverse=True)
        return feedbacks

    def summarize(self, *, job_id: str | None = None) -> dict[str, object]:
        """Summarize stored feedback ratings."""
        feedbacks = self.list_by_job(job_id) if job_id is not None else self.list_all()
        quality_ratings = [feedback.quality_rating for feedback in feedbacks]
        semantic_ratings = [
            feedback.semantic_rating
            for feedback in feedbacks
            if feedback.semantic_rating is not None
        ]
        comment_count = sum(1 for feedback in feedbacks if feedback.comments.strip())

        return {
            "total_feedback": len(feedbacks),
            "average_quality_rating": _average_rating(quality_ratings),
            "average_semantic_rating": _average_rating(semantic_ratings),
            "comment_count": comment_count,
            "latest_feedback_at": feedbacks[0].created_at if feedbacks else None,
        }

    def delete(self, feedback_id: str) -> bool:
        """Delete feedback."""
        feedback_file = self.feedback_dir / f"{feedback_id}.json"
        if feedback_file.exists():
            feedback_file.unlink()
            return True
        return False

    def _save_feedback(self, feedback: Feedback) -> None:
        """Persist feedback to disk."""
        feedback_file = self.feedback_dir / f"{feedback.id}.json"
        feedback_file.write_text(
            json.dumps(feedback.to_dict(), ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _average_rating(values: list[int | None]) -> float | None:
    normalized = [int(value) for value in values if isinstance(value, int)]
    if not normalized:
        return None
    return round(sum(normalized) / len(normalized), 2)


__all__ = ["Feedback", "FeedbackRepository"]
