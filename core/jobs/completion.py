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
    from core.story.replay_selection import SceneCandidateIndex

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
        # PR3 exact-HEAD audit, seventh round, finding 4: job ids whose
        # startup-recovery poison quarantine write failed transiently --
        # see `register_poison_retry_candidate()`.
        self._poison_retry_candidates: dict[str, Exception] = {}

    def register_poison_retry_candidate(self, job_id: str, exc: Exception) -> None:
        """Record `job_id` as needing another quarantine attempt later.

        Called by `run_startup_recovery()` when its own attempt to
        quarantine an undecodable row (`quarantine_poison_row_safely()`)
        fails transiently -- such a row's raw status stays queued/active/
        `cancel_requested`, invisible to every other existing retry
        mechanism (`list_tolerant()` can't decode it to begin with,
        `list_terminal_pending_completion()` only ever sees terminal
        rows), so without an explicit candidate like this it would stay
        stranded for this entire process's lifetime, until the next full
        restart. `run_retry_loop()`'s own tick (see `_retry_poison_
        quarantine_candidates()`) is what actually re-attempts it -- no
        new background mechanism.
        """

        self._poison_retry_candidates[job_id] = exc

    def _retry_poison_quarantine_candidates(self) -> None:
        """Re-attempt quarantine for every still-pending poison retry
        candidate, reusing the exact same `quarantine_poison_row_safely()`
        primitive startup recovery itself uses -- never reimplementing
        any of PR #397's quarantine semantics. A candidate is dropped the
        moment its own attempt no longer reports "transient_write_failure"
        -- resolved one way or another (successfully quarantined this
        time, already resolved by something else, or the row is simply
        gone now) -- so this set can never grow without bound: it only
        ever holds job ids genuinely still stuck right now.
        """

        if not self._poison_retry_candidates:
            return
        from core.jobs.startup_recovery import quarantine_poison_row_safely

        still_pending: dict[str, Exception] = {}
        for job_id, exc in self._poison_retry_candidates.items():
            outcome = quarantine_poison_row_safely(self.job_repository, job_id, exc)
            if outcome == "transient_write_failure":
                still_pending[job_id] = exc
        self._poison_retry_candidates = still_pending

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

    def converge_job(
        self, job_id: str, *, candidate_index: "SceneCandidateIndex | None" = None
    ) -> CompletionOutcome:
        """Converge one job's post-terminal side effects, idempotently.

        Safe to call for a job that is not terminal yet, does not exist, or
        already converged (`SAFE_NOOP` in every case) -- callers (the event
        subscriber, startup recovery, a runtime retry loop) do not need to
        pre-filter.

        `candidate_index`: an optional pre-built `SceneCandidateIndex`
        (see `core.story.replay_selection`) a batch caller converging
        many jobs in one pass builds once (via `build_scene_candidate_
        index()` below) and passes to every `converge_job()` call in that
        same pass, so N jobs cost one Story-replay-candidate scan total
        instead of N (PR3 exact-HEAD audit, sixth round, finding 2). Left
        unset by the live event-bus subscriber, which converges exactly
        one job at a time -- a fresh index is built lazily inside
        `converge_scene_binding()` only if this one job's own convergence
        actually needs candidate selection.
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
                story_outcome_converged, story_outcome_retryable = self._converge_story(
                    job, candidate_index=candidate_index
                )
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

    def _converge_story(
        self, job, *, candidate_index: "SceneCandidateIndex | None" = None
    ) -> tuple[bool, bool]:
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
            candidate_index=candidate_index,
        )
        if outcome is ReplayOutcome.CONVERGED:
            return True, False
        if outcome is ReplayOutcome.RETRYABLE:
            return False, True
        return False, False

    def build_scene_candidate_index(self) -> "SceneCandidateIndex | None":
        """Build one `SceneCandidateIndex` for a batch convergence pass.

        Returns `None` if Story replay is not configured on this
        converger at all (`story_repository`/`scene_binder` unset) --
        mirroring `_converge_story()`'s own early return -- so a caller
        that does not use the Story feature never pays for a scan whose
        result would go unused. A batch caller (startup recovery, `run_
        retry_loop()`) should call this exactly once per pass and pass
        the result to every `converge_job()` call in that same pass (PR3
        exact-HEAD audit, sixth round, finding 2).
        """

        if self.story_repository is None or self.scene_binder is None:
            return None

        from core.story.replay_selection import build_scene_candidate_index

        return build_scene_candidate_index(self.job_repository)

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

        Builds one `SceneCandidateIndex` (see `build_scene_candidate_
        index()`) per tick, shared across every job this tick converges
        -- rebuilt fresh every tick, so it is never held stale across
        ticks, but reused within one tick so a burst of N pending jobs
        costs one Story-replay-candidate scan total, not N (PR3
        exact-HEAD audit, sixth round, finding 2). Only built at all when
        there is at least one completion-pending job this tick: on an
        otherwise-idle system with a large job history, building the
        index unconditionally every tick would itself be a full-table
        `list_tolerant()` scan+decode every `poll_interval_seconds`
        forever, with nothing to actually use it for -- silently
        defeating the whole point of `list_terminal_pending_
        completion()`'s own supporting SQLite index (PR3 exact-HEAD
        audit, seventh round, finding 2).

        Also drives `_retry_poison_quarantine_candidates()` every tick,
        in its own separate `try`/`except` -- a startup-recovery poison
        quarantine write that failed transiently gets retried here too
        (PR3 exact-HEAD audit, seventh round, finding 4), and neither
        this nor the completion-retry work above can block the other:
        an unexpected failure in one still lets the other run every tick.
        """

        while not stop_event.is_set():
            try:
                pending_jobs = self.job_repository.list_terminal_pending_completion()
                if pending_jobs:
                    candidate_index = self.build_scene_candidate_index()
                    for job in pending_jobs:
                        if stop_event.is_set():
                            break
                        self.converge_job(job.id, candidate_index=candidate_index)
            except Exception:
                logger.exception("Completion retry loop iteration failed; continuing.")
            try:
                self._retry_poison_quarantine_candidates()
            except Exception:
                logger.exception(
                    "Poison quarantine retry failed this tick; continuing."
                )
            stop_event.wait(poll_interval_seconds)


__all__ = ["CompletionConverger", "CompletionOutcome"]
