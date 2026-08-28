"""Fan out one story's scene visual requests into trackable generation jobs.

Parent: #65. #250 (``core.story.visual_strategy``) answers *which* strategy
produces a scene's visual (still / Ken Burns / text-to-video / image-to-video);
this module answers what happens next: turn that decision, plus the frozen
composed context #250 already snapshotted onto ``SceneVisualRequest``
(``core.story.visual_manifest``), into a normal ``GenerationRequest`` and
submit it through the existing ``JobService.create_job`` -- the same
job-creation path every other generation request in the studio uses. Nothing
here starts a generator, resolves a model manifest, or talks to the job queue
directly: submission is entirely ``JobService``'s job, and this module's only
responsibility is building the request it is given correctly and recording
what happened, scene by scene.

One scene failing -- an unsupported strategy, no model configured for the
strategy chosen, a ``JobService`` validation error -- must never discard
another scene's already submitted job (see #251's acceptance criteria:
partial failure does not discard successful scene outputs). Every scene is
therefore processed independently and every outcome, success or failure, is
recorded in the returned ``SceneVisualFanoutResult`` rather than raised.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from core.jobs.statuses import JOB_STATUS_FAILED, TERMINAL_JOB_STATUSES
from core.schemas import GenerationRequest
from core.schemas.generation import MediaType

from .binding import scene_binding_params
from .visual_manifest import SceneVisualManifest, SceneVisualRequest
from .visual_strategy import (
    IMAGE_TO_VIDEO,
    KEN_BURNS,
    STILL,
    TEXT_TO_VIDEO,
    VisualCapabilities,
    VisualResourceBudget,
    VisualStrategyDecision,
    VisualStrategyUnavailableError,
    select_visual_strategy,
)

if TYPE_CHECKING:
    from core.jobs.service import JobService
    from core.storage.repositories.job_repository import JobRepository

# Every strategy #250 can choose produces either a still image (STILL,
# KEN_BURNS -- the pan/zoom itself is applied later, at assembly time, over a
# still; see core.story.timeline) or a generated video clip (TEXT_TO_VIDEO,
# IMAGE_TO_VIDEO). This split is a structural fact of the domain, not a
# runtime configuration choice, so unlike the model id it does not need to
# come from the caller.
STRATEGY_MEDIA_TYPE: dict[str, MediaType] = {
    STILL: "image",
    KEN_BURNS: "image",
    TEXT_TO_VIDEO: "video",
    IMAGE_TO_VIDEO: "video",
}


class SceneVisualJobPlan(BaseModel):
    """One scene's fan-out outcome: what was decided, and what happened.

    ``strategy`` and ``request`` are ``None`` only when nothing could be built
    for this scene at all (no usable strategy, or no model configured for the
    one selected) -- the scene still gets an entry here, with ``status``
    ``"failed"`` and ``error_message`` naming why, so a scene that never
    reached job submission stays visible instead of silently missing from the
    result.
    """

    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    visual_request_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    strategy: VisualStrategyDecision | None = None
    request: GenerationRequest | None = None
    job_id: str | None = None
    status: str = JOB_STATUS_FAILED
    error_message: str | None = None


class SceneVisualFanoutResult(BaseModel):
    """The ordered, per-scene outcome of one ``fan_out_scene_visuals`` call."""

    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1)
    items: list[SceneVisualJobPlan] = Field(default_factory=list)

    def refresh(self, job_repository: "JobRepository") -> "SceneVisualFanoutResult":
        """Return a copy with every submitted item's status re-read from ``job_repository``.

        Pure, like ``core.story.merge.apply_text_result``: it reads live job
        state without mutating this result, the repository, or the items list
        in place, so it is safe to call repeatedly (e.g. every time a caller
        polls for progress).
        """

        refreshed: list[SceneVisualJobPlan] = []
        for item in self.items:
            if item.job_id is None:
                refreshed.append(item)
                continue
            job = job_repository.get(item.job_id)
            if job is None:
                refreshed.append(item)
                continue
            refreshed.append(
                item.model_copy(
                    update={"status": job.status, "error_message": job.error_message}
                )
            )
        return self.model_copy(update={"items": refreshed})

    def is_complete(self) -> bool:
        """Whether every scene has reached a terminal outcome.

        A submission failure (``job_id is None``) is already terminal -- there
        is no job left to wait on -- so it counts as complete alongside a
        ``succeeded``/``failed``/``cancelled`` job status.
        """

        return all(
            item.job_id is None or item.status in TERMINAL_JOB_STATUSES
            for item in self.items
        )

    def succeeded_scene_ids(self) -> list[str]:
        """Scene ids whose job has reached ``succeeded``, in manifest order."""

        return [
            item.scene_id
            for item in self.items
            if item.job_id is not None and item.status == "succeeded"
        ]

    def failed_items(self) -> list[SceneVisualJobPlan]:
        """Items that never produced a running job, or whose job failed."""

        return [item for item in self.items if item.status == JOB_STATUS_FAILED]


def fan_out_scene_visuals(
    manifest: SceneVisualManifest,
    job_service: "JobService",
    capabilities: VisualCapabilities,
    strategy_models: dict[str, str],
    budget: VisualResourceBudget | None = None,
    *,
    strategy_task_types: dict[str, str] | None = None,
    project_id: str | None = None,
) -> SceneVisualFanoutResult:
    """Submit one generation job per scene in ``manifest``, independently.

    ``strategy_models`` maps a strategy name (one of
    ``core.story.visual_strategy.VISUAL_STRATEGIES``) to the ``model_id`` to
    request when that strategy is selected for a scene. This module makes no
    model-manifest calls itself -- the same boundary ``select_visual_strategy``
    holds -- so the caller, which already knows what is actually configured in
    this environment, supplies it; a strategy missing from the map fails only
    the scenes that select it. ``strategy_task_types`` is the analogous,
    optional map to a ``task_type``; a strategy absent from it submits with
    ``task_type=None``, which routes to the default generator registered for
    its media type.

    Every scene in ``manifest.requests_in_order()`` is attempted regardless of
    whether an earlier scene's strategy selection or job submission failed:
    partial failure never discards another scene's successfully submitted
    job.
    """

    strategy_task_types = strategy_task_types or {}
    items: list[SceneVisualJobPlan] = []

    for scene_request in manifest.requests_in_order():
        try:
            decision = select_visual_strategy(scene_request, capabilities, budget)
        except VisualStrategyUnavailableError as exc:
            items.append(
                _failed_plan(scene_request, error_message=str(exc))
            )
            continue

        model_id = strategy_models.get(decision.strategy)
        if not model_id:
            items.append(
                _failed_plan(
                    scene_request,
                    strategy=decision,
                    error_message=(
                        f"No model configured for visual strategy "
                        f"{decision.strategy!r}; scene {scene_request.scene_id!r} "
                        "was not submitted."
                    ),
                )
            )
            continue

        request = _build_generation_request(
            scene_request,
            decision,
            model_id=model_id,
            task_type=strategy_task_types.get(decision.strategy),
        )

        try:
            job = job_service.create_job(request, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 - a bad scene must not abort the fan-out
            items.append(
                _failed_plan(
                    scene_request,
                    strategy=decision,
                    request=request,
                    error_message=str(exc),
                )
            )
            continue

        items.append(
            SceneVisualJobPlan(
                story_id=scene_request.story_id,
                scene_id=scene_request.scene_id,
                visual_request_id=scene_request.id,
                order=scene_request.order,
                strategy=decision,
                request=request,
                job_id=job.id,
                status=job.status,
            )
        )

    return SceneVisualFanoutResult(story_id=manifest.story_id, items=items)


def _failed_plan(
    scene_request: SceneVisualRequest,
    *,
    strategy: VisualStrategyDecision | None = None,
    request: GenerationRequest | None = None,
    error_message: str,
) -> SceneVisualJobPlan:
    return SceneVisualJobPlan(
        story_id=scene_request.story_id,
        scene_id=scene_request.scene_id,
        visual_request_id=scene_request.id,
        order=scene_request.order,
        strategy=strategy,
        request=request,
        status=JOB_STATUS_FAILED,
        error_message=error_message,
    )


def _build_generation_request(
    scene_request: SceneVisualRequest,
    decision: VisualStrategyDecision,
    *,
    model_id: str,
    task_type: str | None,
) -> GenerationRequest:
    params: dict[str, Any] = {
        **scene_binding_params(scene_request.story_id, scene_request.scene_id, "visual"),
        "visual_strategy": decision.strategy,
        "visual_strategy_decision": decision.model_dump(mode="json"),
        # The manifest's own request *is* the frozen composed context (#250):
        # composed prompt, Bible/version snapshot, character/location/style
        # refs, reference asset ids, seed, and conflicts. Freezing it wholesale
        # here, rather than re-projecting individual fields by hand, is what
        # keeps this job's provenance exactly in sync with the manifest it
        # came from -- there is no second place a field could be dropped.
        "scene_visual_request": scene_request.model_dump(mode="json"),
    }
    return GenerationRequest(
        media_type=STRATEGY_MEDIA_TYPE[decision.strategy],
        task_type=task_type,
        prompt=scene_request.prompt,
        negative_prompt=scene_request.negative_prompt,
        model_id=model_id,
        seed=scene_request.seed,
        params=params,
    )


__all__ = [
    "STRATEGY_MEDIA_TYPE",
    "SceneVisualFanoutResult",
    "SceneVisualJobPlan",
    "fan_out_scene_visuals",
]
