"""Resolve declarative prompt inputs into the strings a generator sends.

Requests store *intent* (bible references, axis patches) rather than a baked
prompt, so a sweep re-run after editing the Creative Bible reflects the edit.
Resolution happens here, in one place, at generation time — and the audit trail
goes into job metadata so the final string is never a mystery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.prompting import ComposedPrompt, PromptComposer, PromptSpec
from core.schemas import GenerationRequest

# Params consumed by resolution rather than passed to the model.
_RESOLUTION_PARAM_KEYS = (
    "bible_refs",
    "axis_values",
    "extra_fragments",
    "prompt_template",
    "freeze_composed_prompt",
    "batch_axis_labels",
    "batch_stage",
)


@dataclass(slots=True)
class ResolvedPrompt:
    """The strings and settings a generator should actually use."""

    prompt: str
    negative_prompt: str | None
    seed: int | None
    lora: dict[str, Any] | None = None
    reference_asset_ids: list[str] = field(default_factory=list)
    composition: dict[str, Any] | None = None


def resolve_generation_prompt(
    request: GenerationRequest,
    params: dict[str, Any],
    *,
    composer: PromptComposer | None,
    template: str = "image",
) -> ResolvedPrompt:
    """Return the resolved prompt, consuming resolution keys from ``params``.

    ``params`` is mutated: the resolution keys are removed so they are never
    forwarded to a diffusion pipeline that would reject unknown keyword
    arguments. When there is nothing to resolve, the request passes through
    unchanged and no composition metadata is recorded.
    """

    bible_refs = _string_list(params.pop("bible_refs", None))
    axis_values = params.pop("axis_values", None)
    extra_fragments = _string_list(params.pop("extra_fragments", None))
    resolved_template = str(params.pop("prompt_template", template) or template)
    # Batch bookkeeping is useful in metadata but must not reach the model.
    axis_labels = params.pop("batch_axis_labels", None)
    batch_stage = params.pop("batch_stage", None)
    params.pop("freeze_composed_prompt", None)

    if not bible_refs and not axis_values and not extra_fragments:
        return ResolvedPrompt(
            prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            seed=request.seed,
        )

    active_composer = composer or PromptComposer()
    composed: ComposedPrompt = active_composer.compose(
        PromptSpec(
            base_prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            bible_refs=bible_refs,
            axis_values=axis_values if isinstance(axis_values, dict) else {},
            template=resolved_template,
            extra_fragments=extra_fragments,
            seed=request.seed,
        )
    )

    composition = composed.model_dump(mode="json")
    if axis_labels is not None:
        composition["axis_labels"] = axis_labels
    if batch_stage is not None:
        composition["batch_stage"] = batch_stage

    return ResolvedPrompt(
        prompt=composed.prompt,
        negative_prompt=composed.negative_prompt,
        seed=composed.seed,
        lora=composed.lora,
        reference_asset_ids=composed.reference_asset_ids,
        composition=composition,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(entry) for entry in value if str(entry).strip()]


__all__ = ["ResolvedPrompt", "resolve_generation_prompt"]
