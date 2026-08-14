"""Human-feedback calibration helpers for quality reports."""

from __future__ import annotations

from typing import Any


def calibrate_quality_report(
    quality_report: dict[str, Any],
    feedback_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Blend automated quality scores with human feedback when available."""

    enriched = dict(quality_report)
    if feedback_summary is None:
        return enriched

    enriched["feedback_summary"] = feedback_summary
    total_feedback = feedback_summary.get("total_feedback")
    if not isinstance(total_feedback, int) or total_feedback <= 0:
        return enriched

    calibration_weight = min(0.45, 0.12 * total_feedback)

    human_quality = _as_number(feedback_summary.get("human_quality_score"))
    human_semantic = _as_number(feedback_summary.get("human_semantic_alignment_score"))
    human_creative = _as_number(feedback_summary.get("human_creative_alignment_score"))

    base_quality = _as_number(enriched.get("quality_score"))
    base_semantic = _as_number(enriched.get("semantic_alignment_score"))
    base_creative = _as_number(enriched.get("creative_alignment_score"))

    enriched["quality_score_calibrated"] = _blend(base_quality, human_quality, calibration_weight)
    enriched["semantic_alignment_score_calibrated"] = _blend(
        base_semantic,
        human_semantic,
        calibration_weight,
    )
    enriched["creative_alignment_score_calibrated"] = _blend(
        base_creative,
        human_creative if human_creative is not None else human_quality,
        calibration_weight,
    )
    enriched["feedback_calibration_weight"] = round(calibration_weight, 2)
    return enriched


def _as_number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _blend(base: float | None, human: float | None, weight: float) -> float | None:
    if human is None and base is None:
        return None
    if human is None:
        assert base is not None
        return round(float(base), 1)
    if base is None:
        return round(float(human), 1)
    return round(float(base) * (1.0 - weight) + float(human) * weight, 1)


__all__ = ["calibrate_quality_report"]
