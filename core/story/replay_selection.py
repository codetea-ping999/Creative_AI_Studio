"""PR3: choose which succeeded Job replays onto a Story scene/role.

``SceneBinder.replay_job_safely()`` (PR #396) only answers "is it safe to
apply *this one* job's binding right now?" -- it deliberately does not decide
*which* job to replay when a scene/role has more than one succeeded, not-yet-
applied candidate (its own docstring says so explicitly). That selection
responsibility is this module's job.

This module never mutates a ``StoryDocument`` itself and never calls
anything other than ``SceneBinder.replay_job_safely()`` to apply a binding --
it only reads (``StoryRepository.get``, ``JobRepository`` job records,
``AssetRepository.get_primary_by_job``) to classify the outcome for one
specific job, deterministically, before deciding whether to call replay at
all.
"""

from __future__ import annotations

from enum import Enum
import logging
from typing import TYPE_CHECKING

from .binding import SCENE_ROLE_PARAM, SCENE_ID_PARAM, STORY_ID_PARAM, SceneBinder

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.jobs.schemas import JobRecord
    from core.storage.repositories.job_repository import JobRepository

    from .repository import StoryRepository

logger = logging.getLogger(__name__)


class ReplayOutcome(Enum):
    """Result of attempting to converge one succeeded job's Story binding."""

    # Applied just now, or was already applied / superseded by a
    # legitimately-chosen sibling candidate, or the job simply is not
    # scene-bound at all -- there is nothing further for this job to do.
    CONVERGED = "converged"
    # A precondition this step depends on has not completed yet (no Asset
    # for this job yet); safe to retry once that catches up.
    RETRYABLE = "retryable"
    # Pre- and post-checks disagree in a way this module cannot explain by
    # any of the known benign races (deleted story/scene, a sibling
    # winning) -- do not guess; leave it for a human or a later retry to
    # resolve rather than silently treating it as success.
    UNRESOLVED = "unresolved"


def select_scene_bound_params(
    job: "JobRecord",
) -> tuple[str, str, str] | None:
    """Return `(story_id, scene_id, role)` if `job` targets a scene, else None."""

    params = job.request.params if isinstance(job.request.params, dict) else {}
    story_id = params.get(STORY_ID_PARAM)
    scene_id = params.get(SCENE_ID_PARAM)
    role = params.get(SCENE_ROLE_PARAM)
    if isinstance(story_id, str) and isinstance(scene_id, str) and isinstance(role, str):
        return story_id, scene_id, role
    return None


def _succeeded_candidates_for_role(
    job_repository: "JobRepository",
    *,
    story_id: str,
    scene_id: str,
    role: str,
) -> list["JobRecord"]:
    """Every succeeded job targeting the same (story, scene, role).

    Uses the decode-failure-tolerant scan (`list_tolerant`), not `list()`:
    an unrelated poison row elsewhere in the table must never block Story
    replay convergence for a completely different job.
    """

    records, _failures = job_repository.list_tolerant()
    candidates = []
    for record in records:
        if record.status != "succeeded":
            continue
        params = select_scene_bound_params(record)
        if params == (story_id, scene_id, role):
            candidates.append(record)
    return candidates


def _select_winner(candidates: list["JobRecord"]) -> "JobRecord":
    """Deterministically pick one candidate to actually attempt replay.

    Newest `created_at` wins, tie-broken by `id` -- but this tie-break only
    ever chooses among candidates that have *not yet been applied at all*
    (the caller only reaches this once it has confirmed the scene/role is
    still unresolved); it is never used to overwrite an already-established
    binding, which `replay_job_safely()`'s own rule, and the pre-check in
    `converge_scene_binding()` below, both already forbid regardless of
    this ordering.
    """

    return max(candidates, key=lambda job: (job.created_at, job.id))


def converge_scene_binding(
    job: "JobRecord",
    *,
    scene_binder: SceneBinder,
    story_repository: "StoryRepository",
    job_repository: "JobRepository",
    asset_repository: "AssetRepository",
) -> ReplayOutcome:
    """Converge `job`'s Story binding, choosing a candidate if needed.

    `job` must already be `status == "succeeded"`; this does not check
    lifecycle state itself (that is the caller's -- completion convergence's
    -- job). Never calls anything other than
    `scene_binder.replay_job_safely()` to mutate the story.
    """

    params = select_scene_bound_params(job)
    if params is None:
        # Not a scene-bound job (a story-text job, an ad-hoc generation) --
        # there is no Story binding to converge; trivially done.
        return ReplayOutcome.CONVERGED
    story_id, scene_id, role = params

    story = story_repository.get(story_id)
    if story is None:
        # Deleted story: no resurrection, and nothing will ever change
        # about that fact -- converged (a safe no-op), not an error.
        return ReplayOutcome.CONVERGED
    scene = next((entry for entry in story.scenes if entry.id == scene_id), None)
    if scene is None:
        # Deleted/regenerated scene list -- same reasoning as above.
        return ReplayOutcome.CONVERGED

    asset = asset_repository.get_primary_by_job(job.id)
    if asset is None:
        # Asset sync for this job has not produced anything (yet). Since
        # completion convergence runs Asset sync before Story replay, this
        # is normally transient (sync hasn't committed) -- retry later.
        return ReplayOutcome.RETRYABLE

    if job.id in scene.job_ids or scene.asset_ids.get(role) is not None:
        # Either this exact job already applied, or a different job
        # currently holds the role -- an old result must never overwrite
        # newer work. replay_job_safely() would independently no-op this
        # too; calling it keeps this the single call site that ever
        # mutates the story, and its own audit trail (log line) intact.
        scene_binder.replay_job_safely(job.id)
        return ReplayOutcome.CONVERGED

    # The role is genuinely unresolved. There may be other succeeded,
    # not-yet-converged candidates racing for it -- pick a deterministic
    # winner rather than assuming this job is the only one.
    candidates = _succeeded_candidates_for_role(
        job_repository, story_id=story_id, scene_id=scene_id, role=role
    )
    if not candidates:  # pragma: no cover - job itself always matches
        candidates = [job]
    winner = _select_winner(candidates)
    if winner.id != job.id:
        # This job lost the race to a candidate that will (or already did)
        # bind the role -- nothing for this job to do; when the winner's
        # own convergence runs, it applies the binding. This is a safe,
        # converged outcome for this job, not a failure.
        return ReplayOutcome.CONVERGED

    result = scene_binder.replay_job_safely(job.id)
    if result is not None:
        return ReplayOutcome.CONVERGED

    # replay_job_safely() returned None even though our own pre-checks said
    # this job should be the winner and the role was unresolved -- re-check
    # rather than assume either success or failure.
    story_after = story_repository.get(story_id)
    if story_after is None:
        return ReplayOutcome.CONVERGED
    scene_after = next((entry for entry in story_after.scenes if entry.id == scene_id), None)
    if scene_after is None:
        return ReplayOutcome.CONVERGED
    if scene_after.asset_ids.get(role) is not None:
        # Someone else legitimately won a concurrent race -- also converged.
        return ReplayOutcome.CONVERGED

    logger.warning(
        "Job %s's Story replay for story %s scene %s role %s returned no "
        "result under conditions this module cannot explain; leaving it "
        "unresolved rather than assuming success.",
        job.id,
        story_id,
        scene_id,
        role,
    )
    return ReplayOutcome.UNRESOLVED


__all__ = [
    "ReplayOutcome",
    "converge_scene_binding",
    "select_scene_bound_params",
]
