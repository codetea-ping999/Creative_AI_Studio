"""Tests for deterministic prompt composition and the axis catalogs."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.bible import BibleRepository  # noqa: E402
from core.prompting import (  # noqa: E402
    PromptComposer,
    PromptSpec,
    get_axis_catalog,
    list_axis_catalogs,
)


class PromptCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.repository = BibleRepository(Path(self._temporary.name) / "bible")
        self.composer = PromptComposer(self.repository)

        self.character = self.repository.create(
            kind="character",
            name="Mina",
            prompt_fragment="young woman, long black straight hair, purple eyes",
            negative_fragment="red eyes, blue hair",
            attributes={"hair": "long black straight", "eyes": "purple"},
            tokens=["mina_v1"],
            locked_fields=["hair"],
            seed_policy={"mode": "locked", "seed": 4242},
            lora={"path": "models/loras/mina.safetensors", "scale": 0.8},
            reference_asset_ids=["asset_ref_1"],
        )
        self.style = self.repository.create(
            kind="style",
            name="Noir",
            prompt_fragment="noir lighting, deep shadow, highly detailed",
            negative_fragment="flat lighting",
            palette=["#111111"],
        )

    def test_composition_is_deterministic(self) -> None:
        spec = PromptSpec(
            base_prompt="rooftop at dawn",
            bible_refs=[self.character.id, self.style.id],
            axis_values={"tone_and_manner": {"prompt_fragment": "premium finish"}},
        )
        first = self.composer.compose(spec)
        second = self.composer.compose(spec)
        self.assertEqual(first.prompt, second.prompt)
        self.assertEqual(first.negative_prompt, second.negative_prompt)

    def test_reference_order_changes_the_prompt(self) -> None:
        forward = self.composer.compose(
            PromptSpec(bible_refs=[self.character.id, self.style.id])
        )
        # Both entries have different kinds, so kind ranking dominates; use two
        # entries of the same kind to prove caller order is respected.
        second_style = self.repository.create(
            kind="style",
            name="Watercolor",
            prompt_fragment="watercolor wash",
        )
        reordered = self.composer.compose(
            PromptSpec(bible_refs=[second_style.id, self.style.id])
        )
        reordered_other = self.composer.compose(
            PromptSpec(bible_refs=[self.style.id, second_style.id])
        )
        self.assertNotEqual(reordered.prompt, reordered_other.prompt)
        self.assertIn("noir lighting", forward.prompt)

    def test_subject_entries_precede_style_entries(self) -> None:
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.style.id, self.character.id])
        )
        self.assertLess(
            composed.prompt.index("young woman"),
            composed.prompt.index("noir lighting"),
        )

    def test_duplicate_tokens_are_collapsed(self) -> None:
        duplicate = self.repository.create(
            kind="style",
            name="Detail",
            prompt_fragment="Highly Detailed, sharp focus",
        )
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.style.id, duplicate.id], template="image")
        )
        self.assertEqual(composed.prompt.lower().count("highly detailed"), 1)
        self.assertEqual(composed.prompt.lower().count("sharp focus"), 1)

    def test_negative_prompts_merge(self) -> None:
        composed = self.composer.compose(
            PromptSpec(
                negative_prompt="watermark",
                bible_refs=[self.character.id, self.style.id],
            )
        )
        self.assertIn("watermark", composed.negative_prompt)
        self.assertIn("red eyes", composed.negative_prompt)
        self.assertIn("flat lighting", composed.negative_prompt)

    def test_seed_lock_overrides_requested_seed(self) -> None:
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.character.id], seed=1)
        )
        self.assertEqual(composed.seed, 4242)
        self.assertTrue(
            any(entry.get("kind") == "seed_lock" for entry in composed.applied)
        )

    def test_conflicting_seed_locks_keep_the_first_reference(self) -> None:
        other = self.repository.create(
            kind="character",
            name="Rio",
            seed_policy={"mode": "locked", "seed": 99},
        )
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.character.id, other.id])
        )
        self.assertEqual(composed.seed, 4242)
        self.assertTrue(
            any("Seed lock conflict" in message for message in composed.conflicts)
        )

    def test_conflicting_lora_is_reported_and_first_wins(self) -> None:
        other = self.repository.create(
            kind="style",
            name="Ink",
            lora={"path": "models/loras/ink.safetensors", "scale": 0.5},
        )
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.character.id, other.id])
        )
        self.assertEqual(composed.lora["path"], "models/loras/mina.safetensors")
        self.assertTrue(
            any("LoRA conflict" in message for message in composed.conflicts)
        )

    def test_identical_lora_paths_do_not_conflict(self) -> None:
        twin = self.repository.create(
            kind="style",
            name="Twin",
            lora={"path": "models/loras/mina.safetensors", "scale": 0.8},
        )
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.character.id, twin.id])
        )
        self.assertEqual(composed.conflicts, [])

    def test_locked_attribute_survives_an_axis_override(self) -> None:
        composed = self.composer.compose(
            PromptSpec(
                bible_refs=[self.character.id],
                axis_values={"look": {"attributes": {"hair": "short pink bob"}}},
            )
        )
        self.assertEqual(composed.attributes["hair"], "long black straight")
        self.assertTrue(
            any("locked attribute" in message for message in composed.conflicts)
        )
        self.assertNotIn("short pink bob", composed.prompt)

    def test_unlocked_attribute_can_be_overridden(self) -> None:
        composed = self.composer.compose(
            PromptSpec(
                bible_refs=[self.character.id],
                axis_values={"look": {"attributes": {"eyes": "green"}}},
            )
        )
        self.assertEqual(composed.attributes["eyes"], "green")
        self.assertEqual(composed.conflicts, [])

    def test_unknown_reference_degrades_to_a_conflict(self) -> None:
        composed = self.composer.compose(
            PromptSpec(
                base_prompt="rooftop",
                bible_refs=["bible_missing", self.style.id],
            )
        )
        self.assertIn("unknown bible entry: bible_missing", composed.conflicts)
        self.assertIn("noir lighting", composed.prompt)

    def test_references_without_a_repository_are_reported(self) -> None:
        composed = PromptComposer().compose(
            PromptSpec(base_prompt="rooftop", bible_refs=["bible_1"])
        )
        self.assertIn("rooftop", composed.prompt)
        self.assertTrue(composed.conflicts)

    def test_string_axis_values_are_treated_as_fragments(self) -> None:
        composed = self.composer.compose(
            PromptSpec(base_prompt="logo", axis_values={"tone": "premium finish"})
        )
        self.assertIn("premium finish", composed.prompt)

    def test_template_tails_are_applied_last(self) -> None:
        composed = self.composer.compose(
            PromptSpec(base_prompt="acme mark", template="logo")
        )
        self.assertTrue(composed.prompt.endswith("centered"))
        self.assertIn("photograph", composed.negative_prompt)

    def test_text_template_has_no_visual_tail(self) -> None:
        composed = self.composer.compose(
            PromptSpec(base_prompt="write a scene", template="text")
        )
        self.assertEqual(composed.prompt, "write a scene")
        self.assertIsNone(composed.negative_prompt)

    def test_references_and_palette_are_collected(self) -> None:
        composed = self.composer.compose(
            PromptSpec(bible_refs=[self.character.id, self.style.id])
        )
        self.assertEqual(composed.reference_asset_ids, ["asset_ref_1"])
        self.assertEqual(composed.palette, ["#111111"])

    def test_audit_trail_names_every_source(self) -> None:
        composed = self.composer.compose(
            PromptSpec(
                base_prompt="rooftop",
                bible_refs=[self.character.id],
                axis_values={"tone": "premium"},
                extra_fragments=["golden hour"],
                template="image",
            )
        )
        sources = {entry["source"] for entry in composed.applied}
        self.assertIn("base", sources)
        self.assertIn(f"bible:{self.character.id}", sources)
        self.assertIn("axis:tone", sources)
        self.assertIn("extra", sources)
        self.assertIn("template", sources)


class AxisCatalogTests(unittest.TestCase):
    def test_catalog_sizes_are_pinned(self) -> None:
        self.assertEqual(len(get_axis_catalog("logo_structure")), 30)
        self.assertEqual(len(get_axis_catalog("thumbnail_structure")), 30)
        self.assertEqual(len(get_axis_catalog("tone_and_manner")), 10)

    def test_every_entry_is_usable(self) -> None:
        for name in list_axis_catalogs():
            with self.subTest(catalog=name):
                catalog = get_axis_catalog(name)
                labels = [entry["label"] for entry in catalog]
                self.assertEqual(len(labels), len(set(labels)))
                for entry in catalog:
                    self.assertTrue(entry["patch"]["prompt_fragment"].strip())
                    self.assertRegex(entry["label"], r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_tone_entries_carry_palette_and_typography(self) -> None:
        for entry in get_axis_catalog("tone_and_manner"):
            with self.subTest(tone=entry["label"]):
                self.assertGreaterEqual(len(entry["patch"]["palette"]), 3)
                self.assertIn("typography", entry["patch"]["attributes"])

    def test_unknown_catalog_raises_lookup_error(self) -> None:
        with self.assertRaises(LookupError) as context:
            get_axis_catalog("vibes")
        self.assertIn("logo_structure", str(context.exception))

    def test_returned_catalog_is_a_copy(self) -> None:
        catalog = get_axis_catalog("tone_and_manner")
        catalog[0]["patch"]["prompt_fragment"] = "mutated"
        catalog[0]["patch"]["palette"].append("#123456")
        catalog[0]["patch"]["attributes"]["typography"] = "mutated"
        fresh = get_axis_catalog("tone_and_manner")
        self.assertNotEqual(fresh[0]["patch"]["prompt_fragment"], "mutated")
        self.assertNotIn("#123456", fresh[0]["patch"]["palette"])
        self.assertNotEqual(fresh[0]["patch"]["attributes"]["typography"], "mutated")

    def test_catalog_values_compose_into_prompts(self) -> None:
        composer = PromptComposer()
        structure = get_axis_catalog("logo_structure")[0]
        tone = get_axis_catalog("tone_and_manner")[0]
        composed = composer.compose(
            PromptSpec(
                base_prompt="acme coffee roasters logo",
                axis_values={
                    "logo_structure": structure["patch"],
                    "tone_and_manner": tone["patch"],
                },
                template="logo",
            )
        )
        self.assertIn("centered wordmark", composed.prompt)
        self.assertIn("minimal design", composed.prompt)
        self.assertIn("ornament", composed.negative_prompt)


if __name__ == "__main__":
    unittest.main()
