"""Select and render the bounded continuity context injected into a chapter prompt.

A language model writing the next chapter cannot re-read every prior chapter;
this module decides what slice of a story's ``ContinuityMemory`` (built by
``core.story.continuity_builder``) earns a place in its limited context
window, deterministically, and records exactly what was chosen so the result
is both inspectable and reproducible (see parent issue #46 and issue #190).

Selection is governed by a single priority order, checked in this file's
tests: highest priority first, and once one candidate does not fit the
remaining budget, every later candidate — regardless of category — is
omitted rather than let a smaller, lower-priority item jump the queue. That
keeps truncation a stable prefix cut instead of a repacking that would make
"why was this omitted" depend on the exact sizes involved.

1. The single most recent chapter summary — "what just happened" is what the
   very next chapter must not contradict or simply repeat.
2. Unresolved threads, oldest first (``UnresolvedThread.order``) — what the
   reader is owed; a long-open thread should not be the first thing cut.
3. Canon facts, in ``order`` — standing character/world truths.
4. Timeline facts, in ``order`` — chronology, useful but the least likely to
   be directly contradicted by a single new chapter.

Vector retrieval and any judgment of literary quality are out of scope (see
issue #190's non-goals); this module only selects and orders content that
already exists in ``ContinuityMemory``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .continuity import CanonFact, ChapterSummary, ContinuityMemory, TimelineFact, UnresolvedThread

# A chapter-writing prompt has room for far more than this, but the budget
# exists precisely so continuity does not crowd out the writing instructions
# and the story's own brief once a novel runs to dozens of chapters.
DEFAULT_CONTEXT_CHARACTER_BUDGET = 4000

CONTINUITY_PROMPT_HEADING = "### CONTINUITY MEMORY"


class ContinuityContext(BaseModel):
    """The exact, bounded slice of continuity memory chosen for one chapter prompt.

    Every field here either appears verbatim in the rendered prompt block or
    accounts for content that would have qualified by priority order but did
    not fit (``omitted_counts``), so this record alone is enough to reproduce
    or explain what a chapter was written against.
    """

    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1)
    as_of_chapter_id: str | None = None
    character_budget: int = Field(gt=0)
    chapter_summary: ChapterSummary | None = None
    unresolved_threads: list[UnresolvedThread] = Field(default_factory=list)
    canon_facts: list[CanonFact] = Field(default_factory=list)
    timeline_facts: list[TimelineFact] = Field(default_factory=list)
    # Counts of items that qualified by priority order but were cut for
    # space, keyed by the field name above. A category absent from this dict
    # lost nothing.
    omitted_counts: dict[str, int] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        """True when there is truly nothing to inject — a story's first chapter.

        A context that had to cut everything for space (``omitted_counts``
        non-empty even though nothing survived) is deliberately *not* empty:
        there is still something worth telling the model — "continuity
        memory exists but did not fit" — which is a different, and more
        honest, situation than "this story has no continuity memory yet".
        """

        return not (
            self.chapter_summary
            or self.unresolved_threads
            or self.canon_facts
            or self.timeline_facts
            or self.omitted_counts
        )


def build_continuity_context(
    memory: ContinuityMemory | None,
    *,
    story_id: str,
    character_budget: int = DEFAULT_CONTEXT_CHARACTER_BUDGET,
) -> ContinuityContext:
    """Return the bounded, deterministic context to inject for ``story_id``.

    ``memory`` is ``None`` for a story with no continuity memory yet (its
    first chapter): the result is simply empty, not an error, since "nothing
    to remember" is the correct state at that point.
    """

    if character_budget <= 0:
        raise ValueError(
            f"character_budget must be positive, got {character_budget}"
        )
    if memory is None:
        return ContinuityContext(story_id=story_id, character_budget=character_budget)
    if memory.story_id != story_id:
        raise ValueError(
            f"continuity memory {memory.id!r} belongs to story_id "
            f"{memory.story_id!r}, not {story_id!r}"
        )

    # One priority-ordered candidate list, each carrying the field it belongs
    # to and the text whose length counts against the budget. Walking this
    # single list (rather than budgeting each category independently) is what
    # makes "the first thing that doesn't fit stops everything after it" a
    # single, easy-to-audit loop instead of four interacting ones.
    candidates: list[tuple[str, Any, str]] = []
    summaries = memory.ordered_chapter_summaries()
    if summaries:
        latest = summaries[-1]
        candidates.append(("chapter_summary", latest, latest.summary))
    for thread in memory.ordered_unresolved_threads():
        candidates.append(("unresolved_threads", thread, thread.description))
    for canon_fact in memory.ordered_canon_facts():
        candidates.append(("canon_facts", canon_fact, canon_fact.fact))
    for timeline_fact in memory.ordered_timeline_facts():
        candidates.append(("timeline_facts", timeline_fact, timeline_fact.description))

    kept: dict[str, list[Any]] = {
        "chapter_summary": [],
        "unresolved_threads": [],
        "canon_facts": [],
        "timeline_facts": [],
    }
    omitted_counts: dict[str, int] = {}
    remaining = character_budget
    truncated = False
    for field_name, item, text in candidates:
        if not truncated and len(text) <= remaining:
            kept[field_name].append(item)
            remaining -= len(text)
        else:
            truncated = True
            omitted_counts[field_name] = omitted_counts.get(field_name, 0) + 1

    return ContinuityContext(
        story_id=story_id,
        as_of_chapter_id=memory.as_of_chapter_id,
        character_budget=character_budget,
        chapter_summary=kept["chapter_summary"][0] if kept["chapter_summary"] else None,
        unresolved_threads=kept["unresolved_threads"],
        canon_facts=kept["canon_facts"],
        timeline_facts=kept["timeline_facts"],
        omitted_counts=omitted_counts,
    )


def render_continuity_prompt_block(context: ContinuityContext) -> str:
    """Render ``context`` as the prompt section injected before the task brief.

    Returns an empty string for an empty context (a story's first chapter) so
    generation for it carries no confusing, content-free heading.
    """

    if context.is_empty():
        return ""

    lines = [CONTINUITY_PROMPT_HEADING]
    if context.chapter_summary is not None:
        lines.append(f"Previous chapter summary: {context.chapter_summary.summary}")
    if context.unresolved_threads:
        lines.append("Unresolved threads the story still owes the reader:")
        lines.extend(f"- {thread.description}" for thread in context.unresolved_threads)
    if context.canon_facts:
        lines.append("Established facts this chapter must not contradict:")
        lines.extend(
            f"- {fact.subject}: {fact.fact}" for fact in context.canon_facts
        )
    if context.timeline_facts:
        lines.append("Timeline established so far:")
        lines.extend(f"- {fact.description}" for fact in context.timeline_facts)

    omitted_total = sum(context.omitted_counts.values())
    if omitted_total:
        lines.append(
            f"({omitted_total} additional continuity item(s) omitted to fit the "
            f"{context.character_budget}-character continuity budget.)"
        )
    return "\n".join(lines)


__all__ = [
    "CONTINUITY_PROMPT_HEADING",
    "DEFAULT_CONTEXT_CHARACTER_BUDGET",
    "ContinuityContext",
    "build_continuity_context",
    "render_continuity_prompt_block",
]
