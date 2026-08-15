"""Story writing tasks: prompt, schema, and markdown rendering per stage.

Each stage of writing is its own task with a fixed output schema. Free-form model
output cannot be piped into image and audio generation without something
downstream breaking, so the schema is the contract and the generator validates
against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from core.models.text_runtimes import BRIEF_HEADING
from core.story.timeline import SUPPORTED_MOTIONS

BEAT_STRUCTURES: dict[str, tuple[str, ...]] = {
    "three-act": ("setup", "confrontation", "resolution"),
    "kishotenketsu": (
        "ki (introduction)",
        "sho (development)",
        "ten (twist)",
        "ketsu (conclusion)",
    ),
    "save-the-cat": (
        "opening image",
        "catalyst",
        "debate",
        "fun and games",
        "midpoint",
        "all is lost",
        "finale",
    ),
}


# --------------------------------------------------------------------- schemas


class Logline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    hook: str = ""
    tone: str = ""


class LoglineResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    loglines: list[Logline] = Field(min_length=1)


class BeatItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    act: str
    purpose: str
    summary: str


class BeatSheetResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    beats: list[BeatItem] = Field(min_length=1)


class SceneItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    heading: str
    summary: str = ""
    narration: str = ""
    image_prompt: str
    image_negative: str = ""
    bgm_mood: str = ""
    duration_seconds: float = 4.0
    camera: str = ""


class SceneListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenes: list[SceneItem] = Field(min_length=1)


class ProseResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    prose_markdown: str


class ScriptLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speaker: str
    text: str
    direction: str | None = None


class ScriptResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lines: list[ScriptLine] = Field(min_length=1)


class PromptVariant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    prompt: str
    negative_prompt: str = ""


class PromptPackResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompts: list[PromptVariant] = Field(min_length=1)


class CharacterSheetResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    summary: str = ""
    prompt_fragment: str
    negative_fragment: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    tokens: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- prompts


def _brief(fields: dict[str, Any]) -> str:
    """Render the machine-readable brief block shared by every task prompt.

    The template runtime parses this block, and a language model reads it as a
    clearly delimited statement of inputs, so both backends see the same request.
    """

    lines = [BRIEF_HEADING]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _logline_prompt(params: dict[str, Any]) -> str:
    count = _clamp_int(params.get("count", 3), low=1, high=12)
    return "\n".join(
        [
            f"Write {count} distinct loglines for the following premise.",
            "Each logline names the protagonist, the want, and the obstacle.",
            "Vary the angle across candidates instead of rephrasing one idea.",
            "",
            _brief(
                {
                    "premise": params.get("premise", ""),
                    "genre": params.get("genre", ""),
                    "audience": params.get("audience", ""),
                    "tone": params.get("tone", ""),
                    "language": params.get("language", "ja"),
                    "count": count,
                }
            ),
        ]
    )


def _beat_sheet_prompt(params: dict[str, Any]) -> str:
    structure = str(params.get("structure", "three-act"))
    acts = BEAT_STRUCTURES.get(structure, BEAT_STRUCTURES["three-act"])
    beat_count = _clamp_int(params.get("beat_count", len(acts) * 2), low=2, high=24)
    return "\n".join(
        [
            f"Break the logline into {beat_count} beats using the {structure} structure.",
            f"Available acts: {', '.join(acts)}.",
            "Each beat states which act it belongs to, what it accomplishes, and what happens.",
            "",
            _brief(
                {
                    "logline": params.get("logline", ""),
                    "premise": params.get("premise", ""),
                    "genre": params.get("genre", ""),
                    "tone": params.get("tone", ""),
                    "structure": structure,
                    "language": params.get("language", "ja"),
                    "beat_count": beat_count,
                }
            ),
        ]
    )


def _scene_list_prompt(params: dict[str, Any]) -> str:
    scene_count = _clamp_int(params.get("scene_count", 5), low=1, high=24)
    total_duration = params.get("total_duration_seconds", scene_count * 4)
    return "\n".join(
        [
            f"Turn the beats into {scene_count} shootable scenes.",
            "For each scene provide: a heading, a one-sentence summary, narration text,",
            "an English image_prompt suitable for a diffusion model, an image_negative,",
            "a bgm_mood keyword, a duration_seconds, and a camera movement.",
            # The renderer supports exactly these motions. Asking for "a camera
            # movement" in free text yields phrases like "slow push in", which the
            # timeline can only fall back on rather than render.
            f"camera must be exactly one of: {', '.join(SUPPORTED_MOTIONS)}.",
            f"Scene durations should sum to roughly {total_duration} seconds.",
            "",
            _brief(
                {
                    "logline": params.get("logline", ""),
                    "beats": _summarize_list(params.get("beats")),
                    "tone": params.get("tone", ""),
                    "mood": params.get("mood", params.get("tone", "")),
                    "subject": params.get("subject", params.get("logline", "")),
                    "language": params.get("language", "ja"),
                    "scene_count": scene_count,
                    "total_duration_seconds": total_duration,
                }
            ),
        ]
    )


def _prose_prompt(params: dict[str, Any]) -> str:
    target_words = _clamp_int(params.get("target_words", 800), low=80, high=8000)
    return "\n".join(
        [
            f"Write this scene as prose of about {target_words} words.",
            "Return markdown in prose_markdown with paragraph breaks, and a chapter title.",
            "Stay in the requested point of view and tense throughout.",
            "",
            _brief(
                {
                    "scene": params.get("scene", ""),
                    "title": params.get("title", ""),
                    "subject": params.get("subject", params.get("title", "")),
                    "style": params.get("style", ""),
                    "pov": params.get("pov", "third person limited"),
                    "tense": params.get("tense", "past"),
                    "tone": params.get("tone", ""),
                    "language": params.get("language", "ja"),
                    "target_words": target_words,
                }
            ),
        ]
    )


def _script_prompt(params: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Write the spoken lines for this scene.",
            "Each line has a speaker, the text to be spoken, and an optional direction.",
            "Narration lines use the speaker name 'NARRATOR'.",
            "",
            _brief(
                {
                    "scene": params.get("scene", ""),
                    "subject": params.get("subject", ""),
                    "characters": _summarize_list(params.get("characters")),
                    "tone": params.get("tone", ""),
                    "language": params.get("language", "ja"),
                    "count": _clamp_int(params.get("count", 4), low=1, high=40),
                }
            ),
        ]
    )


def _prompt_pack_prompt(params: dict[str, Any]) -> str:
    count = _clamp_int(params.get("count", 6), low=1, high=40)
    return "\n".join(
        [
            f"Write {count} English diffusion prompts for the subject below.",
            f"Vary them along this axis: {params.get('variation_axis', 'composition')}.",
            "Each entry has a short slug label, the prompt, and a negative_prompt.",
            "",
            _brief(
                {
                    "subject": params.get("subject", ""),
                    "variation_axis": params.get("variation_axis", "composition"),
                    "style": params.get("style", ""),
                    "mood": params.get("mood", ""),
                    "count": count,
                }
            ),
        ]
    )


def _character_sheet_prompt(params: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Turn this character brief into reusable Creative Bible fields.",
            "prompt_fragment is an English comma-separated visual description.",
            "attributes holds stable slots such as hair, eyes, outfit, build.",
            "",
            _brief(
                {
                    "brief": params.get("brief", ""),
                    "subject": params.get("subject", params.get("brief", "")),
                    "name": params.get("name", ""),
                    "style": params.get("style", ""),
                    "mood": params.get("mood", ""),
                    "language": params.get("language", "ja"),
                }
            ),
        ]
    )


# ------------------------------------------------------------------- rendering


def _render_logline(payload: dict[str, Any]) -> str:
    lines = ["# Loglines", ""]
    for index, entry in enumerate(payload["loglines"], start=1):
        lines.append(f"{index}. {entry['text']}")
        if entry.get("hook"):
            lines.append(f"   - hook: {entry['hook']}")
        if entry.get("tone"):
            lines.append(f"   - tone: {entry['tone']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_beat_sheet(payload: dict[str, Any]) -> str:
    lines = ["# Beat Sheet", ""]
    for index, beat in enumerate(payload["beats"], start=1):
        lines.append(f"## {index}. {beat['act']}")
        lines.append("")
        lines.append(f"- purpose: {beat['purpose']}")
        lines.append(f"- {beat['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_scene_list(payload: dict[str, Any]) -> str:
    scenes = payload["scenes"]
    total = sum(float(scene.get("duration_seconds", 0)) for scene in scenes)
    lines = [
        "# Scene List",
        "",
        f"{len(scenes)} scenes, {total:g} seconds total.",
        "",
        "| # | heading | seconds | bgm | camera |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, scene in enumerate(scenes, start=1):
        lines.append(
            f"| {index} | {scene['heading']} | {float(scene.get('duration_seconds', 0)):g} "
            f"| {scene.get('bgm_mood', '')} | {scene.get('camera', '')} |"
        )
    lines.append("")
    for index, scene in enumerate(scenes, start=1):
        lines.append(f"## {index}. {scene['heading']}")
        lines.append("")
        if scene.get("summary"):
            lines.append(scene["summary"])
            lines.append("")
        if scene.get("narration"):
            lines.append(f"**Narration**: {scene['narration']}")
            lines.append("")
        lines.append(f"**Image prompt**: `{scene['image_prompt']}`")
        if scene.get("image_negative"):
            lines.append(f"**Negative**: `{scene['image_negative']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_prose(payload: dict[str, Any]) -> str:
    title = payload.get("title") or "Chapter"
    return f"# {title}\n\n{payload['prose_markdown'].strip()}\n"


def _render_script(payload: dict[str, Any]) -> str:
    lines = ["# Script", ""]
    for entry in payload["lines"]:
        direction = f" *({entry['direction']})*" if entry.get("direction") else ""
        lines.append(f"**{entry['speaker']}**{direction}: {entry['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_prompt_pack(payload: dict[str, Any]) -> str:
    lines = ["# Prompt Pack", ""]
    for index, entry in enumerate(payload["prompts"], start=1):
        lines.append(f"## {index}. {entry['label']}")
        lines.append("")
        lines.append(f"- prompt: `{entry['prompt']}`")
        if entry.get("negative_prompt"):
            lines.append(f"- negative: `{entry['negative_prompt']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_character_sheet(payload: dict[str, Any]) -> str:
    lines = [f"# {payload['name']}", ""]
    if payload.get("summary"):
        lines.extend([payload["summary"], ""])
    lines.append(f"- prompt fragment: `{payload['prompt_fragment']}`")
    if payload.get("negative_fragment"):
        lines.append(f"- negative fragment: `{payload['negative_fragment']}`")
    if payload.get("tokens"):
        lines.append(f"- tokens: {', '.join(payload['tokens'])}")
    attributes = payload.get("attributes") or {}
    if attributes:
        lines.extend(["", "| attribute | value |", "| --- | --- |"])
        for slot in sorted(attributes):
            lines.append(f"| {slot} | {attributes[slot]} |")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ definition


@dataclass(frozen=True, slots=True)
class StoryTask:
    """One writing stage: how to ask, what shape to accept, how to present it."""

    name: str
    system_prompt: str
    build_prompt: Callable[[dict[str, Any]], str]
    response_model: type[BaseModel]
    render_markdown: Callable[[dict[str, Any]], str]
    default_max_tokens: int = 1200

    def json_schema(self) -> dict[str, Any]:
        return self.response_model.model_json_schema()


_WRITER_SYSTEM = (
    "You are a professional story developer working inside a production studio. "
    "Answer with a single JSON object matching the requested schema and nothing "
    "else: no prose, no explanation, no code fence. Write field values in the "
    "language named in the brief, except image prompts which are always English."
)


STORY_TASKS: dict[str, StoryTask] = {
    "logline": StoryTask(
        name="logline",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_logline_prompt,
        response_model=LoglineResponse,
        render_markdown=_render_logline,
        default_max_tokens=900,
    ),
    "beat_sheet": StoryTask(
        name="beat_sheet",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_beat_sheet_prompt,
        response_model=BeatSheetResponse,
        render_markdown=_render_beat_sheet,
        default_max_tokens=1600,
    ),
    "scene_list": StoryTask(
        name="scene_list",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_scene_list_prompt,
        response_model=SceneListResponse,
        render_markdown=_render_scene_list,
        default_max_tokens=2600,
    ),
    "prose": StoryTask(
        name="prose",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_prose_prompt,
        response_model=ProseResponse,
        render_markdown=_render_prose,
        default_max_tokens=4000,
    ),
    "script": StoryTask(
        name="script",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_script_prompt,
        response_model=ScriptResponse,
        render_markdown=_render_script,
        default_max_tokens=1600,
    ),
    "prompt_pack": StoryTask(
        name="prompt_pack",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_prompt_pack_prompt,
        response_model=PromptPackResponse,
        render_markdown=_render_prompt_pack,
        default_max_tokens=1600,
    ),
    "character_sheet": StoryTask(
        name="character_sheet",
        system_prompt=_WRITER_SYSTEM,
        build_prompt=_character_sheet_prompt,
        response_model=CharacterSheetResponse,
        render_markdown=_render_character_sheet,
        default_max_tokens=1200,
    ),
}


def get_story_task(name: str) -> StoryTask:
    try:
        return STORY_TASKS[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown story task {name!r}; "
            f"expected one of {', '.join(sorted(STORY_TASKS))}"
        ) from exc


def _clamp_int(value: Any, *, low: int, high: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return low
    return max(low, min(high, parsed))


def _summarize_list(value: Any) -> str:
    if not isinstance(value, list):
        return str(value or "")
    parts: list[str] = []
    for entry in value:
        if isinstance(entry, dict):
            parts.append(
                str(entry.get("summary") or entry.get("text") or entry.get("act") or "")
            )
        else:
            parts.append(str(entry))
    return " / ".join(part for part in parts if part)


__all__ = [
    "BEAT_STRUCTURES",
    "STORY_TASKS",
    "StoryTask",
    "get_story_task",
]
