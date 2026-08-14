"""Text generator backed by a local language model runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.models import ModelService
from core.models.text_runtimes import extract_json_object
from core.quality import evaluate_text_output
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

from .tasks import STORY_TASKS, StoryTask, get_story_task

_SUPPORTED_OUTPUT_FORMATS = frozenset({"md", "markdown", "json"})

_REPAIR_INSTRUCTION = (
    "The previous response was not valid for the required schema.\n"
    "Error: {error}\n"
    "Return only a single corrected JSON object. No prose, no code fence."
)


class TextGenerator(BaseGenerator):
    """Generate story documents with the resolved text runtime."""

    def __init__(
        self,
        model_service: ModelService,
        output_dir: str | Path = "outputs/text",
        *,
        task_type: str = "story",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.model_service = model_service
        self.task_type = task_type

    def validate_request(self, request: GenerationRequest) -> None:
        if request.media_type != "text":
            raise ValueError("TextGenerator only supports text requests.")
        if not request.prompt.strip():
            raise ValueError("Text prompt must not be empty.")
        task_name = str(request.params.get("task", "logline"))
        if task_name not in STORY_TASKS:
            raise ValueError(
                f"Unknown story task {task_name!r}; "
                f"expected one of {', '.join(sorted(STORY_TASKS))}"
            )
        if (
            request.output_format
            and request.output_format.lower() not in _SUPPORTED_OUTPUT_FORMATS
        ):
            raise ValueError(
                "TextGenerator supports md or json output only, got "
                f"{request.output_format!r}."
            )

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # Intentionally omits `context`: BaseGenerator.run() introspects generate()'s
    # signature (see generators/base.py) and calls context-free generators without
    # it, so cancellation is only honored at the job-boundary for this generator.
    def generate(self, request: GenerationRequest) -> GenerationResult:  # type: ignore[override]
        requested_model_id = request.model_id.strip() or None
        manifest, runtime_obj = self.model_service.resolve_runtime(
            requested_model_id,
            media_type="text",
            task_type=self.task_type,
        )
        generate_text = runtime_obj["generate"]

        effective_params = {**manifest.default_params, **request.params}
        task = get_story_task(str(effective_params.pop("task", "logline")))
        max_tokens = int(
            effective_params.pop("max_tokens", task.default_max_tokens)
        )
        temperature = float(effective_params.pop("temperature", 0.8))
        top_p = float(effective_params.pop("top_p", 0.95))
        # Runtime wiring, not generation parameters: strip them so they are not
        # mistaken for task inputs in the rendered brief.
        for runtime_key in (
            "context_window",
            "n_gpu_layers",
            "model_file",
            "chat_format",
            "model_name",
            "api_key_env",
            "timeout_seconds",
        ):
            effective_params.pop(runtime_key, None)

        task_params = {
            "premise": request.prompt,
            "subject": request.prompt,
            **effective_params,
        }
        prompt = task.build_prompt(task_params)
        json_schema = task.json_schema()

        structured, raw_text, attempts = self._generate_structured(
            generate_text,
            task=task,
            prompt=prompt,
            system=task.system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=request.seed,
            json_schema=json_schema,
            supports_json_schema=bool(runtime_obj.get("supports_json_schema")),
        )

        output_id = f"txt_{uuid4().hex}"
        markdown_path = self.output_dir / f"{output_id}.md"
        structured_path = self.output_dir / f"{output_id}.json"
        markdown_path.write_text(task.render_markdown(structured), encoding="utf-8")
        structured_path.write_text(
            json.dumps(structured, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        quality_report = evaluate_text_output(
            markdown_path,
            structured=structured,
            task=task.name,
            target_words=_optional_int(task_params.get("target_words")),
        )

        return GenerationResult(
            job_id=output_id,
            status="succeeded",
            # One asset per job: the markdown is the readable output and the
            # structured payload travels beside it as a sidecar.
            outputs=[str(markdown_path)],
            previews=[str(markdown_path)],
            metadata={
                "stub": False,
                "generator": self.__class__.__name__,
                "media_type": request.media_type,
                "task_type": self.task_type,
                "story_task": task.name,
                "prompt": request.prompt,
                "resolved_prompt": prompt,
                "requested_model_id": requested_model_id,
                "model_id": manifest.public_model_id,
                "manifest_id": manifest.id,
                "model_display_name": manifest.display_name,
                "model_runtime": manifest.runtime,
                "model_provider": manifest.provider,
                "loader": manifest.loader,
                "runtime_type": type(runtime_obj).__name__,
                "device": runtime_obj.get("device"),
                "context_window": runtime_obj.get("context_window"),
                "supports_json_schema": runtime_obj.get("supports_json_schema"),
                # Recorded so a run against an endpoint always shows where the
                # prompt was sent.
                "endpoint_base_url": runtime_obj.get("endpoint_base_url"),
                "seed": request.seed,
                "output_format": "md",
                "structured_path": str(structured_path),
                "structured": structured,
                "raw_response_characters": len(raw_text),
                "generation_attempts": attempts,
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                **_extract_lineage_metadata(request.params),
                "params": {
                    "task": task.name,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "top_p": top_p,
                    **effective_params,
                },
            },
            error_message=None,
        )

    def cleanup(self, request: GenerationRequest) -> None:
        return None

    def _generate_structured(
        self,
        generate_text: Any,
        *,
        task: StoryTask,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        seed: int | None,
        json_schema: dict[str, Any],
        supports_json_schema: bool,
    ) -> tuple[dict[str, Any], str, int]:
        """Generate, validate, and repair once before failing.

        One repair attempt is worth it because a schema violation is usually a
        formatting slip the model can fix when shown the error. A second failure
        means the model cannot satisfy the contract, and reporting that with the
        raw text preserved is more useful than looping.
        """

        current_prompt = prompt
        last_error: str = ""
        raw_text = ""

        for attempt in range(1, 3):
            raw_text = generate_text(
                current_prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                seed=seed,
                json_schema=json_schema if supports_json_schema else None,
            )
            try:
                payload = extract_json_object(raw_text)
                validated = task.response_model.model_validate(payload)
                return validated.model_dump(mode="json"), raw_text, attempt
            except Exception as exc:
                last_error = str(exc)
                current_prompt = (
                    f"{prompt}\n\n"
                    f"### PREVIOUS RESPONSE\n{raw_text[:2000]}\n\n"
                    f"### CORRECTION\n{_REPAIR_INSTRUCTION.format(error=last_error)}"
                )

        raw_path = self._write_raw_response(task, raw_text)
        raise ValueError(
            f"Story task {task.name!r} did not return schema-valid output after a "
            f"repair attempt: {last_error}. Raw response saved to {raw_path}."
        )

    def _write_raw_response(self, task: StoryTask, raw_text: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.output_dir / f"failed_{task.name}_{uuid4().hex[:8]}.txt"
        raw_path.write_text(raw_text, encoding="utf-8")
        return raw_path


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_lineage_metadata(params: dict[str, Any]) -> dict[str, Any]:
    lineage_keys = (
        "source_asset_id",
        "source_job_id",
        "reference_asset_path",
        "reuse_action",
        "story_id",
    )
    lineage_payload: dict[str, Any] = {}
    for key in lineage_keys:
        value = params.get(key)
        if value is not None:
            lineage_payload[key] = value
    return lineage_payload


__all__ = ["TextGenerator"]
