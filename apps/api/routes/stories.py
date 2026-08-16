"""Story document endpoints: write, expand, and inspect a story."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from apps.api.routes.generate import resolve_timeline_assets
from apps.api.routes.jobs import CreateJobResponse
from bootstrap import ApplicationServices
from core.schemas import GenerationRequest
from core.story import (
    SCENE_ASSET_ROLES,
    STORY_FORMATS,
    SUPPORTED_TASKS,
    Scene,
    StoryDocument,
    apply_text_result,
    build_timeline,
    missing_scene_assets,
    scene_binding_params,
)

router = APIRouter(prefix="/stories", tags=["stories"])

# Tasks that write into one named scene rather than into the story as a whole.
SCENE_SCOPED_TASKS: frozenset[str] = frozenset({"script"})

# The tracks whose entries reference an asset the UI may want to preview.
_PREVIEWABLE_TRACKS = ("visual", "narration", "music")


class StorySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    project_id: str | None
    logline: str
    format: str
    structure: str
    language: str
    beat_count: int
    scene_count: int
    chapter_count: int
    total_duration_seconds: float
    created_at: str
    updated_at: str

    @classmethod
    def from_story(cls, story: StoryDocument) -> "StorySummaryResponse":
        return cls(
            id=story.id,
            title=story.title,
            project_id=story.project_id,
            logline=story.logline,
            format=story.format,
            structure=story.structure,
            language=story.language,
            beat_count=len(story.beats),
            scene_count=len(story.scenes),
            chapter_count=len(story.chapters),
            total_duration_seconds=story.total_duration_seconds(),
            created_at=story.created_at.isoformat(),
            updated_at=story.updated_at.isoformat(),
        )


class StoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StorySummaryResponse]
    formats: list[str] = Field(default_factory=lambda: list(STORY_FORMATS))


class StoryDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story: dict[str, Any]
    missing_assets: list[dict[str, str]]


class CreateStoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    project_id: str | None = None
    premise: str = ""
    logline: str = ""
    genre: str = ""
    tone: str = ""
    audience: str = ""
    language: str = "ja"
    format: str = "short-video"
    structure: str = "three-act"
    characters: list[str] = Field(default_factory=list)


class UpdateStoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    project_id: str | None = None
    premise: str | None = None
    logline: str | None = None
    genre: str | None = None
    tone: str | None = None
    audience: str | None = None
    language: str | None = None
    format: str | None = None
    structure: str | None = None
    characters: list[str] | None = None


class ExpandStoryRequest(BaseModel):
    """Start a text job for the next writing stage of a story."""

    model_config = ConfigDict(extra="forbid")

    task: str
    model_id: str = ""
    seed: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ApplyResultRequest(BaseModel):
    """Merge a finished text job's structured output into the story."""

    model_config = ConfigDict(extra="forbid")

    job_id: str


class TimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: str
    timeline: dict[str, Any]


def _get_story(services: ApplicationServices, story_id: str) -> StoryDocument:
    story = services.story_repository.get(story_id)
    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Story not found"
        )
    return story


def _scene_brief(scene: Scene) -> str:
    """Flatten a scene into the brief the script task writes dialogue against."""

    parts = (scene.heading, scene.summary, scene.narration)
    return " / ".join(part.strip() for part in parts if part.strip())


def _bind_task_to_scene(story: StoryDocument, params: dict[str, Any]) -> None:
    """Pin a scene-scoped writing task to one scene, in place.

    A language model cannot invent the scene id it is writing for, so the target
    is chosen here and travels on the request. Without it, every multi-scene
    story would merge its dialogue into ``metadata["unassigned_script_lines"]``
    instead of into a scene.
    """

    scene_ids = [scene.id for scene in story.scenes_in_order()]
    if not scene_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Story {story.id} has no scenes yet; run the scene_list stage "
                "before writing a script."
            ),
        )

    raw_scene_id = params.get("scene_id")
    # One scene is unambiguous, so naming it is optional there.
    if raw_scene_id is None and len(scene_ids) == 1:
        raw_scene_id = scene_ids[0]
    if not isinstance(raw_scene_id, str) or not raw_scene_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This task writes into one scene and needs params.scene_id; "
                f"story {story.id} has {', '.join(scene_ids)}."
            ),
        )

    scene = _find_scene(story, raw_scene_id.strip())
    params["scene_id"] = scene.id
    if not str(params.get("scene") or "").strip():
        params["scene"] = _scene_brief(scene)
    if story.characters and not params.get("characters"):
        params["characters"] = list(story.characters)


