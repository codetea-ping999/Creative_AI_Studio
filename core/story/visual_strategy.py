"""Deterministic visual-strategy selection for scene visual requests.

Parent: #65. This module answers the question #250 exists to answer: how does
one scene's visual asset actually get produced — a single still image, a
Ken Burns pan/zoom over a still, a learned text-to-video generation, or a
learned image-to-video generation? The answer comes from three inputs: what
the scene asked for (``SceneVisualRequest.visual_intent``), what this
environment's models can actually do right now (:class:`VisualCapabilities`),
and how much of that capability we are willing to spend on this scene
(:class:`VisualResourceBudget`).

This module makes no model calls, resolves no manifests, and starts no jobs
(see #250's non-goals: "Implementing new model runtimes", "Starting child
jobs"). Callers are expected to have already turned real readiness checks
(``core.model_readiness.evaluate_readiness(...).is_ready``) into the boolean
flags on :class:`VisualCapabilities` — this module only decides, from those
flags, which of the four strategies to use, and records why, so the decision
survives as inspectable metadata (:class:`VisualStrategyDecision`) rather than
being re-derived — and potentially re-answered differently — every time it is
needed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .visual_manifest import SceneVisualRequest

STILL = "still"
KEN_BURNS = "ken_burns"
TEXT_TO_VIDEO = "text_to_video"
IMAGE_TO_VIDEO = "image_to_video"

#: Every strategy this module can choose between.
VISUAL_STRATEGIES: tuple[str, ...] = (STILL, KEN_BURNS, TEXT_TO_VIDEO, IMAGE_TO_VIDEO)

# Safe default/fallback order. Read left to right as "most production value
# first, cheapest-and-always-available last": a generated video is preferred
# to an animated still when both are supported, and motion is preferred to a
# static frame when nothing else constrains the choice. Critically, every
# strategy above KEN_BURNS can be knocked out by capability or budget, while
# KEN_BURNS and STILL both need nothing beyond image generation — neither
# depends on learned video weights — so an image-only environment (image
# generation ready, no learned video model) always bottoms out on one of the
# last two instead of raising.
STRATEGY_FALLBACK_ORDER: tuple[str, ...] = (
    TEXT_TO_VIDEO,
    IMAGE_TO_VIDEO,
    KEN_BURNS,
    STILL,
)

# Free-text phrasings a writer or a language model reaches for in
# ``visual_intent``, mapped to the strategy they mean. Deliberately mirrors
# the shape of ``core.story.timeline._MOTION_ALIASES`` (alias table + collapse
# underscores/hyphens/case) so the two vocabularies read the same way to
# anyone editing scenes, even though they normalize to different targets.
_INTENT_ALIASES: dict[str, str] = {
    "still": STILL,
    "static": STILL,
    "photo": STILL,
    "single image": STILL,
    "ken burns": KEN_BURNS,
    "pan": KEN_BURNS,
    "push in": KEN_BURNS,
    "zoom": KEN_BURNS,
    "motion": KEN_BURNS,
    "video": TEXT_TO_VIDEO,
    "text to video": TEXT_TO_VIDEO,
    "generated video": TEXT_TO_VIDEO,
    "cinematic": TEXT_TO_VIDEO,
    "t2v": TEXT_TO_VIDEO,
    "animate": IMAGE_TO_VIDEO,
    "image to video": IMAGE_TO_VIDEO,
    "i2v": IMAGE_TO_VIDEO,
}


def normalize_visual_intent(raw_intent: str | None) -> str | None:
    """Resolve free-text ``visual_intent`` to a known strategy name, or ``None``.

    ``None`` means "no preference stated", not "invalid input": scenes
    routinely leave ``visual_intent`` empty (``build_visual_manifest``'s
    default is ``visual_intents={}``), and an unrecognized phrase should fall
    through to the safe default order rather than raise — the same tolerance
    ``normalize_motion`` in ``core.story.timeline`` gives ``Scene.camera``.
    """

    text = (raw_intent or "").strip().lower()
    if not text:
        return None
    if text in VISUAL_STRATEGIES:
        return text
    collapsed = " ".join(text.replace("_", " ").replace("-", " ").split())
    if collapsed.replace(" ", "_") in VISUAL_STRATEGIES:
        return collapsed.replace(" ", "_")
    return _INTENT_ALIASES.get(collapsed)


class VisualCapabilities(BaseModel):
    """Which visual-generation capabilities this environment can serve right now.

    These are readiness flags, not existence flags. ``text_to_video_ready``
    should reflect whether the learned-video model's weights actually
    resolve (``core.model_readiness.evaluate_readiness(...).is_ready``), not
    merely whether a manifest for it is declared — a manifest can exist with
    its weight files missing (see ``models/manifests/video/learned-local.json``).
    """

    model_config = ConfigDict(extra="forbid")

    image_generation_ready: bool = False
    text_to_video_ready: bool = False
    image_to_video_ready: bool = False


class VisualResourceBudget(BaseModel):
    """Local resource limits that can veto a capability-supported strategy.

    Capability answers "can this run at all"; budget answers "should it run
    *here, now*". A machine with the learned video weights fully installed
    (``text_to_video_ready=True``) may still want every scene forced onto the
    cheap path — a battery-powered laptop, a machine already saturated by
    another job — without that being confused with the model being unusable.
    """

    model_config = ConfigDict(extra="forbid")

    allow_text_to_video: bool = True
    allow_image_to_video: bool = True
    #: Scenes longer than this never use a learned video strategy, regardless
    #: of readiness. ``None`` means no duration limit is enforced.
    max_video_duration_seconds: float | None = Field(default=None, ge=0)


class VisualStrategyDecision(BaseModel):
    """The chosen strategy and why, ready to persist as request/job metadata."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    rationale: str = Field(min_length=1)
    requested_strategy: str | None = None
    considered: list[str] = Field(default_factory=list)


