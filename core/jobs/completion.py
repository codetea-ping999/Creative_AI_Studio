"""PR3: converge a terminal Job's post-completion side effects, durably.

"succeeded Job != completion fully applied": a Job's own `status` only says
the generator finished (or failed, or was cancelled). Whether its Asset was
synced, its Story scene bound, and its parent Batch reconciled is tracked
separately, on `JobRecord.completion_state` (see `core/jobs/schemas.py`).
`CompletionConverger.converge_job()` is the single place that ever moves a
job from `completion_state="pending"` to `"done"` -- called from the live
event-bus path, a startup recovery pass, and a runtime retry loop alike, so
all three collapse onto the exact same idempotent logic.

A **succeeded** job's converged side effects are, in order:

1. Asset synchronization (`AssetRepository.sync_job()` -- already
   idempotent: re-syncing an unchanged result is a no-op).
2. Story replay, via `core.story.replay_selection.converge_scene_binding()`,
   which calls `SceneBinder.replay_job_safely()` -- never `bind_job()` or a
   re-published terminal event; see that module for why.
3. Batch reconciliation (`BatchService.reconcile_child_job()`), for every
   terminal job (succeeded, failed, or cancelled) that belongs to a batch.

This module never calls a generator, never touches `status`/`result`/
`error_message` (the generation-level fields), and never resolves a Story
replay ambiguity by guessing -- an unresolved case is recorded via
`completion_error` and left `pending` for a human or a later retry.
"""

from __future__ import annotations

from enum import Enum
import logging
from typing import TYPE_CHECKING

from .statuses import JOB_STATUS_SUCCEEDED, is_terminal_status

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.batches import BatchService
    from core.story import SceneBinder, StoryRepository

    from .events import JobEvent
    from core.storage.repositories.job_repository import JobRepository

logger = logging.getLogger(__name__)

_TERMINAL_JOB_EVENT_TYPES = frozenset({"job_succeeded", "job_failed", "job_cancelled"})


class CompletionOutcome(Enum):
    """Result of one `converge_job()` attempt -- never silently "success"."""

    # Applied just now: this attempt is what made completion_state="done".
    DONE = "done"
    # Nothing to do -- not terminal yet, does not exist, or was already
    # "done" (idempotent re-entry: the live path and a retry/startup pass
    # can both reach the same job).
    SAFE_NOOP = "safe_noop"
    # A step raised, or a Story replay precondition was not met yet
    # (Asset sync had not produced anything). completion_state stays
    # "pending"; completion_error records why. Safe to retry.
    RETRYABLE_FAILURE = "retryable_failure"
    # A Story replay outcome could not be explained by any known benign
    # race. completion_state stays "pending"; a human or a later retry
    # needs to look at completion_error. Never silently treated as done.
    UNRESOLVED = "unresolved"


