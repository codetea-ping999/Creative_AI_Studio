"""Batch presets: ready-made sweeps for recurring exploration jobs.

The two-stage shape is the point. A 30-item sweep at full resolution takes tens
of minutes on a laptop GPU, which is slower than thought; a low-resolution probe
pass narrows 30 candidates to a handful in a few minutes, and only those are
rendered properly.
"""

from __future__ import annotations

from typing import Any

from .schemas import Axis, AxisValue, BatchSpec, Stage

PROBE_STAGE_PARAMS: dict[str, Any] = {
    "width": 640,
    "height": 640,
    "steps": 14,
    "num_inference_steps": 14,
}

REFINE_STAGE_PARAMS: dict[str, Any] = {
    "width": 1024,
    "height": 1024,
    "steps": 34,
    "num_inference_steps": 34,
}


def _axis_from_catalog(name: str, *, catalog: str, labels: list[str] | None = None) -> Axis:
    # Imported lazily so a missing or renamed catalog surfaces when a template is
    # requested, not when this module is first imported.
    from core.prompting.patterns import get_axis_catalog

    entries = get_axis_catalog(catalog)
    if labels is not None:
        wanted = set(labels)
        entries = [entry for entry in entries if entry["label"] in wanted]
        missing = wanted - {entry["label"] for entry in entries}
        if missing:
            raise LookupError(
                f"Unknown {catalog} labels: {', '.join(sorted(missing))}"
            )
    return Axis(
        name=name,
        values=[
            AxisValue(label=entry["label"], patch=entry["patch"], tags=entry["tags"])
            for entry in entries
        ],
    )


def _logo_30(**overrides: Any) -> BatchSpec:
    spec = BatchSpec(
        name="Logo 30 patterns",
        media_type="image",
        model_id=overrides.pop("model_id", ""),
        prompt=overrides.pop("prompt", ""),
        negative_prompt=overrides.pop("negative_prompt", None),
        params={"width": 1024, "height": 1024, "guidance_scale": 7.0},
        axes=[_axis_from_catalog("logo_structure", catalog="logo_structure")],
        strategy="grid",
        seed_policy="shared",
        stages=[
            Stage(name="probe", param_overrides=PROBE_STAGE_PARAMS, keep_top_n=6),
            Stage(name="refine", param_overrides=REFINE_STAGE_PARAMS),
        ],
        limit=64,
    )
    return spec.model_copy(update=overrides)


def _thumbnail_tone_grid(**overrides: Any) -> BatchSpec:
    structures = [
        "left-face-right-text",
        "big-number",
        "before-after-split",
        "versus-split",
        "bold-outline-subject",
        "full-bleed-text",
    ]
    tones = ["minimal", "premium", "playful", "technical", "neon"]
    spec = BatchSpec(
        name="Thumbnail structure x tone",
        media_type="image",
        model_id=overrides.pop("model_id", ""),
        prompt=overrides.pop("prompt", ""),
        negative_prompt=overrides.pop("negative_prompt", None),
        params={"width": 1344, "height": 768, "guidance_scale": 7.0},
        axes=[
            _axis_from_catalog(
                "thumbnail_structure",
                catalog="thumbnail_structure",
                labels=structures,
            ),
            _axis_from_catalog(
                "tone_and_manner", catalog="tone_and_manner", labels=tones
            ),
        ],
        strategy="grid",
        seed_policy="shared",
        stages=[
            Stage(
                name="probe",
                param_overrides={"width": 768, "height": 448, "steps": 14},
                keep_top_n=6,
            ),
            Stage(
                name="refine",
                param_overrides={"width": 1344, "height": 768, "steps": 34},
            ),
        ],
        limit=64,
    )
    return spec.model_copy(update=overrides)