class VisualStrategyUnavailableError(RuntimeError):
    """No strategy in the fallback order is usable for this request.

    This should only happen when ``image_generation_ready`` is also
    ``False`` — an environment with neither image nor video capability at
    all. Every other capability combination resolves to at least
    ``ken_burns`` or ``still``, which is what guarantees an image-only
    environment a valid fallback path.
    """

    def __init__(
        self,
        request: SceneVisualRequest,
        capabilities: VisualCapabilities,
        budget: VisualResourceBudget,
        considered: list[str],
    ) -> None:
        super().__init__(
            f"No visual strategy is usable for scene {request.scene_id!r}: "
            f"tried {', '.join(considered)}; capabilities="
            f"{capabilities.model_dump()}, budget={budget.model_dump()}."
        )
        self.request = request
        self.capabilities = capabilities
        self.budget = budget
        self.considered = considered


#: The capability flags each strategy requires, all of which must be True.
_STRATEGY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    STILL: ("image_generation_ready",),
    KEN_BURNS: ("image_generation_ready",),
    IMAGE_TO_VIDEO: ("image_generation_ready", "image_to_video_ready"),
    TEXT_TO_VIDEO: ("text_to_video_ready",),
}


def _capability_blocks(strategy: str, capabilities: VisualCapabilities) -> bool:
    return not all(
        getattr(capabilities, flag) for flag in _STRATEGY_REQUIREMENTS[strategy]
    )


def _budget_blocks(
    strategy: str, request: SceneVisualRequest, budget: VisualResourceBudget
) -> bool:
    if strategy == TEXT_TO_VIDEO and not budget.allow_text_to_video:
        return True
    if strategy == IMAGE_TO_VIDEO and not budget.allow_image_to_video:
        return True
    if (
        strategy in (TEXT_TO_VIDEO, IMAGE_TO_VIDEO)
        and budget.max_video_duration_seconds is not None
        and request.duration_seconds > budget.max_video_duration_seconds
    ):
        return True
    return False


def _candidate_order(requested: str | None) -> tuple[str, ...]:
    if requested is None:
        return STRATEGY_FALLBACK_ORDER
    # A stated preference is tried first; the rest of the safe order follows
    # so a preference that turns out unsupported here still degrades to the
    # same safe fallback instead of raising.
    rest = tuple(s for s in STRATEGY_FALLBACK_ORDER if s != requested)
    return (requested, *rest)


def _rationale(strategy: str, requested: str | None, considered: list[str]) -> str:
    rejected = considered[:-1]
    if requested is None:
        if not rejected:
            return (
                f"No visual intent stated; selected {strategy!r}, "
                "first in the safe fallback order."
            )
        return (
            f"No visual intent stated; selected {strategy!r} after "
            f"{', '.join(rejected)} were unavailable in this environment."
        )
    if strategy == requested:
        return f"Scene requested {requested!r} and it is supported here; selected as-is."
    return (
        f"Scene requested {requested!r}, which is unavailable here "
        f"({', '.join(rejected)}); fell back to {strategy!r}."
    )


def select_visual_strategy(
    request: SceneVisualRequest,
    capabilities: VisualCapabilities,
    budget: VisualResourceBudget | None = None,
) -> VisualStrategyDecision:
    """Deterministically choose one visual strategy for ``request``.

    The same ``request``/``capabilities``/``budget`` always yields the same
    decision: no randomness, no clock, no I/O, and no reach into
    ``ModelService`` — the same guarantee ``build_visual_manifest`` makes for
    prompt composition, extended one step further into strategy choice.

    Raises :class:`VisualStrategyUnavailableError` when every strategy in the
    fallback order is blocked by capability or budget — never silently
    returns a strategy nothing here can actually produce.
    """

    budget = budget or VisualResourceBudget()
    requested = normalize_visual_intent(request.visual_intent)

    considered: list[str] = []
    for strategy in _candidate_order(requested):
        considered.append(strategy)
        if _capability_blocks(strategy, capabilities):
            continue
        if _budget_blocks(strategy, request, budget):
            continue
        return VisualStrategyDecision(
            strategy=strategy,
            rationale=_rationale(strategy, requested, considered),
            requested_strategy=requested,
            considered=list(considered),
        )

    raise VisualStrategyUnavailableError(request, capabilities, budget, considered)


__all__ = [
    "IMAGE_TO_VIDEO",
    "KEN_BURNS",
    "STILL",
    "STRATEGY_FALLBACK_ORDER",
    "TEXT_TO_VIDEO",
    "VISUAL_STRATEGIES",
    "VisualCapabilities",
    "VisualResourceBudget",
    "VisualStrategyDecision",
    "VisualStrategyUnavailableError",
    "normalize_visual_intent",
    "select_visual_strategy",
]