class CompletionConverger:
    """Owns the single convergence path for terminal Job side effects."""

    def __init__(
        self,
        job_repository: "JobRepository",
        asset_repository: "AssetRepository",
        *,
        story_repository: "StoryRepository | None" = None,
        scene_binder: "SceneBinder | None" = None,
        batch_service: "BatchService | None" = None,
    ) -> None:
        self.job_repository = job_repository
        self.asset_repository = asset_repository
        self.story_repository = story_repository
        self.scene_binder = scene_binder
        self.batch_service = batch_service

    def attach_to_event_bus(self, event_bus) -> None:
        if event_bus is None:
            return
        event_bus.subscribe(self.handle_job_event)

    def handle_job_event(self, event: "JobEvent") -> None:
        """React to a job finishing. Runs on the job runner thread.

        Never raises (`converge_job()` catches everything itself) and never
        needs its own try/except to "protect" completion_state: the only
        statement anywhere that ever writes completion_state="done" is at
        the tail of a fully successful `converge_job()` attempt, so an
        `EventBus.publish()` swallowing an exception from this handler (see
        `EventBus.publish()`'s own per-subscriber isolation) can never be
        mistaken for a completed convergence -- there is no code path where
        a failure here leaves the row anything but "pending".
        """

        if event.type not in _TERMINAL_JOB_EVENT_TYPES:
            return
        job_id = event.payload.get("job_id")
        if not isinstance(job_id, str):
            return
        self.converge_job(job_id)

    def converge_job(self, job_id: str) -> CompletionOutcome:
        """Converge one job's post-terminal side effects, idempotently.

        Safe to call for a job that is not terminal yet, does not exist, or
        already converged (`SAFE_NOOP` in every case) -- callers (the event
        subscriber, startup recovery, a runtime retry loop) do not need to
        pre-filter.
        """

        try:
            job = self.job_repository.get(job_id)
        except Exception:
            # A poison row (see JobRecordDecodeError) or a transient
            # storage error -- either way, this call cannot determine
            # anything about the job right now. Startup recovery's own
            # row-level scan is what quarantines a genuinely poison row;
            # this call just declines to guess.
            logger.exception(
                "Could not read job %s for completion convergence.", job_id
            )
            return CompletionOutcome.UNRESOLVED

        if job is None or not is_terminal_status(job.status):
            return CompletionOutcome.SAFE_NOOP
        if job.completion_state == "done":
            return CompletionOutcome.SAFE_NOOP

        try:
            story_outcome_converged = True
            story_outcome_retryable = False
            if job.status == JOB_STATUS_SUCCEEDED:
                # Never re-runs the generator: this is purely a replay of
                # the already-persisted GenerationResult.
                self.asset_repository.sync_job(job)
                story_outcome_converged, story_outcome_retryable = self._converge_story(job)
            batch_reconciliation_retryable = self._reconcile_batch(job_id)
        except Exception as exc:
            self.job_repository.mark_completion_pending_with_error(job_id, str(exc))
            logger.exception("Completion convergence failed for job %s.", job_id)
            return CompletionOutcome.RETRYABLE_FAILURE

        if not story_outcome_converged:
            reason = (
                "Story replay precondition not met yet (no Asset for this "
                "job); will retry."
                if story_outcome_retryable
                else "Story replay outcome is ambiguous; needs investigation."
            )
            self.job_repository.mark_completion_pending_with_error(job_id, reason)
            return (
                CompletionOutcome.RETRYABLE_FAILURE
                if story_outcome_retryable
                else CompletionOutcome.UNRESOLVED
            )

        if batch_reconciliation_retryable:
            # The owning Batch, if any, could not be read just now (a
            # transient storage failure, not a confirmed "no parent") --
            # marking completion done here would permanently exclude this
            # job from every future retry (PR3 exact-HEAD audit P1-5),
            # even though the Batch's own state never actually reflected
            # this job's terminal outcome.
            self.job_repository.mark_completion_pending_with_error(
                job_id,
                "Owning Batch could not be reconciled right now (transient "
                "storage failure); will retry.",
            )
            return CompletionOutcome.RETRYABLE_FAILURE

        self.job_repository.mark_completion_done(job_id)
        return CompletionOutcome.DONE

    def _converge_story(self, job) -> tuple[bool, bool]:
        """Return `(converged, retryable)` for `job`'s Story replay step."""

        if self.story_repository is None or self.scene_binder is None:
            return True, False

        # Local import: core/jobs must not import core/story at module load
        # time (core/story already imports from core/jobs -- schemas,
        # JobRepository -- so a top-level import here would cycle).
        from core.story.replay_selection import ReplayOutcome, converge_scene_binding

        outcome = converge_scene_binding(
            job,
            scene_binder=self.scene_binder,
            story_repository=self.story_repository,
            job_repository=self.job_repository,
            asset_repository=self.asset_repository,
        )
        if outcome is ReplayOutcome.CONVERGED:
            return True, False
        if outcome is ReplayOutcome.RETRYABLE:
            return False, True
        return False, False

    def _reconcile_batch(self, job_id: str) -> bool:
        """Reconcile `job_id`'s owning Batch, if any.

        Returns whether this step is retryable -- i.e. whether the caller
        must *not* let completion proceed to "done" this attempt. `False`
        covers both "no parent Batch" and "reconciled successfully";
        only a genuinely uncertain read (see `BatchReconciliationOutcome.
        RETRYABLE_FAILURE`) returns `True`.
        """

        if self.batch_service is None:
            return False

        # Local import: mirrors _converge_story()'s core.story import
        # above -- avoids a core.jobs <-> core.batches import-order
        # dependency at module load time.
        from core.batches.service import BatchReconciliationOutcome

        _record, outcome = self.batch_service.reconcile_child_job(job_id)
        return outcome is BatchReconciliationOutcome.RETRYABLE_FAILURE

    def run_retry_loop(self, *, stop_event, poll_interval_seconds: float = 5.0) -> None:
        """Periodically retry every terminal job still completion-pending.

        A minimal, single background thread -- not a `WorkerPool`, not a new
        lane, not a distributed scheduler -- for the *runtime* (not just
        startup) half of "completion retry must be possible": an Asset sync
        that failed transiently, or a Story replay that was retryable, gets
        another chance without requiring a full process restart. Stops as
        soon as `stop_event` is set; a caller (see `apps/api/main.py`) must
        join this thread before releasing data-directory ownership, exactly
        like the main job runner thread.
        """

        while not stop_event.is_set():
            try:
                for job in self.job_repository.list_terminal_pending_completion():
                    if stop_event.is_set():
                        break
                    self.converge_job(job.id)
            except Exception:
                logger.exception("Completion retry loop iteration failed; continuing.")
            stop_event.wait(poll_interval_seconds)


__all__ = ["CompletionConverger", "CompletionOutcome"]
