"""Build an assembly timeline from a story document."""

from __future__ import annotations

from typing import Any, Callable

from .schemas import Scene, StoryDocument
from .text_utils import split_subtitle_lines

# Music sits under narration, so it is mixed well below unity gain by default.
DEFAULT_MUSIC_GAIN_DB = -14.0

# The motions the assembly generator can actually render. ``Scene.camera`` is
# written by a language model, so it is free text: "slow push in" is a reasonable
# thing to write and a useless thing to render. Normalizing here — rather than
# letting assembly silently degrade every unknown value to a static shot — keeps
# the substitution visible and testable.
SUPPORTED_MOTIONS: tuple[str, ...] = (
    "none",
    "ken_burns_in",
    "ken_burns_out",
    "pan_left",
    "pan_right",
)

# Phrasings a writer or a model reaches for, mapped to what we can render.
_MOTION_ALIASES: dict[str, str] = {
    "push in": "ken_burns_in",
    "pushin": "ken_burns_in",
    "slow push in": "ken_burns_in",
    "zoom in": "ken_burns_in",
    "dolly in": "ken_burns_in",
    "pull out": "ken_burns_out",
    "pull back": "ken_burns_out",
    "zoom out": "ken_burns_out",
    "dolly out": "ken_burns_out",
    "pan left": "pan_left",
    "truck left": "pan_left",
    "pan right": "pan_right",
    "truck right": "pan_right",
    "static": "none",
    "still": "none",
    "locked off": "none",
    "fixed": "none",
}


def normalize_motion(
    raw_camera: str | None,
    *,
    default_motion: str,
) -> tuple[str, bool]:
    """Resolve a scene's camera text to a renderable motion.

    Returns the motion and whether the original value had to be reinterpreted, so
    the caller can record the substitution rather than losing it silently.
    """

    text = (raw_camera or "").strip().lower()
    if not text:
        return default_motion, False
    if text in SUPPORTED_MOTIONS:
        return text, False
    # Treat "ken-burns in" and "ken_burns_in" as the same request.
    collapsed = text.replace("_", " ").replace("-", " ")
    collapsed = " ".join(collapsed.split())
    if collapsed.replace(" ", "_") in SUPPORTED_MOTIONS:
        return collapsed.replace(" ", "_"), True
    aliased = _MOTION_ALIASES.get(collapsed)
    if aliased is not None:
        return aliased, True
    return default_motion, True


def missing_scene_assets(story: StoryDocument) -> list[dict[str, str]]:
    """List the assets a story still needs before it can be assembled.

    A scene always needs a visual. It needs narration audio only when it has
    narration text, because a silent establishing shot is a legitimate choice.
    """

    missing: list[dict[str, str]] = []
    for scene in story.scenes_in_order():
        if not scene.asset_ids.get("visual"):
            missing.append({"scene_id": scene.id, "role": "visual"})
        if scene.narration.strip() and not scene.asset_ids.get("narration"):
            missing.append({"scene_id": scene.id, "role": "narration"})
    return missing


