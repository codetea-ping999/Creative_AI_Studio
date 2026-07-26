"""Story document endpoints: write, expand, and inspect a story."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from apps.api.routes.jobs import CreateJobResponse
from bootstrap import ApplicationServices
from core.schemas import GenerationRequest
from core.story import (
    STORY_FORMATS,
    SUPPORTED_TASKS,
    StoryDocument,
    apply_text_result,
    build_timeline,
    missing_scene_assets,
)

router = APIRouter(prefix="/stories", tags=["stories"])


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
        "task": request.task,
        "story_id": story.id,
        "language": story.language,
        "genre": story.genre,
        "tone": story.tone,
        "audience": story.audience,
        "structure": story.structure,
        **request.params,
    }
    if story.logline and "logline" not in params:
        params["logline"] = story.logline
    if story.beats and "beats" not in params:
        params["beats"] = [beat.model_dump(mode="json") for beat in story.beats]

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
    """Merge a completed text job into the story document."""

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

    structured = job.result.metadata.get("structured")
    task = job.result.metadata.get("story_task")
    if not isinstance(structured, dict) or not isinstance(task, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job result does not carry a structured story payload.",
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


@router.get("/{story_id}/timeline", response_model=TimelineResponse)
def get_story_timeline(
    story_id: str,
    width: int = Query(default=1920, ge=64, le=7680),
    height: int = Query(default=1080, ge=64, le=7680),
    fps: int = Query(default=30, ge=1, le=120),
    services: ApplicationServices = Depends(get_services),
) -> TimelineResponse:
    story = _get_story(services, story_id)

    def lookup(asset_id: str) -> str | None:
        asset = services.asset_repository.get(asset_id)
        return asset.path if asset is not None else None

    try:
        timeline = build_timeline(
            story,
            resolution=(width, height),
            fps=fps,
            asset_path_lookup=lookup,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return TimelineResponse(story_id=story.id, timeline=timeline)


__all__ = ["router"]
