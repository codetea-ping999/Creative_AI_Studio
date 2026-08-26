"""Continuity memory: story facts carried across chapters.

A language model writing chapter N has no memory of chapters 1..N-1 beyond what
is placed back in its context window. ``ContinuityMemory`` is the single,
explicit record of what to place there: prior-chapter summaries, canon facts
(character/world state established so far), timeline facts (what happened
when), and threads the story has opened but not yet resolved.

The three fact categories are kept separate rather than folded into one prose
blob because each answers a different question a chapter-writing prompt needs
answered independently: "what happened" (chapter summaries), "what is true now"
(canon facts), "what order did things happen in" (timeline facts), and "what is
the reader still owed" (unresolved threads). Collapsing them would make it
impossible to, for example, inject only unresolved threads into an outline pass
while omitting full prose summaries.

This module defines the contract only. Building/updating a ``ContinuityMemory``
from a completed chapter lives in ``core.story.continuity_builder``; turning a
``ContinuityMemory`` into prompt text is deliberately out of scope for both
modules (see parent issue #46) — the schema needed to be reviewable and
testable on its own before either consumer was built on top of it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.storage.json_files import ensure_utc

# List-length ceilings. A continuity memory is re-read (by a human or re-sent
# to a model) on every chapter, so its growth must be bounded rather than
# accumulating without limit as a story grows to hundreds of chapters.
MAX_CHAPTER_SUMMARIES = 200
MAX_CANON_FACTS = 500
MAX_TIMELINE_FACTS = 500
MAX_UNRESOLVED_THREADS = 200
MAX_RESOLVED_THREADS = 200

# Per-entry text ceilings. A summary is allowed to be a paragraph; a fact or
# thread description is a single claim and stays short so the memory does not
# become an unbounded copy of the prose it is meant to condense.
MAX_SUMMARY_CHARS = 4000
MAX_FACT_CHARS = 1000


class ChapterSummary(BaseModel):
    """A condensed account of one already-written chapter."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)


class CanonFact(BaseModel):
    """A character/world-state fact that later chapters must not contradict."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    fact: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    order: int = Field(ge=0)
    source_chapter_id: str | None = None


class TimelineFact(BaseModel):
    """A fact about when something happened, independent of narration order."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    order: int = Field(ge=0)
    source_chapter_id: str | None = None


class UnresolvedThread(BaseModel):
    """A foreshadowed or opened thread the story has not yet paid off."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    order: int = Field(ge=0)
    introduced_chapter_id: str | None = None


class ResolvedThread(BaseModel):
    """A thread that has been paid off, kept for provenance after removal.

    ``UnresolvedThread`` entries are removed from ``ContinuityMemory`` once a
    chapter resolves them (see the "still owed" framing above): a resolved
    thread has nothing left for a chapter-writing prompt to act on. But
    "removed from the reader-facing list" must not mean "the fact that it
    existed and when it was closed is lost" — an editor asking "wait, did we
    ever explain the letter?" needs a traceable answer. This record is that
    answer: the same identity and description the thread had while open, plus
    the chapter that closed it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    order: int = Field(ge=0)
    introduced_chapter_id: str | None = None
    resolved_chapter_id: str | None = None


def _require_unique(items: list, key: str, list_name: str) -> None:
    seen: set[str] = set()
    for item in items:
        value = getattr(item, key)
        if value in seen:
            raise ValueError(
                f"{list_name} has duplicate {key} {value!r}; each entry in "
                f"{list_name} must have a unique {key} so it can be traced back "
                "to exactly one fact."
            )
        seen.add(value)


class ContinuityMemory(BaseModel):
    """Everything a chapter-writing prompt needs to remember about prior chapters.

    Linked to a story via ``story_id``, and optionally pinned to a point in that
    story's timeline via ``as_of_chapter_id`` (the last chapter this memory
    accounts for; ``None`` means "before any chapter has been written").
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    as_of_chapter_id: str | None = None
    chapter_summaries: list[ChapterSummary] = Field(
        default_factory=list, max_length=MAX_CHAPTER_SUMMARIES
    )
    canon_facts: list[CanonFact] = Field(
        default_factory=list, max_length=MAX_CANON_FACTS
    )
    timeline_facts: list[TimelineFact] = Field(
        default_factory=list, max_length=MAX_TIMELINE_FACTS
    )
    unresolved_threads: list[UnresolvedThread] = Field(
        default_factory=list, max_length=MAX_UNRESOLVED_THREADS
    )
    resolved_threads: list[ResolvedThread] = Field(
        default_factory=list, max_length=MAX_RESOLVED_THREADS
    )
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        # Records written before timestamps were made tz-aware deserialize as
        # naive datetimes; normalizing on load keeps comparisons from raising on
        # a mix of aware and naive values, matching StoryDocument's handling.
        return ensure_utc(value)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "ContinuityMemory":
        _require_unique(self.chapter_summaries, "chapter_id", "chapter_summaries")
        _require_unique(self.canon_facts, "id", "canon_facts")
        _require_unique(self.timeline_facts, "id", "timeline_facts")
        _require_unique(self.unresolved_threads, "id", "unresolved_threads")
        _require_unique(self.resolved_threads, "id", "resolved_threads")
        return self

    def ordered_chapter_summaries(self) -> list[ChapterSummary]:
        """Return chapter summaries sorted by ``order``, breaking ties by id.

        Ties are broken by ``chapter_id`` (rather than left to insertion order)
        so that two callers building the same memory from the same facts, in
        any insertion order, get byte-identical serialized output.
        """

        return sorted(
            self.chapter_summaries, key=lambda item: (item.order, item.chapter_id)
        )

    def ordered_canon_facts(self) -> list[CanonFact]:
        return sorted(self.canon_facts, key=lambda item: (item.order, item.id))

    def ordered_timeline_facts(self) -> list[TimelineFact]:
        return sorted(self.timeline_facts, key=lambda item: (item.order, item.id))

    def ordered_unresolved_threads(self) -> list[UnresolvedThread]:
        return sorted(self.unresolved_threads, key=lambda item: (item.order, item.id))

    def ordered_resolved_threads(self) -> list[ResolvedThread]:
        return sorted(self.resolved_threads, key=lambda item: (item.order, item.id))


__all__ = [
    "MAX_CANON_FACTS",
    "MAX_CHAPTER_SUMMARIES",
    "MAX_FACT_CHARS",
    "MAX_RESOLVED_THREADS",
    "MAX_SUMMARY_CHARS",
    "MAX_TIMELINE_FACTS",
    "MAX_UNRESOLVED_THREADS",
    "CanonFact",
    "ChapterSummary",
    "ContinuityMemory",
    "ResolvedThread",
    "TimelineFact",
    "UnresolvedThread",
]
