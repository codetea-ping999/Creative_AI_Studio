"""Video generator for procedural and learned local text-to-video runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.jobs.context import GenerationCancelled
from core.models import ModelService
from core.models.service import RuntimeHandle
from core.quality import (
    enrich_quality_report,
    evaluate_video_output,
    evaluate_video_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator
from generators.common import safe_is_cancelled

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext

from .runtime import (
    LEARNED_VIDEO_OUTPUT_FORMATS,
    PROCEDURAL_VIDEO_OUTPUT_FORMATS,
    ProceduralStoryboardRuntime,
    VideoRuntimeRouter,
    coerce_procedural_render_params,
    encode_frames_as_gif,
    frame_duration_ms_from_fps,
)

_CANCELLATION_POLL_SECONDS = 0.1


class VideoGenerator(BaseGenerator):
    """Generate local storyboard or learned-runtime video assets."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/videos",
        *,
        task_type: str = "text-to-video",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.task_type = task_type
        self.runtime_router = VideoRuntimeRouter()

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "video":
            raise ValueError("VideoGenerator only supports video requests.")
        if not request.prompt.strip():
            raise ValueError("Video prompt must not be empty.")
        if request.output_format:
            manifest = self.model_service.get_manifest(
                request.model_id.strip() or None,
                media_type="video",
                task_type=self.task_type,
            )
            output_format = request.output_format.lower()
            supported_formats = (
                LEARNED_VIDEO_OUTPUT_FORMATS
                if manifest.runtime == "learned"
                else PROCEDURAL_VIDEO_OUTPUT_FORMATS
            )
            if output_format not in supported_formats:
                supported = ", ".join(sorted(supported_formats))
                raise ValueError(
                    f"Video model {manifest.public_model_id!r} supports {supported} output only."
                )

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        request: GenerationRequest,
        context: "GenerationContext | None" = None,
    ) -> GenerationResult:
        requested_model_id = request.model_id.strip() or None

        # PR4b / FP-001 + FP-002: render is the runtime-use interval.  Keep
        # the leased execution boundary through runtime routing, mutable
        # runtime work, and cancellation checks that guard the render.  Exit
        # it before file-only quality and semantic scoring so CLIP/CLAP can
        # later share the same process-wide admission domain without a
        # forbidden same-thread nested acquisition.
        #
        # Safety convergence pass: request/cancellation-boundary failures are
        # distinguished from a genuine runtime-use failure and deferred
        # until after a clean lease exit, instead of raising from inside the
        # `with` block (which would conservatively mark a healthy entry
        # INVALID):
        #   - `cancelled_before_render`: cancellation observed before
        #     render() was ever called.
        #   - `cancelled_before_invocation`: for a learned runtime,
        #     cancellation observed by LearnedVideoRuntime.render()'s own
        #     pre-call check, immediately before invoking the opaque
        #     renderer callable -- it returns a sentinel instead of raising
        #     (see generators/video/runtime.py) so the callable is never
        #     invoked, yet nothing unwinds through this `with` block.
        #   - `late_cancellation`: render() returned successfully (the
        #     runtime callable ran to completion) and cancellation is only
        #     now observable -- a request to stop *after* successful use,
        #     not a runtime fault.
        #   - `procedural_param_error`: a procedural request's own
        #     width/height/fps/duration_seconds/num_frames coercion raised
        #     (ValueError/TypeError). This is a pure input error, never a
        #     runtime fault.
        #
        # Codex P2 finding "Video: restrict deferred procedural parameter
        # failures": `procedural_param_error` used to be caught with a
        # broad `except (ValueError, TypeError)` around the *entire*
        # `runtime.render(...)` call below -- which correctly protected a
        # healthy runtime from a malformed request-owned numeric parameter,
        # but also masked a genuine runtime defect (e.g. a malformed
        # palette hex string owned by the cached runtime raising inside
        # `_hex_to_rgb()`, deep in the render call this same broad catch
        # covered). The five numeric params are now coerced by
        # `coerce_procedural_render_params()` in a *narrow* `try/except`,
        # immediately before `render()` is even called -- classifying only
        # the exact request-owned conversion, never anything the renderer
        # itself raises. `render()` is no longer wrapped in any
        # exception-deferring `try/except` at all: any exception it raises
        # (a genuine renderer/runtime fault, `ValueError`/`TypeError`
        # included) now always escapes through the lease and invalidates,
        # exactly like every other runtime-use failure.
        #   - `progress_error`: Codex P2 finding "Video: progress
        #     publication must not invalidate procedural runtime" --
        #     `ProceduralStoryboardRuntime.render()` captures a
        #     `context.report_progress()` failure itself (external
        #     JobRepository/event-publication bookkeeping, not a rendering
        #     fault) and hands it back via `render_result["progress_error"]`
        #     instead of letting it escape the lease; raised here only
        #     after that lease has already exited cleanly.
        # A `GenerationCancelled` that instead escapes render() itself (a
        # renderer/adapter observing cancellation once its own callable is
        # already running) is not caught here and still conservatively
        # invalidates.
        # P2-A: `context.is_cancelled()` itself can raise (JobRepository I/O
        # -- see `generators/common/cancellation.py`). Called this early,
        # before `runtime.render()` has ever been invoked, that is a
        # bookkeeping failure, not a runtime fault; `safe_is_cancelled()`
        # keeps it from propagating through the `with` block below and
        # marking a healthy, never-used runtime INVALID.
        # `render_cancellation_probe_error` is raised only after the lease
        # has already exited cleanly.
        #
        # Lane A/B follow-up: the same fault class reaches two more probe
        # sites still inside this lease -- `render_probe_error` covers a
        # `safe_is_cancelled()`-wrapped probe *inside* `runtime.render()`
        # itself (`ProceduralStoryboardRuntime`'s per-frame check and
        # `LearnedVideoRuntime`'s pre-invocation check, both in
        # generators/video/runtime.py, handed back via the same
        # `render_result["probe_error"]` sentinel as a `progress_error`),
        # and `post_render_probe_error` covers the post-render recheck just
        # below, once `render()` has already returned successfully. Both
        # are raised only after the lease has already exited cleanly, same
        # as `render_cancellation_probe_error`.
        cancelled_before_render = False
        render_cancellation_probe_error: Exception | None = None
        render_probe_error: Exception | None = None
        post_render_probe_error: Exception | None = None
        cancelled_before_invocation = False
        late_cancellation = False
        procedural_param_error: Exception | None = None
        progress_error: Exception | None = None
        with self._acquire_runtime(requested_model_id, context) as handle:
            manifest = handle.manifest
            runtime_obj = handle.runtime
            effective_params = {**manifest.default_params, **request.params}
            runtime = self.runtime_router.resolve(runtime_obj)
            if isinstance(runtime, ProceduralStoryboardRuntime):
                try:
                    effective_params.update(
                        coerce_procedural_render_params(effective_params)
                    )
                except (ValueError, TypeError) as exc:
                    procedural_param_error = exc
            if procedural_param_error is None:
                cancelled_before_render, render_cancellation_probe_error = (
                    safe_is_cancelled(context)
                )
                if not cancelled_before_render and render_cancellation_probe_error is None:
                    render_result = runtime.render(
                        request=request,
                        manifest=manifest,
                        runtime_obj=runtime_obj,
                        output_dir=self.output_dir,
                        effective_params=effective_params,
                        context=context,
                    )
                    if isinstance(render_result, dict) and render_result.get(
                        "cancelled_before_invocation"
                    ):
                        cancelled_before_invocation = True
                    elif (
                        isinstance(render_result, dict)
                        and render_result.get("probe_error") is not None
                    ):
                        render_probe_error = render_result["probe_error"]
                    else:
                        if context is not None:
                            late_cancellation, post_render_probe_error = (
                                safe_is_cancelled(context)
                            )
                        if isinstance(render_result, dict):
                            progress_error = render_result.get("progress_error")
            runtime_type = type(runtime_obj).__name__

        # No inference took place, or it completed successfully: exit
        # cleanly instead of marking the cache INVALID.
        if render_cancellation_probe_error is not None:
            # `runtime.render()` was never called -- the lease has already
            # exited cleanly above -- so `render_result` does not exist yet;
            # this must be checked and raised before anything below ever
            # references it.
            raise render_cancellation_probe_error
        if render_probe_error is not None:
            # A probe inside `runtime.render()` itself raised (frame-loop
            # or pre-invocation check, generators/video/runtime.py) --
            # `render_result` only ever carries the minimal `probe_error`
            # sentinel in this case, never `pending_frames`/`output_path`,
            # so this must also be checked before anything below reads it.
            raise render_probe_error
        if post_render_probe_error is not None:
            # render() already returned successfully, so `render_result`
            # may carry a real, already-written direct-output artifact --
            # see the `late_cancellation` branch below for why. It must not
            # outlive this discarded result any more than an ordinary late
            # cancellation's does. A `pending_frames` result (procedural, or
            # a learned adapter returning raw frames) has not touched disk
            # yet, so this is a safe no-op for that case.
            _discard_render_artifacts(render_result)
            raise post_render_probe_error
        if cancelled_before_render or cancelled_before_invocation or late_cancellation:
            # A learned renderer that writes its own file and returns
            # `output_path` directly (rather than `pending_frames`) has
            # already produced that artifact -- and possibly distinct
            # `preview_paths` alongside it -- by the time `late_cancellation`
            # is observed here -- the lease has already released cleanly
            # above, so this is ordinary post-lease request cleanup, not a
            # runtime fault, and must run before `GenerationCancelled`
            # propagates so no returned artifact outlives the discarded
            # result. `render_result` only carries a populated
            # `output_path`/`preview_paths` for this direct-output case: the
            # `pending_frames` case (procedural, or a learned adapter
            # returning raw frames) never touches disk until the encode
            # step below, which runs only when cancellation was not
            # already observed here, so it is never a live file at this
            # point.
            if late_cancellation:
                _discard_render_artifacts(render_result)
            raise GenerationCancelled()
        if procedural_param_error is not None:
            raise procedural_param_error
        if progress_error is not None:
            raise progress_error

        # Safety convergence pass: `render()` (for a runtime that returns
        # raw frames rather than an already-encoded file -- procedural
        # storyboards, and a learned adapter returning a frame list) no
        # longer performs the GIF/filesystem encode itself; it hands back
        # `pending_frames` instead. Encoding happens here, with the lease
        # already released, so an output-filesystem failure (disk-full,
        # permission denial) can no longer invalidate a runtime that
        # finished rendering correctly.
        pending_frames = render_result.get("pending_frames")
        if pending_frames:
            if "pending_frame_duration_ms" in render_result:
                frame_duration_ms = int(render_result["pending_frame_duration_ms"])
            else:
                # Codex P2 finding "learned-video request parameter
                # parsing": the learned adapter's `fps` is parsed here,
                # after its lease has already released, so a malformed
                # request value (e.g. `fps="bad"`) raises with no runtime
                # involved at all instead of invalidating a healthy one.
                frame_duration_ms = frame_duration_ms_from_fps(
                    render_result.get("pending_frame_fps", 8)
                )
            output_id, encoded_path = encode_frames_as_gif(
                pending_frames,
                self.output_dir,
                frame_duration_ms,
            )
            render_result = {
                **render_result,
                "output_id": output_id,
                "output_path": str(encoded_path),
                "preview_paths": [str(encoded_path)],
            }
            # Codex P2 finding "cancellation during deferred GIF encoding":
            # a cancellation requested while this post-lease encode ran was
            # otherwise never observed before quality/semantic scoring --
            # JobRunner discards the cancelled result afterwards, leaving
            # the file just written above as an orphan. The lease already
            # released above, so raising here cannot re-enter or invalidate
            # it. `render_result["output_path"]`/`["preview_paths"]` were
            # just updated above to the freshly encoded path, so
            # `_discard_render_artifacts()` removes exactly that file
            # (deduplicated, since both keys point at it here).
            #
            # Video artifact cleanup lane: `context.is_cancelled()` itself
            # can raise (JobRepository I/O) the same as every other probe
            # site in this generator -- this recheck runs entirely after
            # the runtime lease has already released, so a probe failure
            # here was never a runtime-invalidation risk, but it must still
            # not leave the just-encoded GIF behind as an orphan when the
            # exact original probe error is re-raised.
            if context is not None:
                gif_cancelled, gif_probe_error = safe_is_cancelled(context)
                if gif_probe_error is not None:
                    _discard_render_artifacts(render_result)
                    raise gif_probe_error
                if gif_cancelled:
                    _discard_render_artifacts(render_result)
                    raise GenerationCancelled()

        output_path = Path(str(render_result["output_path"]))
        quality_report = evaluate_video_output(output_path)
        semantic_report = evaluate_video_semantics(
            output_path,
            request.prompt,
            request.negative_prompt,
        )
        enrich_quality_report(quality_report, semantic_report)

        lineage_metadata = _extract_lineage_metadata(request.params)
        runtime_metadata = (
            dict(render_result.get("runtime_metadata", {}))
            if isinstance(render_result.get("runtime_metadata"), dict)
            else {}
        )
        params_payload = (
            dict(render_result.get("params", {}))
            if isinstance(render_result.get("params"), dict)
            else dict(effective_params)
        )

        return GenerationResult(
            job_id=str(render_result["output_id"]),
            status="succeeded",
            outputs=[str(output_path)],
            previews=list(render_result.get("preview_paths", [str(output_path)])),
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": runtime_type,
                "runtime_adapter": runtime_metadata.get("runtime_adapter"),
                "output_format": render_result.get(
                    "output_format", output_path.suffix.lstrip(".") or "gif"
                ),
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                "params": params_payload,
                **runtime_metadata,
                **lineage_metadata,
            },
            error_message=None,
        )

    def _acquire_runtime(
        self, model_id: str | None, context: "GenerationContext | None"
    ) -> RuntimeHandle:
        if context is None:
            return self.model_service.acquire_runtime(
                model_id, media_type="video", task_type=self.task_type
            )
        # One logical, cancellation-aware wait: ModelService polls in bounded
        # slices and calls the checkpoint between them with nothing held.
        # Only synchronization waits are waited out; cache capacity, invalid
        # entries and loader errors still surface. This does not preempt a
        # synchronous loader that already started.
        return self.model_service.acquire_runtime(
            model_id, media_type="video", task_type=self.task_type,
            wait_checkpoint=context.raise_if_cancelled,
            poll_interval=_CANCELLATION_POLL_SECONDS,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None


def _discard_render_artifacts(render_result: "dict[str, Any] | None") -> None:
    """Remove every request-owned artifact a discarded render produced.

    Collects `output_path` and every entry of `preview_paths` from
    `render_result` -- the only two fields a renderer ever uses to report
    files it wrote for this request -- deduplicates them (the main output
    commonly reappears as its own preview, or a renderer that skips
    `preview_paths` entirely defaults to `[output_path]`, see
    `LearnedVideoRuntime._normalize_generated_output()`), and unlinks each
    exactly once. Always called post-lease. A missing path is silently
    ignored (never a runtime fault); nothing outside these two fields is
    ever touched, so an unrelated file in the same output directory is
    never at risk. A no-op when `render_result` is `None`/not a dict, or
    carries neither field (e.g. a `pending_frames` result discarded before
    the deferred GIF/file encode ever ran -- nothing has touched disk yet).
    """

    if not isinstance(render_result, dict):
        return
    paths: set[str] = set()
    output_path = render_result.get("output_path")
    if output_path:
        paths.add(str(output_path))
    for preview_path in render_result.get("preview_paths") or []:
        if preview_path:
            paths.add(str(preview_path))
    for path in paths:
        Path(path).unlink(missing_ok=True)


def _extract_lineage_metadata(params: dict[str, Any]) -> dict[str, Any]:
    lineage_keys = (
        "source_asset_id",
        "source_job_id",
        "reference_asset_path",
        "reuse_action",
    )
    lineage_payload: dict[str, Any] = {}
    for key in lineage_keys:
        value = params.get(key)
        if value is not None:
            lineage_payload[key] = value
    return lineage_payload


__all__ = ["VideoGenerator"]
