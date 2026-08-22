"""Heuristic quality scoring for locally generated text output.

This is a *technical* proxy, exactly like the image and audio evaluators: it can
tell that a draft is structurally incomplete, degenerate, or still carrying
instruction leftovers, and it cannot tell whether the writing is any good. Story
quality stays a human judgement, recorded through feedback.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from core.story.text_utils import count_words, is_cjk_text

from .evaluators import _centered_score, _quality_level, _score_band

# Fragments that mean the model echoed its instructions or stopped early instead
# of producing finished prose.
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\blorem ipsum\b", re.IGNORECASE),
    re.compile(r"\binsert [a-z ]+ here\b", re.IGNORECASE),
    re.compile(r"\byour (?:text|title|name) here\b", re.IGNORECASE),
    re.compile(r"\{[a-z_][a-z0-9_]*\}"),  # unresolved template braces
    re.compile(r"\[[A-Z_]{3,}\]"),  # [PLACEHOLDER] style markers
    re.compile(r"^\s*(?:As an AI|I cannot|申し訳)", re.MULTILINE),
    re.compile(r"（?ここに.*?を(?:記述|挿入)）?"),
)

_SENTENCE_SPLIT = re.compile(r"[.!?。！？\n]+")


def evaluate_text_output(
    output_path: str | Path,
    *,
    structured: dict[str, Any] | None = None,
    task: str | None = None,
    target_words: int | None = None,
) -> dict[str, object]:
    """Return a lightweight technical quality report for a text asset."""

    path = Path(output_path)
    text = path.read_text(encoding="utf-8")
    file_size_bytes = path.stat().st_size

    word_count = count_words(text)
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(text)
        if sentence.strip()
    ]
    sentence_count = len(sentences)
    sentence_lengths = [count_words(sentence) for sentence in sentences]
    mean_sentence_length = (
        sum(sentence_lengths) / sentence_count if sentence_count else 0.0
    )
    sentence_length_stddev = _stddev(sentence_lengths, mean_sentence_length)

    repetition_ratio = _repetition_ratio(text)
    placeholder_hits = _placeholder_hits(text)
    structure_completeness = _structure_completeness(structured)

    # Rhythm is measured relative to the script: a 20-word Japanese sentence is
    # long, an English one is ordinary.
    length_center = 14.0 if is_cjk_text(text) else 18.0

    length_score = (
        _score_band(
            word_count,
            floor=max(1.0, target_words * 0.35),
            target=float(target_words),
            ceiling=target_words * 2.5,
        )
        if target_words
        else _score_band(word_count, floor=12, target=180, ceiling=4000)
    )
    rhythm_score = _centered_score(
        mean_sentence_length, center=length_center, tolerance=length_center * 0.9
    )
    variance_score = _score_band(
        sentence_length_stddev, floor=0.4, target=6.0, ceiling=24.0
    )
    repetition_score = max(0.0, 1.0 - repetition_ratio * 2.5)
    placeholder_penalty = min(30.0, placeholder_hits * 12.0)

    technical_quality_score = round(
        max(
            0.0,
            min(
                100.0,
                structure_completeness * 30
                + length_score * 24
                + repetition_score * 22
                + rhythm_score * 12
                + variance_score * 12
                - placeholder_penalty,
            ),
        ),
        1,
    )
    business_readiness_score = round(
        max(
            0.0,
            min(
                100.0,
                technical_quality_score * 0.75
                + structure_completeness * 25,
            ),
        ),
        1,
    )

    checks: list[str] = []
    if technical_quality_score >= 80:
        checks.append("technical quality is strong for local review")
    if structured is not None and structure_completeness < 1.0:
        checks.append("structured payload has empty required fields")
    if placeholder_hits:
        checks.append("text still contains placeholder or instruction leftovers")
    if repetition_ratio > 0.28:
        checks.append("wording repeats heavily")
    if sentence_count and sentence_length_stddev < 1.5:
        checks.append("sentence rhythm is monotonous")
    if word_count < 12:
        checks.append("output is very short")
    if not checks:
        checks.append("no major technical warning detected")

    return {
        "method": "heuristic_local_v1",
        "quality_score": technical_quality_score,
        "quality_level": _quality_level(technical_quality_score),
        "business_readiness_score": business_readiness_score,
        "business_readiness_level": _quality_level(business_readiness_score),
        "checks": checks,
        "metrics": {
            "task": task,
            "word_count": word_count,
            "sentence_count": sentence_count,
            "mean_sentence_length": round(mean_sentence_length, 2),
            "sentence_length_stddev": round(sentence_length_stddev, 2),
            "repetition_ratio": round(repetition_ratio, 4),
            "placeholder_hits": placeholder_hits,
            "structure_completeness": round(structure_completeness, 3),
            "file_size_bytes": file_size_bytes,
        },
        "notes": [
            "narrative quality and originality are not measured here",
            "score reflects technical proxy quality only",
        ],
    }


def _stddev(values: list[int], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance**0.5


def _repetition_ratio(text: str) -> float:
    """Return how much of the text is repeated content.

    Latin text is measured on repeated words; CJK text on repeated 3-character
    shingles, because per-character repetition is normal in Japanese while a
    repeated three-character run usually means the model looped.
    """

    if is_cjk_text(text):
        compact = "".join(text.split())
        shingles = [compact[index : index + 3] for index in range(len(compact) - 2)]
        if len(shingles) < 4:
            return 0.0
        return 1.0 - (len(set(shingles)) / len(shingles))

    words = [word.lower() for word in re.findall(r"[\w']+", text)]
    if len(words) < 8:
        return 0.0
    return 1.0 - (len(set(words)) / len(words))


def _placeholder_hits(text: str) -> int:
    return sum(len(pattern.findall(text)) for pattern in _PLACEHOLDER_PATTERNS)


def _structure_completeness(structured: dict[str, Any] | None) -> float:
    """Return the share of leaf values in the payload that carry content.

    An unstructured task (plain prose) scores 1.0: there is no schema to be
    incomplete against, and penalizing it would make prose look worse than a
    filled-in form.
    """

    if structured is None:
        return 1.0

    filled, total = _count_leaves(structured)
    if total == 0:
        return 0.0
    return filled / total


def _count_leaves(value: Any) -> tuple[int, int]:
    if isinstance(value, dict):
        filled = total = 0
        for item in value.values():
            item_filled, item_total = _count_leaves(item)
            filled += item_filled
            total += item_total
        return filled, total
    if isinstance(value, list):
        if not value:
            return 0, 1
        filled = total = 0
        for item in value:
            item_filled, item_total = _count_leaves(item)
            filled += item_filled
            total += item_total
        return filled, total
    if value is None:
        return 0, 1
    if isinstance(value, str):
        return (1 if value.strip() else 0), 1
    return 1, 1


__all__ = ["evaluate_text_output"]