def build_timeline(
    story: StoryDocument,
    *,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 30,
    asset_path_lookup: Callable[[str], str | None] | None = None,
    default_motion: str = "ken_burns_in",
    transition: str = "crossfade",
    include_subtitles: bool = True,
    music_gain_db: float = DEFAULT_MUSIC_GAIN_DB,
) -> dict[str, Any]:
    """Turn scenes into the timeline consumed by the assembly generator.

    Raises ``ValueError`` naming the offending scenes when a visual is missing: a
    half-empty video is harder to diagnose than a refusal to render one.
    """

    scenes = story.scenes_in_order()
    if not scenes:
        raise ValueError(f"Story {story.id!r} has no scenes to assemble.")

    scenes_without_visual = [
        scene.id for scene in scenes if not scene.asset_ids.get("visual")
    ]
    if scenes_without_visual:
        raise ValueError(
            "Cannot build a timeline: scenes are missing a visual asset: "
            f"{', '.join(scenes_without_visual)}"
        )

    visual: list[dict[str, Any]] = []
    narration: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    music: list[dict[str, Any]] = []

    start_seconds = 0.0
    for scene in scenes:
        duration = float(scene.duration_seconds)
        motion, motion_substituted = normalize_motion(
            scene.camera, default_motion=default_motion
        )
        visual_entry: dict[str, Any] = {
            "scene_id": scene.id,
            "asset_id": scene.asset_ids["visual"],
            "duration_seconds": duration,
            "transition": transition,
            "motion": motion,
        }
        if motion_substituted:
            # Keep the writer's wording so a reviewer can see why the shot moves
            # the way it does, instead of wondering where their direction went.
            visual_entry["requested_camera"] = scene.camera
        visual.append(
            _with_path(
                visual_entry,
                scene.asset_ids["visual"],
                asset_path_lookup,
            )
        )

        narration_asset_id = scene.asset_ids.get("narration")
        if narration_asset_id:
            narration.append(
                _with_path(
                    {
                        "scene_id": scene.id,
                        "asset_id": narration_asset_id,
                        "start_seconds": round(start_seconds, 3),
                    },
                    narration_asset_id,
                    asset_path_lookup,
                )
            )

        if include_subtitles and scene.narration.strip():
            subtitles.extend(
                _build_scene_subtitles(scene, start_seconds=start_seconds)
            )

        start_seconds += duration

    music = _build_music_runs(scenes, asset_path_lookup, music_gain_db)

    return {
        "resolution": [int(resolution[0]), int(resolution[1])],
        "fps": int(fps),
        "total_duration_seconds": round(start_seconds, 3),
        "tracks": {
            "visual": visual,
            "narration": narration,
            "music": music,
            "subtitles": subtitles,
        },
    }


def _build_music_runs(
    scenes: list[Scene],
    asset_path_lookup: Callable[[str], str | None] | None,
    music_gain_db: float,
) -> list[dict[str, Any]]:
    """Emit one music entry per run of consecutive scenes sharing a track.

    Restarting the same track at every cut is audible and amateurish, so adjacent
    scenes that share a music asset become a single spanning entry.
    """

    runs: list[dict[str, Any]] = []
    start_seconds = 0.0
    for scene in scenes:
        duration = float(scene.duration_seconds)
        music_asset_id = scene.asset_ids.get("music")
        if music_asset_id:
            if runs and runs[-1]["asset_id"] == music_asset_id and runs[-1]["_open"]:
                runs[-1]["duration_seconds"] = round(
                    runs[-1]["duration_seconds"] + duration, 3
                )
            else:
                for run in runs:
                    run["_open"] = False
                runs.append(
                    _with_path(
                        {
                            "asset_id": music_asset_id,
                            "start_seconds": round(start_seconds, 3),
                            "duration_seconds": round(duration, 3),
                            "gain_db": music_gain_db,
                            "loop": True,
                            "duck": True,
                            "_open": True,
                        },
                        music_asset_id,
                        asset_path_lookup,
                    )
                )
        else:
            for run in runs:
                run["_open"] = False
        start_seconds += duration

    for run in runs:
        run.pop("_open", None)
    return runs


def _build_scene_subtitles(
    scene: Scene,
    *,
    start_seconds: float,
) -> list[dict[str, Any]]:
    lines = split_subtitle_lines(scene.narration)
    if not lines:
        return []

    # Screen time is split proportionally to line length so a long line is not
    # flashed for the same duration as a two-word one.
    total_characters = sum(len(line) for line in lines) or 1
    entries: list[dict[str, Any]] = []
    cursor = start_seconds
    for index, line in enumerate(lines):
        share = len(line) / total_characters
        line_duration = scene.duration_seconds * share
        end = (
            start_seconds + scene.duration_seconds
            if index == len(lines) - 1
            else cursor + line_duration
        )
        entries.append(
            {
                "scene_id": scene.id,
                "text": line,
                "start_seconds": round(cursor, 3),
                "end_seconds": round(end, 3),
            }
        )
        cursor = end
    return entries


def _with_path(
    entry: dict[str, Any],
    asset_id: str,
    asset_path_lookup: Callable[[str], str | None] | None,
) -> dict[str, Any]:
    if asset_path_lookup is None:
        return entry
    resolved = asset_path_lookup(asset_id)
    if resolved:
        entry["path"] = resolved
    return entry


__all__ = ["DEFAULT_MUSIC_GAIN_DB", "build_timeline", "missing_scene_assets"]