def _character_sheet(**overrides: Any) -> BatchSpec:
    expressions = ["neutral", "smiling", "determined", "surprised"]
    angles = ["front view", "three-quarter view", "profile view", "back view"]
    spec = BatchSpec(
        name="Character sheet",
        media_type="image",
        model_id=overrides.pop("model_id", ""),
        prompt=overrides.pop("prompt", ""),
        negative_prompt=overrides.pop("negative_prompt", None),
        params={"width": 832, "height": 1024, "guidance_scale": 7.0},
        axes=[
            Axis(
                name="expression",
                values=[
                    AxisValue(
                        label=expression,
                        patch={"prompt_suffix": f"{expression} expression"},
                        tags=["expression"],
                    )
                    for expression in expressions
                ],
            ),
            Axis(
                name="angle",
                values=[
                    AxisValue(
                        label=angle.replace(" ", "-"),
                        patch={"prompt_suffix": angle},
                        tags=["angle"],
                    )
                    for angle in angles
                ],
            ),
        ],
        strategy="grid",
        # A character sheet exists to prove identity holds, so every cell must use
        # the same seed; varying it would confuse "the model drifted" with "the
        # seed changed".
        seed_policy="shared",
        stages=[Stage(name="sheet", param_overrides={"steps": 26})],
        limit=32,
    )
    return spec.model_copy(update=overrides)


def _logline_candidates(**overrides: Any) -> BatchSpec:
    tones = ["hopeful", "melancholic", "tense", "playful", "epic"]
    spec = BatchSpec(
        name="Logline candidates",
        media_type="text",
        model_id=overrides.pop("model_id", ""),
        prompt=overrides.pop("prompt", ""),
        params={"task": "logline", "count": 3},
        axes=[
            Axis(
                name="tone",
                values=[
                    AxisValue(label=tone, patch={"params": {"tone": tone}}, tags=["tone"])
                    for tone in tones
                ],
            )
        ],
        strategy="grid",
        seed_policy="per_item",
        stages=[Stage(name="draft")],
        limit=16,
    )
    return spec.model_copy(update=overrides)


_TEMPLATES = {
    "logo-30": (
        _logo_30,
        "30 logo construction patterns, probed at 640px then the top 6 refined",
    ),
    "thumbnail-tone-grid": (
        _thumbnail_tone_grid,
        "6 thumbnail layouts x 5 tones, probed then the top 6 refined",
    ),
    "character-sheet": (
        _character_sheet,
        "4 expressions x 4 angles at a shared seed, to fix a character's look",
    ),
    "logline-candidates": (
        _logline_candidates,
        "5 tonal takes on one premise, 3 loglines each",
    ),
}


def list_batch_templates() -> list[dict[str, Any]]:
    """Return the available presets with their item counts."""

    templates: list[dict[str, Any]] = []
    for name, (factory, description) in sorted(_TEMPLATES.items()):
        try:
            spec = factory()
        except LookupError as exc:  # pragma: no cover - catalog drift guard
            templates.append({"name": name, "description": description, "error": str(exc)})
            continue
        stages = spec.resolved_stages()
        first_stage_items = 1
        for axis in spec.axes:
            first_stage_items *= len(axis.values)
        templates.append(
            {
                "name": name,
                "description": description,
                "media_type": spec.media_type,
                "axes": [
                    {"name": axis.name, "value_count": len(axis.values)}
                    for axis in spec.axes
                ],
                "first_stage_items": first_stage_items,
                "stages": [
                    {"name": stage.name, "keep_top_n": stage.keep_top_n}
                    for stage in stages
                ],
            }
        )
    return templates


def build_batch_template(template_name: str, /, **overrides: Any) -> BatchSpec:
    """Build a preset spec, applying caller overrides on top.

    The template name is positional-only so that ``name`` stays available as an
    override for the resulting spec's own display name.
    """

    try:
        factory, _ = _TEMPLATES[template_name]
    except KeyError as exc:
        raise LookupError(
            f"Unknown batch template {template_name!r}; "
            f"expected one of {', '.join(sorted(_TEMPLATES))}"
        ) from exc
    return factory(**overrides)


__all__ = [
    "PROBE_STAGE_PARAMS",
    "REFINE_STAGE_PARAMS",
    "build_batch_template",
    "list_batch_templates",
]
