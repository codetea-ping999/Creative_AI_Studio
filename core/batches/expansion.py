"""Pure axis expansion: turn a spec into concrete child requests.

Kept free of repositories and job services so the combinatorics can be tested
directly — this is the part that decides what 30 patterns actually are.
"""

from __future__ import annotations

from itertools import product
import random
from typing import Any

from core.schemas import GenerationRequest

from .schemas import Axis, BatchItem, BatchSpec, Stage

# Patch keys that append to a text field instead of replacing it. This is how a
# structure axis and a tone axis stack onto one base prompt.
_SUFFIX_KEYS: dict[str, str] = {
    "prompt_suffix": "prompt",
    "prompt_fragment": "prompt",
    "negative_suffix": "negative_prompt",
    "negative_fragment": "negative_prompt",
}

_TOP_LEVEL_KEYS = frozenset(
    {"prompt", "negative_prompt", "model_id", "seed", "output_format", "task_type"}
)


def expand_items(
    spec: BatchSpec,
    *,
    stage: Stage,
    stage_index: int,
    seed_items: list[BatchItem] | None = None,
    id_prefix: str = "item",
) -> list[BatchItem]:
    """Expand a stage into items, optionally seeded by a previous stage's winners.

    Items are ordered by ``model_id`` so children that share a model run
    consecutively; the runtime cache holds one model by default, and interleaving
    models would reload weights between every image.
    """

    if seed_items is None:
        combinations = _combinations(spec)
    else:
        combinations = [
            [(name, label) for name, label in item.axis_values.items()]
            for item in seed_items
        ]

    items: list[BatchItem] = []
    for position, combination in enumerate(combinations):
        axis_values = dict(combination)
        label = _build_label(axis_values, position, stage.name)
        request = _build_request(
            spec,
            axis_values=axis_values,
            stage=stage,
            item_index=position,
        )
        items.append(
            BatchItem(
                id=f"{id_prefix}_{stage_index}_{position:03d}",
                index=position,
                label=label,
                stage_name=stage.name,
                stage_index=stage_index,
                axis_values=axis_values,
                request=request,
            )
        )

    items.sort(key=lambda item: (item.request.model_id, item.index))
    for order, item in enumerate(items):
        item.index = order
    return items


def _combinations(spec: BatchSpec) -> list[list[tuple[str, str]]]:
    if not spec.axes:
        return [[]]

    # Declaration order matters: the first axis varies slowest, so a reader
    # scanning results sees all tones of structure A before structure B.
    all_combinations = [
        [(axis.name, value.label) for axis, value in zip(spec.axes, choice)]
        for choice in product(*[axis.values for axis in spec.axes])
    ]

    if spec.strategy == "sample":
        max_items = spec.max_items
        if max_items is None:
            raise ValueError("A sample strategy requires max_items.")
        if max_items < len(all_combinations):
            sampler = random.Random(spec.sample_seed)
            # sample() over indices keeps the result order stable for a seed and
            # independent of how Python orders the source list.
            chosen = sorted(sampler.sample(range(len(all_combinations)), max_items))
            all_combinations = [all_combinations[index] for index in chosen]

    if len(all_combinations) > spec.limit:
        raise ValueError(
            f"Batch would expand to {len(all_combinations)} items, "
            f"which exceeds the limit of {spec.limit}. "
            "Reduce the axes, use strategy='sample' with max_items, or raise the limit."
        )
    return all_combinations


def _axis_by_name(spec: BatchSpec, name: str) -> Axis | None:
    return next((axis for axis in spec.axes if axis.name == name), None)


def _build_request(
    spec: BatchSpec,
    *,
    axis_values: dict[str, str],
    stage: Stage,
    item_index: int,
) -> GenerationRequest:
    payload: dict[str, Any] = {
        "media_type": spec.media_type,
        "task_type": spec.task_type,
        "prompt": spec.prompt,
        "negative_prompt": spec.negative_prompt,
        "model_id": spec.model_id,
        "seed": _resolve_seed(spec, item_index, axis_values),
        "output_format": spec.output_format,
        "params": _deep_copy(spec.params),
    }

    applied_axis_patches: dict[str, Any] = {}
    for axis_name, value_label in axis_values.items():
        axis = _axis_by_name(spec, axis_name)
        if axis is None:
            continue
        value = next(
            (entry for entry in axis.values if entry.label == value_label), None
        )
        if value is None:
            continue
        _apply_patch(payload, value.patch)
        applied_axis_patches[axis_name] = _deep_copy(value.patch)

    if stage.param_overrides:
        payload["params"] = _deep_merge(
            payload["params"], _deep_copy(stage.param_overrides)
        )

    # Child requests stay declarative: the generator resolves bible references and
    # axis fragments at run time, so re-running a batch after editing the bible
    # picks up the change instead of replaying a frozen string.
    if spec.bible_refs:
        payload["params"]["bible_refs"] = list(spec.bible_refs)
    if applied_axis_patches:
        payload["params"]["axis_values"] = applied_axis_patches
    payload["params"]["batch_axis_labels"] = dict(axis_values)
    payload["params"]["batch_stage"] = stage.name

    return GenerationRequest(**payload)


def _resolve_seed(
    spec: BatchSpec,
    item_index: int,
    axis_values: dict[str, str],
) -> int | None:
    if spec.seed_policy == "shared":
        return spec.seed
    # per_item and sweep both derive a distinct but reproducible seed: base + index.
    # A user can reproduce item 7 of a batch by hand with seed = base + 7.
    base = spec.seed if spec.seed is not None else 0
    return base + item_index


def _build_label(
    axis_values: dict[str, str],
    position: int,
    stage_name: str,
) -> str:
    if not axis_values:
        return f"item-{position:03d}"
    return "__".join(axis_values[name] for name in axis_values)


def _apply_patch(payload: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if key in _SUFFIX_KEYS:
            target = _SUFFIX_KEYS[key]
            existing = payload.get(target) or ""
            payload[target] = f"{existing}, {value}".strip(", ") if existing else value
            continue
        if key in _TOP_LEVEL_KEYS:
            payload[key] = value
            continue
        if key == "params" and isinstance(value, dict):
            payload["params"] = _deep_merge(payload["params"], _deep_copy(value))
            continue
        # Anything else is a media parameter (width, steps, ...) or extra data the
        # generator reads from params, such as attributes or palette.
        payload["params"] = _deep_merge(payload["params"], {key: _deep_copy(value)})


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


__all__ = ["expand_items"]
