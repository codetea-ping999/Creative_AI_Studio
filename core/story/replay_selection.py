"""PR3: choose which succeeded Job replays onto a Story scene/role.

``SceneBinder.replay_job_safely()`` (PR #396) only answers "is it safe to
apply *this one* job's binding right now?" -- it deliberately does not decide
*which* job to replay when a scene/role has more than one succeeded, not-yet-
applied candidate (its own docstring says so explicitly). That selection
responsibility is this module's job.

This module never mutates a ``StoryDocument`` itself and never calls
anything other than ``SceneBinder.replay_job_safely()`` to apply a binding --
it only reads (``StoryRepository.get_for_recovery``, ``JobRepository`` job
records, ``AssetRepository.get_primary_by_job``) to classify the outcome for
one specific job, deterministically, before deciding whether to call replay
at all.
"""

from __future__ import annotations

from enum import Enum
import logging
from typing import TYPE_CHECKING

from .binding import (
    SCENE_ASSET_ROLES,
    SCENE_ID_PARAM,
    SCENE_ROLE_PARAM,
    STORY_ID_PARAM,
    SceneBinder,
)

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
    # for this job yet, or the Story itself is transiently unreadable);
    # safe to retry once that catches up.
    RETRYABLE = "retryable"
    # Pre- and post-checks disagree in a way this module cannot explain by
    # any of the known benign races (deleted story/scene, a sibling
    # winning), or a relevant candidate exists but cannot currently be
    # decoded well enough to rule out -- do not guess; leave it for a
    # human or a later retry to resolve rather than silently treating it
    # as success.
    UNRESOLVED = "unresolved"


def select_scene_bound_params(
    job: "JobRecord",
) -> tuple[str, str, str] | None:
    """Return `(story_id, scene_id, role)` if `job` targets a scene, else None.

    Validates `role` against `SCENE_ASSET_ROLES`, the same set
    `SceneBinder`/`scene_binding_params()` enforce when a scene-bound
    request is built: a direct `POST /jobs` (or `/generate/...`) caller can
    put an arbitrary string in `params["scene_role"]`, bypassing that
    construction-time check. An unrecognized role has no matching key in
    `scene.asset_ids` for `replay_job_safely()` to ever resolve, so without
    this check here a job with a misspelled role would look scene-bound
    forever (story_id/scene_id both genuinely valid) while replay silently
    no-ops every single attempt -- retried indefinitely instead of being
    classified as "not actually scene-bound" once, up front.
    """

    params = job.request.params if isinstance(job.request.params, dict) else {}
    story_id = params.get(STORY_ID_PARAM)
    scene_id = params.get(SCENE_ID_PARAM)
    role = params.get(SCENE_ROLE_PARAM)
    if (
        isinstance(story_id, str)
        and isinstance(scene_id, str)
        and isinstance(role, str)
        and role in SCENE_ASSET_ROLES
    ):
        return story_id, scene_id, role
    return None


def _succeeded_candidates_for_role(
    job_repository: "JobRepository",
    *,
    story_id: str,
    scene_id: str,
    role: str,
) -> tuple[list["JobRecord"], bool]:
    """Decodable succeeded candidates for `(story_id, scene_id, role)`, plus
    whether an *undecodable* succeeded row might also be relevant.

    Uses the decode-failure-tolerant scan (`list_tolerant`), not `list()`:
    an unrelated poison row elsewhere in the table must never block Story
    replay convergence for a completely different job. A poison row's own
    `failures` entry is checked too (via a raw, payload-only peek that does
    not require the row to fully decode): a succeeded job whose `status`
    column reads fine but whose `request_json`/params cannot be confirmed
    one way or the other must not be silently excluded from candidate
    selection, since it could be the *newest* one -- committing an older
    decodable candidate as the winner would let it bind permanently, and
    `replay_job_safely()` correctly refuses to ever replace an
    already-populated role afterward, even once the poison row is repaired.
    """

    records, failures = job_repository.list_tolerant()
    candidates = []
    for record in records:
        if record.status != "succeeded":
            continue
        params = select_scene_bound_params(record)
        if params == (story_id, scene_id, role):
            candidates.append(record)

    has_unresolvable_poison_sibling = False
    for job_id, _exc in failures:
        raw_status, poison_params = job_repository.peek_raw_request_params(job_id)
        if raw_status != "succeeded":
            continue
        if poison_params is None:
            # request_json itself didn't parse (or the row vanished) --
            # cannot rule out relevance at all.
            has_unresolvable_poison_sibling = True
            break
        candidate = (
            poison_params.get(STORY_ID_PARAM),
            poison_params.get(SCENE_ID_PARAM),
            poison_params.get(SCENE_ROLE_PARAM),
        )
        if candidate == (story_id, scene_id, role):
            has_unresolvable_poison_sibling = True
            break

    return candidates, has_unresolvable_poison_sibling


