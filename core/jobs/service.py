"""Job orchestration service used by the API layer."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from typing import TYPE_CHECKING, cast

from core.models import ModelService
from core.reference_capabilities import (
    DEFAULT_REFERENCE_STRENGTH,
    REFERENCE_ROLES,
    MissingReferenceAssetError,
    ReferenceImageInput,
    ReferenceRole,
    UnsupportedReferenceError,
    validate_reference_inputs,
)
from core.schemas import GenerationRequest, GenerationResult, GenerationStatus

from .events import EventBus
from .lanes import LaneConfig, assign_lane

if TYPE_CHECKING:
    from core.assets import AssetRepository
    from core.bible import BibleRepository
    from core.models.manifest import ModelManifest
    from core.storage.repositories.job_repository import JobRepository
    from .cancellation import CancellationRegistry
    from .queue import JobQueue
from .schemas import JobRecord
from .statuses import (
    ACTIVE_JOB_STATUSES,
    JOB_STATUS_CANCEL_REQUESTED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
)

_STATUS_TO_EVENT = {
    JOB_STATUS_QUEUED: "job_queued",
    JOB_STATUS_PREPARING: "job_preparing",
    JOB_STATUS_RUNNING: "job_started",
    JOB_STATUS_POSTPROCESSING: "job_postprocessing",
    JOB_STATUS_SUCCEEDED: "job_succeeded",
    JOB_STATUS_FAILED: "job_failed",
    JOB_STATUS_CANCEL_REQUESTED: "job_cancel_requested",
    JOB_STATUS_CANCELLED: "job_cancelled",
}

# A result is only committed after generation has crossed the postprocessing
# boundary. This makes a persisted cancellation request authoritative even if
# a generator returns a result at the same instant: losing this CAS to a
# concurrent cancel leaves the job `cancel_requested`, not `succeeded`.
# Resolving that `cancel_requested` to a terminal state is deliberately left
# to the caller (see JobRunner.process_job's check right after calling
# mark_succeeded, post-#395 audit P1) rather than done unconditionally here:
# a bare CAS-miss can also mean "already succeeded/failed" or a stale direct
# call with no runner in the loop, and this method stays a no-op for those
# (see test_success_only_commits_from_postprocessing).
_SUCCEEDABLE_JOB_STATUSES = (JOB_STATUS_POSTPROCESSING,)


class JobService:
    """Create, queue, and update jobs without executing generators directly."""

    def __init__(
        self,
        job_repository: JobRepository,
        job_queue: "JobQueue",
        event_bus: EventBus | None = None,
        asset_repository: AssetRepository | None = None,
        cancellation_registry: "CancellationRegistry | None" = None,
        model_service: ModelService | None = None,
        lane_config: LaneConfig | None = None,
        bible_repository: "BibleRepository | None" = None,
    ) -> None:
        self.job_repository = job_repository
        self.job_queue = job_queue
        self.event_bus = event_bus
        self.asset_repository = asset_repository
        self.cancellation_registry = cancellation_registry
        self.model_service = model_service
        # Only used by create_job()'s project-boundary check on Bible-derived
        # (bible_refs) references (#201 follow-up) -- optional like the
        # other repositories above, so existing callers that construct
        # JobService without it keep working, just without that check.
        self.bible_repository = bible_repository
        # #180: when set to a genuinely multi-lane configuration, enqueue_job
        # routes each job to `assign_lane(media_type, task_type, lane_config)`
        # instead of the queue's implicit single lane. Left as None (the
        # default) this is a no-op and enqueue_job behaves exactly as before
        # lanes existed -- required so every existing caller that constructs
        # a `JobQueue()` with no lanes keeps working unchanged.
        self.lane_config = lane_config

    def create_job(
        self,
        request: GenerationRequest,
        project_id: str | None = None,
    ) -> JobRecord:
        self.validate_references(request, project_id)
        now = datetime.now(timezone.utc)
        job = JobRecord(
            id=f"job_{uuid4().hex}",
            project_id=project_id,
            media_type=request.media_type,
            status=JOB_STATUS_QUEUED,
            request=request,
            progress=0.0,
            created_at=now,
            updated_at=now,
        )
        created_job = self.job_repository.create(job)
        self._publish(
            "job_created",
            {
                "job_id": created_job.id,
                "status": created_job.status,
                "media_type": created_job.media_type,
            },
        )
        self.enqueue_job(created_job.id)
        return created_job

    def validate_references(
        self,
        request: GenerationRequest,
        project_id: str | None = None,
    ) -> None:
        """Raise if `request`'s references can't be honored by `create_job()`.

        Split out from `create_job()` (#201 follow-up, tenth Codex round on
        PR #376) so `BatchService` can preflight every expanded item's
        request *before* creating the batch record or any of its jobs --
        otherwise a later item's reference failure surfaces after earlier
        items already queued jobs, leaving orphaned state behind a 4xx
        response that told the caller nothing was created.
        """

        # #198/#50: a request carrying reference images must fail here, before a
        # job is ever persisted or queued, if the resolved model can't honor
        # them -- not silently once a generator that ignores `references`
        # reaches the front of the queue. model_service is optional (some
        # tests construct JobService without one); without it we cannot
        # resolve a manifest, so there is nothing to validate against.
        if request.references:
            manifest = self._resolve_manifest_for_references(request)
            if manifest is not None:
                # #201 follow-up (Codex P2, fifteenth round): validating the
                # raw list let two identical entries in request.references
                # (same asset_id/role/strength/preprocessing) trip the
                # shipped max_references_per_role=1 here, even though
                # ImageGenerator._resolve_references_for_conditioning()
                # already collapses identical entries -- within
                # request.references, and across it and Bible-derived
                # references -- to one semantic lock before it validates
                # anything, so execution could have honored this as one
                # reference.
                seen_direct_references: set[tuple[str, str, float, str]] = set()
                deduped_direct_references: list[ReferenceImageInput] = []
                for reference in request.references:
                    direct_dedupe_key = (
                        reference.asset_id,
                        reference.role,
                        reference.strength,
                        reference.preprocessing,
                    )
                    if direct_dedupe_key in seen_direct_references:
                        continue
                    seen_direct_references.add(direct_dedupe_key)
                    deduped_direct_references.append(reference)
                validate_reference_inputs(
                    deduped_direct_references,
                    capability=manifest.reference_capability,
                    model_id=request.model_id or manifest.public_model_id,
                )
                self._require_img2img_mode(manifest, request.model_id)
        if request.references and self.asset_repository is not None:
            # #201 follow-up (Codex P1 on PR #376): a reference asset from a
            # different project must not silently condition a job in this
            # one -- the same exact-project-membership boundary
            # apps/api/routes/generate.py's assembly-request path already
            # enforces for timeline assets (asset.project_id != project_id).
            # An asset that does not exist at all is left to the generator's
            # own MissingReferenceAssetError at execution time (unchanged);
            # this only rejects a reference that resolves to an asset
            # belonging to a *different* project than this job targets.
            for reference in request.references:
                self._reject_cross_project_reference_asset(
                    self.asset_repository, reference.asset_id, project_id
                )
        bible_reference_inputs: list[ReferenceImageInput] = []
        if (
            request.media_type == "image"
            and self.asset_repository is not None
            and self.bible_repository is not None
        ):
            # #201 follow-up (Codex P1, second round): the check above only
            # covers the documented request.references field. A
            # Bible-derived character/location reference (params.bible_refs)
            # resolves its asset the same way once PromptComposer runs
            # inside the generator (resolved_prompt.resolved_references),
            # with the identical cross-project exposure risk if left
            # unchecked here. Only character/location entries ever feed
            # pixel conditioning (see PromptComposer.compose()) -- a
            # style/brand/prop entry's reference_asset_ids never reach
            # img2img, so those are left alone. An unknown bible entry id is
            # tolerated here exactly as it is everywhere else
            # (PromptComposer degrades it to a warning, see
            # _resolve_entries): this early check must not reject a request
            # for a problem the real resolution already handles gracefully.
            # Gated to image jobs (Codex P2, third round): no other media
            # type performs reference-image conditioning at all, so a
            # text/audio/video job carrying bible_refs that happens to name
            # a character/location entry with a cross-project image must not
            # be rejected for a risk that generator can never act on.
            bible_refs = request.params.get("bible_refs")
            if isinstance(bible_refs, list):
                seen_bible_references: set[tuple[str, str]] = set()
                for entry_id in bible_refs:
                    if not isinstance(entry_id, str):
                        continue
                    entry = self.bible_repository.get(entry_id)
                    if entry is None or entry.kind not in REFERENCE_ROLES:
                        continue
                    role = cast(ReferenceRole, entry.kind)
                    for asset_id in entry.reference_asset_ids:
                        self._reject_cross_project_reference_asset(
                            self.asset_repository, asset_id, project_id
                        )
                        # Mirrors PromptComposer.compose()'s own dedup+
                        # construction (core/prompting/composer.py's
                        # seen_resolved_references) so this validates exactly
                        # the reference inputs the generator will later
                        # resolve: two Bible entries naming the same
                        # (asset, role) collapse to one reference there, so
                        # counting each occurrence here would reject a
                        # request the generator can actually honor.
                        dedupe_key = (asset_id, role)
                        if dedupe_key in seen_bible_references:
                            continue
                        seen_bible_references.add(dedupe_key)
                        bible_reference_inputs.append(
                            ReferenceImageInput(
                                asset_id=asset_id,
                                role=role,
                                strength=DEFAULT_REFERENCE_STRENGTH,
                            )
                        )
                if bible_reference_inputs:
                    # #201 follow-up (Codex P2, ninth round): request.references
                    # is validated against manifest.reference_capability above
                    # (line ~108), but a Bible-derived reference reached job
                    # creation with no equivalent check -- a same-project
                    # reference against a manifest that doesn't support it (or
                    # the wrong role/strength/count) was queued successfully
                    # and only failed later, asynchronously, once the
                    # generator's own validate_reference_inputs() ran.
                    manifest = self._resolve_manifest_for_references(request)
                    if manifest is not None:
                        validate_reference_inputs(
                            bible_reference_inputs,
                            capability=manifest.reference_capability,
                            model_id=request.model_id or manifest.public_model_id,
                        )
                        self._require_img2img_mode(manifest, request.model_id)
        # #201 follow-up (Codex P2, tenth round): per-role capability
        # validation (above, for both request.references and Bible-derived
        # references) allows e.g. one character *and* one location reference
        # -- each within its own per-role limit -- but
        # ImageGenerator._resolve_references_for_conditioning() separately
        # enforces a hard "exactly one reference total" limit across
        # request.references and Bible-derived references combined,
        # regardless of role, because this conditioning path only ever
        # performs a single img2img call. Preflight that same total here
        # (unconditionally, not nested under the Bible-specific gates above,
        # so a plain request.references request with more than one entry is
        # also caught) so a combination that is deterministically doomed at
        # execution time fails now, synchronously, instead of after the job
        # is queued.
        #
        # #201 follow-up (Codex P2, eleventh round): request.references and
        # Bible-derived references are independent sources -- a caller can
        # supply the same (asset, role, strength, preprocessing) explicitly
        # and also have it resolve through a Bible entry, which is one
        # semantic lock, not two. ImageGenerator._resolve_references_for_conditioning()
        # dedupes the same way across the same two sources, so the count
        # here must match or a request the generator can honor would be
        # rejected before it ever reaches the generator.
        seen_combined_references: set[tuple[str, str, float, str]] = set()
        combined_references: list[ReferenceImageInput] = []
        for reference in list(request.references or []) + bible_reference_inputs:
            # Named distinctly from the bible-only loop's `dedupe_key` above
            # (a narrower (str, str) pair) -- mypy infers a variable's type
            # from its first assignment in the function, so reusing the same
            # name for this wider (str, str, float, str) tuple was a type
            # error CI caught (round twelve): "Incompatible types in
            # assignment" / "Argument 1 to add() ... incompatible type".
            combined_dedupe_key = (
                reference.asset_id,
                reference.role,
                reference.strength,
                reference.preprocessing,
            )
            if combined_dedupe_key in seen_combined_references:
                continue
            seen_combined_references.add(combined_dedupe_key)
            combined_references.append(reference)
        # #201 follow-up (Codex P2, fourteenth round, confirmed product
        # decision): strength=0 means "no effect" -- ImageGenerator._resolve_
        # references_for_conditioning() excludes a zero-strength reference
        # from its "exactly one reference" limit and primary selection,
        # since it never reaches img2img either way. Preflighting the raw
        # (unfiltered) combined count here rejected a request the generator
        # could actually honor; only non-zero-strength references consume
        # the single applied-image slot.
        effective_combined_references = [
            reference for reference in combined_references if reference.strength > 0.0
        ]
        if len(effective_combined_references) > 1:
            raise UnsupportedReferenceError(
                f"Model {request.model_id!r}: reference-image conditioning "
                "honors exactly one reference image per request (across "
                "`references` and Bible-derived character/location entries "
                f"combined); got {len(effective_combined_references)} with "
                "non-zero strength."
            )
        if len(effective_combined_references) == 1:
            # #201 follow-up (Codex P2, twelfth round): diffusers img2img
            # runs int(num_inference_steps * (1 - strength)) denoising steps
            # -- ImageGenerator.generate() rejects a combination that leaves
            # zero (see its own identical computation), but the shipped
            # "sdxl" manifest advertises max_strength=1.0, which zeroes out
            # regardless of step count. Preflighting the same computation
            # here, using the same effective_params merge (manifest defaults
            # then request.params) the generator uses, catches a
            # deterministically doomed job before it's queued.
            manifest = self._resolve_manifest_for_references(request)
            if manifest is not None:
                primary = effective_combined_references[0]
                # #201 follow-up (Codex P2, thirteenth round): mirrors
                # ImageGenerator.generate()'s own preprocessing and
                # incompatible-param checks -- this conditioning path only
                # ever performs a plain-resize img2img call, so a
                # manifest-permitted preprocessing mode it doesn't implement,
                # or a diffusers timestep-selection param that doesn't
                # compose with the computed `strength`, must fail here too
                # rather than only once the job executes.
                if primary.preprocessing not in ("none", "auto"):
                    raise UnsupportedReferenceError(
                        f"Model {request.model_id!r}: reference preprocessing "
                        f"{primary.preprocessing!r} is not implemented by this "
                        "conditioning path (only 'none'/'auto', a plain resize, "
                        "are)."
                    )
                effective_params = {**manifest.default_params, **request.params}
                for incompatible_param in (
                    "denoising_start",
                    "denoising_end",
                    "timesteps",
                    "sigmas",
                ):
                    if incompatible_param in effective_params:
                        raise UnsupportedReferenceError(
                            f"Model {request.model_id!r}: {incompatible_param!r} "
                            "cannot be combined with reference-image "
                            "conditioning -- diffusers img2img's timestep "
                            "selection from denoising_start/denoising_end/"
                            "timesteps/sigmas does not compose with the "
                            "computed 'strength', so the reference's lock "
                            f"strength would not be honored. Remove "
                            f"{incompatible_param!r} from params or drop the "
                            "reference."
                        )
                # #201 follow-up (Codex P2, thirteenth round): a non-numeric
                # steps/num_inference_steps value (params is an unconstrained
                # dict) made this raise a plain ValueError -- not caught by
                # any route's (UnsupportedReferenceError,
                # MissingReferenceAssetError) handler, so it was an unhandled
                # 500 instead of a 4xx.
                raw_steps = effective_params.get(
                    "num_inference_steps", effective_params.get("steps", 30)
                )
                try:
                    num_inference_steps = int(raw_steps)
                except (TypeError, ValueError) as exc:
                    raise UnsupportedReferenceError(
                        f"Model {request.model_id!r}: num_inference_steps/steps "
                        f"must be a number, not {raw_steps!r}."
                    ) from exc
                img2img_strength = 1.0 - primary.strength
                if int(num_inference_steps * img2img_strength) < 1:
                    raise UnsupportedReferenceError(
                        f"Model {request.model_id!r}: the requested reference "
                        f"lock strength {primary.strength} combined with "
                        f"{num_inference_steps} inference step(s) would leave "
                        "diffusers with zero denoising steps to actually run. "
                        "Increase num_inference_steps or reduce the reference "
                        "strength."
                    )

    def _resolve_manifest_for_references(
        self,
        request: GenerationRequest,
    ) -> "ModelManifest | None":
        """Resolve `request`'s manifest for reference validation, or None.

        `model_service` is optional (some tests construct `JobService`
        without one) -- returns `None` rather than raising in that case,
        since there is nothing to validate reference support against.

        An unknown, disabled, or wrong-task `model_id` makes
        `ModelService.get_manifest()` raise `LookupError`; translated to
        `UnsupportedReferenceError` here (#201 follow-up, twelfth Codex
        round on PR #376) so every calling route's existing
        `(UnsupportedReferenceError, MissingReferenceAssetError)` handler
        covers it too, instead of a bad client-supplied `model_id` on an
        otherwise-valid reference surfacing as an unhandled 500.
        """

        if self.model_service is None:
            return None
        try:
            return self.model_service.get_manifest(
                request.model_id, request.media_type, request.task_type
            )
        except LookupError as exc:
            raise UnsupportedReferenceError(
                f"Cannot validate reference-image conditioning for model "
                f"{request.model_id!r}: {exc}"
            ) from exc

    def _require_img2img_mode(
        self,
        manifest: "ModelManifest",
        model_id: str,
    ) -> None:
        """Raise unless `manifest` advertises img2img reference conditioning.

        `validate_reference_inputs()` only checks that *some* mode is
        declared (via `capability.enabled`) plus role/strength/preprocessing/
        count -- it does not require "img2img" specifically. The only
        reference-conditioning path this codebase implements
        (`ImageGenerator._resolve_references_for_conditioning()`) only ever
        performs an img2img-style call, so a manifest advertising e.g. only
        `ip_adapter` must not pass creation-time validation only to be
        deterministically rejected once the job executes (#201 follow-up,
        eleventh Codex round on PR #376).
        """

        capability = manifest.reference_capability
        if capability is None or "img2img" not in capability.supported_modes:
            raise UnsupportedReferenceError(
                f"Model {model_id or manifest.public_model_id!r} does not "
                "advertise img2img in reference_capability.supported_modes, "
                "which is the only conditioning mode this path implements."
            )

    def _reject_cross_project_reference_asset(
        self,
        asset_repository: AssetRepository,
        asset_id: str,
        project_id: str | None,
    ) -> None:
        """Raise if `asset_id` resolves to an asset outside `project_id`.

        A no-op when the asset does not exist at all -- that case is left to
        the generator's own MissingReferenceAssetError at execution time.
        """

        asset = asset_repository.get(asset_id)
        if asset is not None and asset.project_id != project_id:
            raise MissingReferenceAssetError(
                f"Reference asset {asset_id!r} belongs to project "
                f"{asset.project_id or 'no project'!r}, not "
                f"{project_id or 'no project'!r}; a reference must belong "
                "to the same project as the job it conditions."
            )

    def enqueue_job(self, job_id: str) -> JobRecord | None:
        job = self.get_job(job_id)
        if job is None or job.status == JOB_STATUS_CANCELLED:
            return job

        # #180: a genuinely multi-lane configuration routes by
        # (media_type, task_type); a single-lane configuration (or none at
        # all) collapses onto the queue's implicit lane exactly as before
        # lanes existed, so the `JobQueue` the caller passed in does not need
        # to know any lane names unless it was itself constructed with more
        # than one.
        if self.lane_config is not None and not self.lane_config.is_single_lane:
            lane = assign_lane(job.media_type, job.request.task_type, self.lane_config)
            self.job_queue.enqueue(job_id, lane=lane)
        else:
            self.job_queue.enqueue(job_id)
        self._publish(
            "job_queued",
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
            },
        )
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.job_repository.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        return self.job_repository.list()

    def update_status(
        self,
        job_id: str,
        status: GenerationStatus,
        progress: float | None = None,
    ) -> JobRecord | None:
        job = self.job_repository.update_status(job_id, status, progress=progress)
        if job is None:
            return None
        self._publish(
            _STATUS_TO_EVENT.get(status, "job_status_updated"),
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
            },
        )
        return job

    def mark_failed(self, job_id: str, message: str) -> JobRecord | None:
        job = self.job_repository.update_if_status(
            job_id,
            ACTIVE_JOB_STATUSES,
            status=JOB_STATUS_FAILED,
            progress=1.0,
            error_message=message,
        )
        if job is None:
            return self.get_job(job_id)
        self._publish(
            "job_failed",
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
                "error_message": job.error_message,
            },
        )
        return job

    def mark_succeeded(
        self,
        job_id: str,
        result: GenerationResult,
    ) -> JobRecord | None:
        normalized_result = result.model_copy(
            update={
                "job_id": job_id,
                "status": JOB_STATUS_SUCCEEDED,
                "error_message": None,
            }
        )
        job = self.job_repository.update_if_status(
            job_id,
            _SUCCEEDABLE_JOB_STATUSES,
            status=JOB_STATUS_SUCCEEDED,
            progress=1.0,
            result=normalized_result,
            error_message=None,
        )
        if job is None:
            return self.get_job(job_id)
        if self.asset_repository is not None:
            self.asset_repository.sync_job(job)
        self._publish(
            "job_succeeded",
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
                "outputs": job.result.outputs if job.result else [],
            },
        )
        return job

    def cancel_job(self, job_id: str) -> JobRecord | None:
        """Apply the state-aware cancel contract for `POST /jobs/{id}/cancel` (#207).

        - `queued` has no in-flight work to interrupt, so it cancels
          immediately to the terminal `cancelled` state.
        - `preparing`/`running`/`postprocessing` cannot be discarded
          synchronously; they move to the non-terminal `cancel_requested`
          state instead, and `JobRunner` finalizes them to `cancelled` once it
          observes the cooperative shutdown (see
          `JobRunner._finalize_cancellation`).
        - Anything else -- already `cancel_requested`, or a terminal status --
          is a no-op: repeated/late cancel requests must be idempotent, never
          an error and never a new transition (#206).
        """

        updated: JobRecord | None = None
        changed = False
        while True:
            job = self.get_job(job_id)
            if job is None:
                return None

            if job.status == JOB_STATUS_QUEUED:
                target_status: str | None = JOB_STATUS_CANCELLED
            elif job.status in (
                JOB_STATUS_PREPARING,
                JOB_STATUS_RUNNING,
                JOB_STATUS_POSTPROCESSING,
            ):
                target_status = JOB_STATUS_CANCEL_REQUESTED
            else:
                updated = job
                break

            updated = self.job_repository.update_if_status(
                job_id,
                (job.status,),
                status=target_status,
                progress=1.0 if target_status == JOB_STATUS_CANCELLED else None,
            )
            if updated is not None:
                changed = True
                break
            # A queued worker may claim the job between our read and CAS. Read
            # again and turn that newly active state into cancel_requested.

        # A running worker has already registered its Event on begin(). For a
        # queued job, or a job that already reached a terminal/cancel_requested
        # status, this is a harmless no-op.
        if self.cancellation_registry is not None:
            self.cancellation_registry.request_cancel(job_id)

        if updated is not None and changed:
            self._publish(
                _STATUS_TO_EVENT.get(updated.status, "job_status_updated"),
                {
                    "job_id": updated.id,
                    "status": updated.status,
                    "progress": updated.progress,
                },
            )
        return updated

    def finalize_cancellation(self, job_id: str) -> JobRecord | None:
        """Resolve a `cancel_requested` job to the terminal `cancelled` state.

        Called by `JobRunner` once it has observed generation actually stop
        after a cancel was requested against an in-flight job (#207) -- via
        `GenerationCancelled` propagating out of a generator, or at a status
        checkpoint boundary the runner controls. A job that is not currently
        `cancel_requested` (already `cancelled`, or never was) is returned
        unchanged, so this is safe to call opportunistically.
        """

        job = self.job_repository.update_if_status(
            job_id,
            (JOB_STATUS_CANCEL_REQUESTED,),
            status=JOB_STATUS_CANCELLED,
            progress=1.0,
        )
        if job is None:
            return self.get_job(job_id)
        self._publish(
            "job_cancelled",
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
            },
        )
        return job

    def _publish(self, event_type: str, payload: dict[str, object]) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(event_type, payload)


__all__ = ["JobService"]
