"""Attach finished generation jobs back to the scene that asked for them.

Without this, a story can describe five scenes and the studio can generate five
images, and nothing connects the two: the timeline builder would keep reporting
every scene as missing its visual. Binding is driven by job completion events so
it works the same whether a scene was generated from the UI, from the API, or as
part of a batch.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .repository import StoryRepository
from .schemas import SCENE_ASSET_ROLES, StoryDocument

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.jobs import EventBus, JobEvent
    from core.storage.repositories.job_repository import JobRepository

logger = logging.getLogger(__name__)

# Params a generation request carries so its output can find its way home.
STORY_ID_PARAM = "story_id"
SCENE_ID_PARAM = "scene_id"
SCENE_ROLE_PARAM = "scene_role"


def scene_binding_params(
    story_id: str,
    scene_id: str,
    role: str,
) -> dict[str, str]:
    """Build the params that mark a request as belonging to a scene."""

    if role not in SCENE_ASSET_ROLES:
        raise ValueError(
            f"Unknown scene role {role!r}; "
            f"expected one of {', '.join(SCENE_ASSET_ROLES)}"
        )
    return {
        STORY_ID_PARAM: story_id,
        SCENE_ID_PARAM: scene_id,
        SCENE_ROLE_PARAM: role,
    }


class SceneBinder:
    """Bind succeeded jobs to their scene, driven by job events."""

    def __init__(
        self,
        story_repository: StoryRepository,
        job_repository: JobRepository,
        asset_repository: AssetRepository,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        self.story_repository = story_repository
        self.job_repository = job_repository
        self.asset_repository = asset_repository
        self.event_bus = event_bus
        # Two scenes of one story can finish at the same moment on different
        # lanes, and the API's PATCH/apply/delete routes can write the same
        # story concurrently too. Serializing only within this binder isn't
        # enough to protect against those other writers, so the read-modify
        # -write below goes through StoryRepository.mutate(), which holds a
        # lock shared by every writer of this story document.

    def attach_to_event_bus(self) -> None:
        if self.event_bus is None:
            return
        self.event_bus.subscribe(self.handle_job_event)

    def handle_job_event(self, event: JobEvent) -> None:
        """React to a job succeeding. Runs on the job runner thread."""

        if event.type != "job_succeeded":
            return
        job_id = event.payload.get("job_id")
        if not isinstance(job_id, str):
            return
        try:
            self.bind_job(job_id)
        except Exception:  # pragma: no cover - never break the runner
            logger.exception("Failed to bind job %s to its scene.", job_id)

    def bind_job(self, job_id: str) -> StoryDocument | None:
        """Attach a job that just finished generating to the scene it targets.

        This is the live path, and the only one wired to the event bus. A
        role already holding a different asset is expected and gets
        replaced — the user asked to regenerate it, and the newest attempt
        should win.

        Refuses to resurrect a story or scene a concurrent delete (or
        scene-list regeneration) removed: see ``StoryRepository.mutate``.
        """

        return self._bind(job_id, replay=False)

    def replay_job_safely(self, job_id: str) -> StoryDocument | None:
        """Safely re-apply an *already-succeeded* job's binding.

        This is the boundary a future job-recovery path (PR 3 of the
        job-lifecycle-hardening series; recovery itself is not implemented
        here) should call explicitly instead of ``bind_job`` — the two are
        deliberately separate methods, not one method with a mode flag,
        because the safety rules only make sense for a replay and must never
        leak into the live path by accident:

        - a scene/role already carrying *any* asset is left untouched, even
          if it is a different asset than this job's — an old result must
          never overwrite work that happened after it, whether that work was
          this same job being bound already or a different, newer job;
        - a job already recorded on the scene (``job_id in scene.job_ids``)
          is a no-op, so replaying the same completion twice never duplicates
          anything;
        - a story or scene a concurrent delete (or scene-list regeneration)
          removed is never resurrected.

        It is still the caller's job to decide *which* job to replay when a
        scene/role has more than one succeeded candidate — this method only
        ever considers the single ``job_id`` it is given, so it makes no such
        choice on its own.
        """

        return self._bind(job_id, replay=True)

    def _bind(self, job_id: str, *, replay: bool) -> StoryDocument | None:
        job = self.job_repository.get(job_id)
        if job is None or job.status != "succeeded":
            return None

        params = job.request.params if isinstance(job.request.params, dict) else {}
        story_id = params.get(STORY_ID_PARAM)
        scene_id = params.get(SCENE_ID_PARAM)
        role = params.get(SCENE_ROLE_PARAM)
        if not (
            isinstance(story_id, str)
            and isinstance(scene_id, str)
            and isinstance(role, str)
            and role in SCENE_ASSET_ROLES
        ):
            # Not a scene-bound job: story text jobs and ad-hoc generations
            # legitimately carry no scene, so this is a normal exit.
            return None

        asset = self.asset_repository.get_primary_by_job(job_id)
        if asset is None:
            logger.warning(
                "Job %s is bound to scene %s but produced no asset.", job_id, scene_id
            )
            return None

        def _apply_binding(story: StoryDocument | None) -> StoryDocument | None:
            if story is None:
                logger.warning(
                    "Job %s references unknown story %s.", job_id, story_id
                )
                return None

            scenes = []
            matched = False
            skipped = False
            for scene in story.scenes:
                if scene.id != scene_id:
                    scenes.append(scene)
                    continue
                matched = True

                if replay and (
                    job_id in scene.job_ids or scene.asset_ids.get(role)
                ):
                    # Recovery is replaying a job that either was already
                    # bound (nothing to do) or whose role now belongs to a
                    # different, presumably newer, asset — an old result must
                    # never overwrite what is currently there.
                    skipped = True
                    scenes.append(scene)
                    continue

                asset_ids = {**scene.asset_ids, role: asset.id}
                job_ids = list(scene.job_ids)
                if job_id not in job_ids:
                    job_ids.append(job_id)
                scenes.append(
                    scene.model_copy(update={"asset_ids": asset_ids, "job_ids": job_ids})
                )

            if not matched:
                # The scene list was regenerated and this id no longer exists.
                # Dropping the binding is correct: pointing at a scene that is
                # gone would be worse than leaving the asset in the gallery.
                logger.info(
                    "Job %s targets scene %s which no longer exists in story %s.",
                    job_id,
                    scene_id,
                    story_id,
                )
                return None

            if skipped:
                logger.info(
                    "Recovery replay for job %s skipped: scene %s role %s is "
                    "already resolved.",
                    job_id,
                    scene_id,
                    role,
                )
                return None

            return story.model_copy(update={"scenes": scenes})

        return self.story_repository.mutate(story_id, _apply_binding)


__all__ = [
    "SCENE_ID_PARAM",
    "SCENE_ROLE_PARAM",
    "STORY_ID_PARAM",
    "SceneBinder",
    "scene_binding_params",
]