def _names_a_scene(story: StoryDocument, structured: dict[str, Any]) -> bool:
    """Whether a scene-scoped payload has a target ``apply_text_result`` will use.

    Mirrors the target selection in ``core.story.merge._merge_script``. A named
    but unknown target counts as named: the merge rejects it by name, which is a
    better error than the generic one below.
    """

    scene_id = structured.get("scene_id")
    if isinstance(scene_id, str) and scene_id:
        return True
    scene_index = structured.get("scene_index")
    if isinstance(scene_index, int) and not isinstance(scene_index, bool):
        return True
    # One scene is unambiguous, so an unnamed payload still lands.
    return len(story.scenes) == 1


def _no_scene_target_detail(story: StoryDocument, job_id: str, task: str) -> str:
    """Explain which stage to re-run so the lines land in a scene."""

    scene_ids = [scene.id for scene in story.scenes_in_order()]
    where = (
        f"story {story.id} has no scenes yet, so run the scene_list stage first, then"
        if not scene_ids
        else (
            f"story {story.id} has {len(scene_ids)} scenes ({', '.join(scene_ids)}), "
            "so the target has to be named:"
        )
    )
    return (
        f"Job {job_id} carries {task} lines but names no scene; {where} "
        f"queue it with POST /stories/{story.id}/expand and params.scene_id."
    )


@router.get("", response_model=StoryListResponse)
def list_stories(
    project_id: str | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
    services: ApplicationServices = Depends(get_services),
) -> StoryListResponse:
    stories = services.story_repository.list_all(
        project_id=project_id, query_text=query, limit=limit
    )
    return StoryListResponse(
        items=[StorySummaryResponse.from_story(story) for story in stories]
    )


