"""Tests for deterministic visual-strategy selection (#250)."""

from __future__ import annotations

from itertools import product
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.story.visual_manifest import SceneVisualRequest  # noqa: E402
from core.story.visual_strategy import (  # noqa: E402
    IMAGE_TO_VIDEO,
    KEN_BURNS,
    STILL,
    STRATEGY_FALLBACK_ORDER,
    TEXT_TO_VIDEO,
    VISUAL_STRATEGIES,
    VisualCapabilities,
    VisualResourceBudget,
    VisualStrategyDecision,
    VisualStrategyUnavailableError,
    normalize_visual_intent,
    select_visual_strategy,
)


def _request(**fields) -> SceneVisualRequest:
    defaults = {
        "id": "visreq_1",
        "story_id": "story_1",
        "scene_id": "scene_01",
        "order": 0,
        "prompt": "a rooftop at dawn",
    }
    defaults.update(fields)
    return SceneVisualRequest(**defaults)


class NormalizeVisualIntentTests(unittest.TestCase):
    def test_empty_or_none_means_no_preference(self) -> None:
        self.assertIsNone(normalize_visual_intent(None))
        self.assertIsNone(normalize_visual_intent(""))
        self.assertIsNone(normalize_visual_intent("   "))

    def test_canonical_names_pass_through(self) -> None:
        for strategy in VISUAL_STRATEGIES:
            self.assertEqual(normalize_visual_intent(strategy), strategy)

    def test_common_aliases_resolve(self) -> None:
        self.assertEqual(normalize_visual_intent("Push In"), KEN_BURNS)
        self.assertEqual(normalize_visual_intent("static"), STILL)
        self.assertEqual(normalize_visual_intent("cinematic"), TEXT_TO_VIDEO)
        self.assertEqual(normalize_visual_intent("i2v"), IMAGE_TO_VIDEO)
        self.assertEqual(normalize_visual_intent("ken-burns"), KEN_BURNS)
        self.assertEqual(normalize_visual_intent("Text To Video"), TEXT_TO_VIDEO)

    def test_unrecognized_text_is_not_an_error(self) -> None:
        self.assertIsNone(normalize_visual_intent("a moody establishing shot"))


class SelectVisualStrategyTests(unittest.TestCase):
    def test_selection_is_deterministic_for_the_same_inputs(self) -> None:
        request = _request(visual_intent="cinematic")
        capabilities = VisualCapabilities(
            image_generation_ready=True, text_to_video_ready=True
        )
        budget = VisualResourceBudget()

        first = select_visual_strategy(request, capabilities, budget)
        second = select_visual_strategy(request, capabilities, budget)

        self.assertEqual(first, second)

    def test_full_capability_and_no_stated_intent_prefers_text_to_video(self) -> None:
        request = _request()
        capabilities = VisualCapabilities(
            image_generation_ready=True,
            text_to_video_ready=True,
            image_to_video_ready=True,
        )

        decision = select_visual_strategy(request, capabilities)

        self.assertEqual(decision.strategy, TEXT_TO_VIDEO)
        self.assertIsNone(decision.requested_strategy)

    def test_image_only_environment_falls_back_to_ken_burns(self) -> None:
        """Guarantees an image-only environment a valid, weight-free fallback."""

        request = _request()
        capabilities = VisualCapabilities(image_generation_ready=True)

        decision = select_visual_strategy(request, capabilities)

        self.assertEqual(decision.strategy, KEN_BURNS)
        self.assertNotEqual(decision.strategy, TEXT_TO_VIDEO)

    def test_no_capabilities_at_all_raises_rather_than_faking_output(self) -> None:
        request = _request()
        capabilities = VisualCapabilities()

        with self.assertRaises(VisualStrategyUnavailableError) as ctx:
            select_visual_strategy(request, capabilities)

        self.assertIn("scene_01", str(ctx.exception))
        self.assertEqual(list(ctx.exception.considered), list(STRATEGY_FALLBACK_ORDER))

    def test_unready_text_to_video_is_never_selected_even_when_requested(self) -> None:
        request = _request(visual_intent="text_to_video")
        capabilities = VisualCapabilities(
            image_generation_ready=True, text_to_video_ready=False
        )

        decision = select_visual_strategy(request, capabilities)

        self.assertNotEqual(decision.strategy, TEXT_TO_VIDEO)
        self.assertEqual(decision.strategy, KEN_BURNS)
        self.assertEqual(decision.requested_strategy, TEXT_TO_VIDEO)

    def test_budget_can_disable_text_to_video_even_when_ready(self) -> None:
        request = _request(visual_intent="text_to_video")
        capabilities = VisualCapabilities(
            image_generation_ready=True, text_to_video_ready=True
        )
        budget = VisualResourceBudget(allow_text_to_video=False)

        decision = select_visual_strategy(request, capabilities, budget)

        self.assertEqual(decision.strategy, KEN_BURNS)

    def test_budget_duration_cap_blocks_video_paths_for_long_scenes(self) -> None:
        request = _request(duration_seconds=30.0)
        capabilities = VisualCapabilities(
            image_generation_ready=True,
            text_to_video_ready=True,
            image_to_video_ready=True,
        )
        budget = VisualResourceBudget(max_video_duration_seconds=10.0)

        decision = select_visual_strategy(request, capabilities, budget)

        self.assertEqual(decision.strategy, KEN_BURNS)

    def test_duration_cap_does_not_affect_still_or_ken_burns(self) -> None:
        request = _request(duration_seconds=30.0)
        capabilities = VisualCapabilities(image_generation_ready=True)
        budget = VisualResourceBudget(max_video_duration_seconds=1.0)

        decision = select_visual_strategy(request, capabilities, budget)

        self.assertEqual(decision.strategy, KEN_BURNS)

    def test_image_to_video_requires_both_image_and_video_capability(self) -> None:
        request = _request(visual_intent="image_to_video")
        # image_to_video_ready alone, without image_generation_ready, is not enough:
        # there is nothing to animate.
        capabilities = VisualCapabilities(
            image_generation_ready=False, image_to_video_ready=True
        )

        with self.assertRaises(VisualStrategyUnavailableError):
            select_visual_strategy(request, capabilities)

    def test_stated_preference_is_honored_when_supported(self) -> None:
        request = _request(visual_intent="still")
        capabilities = VisualCapabilities(
            image_generation_ready=True,
            text_to_video_ready=True,
            image_to_video_ready=True,
        )

        decision = select_visual_strategy(request, capabilities)

        self.assertEqual(decision.strategy, STILL)
        self.assertEqual(decision.requested_strategy, STILL)

    def test_rationale_is_never_empty(self) -> None:
        request = _request()
        capabilities = VisualCapabilities(image_generation_ready=True)

        decision = select_visual_strategy(request, capabilities)

        self.assertTrue(decision.rationale.strip())

    def test_decision_round_trips_through_json_for_metadata_persistence(self) -> None:
        request = _request(visual_intent="ken_burns")
        capabilities = VisualCapabilities(image_generation_ready=True)

        decision = select_visual_strategy(request, capabilities)
        payload = decision.model_dump(mode="json")
        restored = VisualStrategyDecision.model_validate(payload)

        self.assertEqual(restored, decision)