def _has_usable_output(
    candidate: "JobRecord", asset_repository: "AssetRepository"
) -> bool:
    """Whether `candidate` could ever actually fill the role it targets.

    A succeeded job with no outputs at all (`result.outputs` empty, or no
    `result`) will never produce a synced Asset -- `AssetRepository.
    sync_job()` has nothing to sync. Letting such a job win a winner
    selection (PR3 exact-HEAD audit, second round, P2-1) lets it "win" a
    role it can never actually fill: the loser gives up permanently
    (`converge_scene_binding()`'s own rule below never revisits a lost
    race), while the winner stays `RETRYABLE` forever waiting for an Asset
    that will never arrive -- the role never gets filled by anyone.
    Checking the Asset repository first (not just `candidate.result`)
    also covers a candidate whose Asset already exists from an earlier
    sync, independent of what its `result` payload currently says.
    """

    if asset_repository.get_primary_by_job(candidate.id) is not None:
        return True
    return bool(candidate.result is not None and any(candidate.result.outputs))


def _select_winner(candidates: list["JobRecord"]) -> "JobRecord":
    """Deterministically pick one candidate to actually attempt replay.

    Newest `created_at` wins, tie-broken by `id` -- but this tie-break only
    ever chooses among candidates that have *not yet been applied at all*
    (the caller only reaches this once it has confirmed the scene/role is
    still unresolved, and that no undecodable candidate might also be
    relevant); it is never used to overwrite an already-established
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
        # Not a scene-bound job (a story-text job, an ad-hoc generation, or
        # an unrecognized scene_role) -- there is no Story binding to
        # converge; trivially done.
        return ReplayOutcome.CONVERGED
    story_id, scene_id, role = params

    story, confirmed_absent = story_repository.get_for_recovery(story_id)
    if story is None:
        if confirmed_absent:
            # Deleted story: no resurrection, and nothing will ever change
            # about that fact -- converged (a safe no-op), not an error.
            return ReplayOutcome.CONVERGED
        # The story file exists but could not be read right now (a
        # transient OSError, or malformed JSON that might still get
        # repaired) -- must not be mistaken for a confirmed deletion, or
        # this job's binding would be abandoned forever the moment a
        # temporary read hiccup coincides with its convergence attempt.
        return ReplayOutcome.RETRYABLE
    scene = next((entry for entry in story.scenes if entry.id == scene_id), None)
    if scene is None:
        # Deleted/regenerated scene list -- same reasoning as above.
        return ReplayOutcome.CONVERGED

    if job.id in scene.job_ids or scene.asset_ids.get(role) is not None:
        # Either this exact job already applied, or a different job
        # currently holds the role -- checked *before* requiring this
        # job's own Asset to exist: an older candidate with no outputs at
        # all (so no Asset ever gets synced for it) must still converge
        # once a newer sibling has legitimately bound the role, rather
        # than staying "pending" forever waiting for an Asset that will
        # never come. replay_job_safely() would independently no-op this
        # too; calling it keeps this the single call site that ever
        # mutates the story, and its own audit trail (log line) intact.
        scene_binder.replay_job_safely(job.id)
        return ReplayOutcome.CONVERGED

    asset = asset_repository.get_primary_by_job(job.id)
    if asset is None:
        # Asset sync for this job has not produced anything (yet). Since
        # completion convergence runs Asset sync before Story replay, this
        # is normally transient (sync hasn't committed) -- retry later.
        return ReplayOutcome.RETRYABLE

    # The role is genuinely unresolved. There may be other succeeded,
    # not-yet-converged candidates racing for it -- pick a deterministic
    # winner rather than assuming this job is the only one, and never
    # commit one while an undecodable sibling might also be relevant.
    candidates, has_unresolvable_poison_sibling = _succeeded_candidates_for_role(
        job_repository, story_id=story_id, scene_id=scene_id, role=role
    )
    if has_unresolvable_poison_sibling:
        logger.warning(
            "Job %s's Story replay for story %s scene %s role %s has an "
            "undecodable succeeded sibling that might also target this "
            "role; not committing a winner until it can be classified.",
            job.id,
            story_id,
            scene_id,
            role,
        )
        return ReplayOutcome.UNRESOLVED
    usable_candidates = [
        candidate for candidate in candidates
        if _has_usable_output(candidate, asset_repository)
    ]
    if not usable_candidates:  # pragma: no cover - job itself always usable here
        # `job` reached this point only after its own Asset was already
        # confirmed to exist (see the check above), so it is always a
        # usable candidate; this only fires if `job` itself were somehow
        # missing from `candidates` entirely.
        usable_candidates = [job]
    winner = _select_winner(usable_candidates)
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
    story_after, confirmed_absent_after = story_repository.get_for_recovery(story_id)
    if story_after is None:
        if confirmed_absent_after:
            return ReplayOutcome.CONVERGED
        return ReplayOutcome.RETRYABLE
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