@router.post("", response_model=StorySummaryResponse, status_code=status.HTTP_201_CREATED)
def create_story(
    request: CreateStoryRequest,
    services: ApplicationServices = Depends(get_services),
) -> StorySummaryResponse:
    if request.project_id is not None:
        if services.project_repository.get(request.project_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
            )
    try:
        story = services.story_repository.create(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return StorySummaryResponse.from_story(story)


@router.get("/tasks", response_model=list[str])
def list_story_tasks() -> list[str]:
    return list(SUPPORTED_TASKS)


@router.get("/{story_id}", response_model=StoryDetailResponse)
def get_story(
    story_id: str,
    services: ApplicationServices = Depends(get_services),
) -> StoryDetailResponse:
    story = _get_story(services, story_id)
    return StoryDetailResponse(
        story=story.model_dump(mode="json"),
        missing_assets=missing_scene_assets(story),
    )


@router.patch("/{story_id}", response_model=StorySummaryResponse)
def update_story(
    story_id: str,
    request: UpdateStoryRequest,
    services: ApplicationServices = Depends(get_services),
) -> StorySummaryResponse:
    _get_story(services, story_id)
    try:
        story = services.story_repository.update(
            story_id, **request.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return StorySummaryResponse.from_story(story)


@router.delete("/{story_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_story(
    story_id: str,
    services: ApplicationServices = Depends(get_services),
) -> None:
    if not services.story_repository.delete(story_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Story not found"
        )


@router.post(
    "/{story_id}/expand",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def expand_story(
    story_id: str,
    request: ExpandStoryRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    """Queue the text job for a writing stage, seeded from the story so far."""

    story = _get_story(services, story_id)
    params: dict[str, Any] = {
        # Story context the caller may tune for a single run (a scene written in
        # a different tone, say) without editing the story itself.
        "language": story.language,
        "genre": story.genre,
        "tone": story.tone,
        "audience": story.audience,
        "structure": story.structure,
        **request.params,
        # Server-owned, so they are written AFTER the caller's params rather than
        # before. `task` is the field the request already names, and `story_id` is
        # the job's owner: if a caller could set it, a job queued from story A
        # could be made to carry story B's id, which `/apply` then reads as
        # permission to merge A's text into B — the exact silent cross-story
        # contamination that check exists to stop.
        "task": request.task,
        "story_id": story.id,
    }
    if story.logline and "logline" not in params:
        params["logline"] = story.logline
    if story.beats and "beats" not in params:
        params["beats"] = [beat.model_dump(mode="json") for beat in story.beats]
    if request.task in SCENE_SCOPED_TASKS:
        _bind_task_to_scene(story, params)

    generation_request = GenerationRequest(
        media_type="text",
        task_type="story",
        prompt=params.pop("prompt", None) or story.premise or story.logline or story.title,
        model_id=request.model_id,
        seed=request.seed,
        params=params,
    )
    if not generation_request.prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Story needs a premise, logline, or title before it can be expanded.",
        )

    job = services.job_service.create_job(
        generation_request, project_id=story.project_id
    )
    if story.project_id is not None:
        services.project_repository.add_job(story.project_id, job.id)
    return CreateJobResponse(job_id=job.id, status=job.status)


@router.post("/{story_id}/apply", response_model=StoryDetailResponse)
def apply_story_result(
    story_id: str,
    request: ApplyResultRequest,
    services: ApplicationServices = Depends(get_services),
) -> StoryDetailResponse:
    """Merge a completed text job into the story document.

    A job started by ``/expand`` carries its ``story_id``, and only that story may
    take the result: mixing one story's prose into another is far more likely to
    happen by a mistyped id than by malice, and it is silent once merged. A job
    with no ``story_id`` (a hand-built ``POST /generate/text``) belongs to no
    story, so any story may adopt it.
    """

    story = _get_story(services, story_id)
    job = services.job_repository.get(request.job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        )
    if job.status != "succeeded" or job.result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {request.job_id} has not succeeded yet (status: {job.status}).",
        )

    job_params = job.request.params if isinstance(job.request.params, dict) else {}
    job_story_id = job_params.get("story_id")
    if isinstance(job_story_id, str) and job_story_id and job_story_id != story.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Job {request.job_id} was written for story {job_story_id}, "
                f"not {story.id}."
            ),
        )

    structured = job.result.metadata.get("structured")
    task = job.result.metadata.get("story_task")
    if not isinstance(structured, dict) or not isinstance(task, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job result does not carry a structured story payload.",
        )

    if task in SCENE_SCOPED_TASKS:
        # The scene was chosen when the job was queued; the model's payload has
        # no way to name it, so the target is restored from the request here.
        scene_id = job_params.get("scene_id")
        if isinstance(scene_id, str) and scene_id:
            structured = {**structured, "scene_id": scene_id}
        elif not _names_a_scene(story, structured):
            # Merging would park the lines in metadata["unassigned_script_lines"],
            # where no route can read them back into a scene: the dialogue would be
            # generated, reported as applied, and lost. Refusing with the stage to
            # re-run keeps the work recoverable.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_no_scene_target_detail(story, request.job_id, task),
            )

    try:
        merged = apply_text_result(story, task, structured, job_id=job.id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    saved = services.story_repository.save(merged)
    return StoryDetailResponse(
        story=saved.model_dump(mode="json"),
        missing_assets=missing_scene_assets(saved),
    )


def _public_output_url(services: ApplicationServices, asset_path: str) -> str | None:
    """Map a stored asset path onto the ``/outputs`` mount, or give up.

    The client is served files through that mount, so it never needs — and must
    never be handed — where the file lives on this machine. Anything outside the
    served root has no public URL, and is simply omitted.
    """

    output_root = services.output_dir.parent
    try:
        relative = Path(asset_path).resolve().relative_to(output_root.resolve())
    except (OSError, ValueError):
        return None
    return f"/outputs/{relative.as_posix()}"


def _attach_preview_urls(
    services: ApplicationServices, timeline: dict[str, Any]
) -> dict[str, Any]:
    """Add a servable URL beside each asset id, in place."""

    tracks = timeline.get("tracks")
    if not isinstance(tracks, dict):
        return timeline
    urls: dict[str, str | None] = {}
    for track_name in _PREVIEWABLE_TRACKS:
        for entry in tracks.get(track_name) or []:
            asset_id = entry.get("asset_id") if isinstance(entry, dict) else None
            if not isinstance(asset_id, str) or not asset_id:
                continue
            if asset_id not in urls:
                asset = services.asset_repository.get(asset_id)
                urls[asset_id] = (
                    _public_output_url(services, asset.path)
                    if asset is not None
                    else None
                )
            preview_url = urls[asset_id]
            if preview_url is not None:
                entry["preview_url"] = preview_url
    return timeline


@router.get("/{story_id}/timeline", response_model=TimelineResponse)
def get_story_timeline(
    story_id: str,
    width: int = Query(default=1920, ge=64, le=7680),
    height: int = Query(default=1080, ge=64, le=7680),
    fps: int = Query(default=30, ge=1, le=120),
    services: ApplicationServices = Depends(get_services),
) -> TimelineResponse:
    """Return the assembly timeline, with previews as ``/outputs`` URLs.

    Entries carry ``asset_id`` and, when the asset is served, ``preview_url`` —
    never a host filesystem path.
    """

    story = _get_story(services, story_id)
    try:
        timeline = build_timeline(
            story,
            resolution=(width, height),
            fps=fps,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return TimelineResponse(
        story_id=story.id, timeline=_attach_preview_urls(services, timeline)
    )


class GenerateSceneRequest(BaseModel):
    """Queue the media a scene still needs, bound back to that scene."""

    model_config = ConfigDict(extra="forbid")

    role: str
    model_id: str = ""
    seed: int | None = None
    output_format: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def _find_scene(story: StoryDocument, scene_id: str) -> Scene:
    scene = next((entry for entry in story.scenes if entry.id == scene_id), None)
    if scene is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scene not found: {scene_id}",
        )
    return scene


def _scene_generation_request(
    story: StoryDocument,
    scene: Scene,
    request: GenerateSceneRequest,
) -> GenerationRequest:
    """Build the media request a scene role implies.

    The scene already carries what each role needs — an image prompt, narration
    text, a music mood — so the caller only chooses the role and the model.
    """

    binding = scene_binding_params(story.id, scene.id, request.role)
    params: dict[str, Any] = {**binding, **request.params}

    if request.role == "visual":
        prompt = str(request.params.get("prompt") or scene.image_prompt).strip()
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Scene {scene.id} has no image prompt to generate from.",
            )
        params.pop("prompt", None)
        if scene.bible_refs and "bible_refs" not in params:
            params["bible_refs"] = list(scene.bible_refs)
        return GenerationRequest(
            media_type="image",
            prompt=prompt,
            negative_prompt=scene.image_negative or None,
            model_id=request.model_id,
            seed=request.seed,
            output_format=request.output_format,
            params=params,
        )

    if request.role == "narration":
        prompt = str(request.params.get("prompt") or scene.narration).strip()
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Scene {scene.id} has no narration text to speak.",
            )
        params.pop("prompt", None)
        return GenerationRequest(
            media_type="audio",
            task_type="text-to-speech",
            prompt=prompt,
            model_id=request.model_id,
            seed=request.seed,
            output_format=request.output_format,
            params=params,
        )

    # music
    mood = str(request.params.get("prompt") or scene.bgm_mood).strip()
    if not mood:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Scene {scene.id} has no bgm mood to score from.",
        )
    params.pop("prompt", None)
    params.setdefault("duration_seconds", max(1, round(scene.duration_seconds)))
    return GenerationRequest(
        media_type="audio",
        task_type="text-to-music",
        prompt=mood,
        model_id=request.model_id,
        seed=request.seed,
        output_format=request.output_format,
        params=params,
    )


