"""Video generator for procedural and learned local text-to-video runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.jobs.context import GenerationCancelled
from core.models import ModelService
from core.models.runtime_lease import RuntimeWaitTimeoutError
from core.models.service import RuntimeHandle
from core.quality import (
    enrich_quality_report,
    evaluate_video_output,
    evaluate_video_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

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
        cancelled_before_render = False
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
                cancelled_before_render = context is not None and context.is_cancelled()
                if not cancelled_before_render:
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
                    else:
                        if context is not None:
                            late_cancellation = context.is_cancelled()
                        if isinstance(render_result, dict):
                            progress_error = render_result.get("progress_error")
            runtime_type = type(runtime_obj).__name__

        # No inference took place, or it completed successfully: exit
        # cleanly instead of marking the cache INVALID.
        if cancelled_before_render or cancelled_before_invocation or late_cancellation:
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
            # it.
            if context is not None and context.is_cancelled():
                encoded_path.unlink(missing_ok=True)
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
        while True:
            context.raise_if_cancelled()
            try:
                return self.model_service.acquire_runtime(
                    model_id, media_type="video", task_type=self.task_type,
                    wait_timeout=_CANCELLATION_POLL_SECONDS,
                )
            except RuntimeWaitTimeoutError:
                # Only synchronization waits are retryable. Cache capacity,
                # invalid entries and loader errors must still surface. This
                # does not preempt a synchronous loader that already started.
                context.raise_if_cancelled()

    def cleanup(self, request: GenerationRequest) -> None:
        return None


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
