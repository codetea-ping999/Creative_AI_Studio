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

from .statuses import (
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_SUCCEEDED,
    is_terminal_status,
)

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.batches import BatchService
    from core.story import SceneBinder, StoryRepository
    from core.story.replay_selection import SceneCandidateIndex

    from .events import JobEvent
    from .service import JobService
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
        job_service: "JobService | None" = None,
    ) -> None:
        self.job_repository = job_repository
        self.asset_repository = asset_repository
        self.story_repository = story_repository
        self.scene_binder = scene_binder
        self.batch_service = batch_service
        # PR3 exact-HEAD audit, eighth round, finding 5: needed so a
        # poison-retry candidate that turns out to be repaired (freshly
        # decodable, still genuinely `queued`) can be handed back to the
        # existing, safe `enqueue_job()` path -- see
        # `_retry_poison_quarantine_candidates()`. Optional, like every
        # other collaborator here: a caller that never registers a
        # poison-retry candidate (e.g. a test using this class directly)
        # need not provide one.
        self.job_service = job_service
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

        Before ever quarantining, freshly re-reads the row (PR3
        exact-HEAD audit, eighth round, finding 5): a candidate can sit
        here for several seconds (or, across ticks, much longer) after
        the ORIGINAL decode exception was captured at startup -- if an
        operator or a concurrent process repairs the payload/timestamp
        in that window while the row's raw status is still genuinely
        `queued`, blindly reusing the stale original exception and
        quarantining anyway would incorrectly fail a now-perfectly-valid
        job that was never actually still poison. Revalidating with a
        fresh `JobRepository.get()` distinguishes:

        - decodes successfully now -- no longer poison; drop the
          candidate (unless the Batch-authorization step below decides
          otherwise), and if its current status is still `queued`, hand
          it to `_authorize_recovered_queued_job()` rather than the raw
          `enqueue_job()` path (PR3 exact-HEAD audit, ninth round,
          finding 1) -- see that helper's own docstring for why a
          repaired row cannot be assumed to be an ordinary, non-Batch
          job.
        - still raises `JobRecordDecodeError` -- genuinely still poison;
          proceed with the quarantine attempt exactly as before. A
          successful quarantine here gets the identical Batch-convergence
          follow-up `run_startup_recovery()` gives its own quarantine
          attempts (PR3 exact-HEAD audit, eighth round, adversarial
          follow-up to finding 4): this method calls the exact same
          `quarantine_poison_row_safely()` primitive, so an outcome
          reached via a runtime retry tick must not be treated any
          differently than the identical outcome reached at startup, or
          a Batch whose last poison child happened to resolve here
          instead of at startup would stay stuck on its old stage for
          the rest of the process's life. That follow-up's own outcome
          is itself checked now (PR3 exact-HEAD audit, ninth round,
          finding 3): a `RETRYABLE_FAILURE` (the owning Batch could not
          be read just now) keeps this candidate scheduled rather than
          dropping it, since the terminalized-but-still-undecodable row
          would otherwise have no other path back to Batch convergence
          -- it is invisible to `list_terminal_pending_completion()`
          (still undecodable as a whole `JobRecord`) and published no
          terminal event of its own. A later tick's revalidation will
          hit this exact branch again; `quarantine_poison_row_safely()`
          is itself idempotent for an already-terminal row (an
          `"already_resolved"`/`"left_untouched_terminal"`-class
          outcome), so re-attempting Batch reconciliation on each
          subsequent tick is always safe.
        - row now absent (`None`) -- nothing left to quarantine or
          resume; drop the candidate.
        - any other exception (a transient repository-level read
          failure, not a decode failure) -- cannot tell which of the
          above applies right now; keep the candidate scheduled for a
          later tick rather than guessing either way.
        """

        if not self._poison_retry_candidates:
            return
        from core.batches.service import BatchReconciliationOutcome
        from core.jobs.startup_recovery import (
            QUARANTINE_OUTCOMES_NEEDING_BATCH_CONVERGENCE,
            quarantine_or_defer_for_batch_cancellation,
        )
        from core.storage.repositories.job_repository import JobRecordDecodeError

        still_pending: dict[str, Exception] = {}
        for job_id, exc in self._poison_retry_candidates.items():
            try:
                repaired = self.job_repository.get(job_id)
            except JobRecordDecodeError:
                # `quarantine_or_defer_for_batch_cancellation()` (PR3
                # exact-HEAD audit, tenth round, adversarial follow-up to
                # finding 4) reuses `run_startup_recovery()`'s own
                # Batch-cancellation-precedence check here too: this
                # candidate could just as easily still be waiting when
                # its owning Batch's cancellation becomes durable during
                # ordinary runtime operation, not only at startup --
                # blindly quarantining a raw QUEUED row to `failed` in
                # that case would override the cancellation intent
                # exactly like the original startup-only gap did.
                outcome = quarantine_or_defer_for_batch_cancellation(
                    self.job_repository, self.batch_service, job_id, exc
                )
                if outcome in ("transient_write_failure", "batch_cancellation_uncertain"):
                    still_pending[job_id] = exc
                elif (
                    outcome in QUARANTINE_OUTCOMES_NEEDING_BATCH_CONVERGENCE
                    and self.batch_service is not None
                ):
                    # Mirrors `run_startup_recovery()`'s own follow-up:
                    # this row is now (or was already) at a genuine
                    # terminal status reached via a raw SQL CAS that
                    # published no terminal event of its own.
                    # `reconcile_child_job()` never calls a generator and
                    # is itself idempotent. Isolated per job (PR3
                    # exact-HEAD audit, tenth round, adversarial
                    # follow-up to finding 2): an unguarded exception
                    # here would propagate out of this entire `for` loop,
                    # meaning `self._poison_retry_candidates =
                    # still_pending` below is never reached -- silently
                    # reverting every OTHER candidate this same tick
                    # already resolved back to "still pending," not just
                    # failing to advance this one job.
                    try:
                        _record, reconcile_outcome = self.batch_service.reconcile_child_job(
                            job_id
                        )
                        retryable = (
                            reconcile_outcome is BatchReconciliationOutcome.RETRYABLE_FAILURE
                        )
                    except Exception:
                        logger.exception(
                            "Batch convergence for quarantined job %s raised "
                            "unexpectedly during a poison-retry tick; "
                            "leaving it for a later retry.",
                            job_id,
                        )
                        retryable = True
                    if retryable:
                        still_pending[job_id] = exc
                continue
            except Exception:
                logger.exception(
                    "Poison-retry revalidation read failed for job %s; "
                    "keeping it scheduled for a later attempt.",
                    job_id,
                )
                still_pending[job_id] = exc
                continue
            if repaired is None:
                continue  # confirmed gone -- nothing left to do
            # Decodes cleanly now -- genuinely repaired, never quarantine.
            # Its CURRENT status decides what happens next, not just
            # `queued` (PR3 exact-HEAD audit, tenth round, finding 3): an
            # operator/concurrent repair can land while the row's raw
            # status is anything a startup-time decode failure could have
            # frozen it at -- `preparing`/`running`/`postprocessing`
            # (this process is not the worker that owned it; that worker
            # is confirmed gone) or `cancel_requested`. Dropping the
            # candidate for any of those without transitioning it first
            # would strand it forever: no runtime owner will ever finish
            # it, and it stays invisible to `list_terminal_pending_
            # completion()` (only sees already-terminal rows).
            if self._resolve_repaired_job(job_id, repaired.status):
                still_pending[job_id] = exc
        self._poison_retry_candidates = still_pending

    def _resolve_repaired_job(self, job_id: str, status: str) -> bool:
        """Classify a poison-retry candidate that now decodes cleanly, by
        its current `status` -- returns `True` if the candidate must stay
        scheduled for a later attempt, `False` once it is resolved one
        way or another (PR3 exact-HEAD audit, tenth round, finding 3).

        Reuses `run_startup_recovery()` step 3's own exact classification
        -- `INTERRUPTED_JOB_STATUSES`/`PROCESS_INTERRUPTED_REASON` -- for
        `queued` (this candidate could not exist otherwise; see finding
        1) or active (`preparing`/`running`/`postprocessing`; this
        process never owned this job before whatever crash/restart left
        it undecodable, so it is finalized `failed`, never resumed or
        re-run) or `cancel_requested` (finalized `cancelled`). Never a
        second, drifting copy of that contract -- the same module-level
        constants, imported here.

        Either transition uses the same CAS primitive (`transition_
        if_status`) execution ownership itself uses. Unlike `run_
        startup_recovery()` step 3 (provably single-threaded, running
        strictly before the job runner/retry-loop threads ever start),
        this method runs from the *live* runtime retry loop, where a
        concurrent, genuinely legitimate actor can race this exact CAS
        -- e.g. an operator's `POST /jobs/{id}/cancel` transitioning
        `preparing` to `cancel_requested` in the gap between the
        revalidation read above and this call's own CAS attempt (PR3
        exact-HEAD audit, tenth round, adversarial follow-up to finding
        3). The CAS's own boolean return is checked for exactly this
        reason: a lost race must not be read as "already resolved,
        nothing left to do" and silently dropped -- that would strand
        the job at whatever status it actually raced to, with no
        runtime owner left to ever finalize it (a raced-to `cancel_
        requested` in particular has no active worker to observe its
        own cooperative shutdown and call `finalize_cancellation()`).
        Kept scheduled instead: the next tick's own revalidation read
        picks up the row's true current status fresh and reclassifies
        it correctly.

        No Batch-convergence follow-up is issued directly on a
        successful transition: the job is now a plain, fully decodable
        terminal `JobRecord` with `completion_state="pending"`, so the
        very next `run_retry_loop()` tick's own `list_terminal_pending_
        completion()` pass -- which runs before this method, each tick
        -- picks it up and converges it (including Batch reconciliation)
        through the exact same path any ordinary terminal job takes;
        nothing new needed.

        A row whose status is already terminal (`succeeded`/`failed`/
        `cancelled`) needs no transition at all -- convergence is
        already `converge_job()`'s job via the normal pass above.
        """

        from core.jobs.startup_recovery import (
            INTERRUPTED_JOB_STATUSES,
            PROCESS_INTERRUPTED_REASON,
        )
        from core.jobs.statuses import JOB_STATUS_CANCEL_REQUESTED, JOB_STATUS_CANCELLED

        if status == JOB_STATUS_QUEUED:
            return self._authorize_recovered_queued_job(job_id) == "uncertain"
        if status in INTERRUPTED_JOB_STATUSES:
            ok = self.job_repository.transition_if_status(
                job_id,
                (status,),
                status=JOB_STATUS_FAILED,
                progress=1.0,
                error_message=PROCESS_INTERRUPTED_REASON,
            )
            return not ok
        if status == JOB_STATUS_CANCEL_REQUESTED:
            ok = self.job_repository.transition_if_status(
                job_id,
                (JOB_STATUS_CANCEL_REQUESTED,),
                status=JOB_STATUS_CANCELLED,
                progress=1.0,
            )
            return not ok
        return False

    def _authorize_recovered_queued_job(self, job_id: str) -> str:
        """Enqueue a repaired, still-`queued` job -- but only after
        re-confirming it is not a child of a Batch that has since been
        durably cancelled (PR3 exact-HEAD audit, ninth round, finding 1).

        A poison row can be exactly the child a Batch's own startup
        cancellation sweep (`resume_pending_cancellations()`) could not
        safely expose or terminalize, because it was unreadable at that
        time -- that scan only ever sees decodable rows. Handing such a
        row straight to `JobService.enqueue_job()` once it becomes
        readable again would bypass the durable-cancellation
        authorization boundary entirely, making a child worker-visible
        again after its owning Batch's cancellation already won.

        Delegates to `BatchService.authorize_recovered_queued_job()`,
        which runs the whole "who owns this job, and are they
        cancelling" decision under the same lock `cancel()`'s own
        durable-intent mutation uses -- no new locking/concurrency
        design here, only a narrow, safe reuse of that existing
        boundary. If `batch_service` is not configured on this
        converger at all (some minimal test setups omit it), falls back
        to the plain, safe `enqueue_job()` path -- unchanged from before
        this round for a caller that never wires Batch support in.

        Returns the underlying `authorize_recovered_queued_job()`
        outcome (`"enqueued"`/`"not_queued"`/`"cancelled"`/
        `"uncertain"`) so the caller can decide whether this candidate
        needs to stay scheduled; returns `"enqueued"` for the no-
        `batch_service` fallback and the no-`job_service` no-op alike,
        since neither case leaves anything for a caller to retry.
        """

        if self.job_service is None:
            return "enqueued"
        if self.batch_service is None:
            self.job_service.enqueue_job(job_id)
            return "enqueued"

        outcome = self.batch_service.authorize_recovered_queued_job(
            job_id, JOB_STATUS_QUEUED
        )
        if outcome == "cancelled":
            # The owning Batch's cancellation intent is durably set --
            # this child must never become worker-visible. Terminalize it
            # now (mirrors `_enqueue_stage()`'s own identical handling of
            # a confirmed-cancelling batch) rather than leaving it
            # `queued`-but-never-enqueued forever; `cancel_job()`
            # publishes a terminal event for an already-`queued` job,
            # which reaches the live event-bus path's own
            # `CompletionConverger.converge_job()` -> Batch reconciliation
            # normally -- no separate reconciliation call needed here.
            self.job_service.cancel_job(job_id)
        return outcome

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
        self,
        job_id: str,
        *,
        candidate_index: "SceneCandidateIndex | None" = None,
        batch_ownership_index: "tuple[frozenset[str], bool] | None" = None,
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

        `batch_ownership_index`: an optional pre-built `(owned_job_ids,
        reliable)` pair (see `build_batch_ownership_index()` below),
        identically shared across every `converge_job()` call in one pass
        (PR3 exact-HEAD audit, eleventh round, finding 5) so N pending
        jobs cost one Batch directory scan total, not N -- see
        `_reconcile_batch()` for how it is used.
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
            batch_reconciliation_retryable = self._reconcile_batch(
                job_id, batch_ownership_index=batch_ownership_index
            )
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

    def build_batch_ownership_index(self) -> "tuple[frozenset[str], bool] | None":
        """Build one Batch-ownership snapshot for a convergence pass.

        Returns `None` if no `batch_service` is configured on this
        converger at all -- mirroring `build_scene_candidate_index()`'s
        own early return -- so a caller with no Batch feature enabled
        never pays for a scan whose result would go unused. A batch
        caller (startup recovery, `run_retry_loop()`) should call this
        exactly once per pass and pass the result to every
        `converge_job()` call in that same pass (PR3 exact-HEAD audit,
        eleventh round, finding 5), exactly like `build_scene_candidate_
        index()` already does for Story replay.
        """

        if self.batch_service is None:
            return None

        return self.batch_service.build_job_ownership_index()

    def _reconcile_batch(
        self,
        job_id: str,
        *,
        batch_ownership_index: "tuple[frozenset[str], bool] | None" = None,
    ) -> bool:
        """Reconcile `job_id`'s owning Batch, if any.

        Returns whether this step is retryable -- i.e. whether the caller
        must *not* let completion proceed to "done" this attempt. `False`
        covers both "no parent Batch" and "reconciled successfully";
        only a genuinely uncertain read (see `BatchReconciliationOutcome.
        RETRYABLE_FAILURE`) returns `True`.

        `batch_ownership_index`: an optional `(owned_job_ids, reliable)`
        pair from `build_batch_ownership_index()`. When `reliable` is
        `True` and `job_id` is absent from `owned_job_ids`, this job is
        confirmed to have no owning Batch *as of that one shared scan* --
        identical to `reconcile_child_job()`'s own `NO_PARENT` outcome --
        so the full per-job reconciliation call (its own fresh Batch
        directory scan) is skipped entirely (PR3 exact-HEAD audit,
        eleventh round, finding 5). Never skipped when the index is
        `None`, unreliable, or `job_id` *is* present in it -- an
        unreliable scan could be hiding this exact job's real owner, and
        a job present in the index still needs the real per-job call to
        actually reconcile (advance stage, materialize children, etc.),
        not just confirm ownership.
        """

        if self.batch_service is None:
            return False

        if batch_ownership_index is not None:
            owned_job_ids, reliable = batch_ownership_index
            if reliable and job_id not in owned_job_ids:
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
        audit, seventh round, finding 2). Narrowed further (PR3
        exact-HEAD audit, eleventh round, finding 6): only a `succeeded`
        job's convergence ever calls `_converge_story()` at all (see
        `converge_job()` -- a `failed`/`cancelled` job skips Story replay
        entirely), so a tick whose pending jobs are all `failed`/
        `cancelled` -- a bursty failure spell, a mass batch cancellation
        -- has no use for this index either; building it anyway would be
        the exact same wasted full-table scan+decode this same finding's
        predecessor (seventh round, finding 2) already ruled out for the
        "nothing pending at all" case, just for a slightly less obvious
        "something pending, but none of it succeeded" case instead.

        Builds one Batch-ownership index (see `build_batch_ownership_
        index()`) per tick under the same rule -- shared across every job
        this tick converges, so N pending jobs cost one Batch directory
        scan total, not N (PR3 exact-HEAD audit, eleventh round, finding
        5). Unlike the Story index, this one is not narrowed by outcome:
        `_reconcile_batch()` runs for every terminal job regardless of
        succeeded/failed/cancelled (a Batch needs to know about a failed
        or cancelled child too), so it is built whenever there is at
        least one pending job at all.

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
                    candidate_index = (
                        self.build_scene_candidate_index()
                        if any(job.status == JOB_STATUS_SUCCEEDED for job in pending_jobs)
                        else None
                    )
                    batch_ownership_index = self.build_batch_ownership_index()
                    for job in pending_jobs:
                        if stop_event.is_set():
                            break
                        self.converge_job(
                            job.id,
                            candidate_index=candidate_index,
                            batch_ownership_index=batch_ownership_index,
                        )
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