class DecisionTableTests(unittest.TestCase):
    """Enumerate capability/budget combinations the way a decision table would."""

    def test_every_capability_combination_either_resolves_or_raises_cleanly(
        self,
    ) -> None:
        request = _request()
        flags = (True, False)
        for image_ready, t2v_ready, i2v_ready in product(flags, flags, flags):
            capabilities = VisualCapabilities(
                image_generation_ready=image_ready,
                text_to_video_ready=t2v_ready,
                image_to_video_ready=i2v_ready,
            )
            with self.subTest(
                image_generation_ready=image_ready,
                text_to_video_ready=t2v_ready,
                image_to_video_ready=i2v_ready,
            ):
                if not image_ready and not t2v_ready:
                    with self.assertRaises(VisualStrategyUnavailableError):
                        select_visual_strategy(request, capabilities)
                    continue

                decision = select_visual_strategy(request, capabilities)

                if decision.strategy == TEXT_TO_VIDEO:
                    self.assertTrue(t2v_ready)
                elif decision.strategy == IMAGE_TO_VIDEO:
                    self.assertTrue(image_ready and i2v_ready)
                elif decision.strategy in (KEN_BURNS, STILL):
                    self.assertTrue(image_ready)
                else:  # pragma: no cover - guards against a new, unhandled strategy
                    self.fail(f"unexpected strategy: {decision.strategy!r}")

    def test_every_capability_combination_is_deterministic_across_two_runs(self) -> None:
        request = _request(visual_intent="animate")
        flags = (True, False)
        for image_ready, t2v_ready, i2v_ready in product(flags, flags, flags):
            capabilities = VisualCapabilities(
                image_generation_ready=image_ready,
                text_to_video_ready=t2v_ready,
                image_to_video_ready=i2v_ready,
            )
            if not image_ready and not t2v_ready:
                continue
            with self.subTest(
                image_generation_ready=image_ready,
                text_to_video_ready=t2v_ready,
                image_to_video_ready=i2v_ready,
            ):
                first = select_visual_strategy(request, capabilities)
                second = select_visual_strategy(request, capabilities)
                self.assertEqual(first, second)

    def test_unsupported_video_paths_are_never_selected_across_the_table(self) -> None:
        flags = (True, False)
        for t2v_ready, i2v_ready in product(flags, flags):
            capabilities = VisualCapabilities(
                image_generation_ready=True,
                text_to_video_ready=t2v_ready,
                image_to_video_ready=i2v_ready,
            )
            for intent in VISUAL_STRATEGIES:
                request = _request(visual_intent=intent)
                with self.subTest(intent=intent, t2v_ready=t2v_ready, i2v_ready=i2v_ready):
                    decision = select_visual_strategy(request, capabilities)
                    if decision.strategy == TEXT_TO_VIDEO:
                        self.assertTrue(t2v_ready)
                    if decision.strategy == IMAGE_TO_VIDEO:
                        self.assertTrue(i2v_ready)


if __name__ == "__main__":
    unittest.main()
