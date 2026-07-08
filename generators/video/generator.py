"""Video generator for procedural and learned local text-to-video runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.models import ModelService
from core.quality import (
    enrich_quality_report,
    evaluate_video_output,
    evaluate_video_semantics,
)
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

from .runtime import (
    LEARNED_VIDEO_OUTPUT_FORMATS,
    PROCEDURAL_VIDEO_OUTPUT_FORMATS,
    VideoRuntimeRouter,
)


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

    def generate(self, request: GenerationRequest) -> GenerationResult:
        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="video",
            task_type=self.task_type,
        )

        effective_params = {**manifest.default_params, **request.params}
        runtime = self.runtime_router.resolve(runtime_obj)
        render_result = runtime.render(
            request=request,
            manifest=manifest,
            runtime_obj=runtime_obj,
            output_dir=self.output_dir,
            effective_params=effective_params,
        )

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
                "runtime_type": type(runtime_obj).__name__,
                "runtime_adapter": runtime_metadata.get("runtime_adapter"),
                "output_format": render_result.get("output_format", output_path.suffix.lstrip(".") or "gif"),
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                "params": params_payload,
                **runtime_metadata,
                **lineage_metadata,
            },
            error_message=None,
        )

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
