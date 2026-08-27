"""Persistence for ``ContinuityMemory`` records, one JSON file per story."""

from __future__ import annotations

import json
from pathlib import Path

from core.storage.json_files import write_json_atomic

from .continuity import ContinuityMemory


class ContinuityRepository:
    """Persist and retrieve continuity memory.

    Mirrors ``StoryRepository``: one atomic-written JSON file per story
    (keyed by ``story_id``, since a story has at most one continuity
    record), and a corrupt file degrades to "no continuity yet" rather than
    raising — a damaged record should not be able to break chapter
    generation for a story that would otherwise work.
    """

    def __init__(self, continuity_dir: str | Path = "data/continuity") -> None:
        self.continuity_dir = Path(continuity_dir)
        self.continuity_dir.mkdir(parents=True, exist_ok=True)

    def get_for_story(self, story_id: str) -> ContinuityMemory | None:
        memory_file = self._path_for_story(story_id)
        if not memory_file.exists():
            return None
        try:
            return ContinuityMemory.model_validate_json(
                memory_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def save(self, memory: ContinuityMemory) -> ContinuityMemory:
        write_json_atomic(
            self._path_for_story(memory.story_id),
            memory.model_dump(mode="json"),
        )
        return memory

    def delete_for_story(self, story_id: str) -> bool:
        memory_file = self._path_for_story(story_id)
        if memory_file.exists():
            memory_file.unlink()
            return True
        return False

    def _path_for_story(self, story_id: str) -> Path:
        return self.continuity_dir / f"{story_id}.json"


__all__ = ["ContinuityRepository"]
