"""Merge structured text-generation output into a story document."""

from __future__ import annotations

from typing import Any

from .schemas import Beat, Chapter, DialogueLine, Scene, StoryDocument
from .text_utils import collapse_whitespace, count_words

SUPPORTED_TASKS: tuple[str, ...] = (
    "logline",
    "beat_sheet",
    "scene_list",
    "prose",
    "script",
    "character_sheet",
)

DEFAULT_SCENE_DURATION_SECONDS = 4.0


def apply_text_result(
    story: StoryDocument,
    task: str,
    structured: dict[str, Any],
    *,
    job_id: str | None = None,
) -> StoryDocument:
    """Return a new document with ``structured`` merged in for ``task``.

    Pure: the input document is never mutated, so a caller can compare before and
    after, or discard a merge it does not like.
    """

    if task not in SUPPORTED_TASKS:
        raise ValueError(
            f"Unsupported story task {task!r}; "
            f"expected one of {', '.join(SUPPORTED_TASKS)}"
        )

    updates = _MERGERS[task](story, structured)

    source_job_ids = list(story.source_job_ids)
    if job_id is not None and job_id not in source_job_ids:
        source_job_ids.append(job_id)
    updates["source_job_ids"] = source_job_ids

    return story.model_copy(update=updates)


def _merge_logline(
    story: StoryDocument,
    structured: dict[str, Any],
) -> dict[str, Any]:
    candidates = [
        entry for entry in structured.get("loglines", []) if isinstance(entry, dict)
    ]
    if not candidates:
        raise ValueError("logline payload contains no loglines")

    metadata = dict(story.metadata)
    # Every candidate is kept: picking the logline is an editorial decision the
    # user makes later, and re-running generation to see a rejected option again
    # would cost another inference pass.
    metadata["logline_candidates"] = candidates
    return {
        "logline": str(candidates[0].get("text", "")).strip(),
        "metadata": metadata,
    }


def _merge_beat_sheet(
    story: StoryDocument,
    structured: dict[str, Any],
) -> dict[str, Any]:
    raw_beats = [
        entry for entry in structured.get("beats", []) if isinstance(entry, dict)
    ]
    if not raw_beats:
        raise ValueError("beat_sheet payload contains no beats")

    beats = [
        Beat(
            id=f"beat_{index + 1:02d}",
            act=str(entry.get("act", "")).strip(),
            purpose=str(entry.get("purpose", "")).strip(),
            summary=str(entry.get("summary", "")).strip(),
            order=index,
        )
        for index, entry in enumerate(raw_beats)
    ]
    return {"beats": beats}


def _merge_scene_list(
    story: StoryDocument,
    structured: dict[str, Any],
) -> dict[str, Any]:
    raw_scenes = [
        entry for entry in structured.get("scenes", []) if isinstance(entry, dict)
    ]
    if not raw_scenes:
        raise ValueError("scene_list payload contains no scenes")

    # Rewriting scene text must not orphan media that was already generated for a
    # given position, otherwise a small wording fix would throw away minutes of
    # image and narration generation. Lineage is therefore carried over by scene
    # order, which is the only identity a regenerated list shares with the old one.
    existing_by_order = {scene.order: scene for scene in story.scenes}
    beat_ids = [beat.id for beat in story.beats]

    scenes: list[Scene] = []
    for index, entry in enumerate(raw_scenes):
        previous = existing_by_order.get(index)
        scenes.append(
            Scene(
                id=f"scene_{index + 1:02d}",
                order=index,
                beat_id=_resolve_beat_id(entry.get("beat_id"), beat_ids),
                heading=str(entry.get("heading", "")).strip(),
                summary=str(entry.get("summary", "")).strip(),
                narration=collapse_whitespace(str(entry.get("narration", ""))),
                dialogue=list(previous.dialogue) if previous is not None else [],
                image_prompt=str(entry.get("image_prompt", "")).strip(),
                image_negative=str(entry.get("image_negative", "")).strip(),
                bgm_mood=str(entry.get("bgm_mood", "")).strip(),
                duration_seconds=_coerce_duration(entry.get("duration_seconds")),
                camera=str(entry.get("camera", "")).strip(),
                bible_refs=list(previous.bible_refs) if previous is not None else [],
                asset_ids=dict(previous.asset_ids) if previous is not None else {},
                job_ids=list(previous.job_ids) if previous is not None else [],
            )
        )
    return {"scenes": scenes}


