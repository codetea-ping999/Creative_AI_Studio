"""Tests for the continuity-memory record contract (schema, ordering, validation)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from core.story.continuity import (  # noqa: E402
    MAX_CHAPTER_SUMMARIES,
    CanonFact,
    ChapterSummary,
    ContinuityMemory,
    TimelineFact,
    UnresolvedThread,
)
from core.storage.json_files import utc_now  # noqa: E402


def _memory(**fields) -> ContinuityMemory:
    now = utc_now()
    defaults: dict = {
        "id": "cont_test",
        "story_id": "story_1",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(fields)
    return ContinuityMemory(**defaults)


class ContinuityMemorySchemaTests(unittest.TestCase):
    def test_valid_record_round_trips_through_json(self) -> None:
        memory = _memory(
            as_of_chapter_id="chapter_02",
            chapter_summaries=[
                ChapterSummary(chapter_id="chapter_01", order=0, summary="第一章の要約"),
                ChapterSummary(chapter_id="chapter_02", order=1, summary="第二章の要約"),
            ],
            canon_facts=[
                CanonFact(
                    id="fact_01",
                    subject="主人公",
                    fact="左腕に古い傷がある",
                    order=0,
                    source_chapter_id="chapter_01",
                ),
            ],
            timeline_facts=[
                TimelineFact(
                    id="time_01",
                    description="物語開始の3年前に事件が起きた",
                    order=0,
                ),
            ],
            unresolved_threads=[
                UnresolvedThread(
                    id="thread_01",
                    description="謎の手紙の差出人は誰か",
                    order=0,
                    introduced_chapter_id="chapter_01",
                ),
            ],
        )

        payload = memory.model_dump(mode="json")
        restored = ContinuityMemory.model_validate(payload)

        self.assertEqual(restored, memory)
        self.assertEqual(restored.story_id, "story_1")
        self.assertEqual(restored.as_of_chapter_id, "chapter_02")

    def test_categories_are_represented_separately(self) -> None:
        memory = _memory(
            chapter_summaries=[
                ChapterSummary(chapter_id="chapter_01", order=0, summary="summary")
            ],
            canon_facts=[
                CanonFact(id="fact_01", subject="hero", fact="has a scar", order=0)
            ],
            timeline_facts=[
                TimelineFact(id="time_01", description="war ended", order=0)
            ],
            unresolved_threads=[
                UnresolvedThread(id="thread_01", description="who sent the letter", order=0)
            ],
        )
        self.assertEqual(len(memory.chapter_summaries), 1)
        self.assertEqual(len(memory.canon_facts), 1)
        self.assertEqual(len(memory.timeline_facts), 1)
        self.assertEqual(len(memory.unresolved_threads), 1)
        # No accidental cross-population between categories.
        self.assertIsInstance(memory.chapter_summaries[0], ChapterSummary)
        self.assertIsInstance(memory.canon_facts[0], CanonFact)

    def test_ordering_is_deterministic_regardless_of_insertion_order(self) -> None:
        # Deliberately inserted out of ``order`` sequence.
        memory = _memory(
            canon_facts=[
                CanonFact(id="fact_b", subject="b", fact="fact b", order=2),
                CanonFact(id="fact_a", subject="a", fact="fact a", order=0),
                CanonFact(id="fact_c", subject="c", fact="fact c", order=1),
            ]
        )

        ordered_once = memory.ordered_canon_facts()
        ordered_twice = memory.ordered_canon_facts()

        self.assertEqual([fact.id for fact in ordered_once], ["fact_a", "fact_c", "fact_b"])
        # Calling it again (and on an independently-constructed equal record)
        # must yield the identical order: nothing here is randomized.
        self.assertEqual(ordered_once, ordered_twice)

    def test_ordering_breaks_ties_by_id_not_insertion_order(self) -> None:
        first = _memory(
            timeline_facts=[
                TimelineFact(id="time_b", description="b", order=0),
                TimelineFact(id="time_a", description="a", order=0),
            ]
        )
        second = _memory(
            timeline_facts=[
                TimelineFact(id="time_a", description="a", order=0),
                TimelineFact(id="time_b", description="b", order=0),
            ]
        )

        self.assertEqual(
            [fact.id for fact in first.ordered_timeline_facts()],
            [fact.id for fact in second.ordered_timeline_facts()],
        )

    def test_duplicate_canon_fact_id_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            _memory(
                canon_facts=[
                    CanonFact(id="fact_01", subject="a", fact="fact a", order=0),
                    CanonFact(id="fact_01", subject="b", fact="fact b", order=1),
                ]
            )
        self.assertIn("duplicate", str(ctx.exception))
        self.assertIn("fact_01", str(ctx.exception))

    def test_duplicate_chapter_summary_chapter_id_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _memory(
                chapter_summaries=[
                    ChapterSummary(chapter_id="chapter_01", order=0, summary="a"),
                    ChapterSummary(chapter_id="chapter_01", order=1, summary="b"),
                ]
            )

    def test_empty_summary_text_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ChapterSummary(chapter_id="chapter_01", order=0, summary="")

    def test_missing_story_id_is_rejected(self) -> None:
        now = utc_now()
        with self.assertRaises(ValidationError):
            ContinuityMemory(id="cont_1", story_id="", created_at=now, updated_at=now)

    def test_chapter_summary_list_over_limit_is_rejected(self) -> None:
        too_many = [
            ChapterSummary(chapter_id=f"chapter_{index:03d}", order=index, summary="x")
            for index in range(MAX_CHAPTER_SUMMARIES + 1)
        ]
        with self.assertRaises(ValidationError) as ctx:
            _memory(chapter_summaries=too_many)
        self.assertIn("chapter_summaries", str(ctx.exception))

    def test_unknown_field_is_rejected(self) -> None:
        now = utc_now()
        with self.assertRaises(ValidationError):
            ContinuityMemory(
                id="cont_1",
                story_id="story_1",
                created_at=now,
                updated_at=now,
                unexpected_field="nope",
            )


if __name__ == "__main__":
    unittest.main()
