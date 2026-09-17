"""Text generator backed by a local language model runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from core.jobs.context import GenerationCancelled
from core.models import ModelService
from core.models.text_runtimes import extract_json_object
from core.quality import evaluate_text_output
from core.schemas import GenerationRequest, GenerationResult
from generators.base import BaseGenerator

from .tasks import STORY_TASKS, StoryTask, get_story_task

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext
    from core.models.service import RuntimeHandle

_SUPPORTED_OUTPUT_FORMATS = frozenset({"md", "markdown", "json"})

_REPAIR_INSTRUCTION = (
    "The previous response was not valid for the required schema.\n"
    "Error: {error}\n"
    "Return only a single corrected JSON object. No prose, no code fence."
)

_CANCELLATION_POLL_SECONDS = 0.1


class StorySchemaValidationError(ValueError):
    """A story task's LLM output never validated against its schema, even
    after one repair attempt.

    A `ValueError` subtype so existing `except ValueError`/`assertRaises`
    callers are unaffected. Distinct from a bare `ValueError` so
    `TextGenerator.generate()` can tell this expected, output-*shape*
    failure (inference completed normally; only its text was malformed)
    apart from an exception the runtime callable itself raised -- only the
    latter still conservatively invalidates the leased entry.

    Carries `task_name`/`raw_text`/`last_error` rather than an
    already-built "Raw response saved to ..." message: `_generate_structured()`
    raises this *without* writing the diagnostic file itself, so a
    filesystem failure while persisting that diagnostic can never be
    mistaken for a runtime fault either. `TextGenerator.generate()`
    persists the raw response and builds the final, actionable message
    only after its lease has already exited cleanly.
    """

    def __init__(self, message: str, *, task_name: str, raw_text: str, last_error: str) -> None:
        super().__init__(message)
        self.task_name = task_name
        self.raw_text = raw_text
        self.last_error = last_error


class _StructuredGenerationCancelled(Exception):
    """Private signal: cancellation observed between schema attempts.

    Codex P2 finding "Text: observe cancellation between schema attempts":
    raised only by `TextGenerator._generate_structured()`, once the first
    inference attempt has completed and cancellation is observable, but
    *before* a repair attempt would otherwise start -- so a job that has
    already been asked to stop never pays for a second, doomed-to-be-
    discarded model call.

    Caught only by `TextGenerator.generate()`'s own call site, still
    inside the active runtime lease. Deliberately not `GenerationCancelled`
    itself: raising that here would unwind through the
    `with self._acquire_runtime(...)` block and mark a healthy runtime
    `INVALID` (see `RuntimeHandle.__exit__`) merely because this caller
    decided not to use its own already-successful first inference attempt.
    `generate()` catches this, records `late_cancellation` exactly like the
    existing post-success cancellation sample, lets the lease exit
    normally, and only then raises the real `GenerationCancelled`.

    Never exported; not a new public exception taxonomy.
    """

    def __init__(self, *, raw_text: str, attempts: int) -> None:
        super().__init__("generation cancelled between schema attempts")
        self.raw_text = raw_text
        self.attempts = attempts


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

    def generate(
        self,
        request: GenerationRequest,
        context: "GenerationContext | None" = None,
    ) -> GenerationResult:
        requested_model_id = request.model_id.strip() or None

        # Request parsing must not invalidate a healthy leased runtime.
        manifest = self.model_service.get_manifest(
            requested_model_id, media_type="text", task_type=self.task_type
        )
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

        # Continuity memory (issue #190): POST /stories/{id}/expand injects
        # `continuity_context` (the rendered prompt block) and
        # `continuity_snapshot` (the exact ContinuityContext used, for
        # reproducibility) into params for the "prose" task. Popped here so
        # neither is echoed into metadata["params"] below, which is about
        # generation knobs, not story content — the snapshot gets its own
        # metadata field instead, so it is inspectable without re-parsing the
        # resolved prompt.
        continuity_context_text = str(
            effective_params.pop("continuity_context", "") or ""
        ).strip()
        continuity_snapshot = effective_params.pop("continuity_snapshot", None)

        task_params = {
            "premise": request.prompt,
            "subject": request.prompt,
            **effective_params,
        }
        if continuity_context_text:
            task_params["continuity_context"] = continuity_context_text
        prompt = task.build_prompt(task_params)
        json_schema = task.json_schema()

        # PR4b / FP-001 + FP-002: the model call and its one allowed repair
        # attempt are the runtime-use interval.  Keep E + the lifetime lease
        # for that whole interval, then release before file/quality work that
        # does not touch the runtime.  This also leaves room for later local
        # semantic judges to enter the same process-wide admission domain
        # without creating a forbidden same-thread nested acquisition.
        #
        # Safety convergence pass: a schema-still-invalid-after-repair
        # failure is an expected output-shape outcome, not a runtime fault
        # (`StorySchemaValidationError`, distinct from an exception the
        # runtime callable itself raises) -- captured here and re-raised
        # only after the lease exits normally, so it never invalidates a
        # healthy runtime.
        #
        # Codex P2 finding "Text late cancellation": a cancellation that
        # arrives while the blocking `generate_text()` call (and its one
        # allowed repair attempt) is running was never rechecked once it
        # returned -- the lease exited normally, but the generator then
        # wrote output files and ran quality evaluation before JobRunner's
        # own outer boundary ever noticed cancellation, leaving orphaned
        # outputs. `late_cancellation` snapshots cancellation once
        # generation has genuinely completed, still inside the `with`
        # block (no runtime mutation happens from reading it), so a stop
        # request observed only now is treated the same as one observed up
        # front: raised after the lease exits cleanly, before any file is
        # written.
        #
        # Codex P2 finding "Text: observe cancellation between schema
        # attempts": that same `late_cancellation` sample used to run only
        # on the success path (the `else` clause below) -- a terminal
        # schema failure (`StorySchemaValidationError`, raised after the
        # repair attempt is also invalid) skipped it entirely, so
        # cancellation observed during either attempt was lost and the
        # failed-response diagnostic got persisted before JobRunner's own
        # boundary ever noticed the stop request. `_generate_structured()`
        # now checks cancellation itself right after the first attempt
        # completes and, if observed, skips the repair call and signals
        # back via `_StructuredGenerationCancelled` (never
        # `GenerationCancelled` itself -- see that class's own docstring
        # for why). The `except StorySchemaValidationError` branch below
        # also re-samples cancellation, so a stop that only becomes
        # observable once *both* attempts have completed (repair included)
        # is caught too.
        cancelled_before_generation = False
        late_cancellation = False
        schema_error: StorySchemaValidationError | None = None
        with self._acquire_runtime(requested_model_id, context) as handle:
            manifest = handle.manifest
            runtime_obj = handle.runtime
            generate_text = runtime_obj["generate"]

            cancelled_before_generation = context is not None and context.is_cancelled()
            if not cancelled_before_generation:
                try:
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
                        context=context,
                    )
                except _StructuredGenerationCancelled:
                    late_cancellation = True
                except StorySchemaValidationError as exc:
                    schema_error = exc
                    if context is not None:
                        late_cancellation = context.is_cancelled()
                else:
                    runtime_metadata = {
                        "runtime_type": type(runtime_obj).__name__,
                        "device": runtime_obj.get("device"),
                        "context_window": runtime_obj.get("context_window"),
                        "supports_json_schema": runtime_obj.get("supports_json_schema"),
                        "endpoint_base_url": runtime_obj.get("endpoint_base_url"),
                    }
                    if context is not None:
                        late_cancellation = context.is_cancelled()

        # No inference took place, or it completed successfully: exit
        # cleanly instead of marking the cache INVALID. An exception raised
        # directly by the runtime callable (`generate_text`) itself is not
        # caught above and still unwinds as unsafe.
        #
        # `late_cancellation` is checked before `schema_error` deliberately:
        # cancellation must take precedence over persisting the
        # failed-response diagnostic (`_write_raw_response()` below) when
        # both became true from the same completed attempt(s) -- this
        # `raise` exits the function before that write ever happens.
        if cancelled_before_generation or late_cancellation:
            raise GenerationCancelled()
        if schema_error is not None:
            # The lease has already exited cleanly above -- persisting the
            # diagnostic and building the final, actionable message
            # happens here, entirely outside it, so a write failure (full
            # or read-only output filesystem) is never mistaken for a
            # runtime fault.
            raw_path = self._write_raw_response(task, schema_error.raw_text)
            raise StorySchemaValidationError(
                f"Story task {schema_error.task_name!r} did not return "
                f"schema-valid output after a repair attempt: "
                f"{schema_error.last_error}. Raw response saved to {raw_path}.",
                task_name=schema_error.task_name,
                raw_text=schema_error.raw_text,
                last_error=schema_error.last_error,
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
                **runtime_metadata,
                "seed": request.seed,
                "output_format": "md",
                "structured_path": str(structured_path),
                "structured": structured,
                "raw_response_characters": len(raw_text),
                "generation_attempts": attempts,
                "default_params": dict(manifest.default_params),
                "quality_report": quality_report,
                # Inspectable, reproducible record of what continuity memory
                # (if any) was injected for this chapter (issue #190). Both
                # are `None`/absent-shaped when no continuity applied, e.g. a
                # story's first chapter or a non-"prose" task.
                "continuity_context": continuity_context_text or None,
                "continuity_snapshot": continuity_snapshot,
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

    def _acquire_runtime(
        self, model_id: str | None, context: "GenerationContext | None"
    ) -> "RuntimeHandle":
        """Cancellation-aware runtime wait, mirroring Image/VideoGenerator.

        One checkpointed `acquire_runtime()` call: ModelService waits in
        bounded slices and calls `context.raise_if_cancelled` between them
        with nothing held. Only synchronization waits are waited out; cache
        capacity, invalid entries and loader errors still surface. Text
        inference itself remains a single blocking call with no preemption
        once it starts (no new preemption contract is introduced here).
        """

        if context is None:
            return self.model_service.acquire_runtime(
                model_id, media_type="text", task_type=self.task_type
            )
        return self.model_service.acquire_runtime(
            model_id, media_type="text", task_type=self.task_type,
            wait_checkpoint=context.raise_if_cancelled,
            poll_interval=_CANCELLATION_POLL_SECONDS,
        )

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
        context: "GenerationContext | None" = None,
    ) -> tuple[dict[str, Any], str, int]:
        """Generate, validate, and repair once before failing.

        One repair attempt is worth it because a schema violation is usually a
        formatting slip the model can fix when shown the error. A second failure
        means the model cannot satisfy the contract, and reporting that with the
        raw text preserved is more useful than looping.

        Codex P2 finding "Text: observe cancellation between schema
        attempts": cancellation is sampled once the first attempt's
        inference has completed (reading it here does not touch the
        runtime) and, if observed, the repair call is skipped entirely --
        raising `_StructuredGenerationCancelled` to tell `generate()`
        rather than starting a second, doomed-to-be-discarded model call.
        This never raises `GenerationCancelled` itself: that would unwind
        through the active `with self._acquire_runtime(...)` block in
        `generate()` and mark a perfectly healthy runtime `INVALID` merely
        for having produced output this caller no longer wants.
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

            if attempt == 1 and context is not None and context.is_cancelled():
                raise _StructuredGenerationCancelled(raw_text=raw_text, attempts=attempt)

        # Deliberately no `_write_raw_response()` call here: persisting the
        # diagnostic is filesystem I/O, not part of the runtime-use
        # interval this method is responsible for. `TextGenerator.generate()`
        # writes it -- and builds the final, actionable message -- only
        # after the lease has already exited cleanly, so a write failure
        # (e.g. a full or read-only output filesystem) is never mistaken
        # for a runtime fault.
        raise StorySchemaValidationError(
            f"Story task {task.name!r} did not return schema-valid output after a "
            f"repair attempt: {last_error}.",
            task_name=task.name,
            raw_text=raw_text,
            last_error=last_error,
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


__all__ = ["StorySchemaValidationError", "TextGenerator"]