@router.post(
    "/{story_id}/scenes/{scene_id}/generate",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_scene_media(
    story_id: str,
    scene_id: str,
    request: GenerateSceneRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    """Generate one role of one scene; the result binds back automatically."""

    if request.role not in SCENE_ASSET_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown scene role {request.role!r}; "
                f"expected one of {', '.join(SCENE_ASSET_ROLES)}"
            ),
        )

    story = _get_story(services, story_id)
    scene = _find_scene(story, scene_id)
    generation_request = _scene_generation_request(story, scene, request)

    job = services.job_service.create_job(
        generation_request, project_id=story.project_id
    )
    if story.project_id is not None:
        services.project_repository.add_job(story.project_id, job.id)
    return CreateJobResponse(job_id=job.id, status=job.status)


class AssembleStoryRequest(BaseModel):
    """Render the story's scenes into one MP4."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1920, ge=64, le=7680)
    height: int = Field(default=1080, ge=64, le=7680)
    fps: int = Field(default=30, ge=1, le=120)
    include_subtitles: bool = True


@router.post(
    "/{story_id}/assemble",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def assemble_story(
    story_id: str,
    request: AssembleStoryRequest,
    services: ApplicationServices = Depends(get_services),
) -> CreateJobResponse:
    """Build the timeline from the story and queue the assembly job.

    Timeline entries stay as asset ids, and every one of them is resolved here
    against the story's project through the same check ``POST /generate/assembly``
    uses, so a story cannot pull in another project's media.
    """

    story = _get_story(services, story_id)
    try:
        timeline = build_timeline(
            story,
            resolution=(request.width, request.height),
            fps=request.fps,
            include_subtitles=request.include_subtitles,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    timeline = resolve_timeline_assets(services, timeline, story.project_id)

    generation_request = GenerationRequest(
        media_type="video",
        task_type="assembly",
        prompt=story.title or story.logline or story.id,
        model_id="",
        output_format="mp4",
        params={"timeline": timeline, "story_id": story.id},
    )
    job = services.job_service.create_job(
        generation_request, project_id=story.project_id
    )
    if story.project_id is not None:
        services.project_repository.add_job(story.project_id, job.id)
    return CreateJobResponse(job_id=job.id, status=job.status)


__all__ = ["SCENE_SCOPED_TASKS", "router"]