def _merge_prose(
    story: StoryDocument,
    structured: dict[str, Any],
) -> dict[str, Any]:
    prose_markdown = str(structured.get("prose_markdown", "")).strip()
    if not prose_markdown:
        raise ValueError("prose payload contains no prose_markdown")
    title = str(structured.get("title", "")).strip()

    chapters = list(story.chapters)
    # Chapters are matched by title so that regenerating "Chapter 3" replaces it
    # instead of appending a second copy. An untitled chapter always appends,
    # because there is nothing to match on.
    existing_index = next(
        (
            index
            for index, chapter in enumerate(chapters)
            if title and chapter.title == title
        ),
        None,
    )
    order = existing_index if existing_index is not None else len(chapters)
    chapter = Chapter(
        id=f"chapter_{order + 1:02d}",
        order=order,
        title=title,
        prose_markdown=prose_markdown,
        word_count=count_words(prose_markdown),
    )
    if existing_index is None:
        chapters.append(chapter)
    else:
        chapters[existing_index] = chapter
    return {"chapters": chapters}


def _merge_script(
    story: StoryDocument,
    structured: dict[str, Any],
) -> dict[str, Any]:
    raw_lines = [
        entry for entry in structured.get("lines", []) if isinstance(entry, dict)
    ]
    if not raw_lines:
        raise ValueError("script payload contains no lines")

    scene_id = structured.get("scene_id")
    scene_index = structured.get("scene_index")
    scenes = [scene.model_copy(deep=True) for scene in story.scenes]
    by_id = {scene.id: scene for scene in scenes}
    by_order = {scene.order: scene for scene in scenes}

    target: Scene | None = None
    if isinstance(scene_id, str) and scene_id in by_id:
        target = by_id[scene_id]
    elif isinstance(scene_index, int) and scene_index in by_order:
        target = by_order[scene_index]
    elif len(scenes) == 1:
        target = scenes[0]

    dialogue = [
        DialogueLine(
            speaker=str(entry.get("speaker", "")).strip(),
            text=str(entry.get("text", "")).strip(),
            direction=(
                str(entry["direction"]).strip()
                if entry.get("direction") is not None
                else None
            ),
        )
        for entry in raw_lines
    ]

    if target is None:
        # Without a scene to attach to, the lines are still worth keeping: losing
        # generated dialogue because the payload omitted an index would be worse
        # than parking it in metadata for the caller to place.
        metadata = dict(story.metadata)
        metadata["unassigned_script_lines"] = [
            line.model_dump(mode="json") for line in dialogue
        ]
        return {"metadata": metadata}

    target.dialogue = dialogue
    return {"scenes": scenes}


def _merge_character_sheet(
    story: StoryDocument,
    structured: dict[str, Any],
) -> dict[str, Any]:
    name = str(structured.get("name", "")).strip()
    if not name:
        raise ValueError("character_sheet payload contains no name")

    metadata = dict(story.metadata)
    drafts = [
        draft
        for draft in metadata.get("character_drafts", [])
        if isinstance(draft, dict) and draft.get("name") != name
    ]
    # Promotion into the Creative Bible is a separate, explicit step: a draft is
    # a suggestion, and the bible is the contract the image generator obeys.
    drafts.append(dict(structured))
    metadata["character_drafts"] = drafts
    return {"metadata": metadata}


_MERGERS = {
    "logline": _merge_logline,
    "beat_sheet": _merge_beat_sheet,
    "scene_list": _merge_scene_list,
    "prose": _merge_prose,
    "script": _merge_script,
    "character_sheet": _merge_character_sheet,
}


def _resolve_beat_id(raw_beat_id: Any, beat_ids: list[str]) -> str | None:
    if isinstance(raw_beat_id, str) and raw_beat_id in beat_ids:
        return raw_beat_id
    if isinstance(raw_beat_id, int) and 0 <= raw_beat_id < len(beat_ids):
        return beat_ids[raw_beat_id]
    return None


def _coerce_duration(raw_duration: Any) -> float:
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        return DEFAULT_SCENE_DURATION_SECONDS
    if duration <= 0:
        return DEFAULT_SCENE_DURATION_SECONDS
    return duration


__all__ = ["DEFAULT_SCENE_DURATION_SECONDS", "SUPPORTED_TASKS", "apply_text_result"]
