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
        # Safety convergence pass: three request/cancellation-boundary
        # failures are distinguished from a genuine runtime-use failure and
        # deferred until after a clean lease exit, instead of raising from
        # inside the `with` block (which would conservatively mark a
        # healthy entry INVALID):
        #   - `cancelled_before_render`: cancellation observed before
        #     render() was ever called.
        #   - `late_cancellation`: render() returned successfully (the
        #     runtime callable ran to completion) and cancellation is only
        #     now observable -- a request to stop *after* successful use,
        #     not a runtime fault.
        #   - `procedural_param_error`: a procedural request's own
        #     width/height/fps/duration_seconds/num_frames coercion raised
        #     (ValueError/TypeError). ProceduralStoryboardRuntime performs
        #     every one of these conversions before touching runtime_obj or
        #     rendering a single frame (see generators/video/runtime.py), so
        #     this is a pure input error, never a runtime fault -- narrowed
        #     to this concrete runtime class and these two exception types
        #     so a genuine mid-render failure is never misclassified.
        # A `GenerationCancelled` that instead escapes render() itself (a
        # renderer/adapter observing cancellation once its own callable is
        # already running) is not caught here and still conservatively
        # invalidates -- see runtime.py's own removal of the redundant
        # pre-call check that used to make this ambiguous.
        cancelled_before_render = False
        late_cancellation = False
        procedural_param_error: Exception | None = None
        with self._acquire_runtime(requested_model_id, context) as handle:
            manifest = handle.manifest
            runtime_obj = handle.runtime
            effective_params = {**manifest.default_params, **request.params}
            runtime = self.runtime_router.resolve(runtime_obj)
            cancelled_before_render = context is not None and context.is_cancelled()
            if not cancelled_before_render:
                try:
                    render_result = runtime.render(
                        request=request,
                        manifest=manifest,
                        runtime_obj=runtime_obj,
                        output_dir=self.output_dir,
                        effective_params=effective_params,
                        context=context,
                    )
                except (ValueError, TypeError) as exc:
                    if not isinstance(runtime, ProceduralStoryboardRuntime):
                        raise
                    procedural_param_error = exc
                else:
                    if context is not None:
                        late_cancellation = context.is_cancelled()
            runtime_type = type(runtime_obj).__name__

        # No inference took place, or it completed successfully: exit
        # cleanly instead of marking the cache INVALID.
        if cancelled_before_render or late_cancellation:
            raise GenerationCancelled()
        if procedural_param_error is not None:
            raise procedural_param_error

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
