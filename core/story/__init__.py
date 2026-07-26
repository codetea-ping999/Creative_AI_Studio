"""Story documents: the spine that connects writing to media production."""

from .binding import (
    SCENE_ID_PARAM,
    SCENE_ROLE_PARAM,
    STORY_ID_PARAM,
    SceneBinder,
    scene_binding_params,
)
from .merge import DEFAULT_SCENE_DURATION_SECONDS, SUPPORTED_TASKS, apply_text_result
from .repository import StoryRepository
from .schemas import (
    SCENE_ASSET_ROLES,
    STORY_FORMATS,
    Beat,
    Chapter,
    DialogueLine,
    Scene,
    StoryDocument,
)
from .text_utils import count_words, split_subtitle_lines
from .timeline import DEFAULT_MUSIC_GAIN_DB, build_timeline, missing_scene_assets

__all__ = [
    "Beat",
    "Chapter",
    "DEFAULT_MUSIC_GAIN_DB",
    "DEFAULT_SCENE_DURATION_SECONDS",
    "DialogueLine",
    "SCENE_ASSET_ROLES",
    "SCENE_ID_PARAM",
    "SCENE_ROLE_PARAM",
    "STORY_ID_PARAM",
    "SceneBinder",
    "STORY_FORMATS",
    "SUPPORTED_TASKS",
    "Scene",
    "StoryDocument",
    "StoryRepository",
    "apply_text_result",
    "build_timeline",
    "scene_binding_params",
    "count_words",
    "missing_scene_assets",
    "split_subtitle_lines",
]
