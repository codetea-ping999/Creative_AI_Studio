"""Persistence for story documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import uuid

from core.storage.json_files import utc_now, write_json_atomic

from .schemas import STORY_FORMATS, StoryDocument


class StoryRepository:
    """Persist and manage story documents on disk.

    Mirrors ``ProjectRepository``: one JSON file per document, atomic writes, and
    a corrupt file is skipped by list operations rather than breaking the whole
    listing.
    """

    def __init__(self, story_dir: str | Path = "data/stories") -> None:
        self.story_dir = Path(story_dir)
        self.story_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        title: str = "",
        project_id: str | None = None,
        **fields: Any,
    ) -> StoryDocument:
        unknown_fields = set(fields) - set(StoryDocument.model_fields)
        if unknown_fields:
            raise ValueError(
                f"Unknown story fields: {', '.join(sorted(unknown_fields))}"
            )
        story_format = fields.get("format", "short-video")
        if story_format not in STORY_FORMATS:
            raise ValueError(
                f"Unknown story format {story_format!r}; "
                f"expected one of {', '.join(STORY_FORMATS)}"
            )

        now = utc_now()
        story = StoryDocument(
            id=f"story_{uuid.uuid4().hex}",
            title=title,
            project_id=project_id,
            created_at=now,
            updated_at=now,
            **fields,
        )
        self._save(story)
        return story

    def get(self, story_id: str) -> StoryDocument | None:
        story_file = self.story_dir / f"{story_id}.json"
        if not story_file.exists():
            return None
        return self._try_load(story_file)

    def save(self, story: StoryDocument) -> StoryDocument:
        updated = story.model_copy(update={"updated_at": utc_now()})
        self._save(updated)
        return updated

    def update(self, story_id: str, **fields: Any) -> StoryDocument | None:
        story = self.get(story_id)
        if story is None:
            return None

        unknown_fields = set(fields) - set(StoryDocument.model_fields)
        if unknown_fields:
            raise ValueError(
                f"Unknown story fields: {', '.join(sorted(unknown_fields))}"
            )
        # id and timestamps are owned by the repository, never by a caller patch.
        for reserved in ("id", "created_at", "updated_at"):
            fields.pop(reserved, None)
        if not fields:
            return story

        updated = story.model_copy(update=fields)
        if updated == story:
            return story
        return self.save(updated)

    def list_all(
        self,
        *,
        project_id: str | None = None,
        query_text: str | None = None,
        limit: int | None = None,
    ) -> list[StoryDocument]:
        normalized_query = query_text.strip().lower() if query_text else None
        stories: list[StoryDocument] = []
        for story_file in sorted(self.story_dir.glob("*.json")):
            story = self._try_load(story_file)
            if story is None:
                continue
            if project_id and story.project_id != project_id:
                continue
            if normalized_query and normalized_query not in self._haystack(story):
                continue
            stories.append(story)

        stories.sort(key=lambda entry: entry.updated_at, reverse=True)
        if limit is not None:
            return stories[:limit]
        return stories

    def delete(self, story_id: str) -> bool:
        story_file = self.story_dir / f"{story_id}.json"
        if story_file.exists():
            story_file.unlink()
            return True
        return False

    def _haystack(self, story: StoryDocument) -> str:
        return " ".join(
            [
                story.title,
                story.logline,
                story.premise,
                story.genre,
                story.tone,
                story.audience,
                " ".join(scene.heading for scene in story.scenes),
                " ".join(scene.summary for scene in story.scenes),
            ]
        ).lower()

    def _save(self, story: StoryDocument) -> None:
        write_json_atomic(
            self.story_dir / f"{story.id}.json",
            story.model_dump(mode="json"),
        )

    def _try_load(self, story_file: Path) -> StoryDocument | None:
        try:
            return StoryDocument.model_validate_json(
                story_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None


__all__ = ["StoryRepository"]
