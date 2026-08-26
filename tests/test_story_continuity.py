"""Tests for continuity memory: the record contract and the chapter-completion builder."""

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
from core.story.continuity_builder import (  # noqa: E402
    ChapterCompletion,
    ProposedCanonFact,
    ProposedThread,
    ProposedTimelineFact,
    update_continuity_memory,
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


def _completion(**fields) -> ChapterCompletion:
    defaults: dict = {
        "chapter_id": "chapter_01",
        "order": 0,
        "summary": "第一章の要約",
    }
    defaults.update(fields)
    return ChapterCompletion(**defaults)


class ContinuityBuilderTests(unittest.TestCase):
    def test_completing_a_chapter_produces_a_valid_new_memory(self) -> None:
        result = update_continuity_memory(
            None,
            _completion(),
            story_id="story_1",
            now=utc_now(),
        )

        self.assertEqual(result.conflicts, [])
        memory = result.memory
        self.assertIsInstance(memory, ContinuityMemory)
        self.assertEqual(memory.story_id, "story_1")
        self.assertEqual(memory.as_of_chapter_id, "chapter_01")
        self.assertEqual(len(memory.chapter_summaries), 1)
        self.assertEqual(memory.chapter_summaries[0].summary, "第一章の要約")

    def test_completing_a_chapter_updates_an_existing_memory(self) -> None:
        first = update_continuity_memory(
            None, _completion(), story_id="story_1", now=utc_now()
        ).memory

        second = update_continuity_memory(
            first,
            _completion(
                chapter_id="chapter_02", order=1, summary="第二章の要約"
            ),
            now=utc_now(),
        )

        memory = second.memory
        self.assertEqual(memory.id, first.id)
        self.assertEqual(memory.created_at, first.created_at)
        self.assertEqual(memory.as_of_chapter_id, "chapter_02")
        self.assertEqual(
            [item.chapter_id for item in memory.ordered_chapter_summaries()],
            ["chapter_01", "chapter_02"],
        )

    def test_missing_story_id_for_a_fresh_memory_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            update_continuity_memory(None, _completion())

    def test_mismatched_story_id_is_rejected(self) -> None:
        memory = update_continuity_memory(
            None, _completion(), story_id="story_1", now=utc_now()
        ).memory
        with self.assertRaises(ValueError):
            update_continuity_memory(
                memory,
                _completion(chapter_id="chapter_02", order=1),
                story_id="story_2",
            )

    def test_re_summarizing_a_chapter_replaces_rather_than_duplicates(self) -> None:
        first = update_continuity_memory(
            None, _completion(summary="draft summary"), story_id="story_1"
        ).memory

        second = update_continuity_memory(
            first, _completion(summary="revised summary")
        ).memory

        self.assertEqual(len(second.chapter_summaries), 1)
        self.assertEqual(second.chapter_summaries[0].summary, "revised summary")

    def test_new_canon_fact_carries_chapter_provenance(self) -> None:
        result = update_continuity_memory(
            None,
            _completion(
                canon_facts=[
                    ProposedCanonFact(id="fact_01", subject="hero", fact="has a scar")
                ]
            ),
            story_id="story_1",
        )

        fact = result.memory.canon_facts[0]
        self.assertEqual(fact.subject, "hero")
        self.assertEqual(fact.fact, "has a scar")
        self.assertEqual(fact.source_chapter_id, "chapter_01")
        self.assertEqual(result.conflicts, [])

    def test_conflicting_canon_fact_is_surfaced_not_silently_replaced(self) -> None:
        first = update_continuity_memory(
            None,
            _completion(
                canon_facts=[
                    ProposedCanonFact(id="fact_01", subject="hero", fact="has a scar")
                ]
            ),
            story_id="story_1",
        ).memory

        second = update_continuity_memory(
            first,
            _completion(
                chapter_id="chapter_02",
                order=1,
                canon_facts=[
                    ProposedCanonFact(
                        id="fact_01", subject="hero", fact="has no scar at all"
                    )
                ],
            ),
        )

        # The original fact is kept — a later chapter cannot silently rewrite it.
        self.assertEqual(len(second.memory.canon_facts), 1)
        self.assertEqual(second.memory.canon_facts[0].fact, "has a scar")
        self.assertEqual(second.memory.canon_facts[0].source_chapter_id, "chapter_01")
        self.assertEqual(len(second.conflicts), 1)
        self.assertIn("fact_01", second.conflicts[0])
        self.assertIn("chapter_02", second.conflicts[0])

    def test_conflicting_timeline_fact_is_surfaced_not_silently_replaced(self) -> None:
        first = update_continuity_memory(
            None,
            _completion(
                timeline_facts=[
                    ProposedTimelineFact(id="time_01", description="war started")
                ]
            ),
            story_id="story_1",
        ).memory

        second = update_continuity_memory(
            first,
            _completion(
                chapter_id="chapter_02",
                order=1,
                timeline_facts=[
                    ProposedTimelineFact(id="time_01", description="war never happened")
                ],
            ),
        )

        self.assertEqual(second.memory.timeline_facts[0].description, "war started")
        self.assertEqual(len(second.conflicts), 1)
        self.assertIn("time_01", second.conflicts[0])

    def test_identical_canon_fact_resubmission_is_not_a_conflict(self) -> None:
        first = update_continuity_memory(
            None,
            _completion(
                canon_facts=[
                    ProposedCanonFact(id="fact_01", subject="hero", fact="has a scar")
                ]
            ),
            story_id="story_1",
        ).memory

        # Same chapter, same fact resubmitted (e.g. a retried extraction job).
        second = update_continuity_memory(
            first,
            _completion(
                canon_facts=[
                    ProposedCanonFact(id="fact_01", subject="hero", fact="has a scar")
                ]
            ),
        )

        self.assertEqual(second.conflicts, [])
        self.assertEqual(len(second.memory.canon_facts), 1)

    def test_thread_introduced_then_resolved_moves_to_resolved_threads(self) -> None:
        introduced = update_continuity_memory(
            None,
            _completion(
                threads_introduced=[
                    ProposedThread(id="thread_01", description="who sent the letter")
                ]
            ),
            story_id="story_1",
        ).memory
        self.assertEqual(len(introduced.unresolved_threads), 1)
        self.assertEqual(introduced.unresolved_threads[0].introduced_chapter_id, "chapter_01")
        self.assertEqual(introduced.resolved_threads, [])

        resolved = update_continuity_memory(
            introduced,
            _completion(
                chapter_id="chapter_05",
                order=4,
                threads_resolved=["thread_01"],
            ),
        ).memory

        # Removed from the open list...
        self.assertEqual(resolved.unresolved_threads, [])
        # ...but provenance is traceable: description, who opened it, who closed it.
        self.assertEqual(len(resolved.resolved_threads), 1)
        closed = resolved.resolved_threads[0]
        self.assertEqual(closed.id, "thread_01")
        self.assertEqual(closed.description, "who sent the letter")
        self.assertEqual(closed.introduced_chapter_id, "chapter_01")
        self.assertEqual(closed.resolved_chapter_id, "chapter_05")

    def test_resolving_an_unknown_thread_is_surfaced_as_a_conflict(self) -> None:
        memory = update_continuity_memory(
            None, _completion(), story_id="story_1"
        ).memory

        result = update_continuity_memory(
            memory,
            _completion(chapter_id="chapter_02", order=1, threads_resolved=["ghost_thread"]),
        )

        self.assertEqual(len(result.conflicts), 1)
        self.assertIn("ghost_thread", result.conflicts[0])
        self.assertEqual(result.memory.resolved_threads, [])

    def test_reintroducing_an_already_resolved_thread_is_surfaced_not_reopened(
        self,
    ) -> None:
        introduced = update_continuity_memory(
            None,
            _completion(
                threads_introduced=[
                    ProposedThread(id="thread_01", description="who sent the letter")
                ]
            ),
            story_id="story_1",
        ).memory
        resolved = update_continuity_memory(
            introduced,
            _completion(chapter_id="chapter_02", order=1, threads_resolved=["thread_01"]),
        ).memory

        reintroduced = update_continuity_memory(
            resolved,
            _completion(
                chapter_id="chapter_03",
                order=2,
                threads_introduced=[
                    ProposedThread(id="thread_01", description="a different mystery")
                ],
            ),
        )

        self.assertEqual(reintroduced.memory.unresolved_threads, [])
        self.assertEqual(len(reintroduced.memory.resolved_threads), 1)
        self.assertEqual(len(reintroduced.conflicts), 1)
        self.assertIn("thread_01", reintroduced.conflicts[0])

    def test_rerunning_the_same_update_is_idempotent(self) -> None:
        completion = _completion(
            canon_facts=[
                ProposedCanonFact(id="fact_01", subject="hero", fact="has a scar")
            ],
            timeline_facts=[
                ProposedTimelineFact(id="time_01", description="war started")
            ],
            threads_introduced=[
                ProposedThread(id="thread_01", description="who sent the letter")
            ],
        )
        now = utc_now()

        first = update_continuity_memory(None, completion, story_id="story_1", now=now)
        second = update_continuity_memory(first.memory, completion, now=now)

        self.assertEqual(first.memory, second.memory)
        self.assertEqual(second.conflicts, [])

    def test_rerunning_including_a_thread_resolution_is_idempotent(self) -> None:
        introduced = update_continuity_memory(
            None,
            _completion(
                threads_introduced=[
                    ProposedThread(id="thread_01", description="who sent the letter")
                ]
            ),
            story_id="story_1",
        ).memory
        resolving_completion = _completion(
            chapter_id="chapter_02", order=1, threads_resolved=["thread_01"]
        )
        now = utc_now()

        first = update_continuity_memory(introduced, resolving_completion, now=now)
        second = update_continuity_memory(first.memory, resolving_completion, now=now)

        self.assertEqual(first.memory, second.memory)
        self.assertEqual(second.conflicts, [])

    def test_as_of_chapter_id_tracks_the_highest_order_processed(self) -> None:
        memory = update_continuity_memory(
            None, _completion(chapter_id="chapter_05", order=4), story_id="story_1"
        ).memory
        # Backfilling an earlier chapter must not move the pointer backwards.
        backfilled = update_continuity_memory(
            memory, _completion(chapter_id="chapter_02", order=1)
        ).memory

        self.assertEqual(backfilled.as_of_chapter_id, "chapter_05")


if __name__ == "__main__":
    unittest.main()
