"""Persistence for story documents."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from threading import RLock
from typing import Any
import uuid

from core.storage.json_files import utc_now, write_json_atomic

from .schemas import STORY_FORMATS, StoryDocument


class StoryRepository:
    """Persist and manage story documents on disk.

    Mirrors ``ProjectRepository``: one JSON file per document, atomic writes, and
    a corrupt file is skipped by list operations rather than breaking the whole
    listing.

    A story document can be written by several independent callers in the same
    process — the story API's PATCH/apply/delete routes, ``SceneBinder``, and
    any future writer — each doing its own read-modify-write. Without a shared
    boundary, two such writers can interleave (A reads, B reads, B saves, A
    saves over B's change) and silently lose one side's update, or resurrect a
    story a concurrent delete just removed. ``mutate()`` is that boundary: it
    holds ``_lock`` across the read, the caller's mutation, and the save, so
    every writer that goes through it is serialized against every other one
    (including ``delete()``). Plain reads (``get``, ``list_all``) stay
    lock-free: ``write_json_atomic`` replaces the file in one ``os.replace``,
    so a concurrent reader only ever sees the fully-old or fully-new file,
    never a torn write.
    """

    def __init__(self, story_dir: str | Path = "data/stories") -> None:
        self.story_dir = Path(story_dir)
        self.story_dir.mkdir(parents=True, exist_ok=True)
        # Shared across every mutate()/save()/delete() call on this repository
        # instance, regardless of which story id is touched — see the class
        # docstring. RLock (not Lock) so save() can be called safely from
        # within a mutate() callback's own critical section.
        self._lock = RLock()

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
        with self._lock:
            updated = story.model_copy(update={"updated_at": utc_now()})
            self._save(updated)
            return updated

    def mutate(
        self,
        story_id: str,
        fn: Callable[[StoryDocument | None], StoryDocument | None],
    ) -> StoryDocument | None:
        """Read, apply ``fn``, and atomically save one story as a single step.

        ``fn`` is called with the current document, or ``None`` if it does not
        exist (never created, or concurrently deleted). Its return value
        controls what happens next:

        - ``None`` — decline the mutation; nothing is saved and ``mutate()``
          itself returns ``None``. Use this both when ``current`` was already
          ``None`` and when the mutation looked at an existing document and
          decided not to touch it (an unmatched scene id, a stale recovery
          replay, ...) — either way there is nothing to hand back.
        - a document equal to ``current`` — nothing is saved (no pointless
          ``updated_at`` bump), and ``mutate()`` returns that document.
        - any other document — saved atomically, and the saved (persisted)
          document is returned.

        The read, the call to ``fn``, and the save all happen while holding
        the repository's shared lock, so this is the boundary every story
        writer (API routes, ``SceneBinder``, ...) should go through instead of
        calling ``get`` + ``save`` on their own — see the class docstring.

        Do not put generation, network, or model work inside ``fn``: it runs
        while every other writer on this repository instance is blocked,
        across every story, not just this one.

        If ``fn`` raises, the exception propagates and nothing is saved; the
        lock is released regardless (``with`` guarantees this even on
        exception), so a failed mutation never leaves the store partially
        written or other writers stuck waiting.
        """

        with self._lock:
            current = self.get(story_id)
            updated = fn(current)
            if updated is None:
                return None
            if updated == current:
                return current
            return self.save(updated)

    def update(self, story_id: str, **fields: Any) -> StoryDocument | None:
        def _apply(story: StoryDocument | None) -> StoryDocument | None:
            if story is None:
                return None

            unknown_fields = set(fields) - set(StoryDocument.model_fields)
            if unknown_fields:
                raise ValueError(
                    f"Unknown story fields: {', '.join(sorted(unknown_fields))}"
                )
            # id and timestamps are owned by the repository, never by a
            # caller patch.
            patch = dict(fields)
            for reserved in ("id", "created_at", "updated_at"):
                patch.pop(reserved, None)
            if not patch:
                return story
            return story.model_copy(update=patch)

        return self.mutate(story_id, _apply)

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
        # Shares _lock with mutate()/save() so a concurrent read-modify-write
        # (SceneBinder, an API PATCH/apply) can never race this: either it
        # fully completes and this delete then removes what it just wrote, or
        # this delete runs first and the other side's mutate() observes a
        # missing story instead of resurrecting it.
        with self._lock:
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
