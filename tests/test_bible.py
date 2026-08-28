"""Tests for the Creative Bible repository."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.bible import BibleEntry, BibleRepository  # noqa: E402


class BibleEntryTests(unittest.TestCase):
    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            BibleEntry(id="b1", kind="soundtrack", name="x")
        self.assertIn("character", str(context.exception))

    def test_empty_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BibleEntry(id="b1", kind="character", name="   ")

    def test_unknown_seed_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BibleEntry(
                id="b1",
                kind="character",
                name="Mina",
                seed_policy={"mode": "sticky", "seed": 1},
            )

    def test_locked_seed_only_reported_when_locked(self) -> None:
        locked = BibleEntry(
            id="b1",
            kind="character",
            name="Mina",
            seed_policy={"mode": "locked", "seed": 42},
        )
        free = BibleEntry(
            id="b2",
            kind="character",
            name="Rio",
            seed_policy={"mode": "free", "seed": 42},
        )
        self.assertEqual(locked.locked_seed, 42)
        self.assertIsNone(free.locked_seed)

    def test_composition_rank_orders_subject_before_style(self) -> None:
        character = BibleEntry(id="b1", kind="character", name="Mina")
        style = BibleEntry(id="b2", kind="style", name="Noir")
        self.assertLess(character.composition_rank(), style.composition_rank())


class BibleRepositoryTests(unittest.TestCase):
    def test_create_get_update_list_delete(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(
                kind="character",
                name="Mina",
                prompt_fragment="long black straight hair, purple eyes",
                attributes={"hair": "long black straight"},
                tokens=["mina_v1"],
                seed_policy={"mode": "locked", "seed": 7},
            )

            fetched = repository.get(created.id)
            self.assertEqual(fetched.name, "Mina")
            self.assertEqual(fetched.attributes["hair"], "long black straight")
            self.assertEqual(fetched.locked_seed, 7)

            updated = repository.update(created.id, summary="protagonist")
            self.assertEqual(updated.summary, "protagonist")
            self.assertGreaterEqual(updated.updated_at, created.updated_at)

            repository.create(kind="style", name="Noir", project_id="proj_1")
            self.assertEqual(len(repository.list_all()), 2)
            self.assertEqual(len(repository.list_all(kind="style")), 1)
            self.assertEqual(len(repository.list_all(project_id="proj_1")), 1)
            self.assertEqual(len(repository.list_all(query_text="purple")), 1)

            self.assertTrue(repository.delete(created.id))
            self.assertFalse(repository.delete(created.id))

    def test_update_of_unchanged_entry_does_not_bump_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(kind="character", name="Mina", summary="a")
            unchanged = repository.update(created.id, summary="a")
            self.assertEqual(unchanged.updated_at, created.updated_at)

    def test_update_revalidates_the_entry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(kind="character", name="Mina")
            with self.assertRaises(ValueError):
                repository.update(created.id, kind="soundtrack")

    def test_unknown_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            with self.assertRaises(ValueError):
                repository.create(kind="character", name="Mina", vibes="good")
            created = repository.create(kind="character", name="Mina")
            with self.assertRaises(ValueError):
                repository.update(created.id, vibes="good")

    def test_update_of_missing_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            self.assertIsNone(repository.update("bible_missing", summary="x"))

    def test_corrupt_file_is_isolated_from_listing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bible_dir = Path(root) / "bible"
            repository = BibleRepository(bible_dir)
            repository.create(kind="character", name="Healthy")
            (bible_dir / "broken.json").write_text("{nope", encoding="utf-8")

            self.assertEqual(len(repository.list_all()), 1)
            self.assertIsNone(repository.get("broken"))

    def test_get_many_preserves_order_and_skips_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            first = repository.create(kind="character", name="A")
            second = repository.create(kind="character", name="B")

            resolved = repository.get_many([second.id, "bible_missing", first.id])
            self.assertEqual([entry.name for entry in resolved], ["B", "A"])

    def test_round_trip_preserves_every_field(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(
                kind="brand",
                name="Studio",
                summary="s",
                prompt_fragment="p",
                negative_fragment="n",
                tokens=["t"],
                attributes={"a": "b"},
                lora={"path": "models/loras/x.safetensors", "scale": 0.7},
                reference_asset_ids=["asset_1"],
                seed_policy={"mode": "locked", "seed": 3},
                palette=["#000000"],
                tone_and_manner={"typography": "sans"},
                locked_fields=["a"],
                metadata={"note": "n"},
            )
            self.assertEqual(repository.get(created.id).to_dict(), created.to_dict())


class BiblePromoteWinnerTests(unittest.TestCase):
    """#195: promote a winning character-sheet asset into its Bible entry."""

    def test_promotion_locks_seed_merges_attributes_and_registers_reference(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(
                kind="character",
                name="Mina",
                attributes={"hair": "black", "outfit": "school uniform"},
            )

            promoted = repository.promote_winner(
                created.id,
                asset_id="asset_winner",
                job_id="job_winner",
                model_id="sdxl",
                seed=4242,
                params={
                    "width": 832,
                    "height": 1024,
                    # Per-cell batch bookkeeping that must NOT survive promotion --
                    # it describes one character-sheet cell, not the baseline.
                    "bible_refs": [created.id],
                    "axis_values": {"angle": {"prompt_suffix": "front view"}},
                    "batch_axis_labels": {"angle": "front-view"},
                },
                attributes={"hair": "long black straight", "eyes": "purple"},
            )

            assert promoted is not None
            self.assertEqual(
                promoted.attributes,
                {"hair": "long black straight", "outfit": "school uniform", "eyes": "purple"},
            )
            self.assertEqual(promoted.seed_policy, {"mode": "locked", "seed": 4242})
            self.assertEqual(promoted.locked_seed, 4242)
            self.assertEqual(promoted.reference_asset_ids, ["asset_winner"])
            self.assertEqual(promoted.metadata["promoted_model_id"], "sdxl")
            self.assertEqual(promoted.metadata["promoted_params"], {"width": 832, "height": 1024})

            history = promoted.metadata["promotion_history"]
            self.assertEqual(len(history), 1)
            record = history[0]
            self.assertEqual(record["asset_id"], "asset_winner")
            self.assertEqual(record["job_id"], "job_winner")
            self.assertEqual(record["applied"]["seed"], 4242)
            self.assertEqual(record["applied"]["params"], {"width": 832, "height": 1024})
            self.assertEqual(
                record["previous"],
                {
                    "attributes": {"hair": "black", "outfit": "school uniform"},
                    "seed_policy": {},
                    "reference_asset_ids": [],
                    "model_id": None,
                    "params": None,
                },
            )

            # Reloaded from disk, the promotion is not lost.
            reloaded = repository.get(created.id)
            self.assertEqual(reloaded.to_dict(), promoted.to_dict())

    def test_second_promotion_appends_history_and_carries_prior_state_forward(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(kind="character", name="Mina")

            repository.promote_winner(
                created.id,
                asset_id="asset_a",
                model_id="sdxl",
                seed=1,
                params={"width": 512},
                attributes={"hair": "black"},
            )
            second = repository.promote_winner(
                created.id,
                asset_id="asset_b",
                model_id="sdxl-refiner",
                seed=2,
                params={"width": 1024},
                attributes={"hair": "silver"},
            )

            assert second is not None
            self.assertEqual(second.reference_asset_ids, ["asset_a", "asset_b"])
            self.assertEqual(second.attributes, {"hair": "silver"})
            self.assertEqual(second.seed_policy, {"mode": "locked", "seed": 2})

            history = second.metadata["promotion_history"]
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["asset_id"], "asset_a")
            self.assertEqual(history[1]["asset_id"], "asset_b")
            # The second entry's "previous" reflects state left by the first
            # promotion, proving the audit trail is a real chain, not two
            # independent snapshots of the pre-promotion entry.
            self.assertEqual(history[1]["previous"]["attributes"], {"hair": "black"})
            self.assertEqual(history[1]["previous"]["model_id"], "sdxl")
            self.assertEqual(history[1]["previous"]["params"], {"width": 512})

    def test_promoting_an_unknown_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            result = repository.promote_winner(
                "bible_missing",
                asset_id="asset_1",
                model_id="sdxl",
                seed=1,
                params={},
                attributes={},
            )
            self.assertIsNone(result)

    def test_promoting_with_a_blank_asset_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            created = repository.create(kind="character", name="Mina")
            with self.assertRaises(ValueError):
                repository.promote_winner(
                    created.id,
                    asset_id="   ",
                    model_id="sdxl",
                    seed=1,
                    params={},
                    attributes={},
                )

    def test_promoting_only_updates_the_intended_entry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repository = BibleRepository(Path(root) / "bible")
            target = repository.create(kind="character", name="Mina")
            other = repository.create(kind="character", name="Rio")

            repository.promote_winner(
                target.id,
                asset_id="asset_1",
                model_id="sdxl",
                seed=99,
                params={},
                attributes={"hair": "red"},
            )

            untouched = repository.get(other.id)
            self.assertEqual(untouched.to_dict(), other.to_dict())


if __name__ == "__main__":
    unittest.main()
