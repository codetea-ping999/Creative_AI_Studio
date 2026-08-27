"""Schemas for the StoryDocument that connects writing to media production."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.storage.json_files import ensure_utc

# Roles used as keys in ``Scene.asset_ids``. Kept as a tuple (not a Literal) so a
# future role does not invalidate documents already on disk.
SCENE_ASSET_ROLES: tuple[str, ...] = ("visual", "narration", "music")

STORY_FORMATS: tuple[str, ...] = ("short-video", "novel", "picture-book", "ad")


class Beat(BaseModel):
    """One structural beat of the story outline."""

    model_config = ConfigDict(extra="forbid")

    id: str
    act: str
    purpose: str
    summary: str
    order: int


class DialogueLine(BaseModel):
    """A single spoken line, optionally carrying an acting direction."""

    model_config = ConfigDict(extra="forbid")

    speaker: str
    text: str
    direction: str | None = None


class Scene(BaseModel):
    """A scene: the unit that becomes one shot in the assembled video."""

    model_config = ConfigDict(extra="forbid")

    id: str
    order: int
    beat_id: str | None = None
    heading: str = ""
    summary: str = ""
    narration: str = ""
    dialogue: list[DialogueLine] = Field(default_factory=list)
    image_prompt: str = ""
    image_negative: str = ""
    bgm_mood: str = ""
    duration_seconds: float = 4.0
    camera: str = ""
    bible_refs: list[str] = Field(default_factory=list)
    # role -> asset id; roles are "visual" | "narration" | "music" (SCENE_ASSET_ROLES).
    # This mapping is what makes a scene renderable, so it survives text regeneration.
    asset_ids: dict[str, str] = Field(default_factory=dict)
    job_ids: list[str] = Field(default_factory=list)


class Chapter(BaseModel):
    """A prose chapter (novel / picture-book formats)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    order: int
    title: str = ""
    prose_markdown: str = ""
    word_count: int = 0
    # The continuity memory's `as_of_chapter_id` at the moment this chapter's
    # prose was written (see `core.story.continuity_context.ContinuityContext`),
    # i.e. which prior chapter's state this chapter was written to be
    # consistent with. `None` when the chapter was written with no continuity
    # memory yet (a story's first chapter) or outside the continuity-injected
    # flow. This is what makes it possible to say, later, exactly what state a
    # chapter assumed (issue #191).
    continuity_as_of_chapter_id: str | None = None
    # Set to an earlier chapter's id when regenerating that earlier chapter may
    # have invalidated this chapter's continuity assumptions (issue #191); this
    # chapter's prose is never rewritten automatically as a result — only
    # flagged, so an editor decides what to do. `None` means "not flagged
    # stale". Cleared automatically when this chapter is itself regenerated
    # (see `core.story.merge._merge_prose`), and can also be cleared directly
    # by `POST /stories/{story_id}/chapters/{chapter_id}/acknowledge-stale`
    # after a human review that finds nothing actually broke.
    stale_after_chapter_id: str | None = None


class StoryDocument(BaseModel):
    """The structured story: outline, scenes, prose, and their media lineage."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    project_id: str | None = None
    logline: str = ""
    premise: str = ""
    genre: str = ""
    tone: str = ""
    audience: str = ""
    language: str = "ja"
    format: str = "short-video"
    structure: str = "three-act"
    characters: list[str] = Field(default_factory=list)
    beats: list[Beat] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    source_job_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        # Documents written before timestamps were tz-aware deserialize as naive
        # datetimes; normalizing on load keeps sorting by updated_at from raising
        # on a comparison between aware and naive values.
        return ensure_utc(value)

    def scenes_in_order(self) -> list[Scene]:
        """Return scenes sorted by ``order`` without mutating the document."""

        return sorted(self.scenes, key=lambda scene: scene.order)

    def chapters_in_order(self) -> list[Chapter]:
        """Return chapters sorted by ``order`` without mutating the document."""

        return sorted(self.chapters, key=lambda chapter: chapter.order)

    def stale_chapters(self) -> list[Chapter]:
        """Chapters flagged as potentially inconsistent with a later regeneration.

        See ``Chapter.stale_after_chapter_id`` (issue #191): non-empty exactly
        for chapters an editor has not yet regenerated or acknowledged since an
        earlier chapter changed under them.
        """

        return [
            chapter
            for chapter in self.chapters_in_order()
            if chapter.stale_after_chapter_id
        ]

    def total_duration_seconds(self) -> float:
        """Return the sum of scene durations, the length of the assembled video."""

        return float(sum(scene.duration_seconds for scene in self.scenes))


__all__ = [
    "Beat",
    "Chapter",
    "DialogueLine",
    "SCENE_ASSET_ROLES",
    "STORY_FORMATS",
    "Scene",
    "StoryDocument",
]
