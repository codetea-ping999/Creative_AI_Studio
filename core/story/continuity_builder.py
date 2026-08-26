"""Build/update ``ContinuityMemory`` from a completed chapter.

Turning a chapter's raw prose into the facts below (subject/fact pairs, timeline
claims, foreshadowing) is an extraction step — likely a future text-generation
task — and is out of scope here (see parent issue #46, and the non-goals on
issue #189). What this module owns is the *merge*: given a chapter's already-
extracted contribution (a ``ChapterCompletion``) and the memory built from
chapters before it, deterministically fold the new facts in.

Two properties matter more than anything else here, because a continuity
record is read back by a human (or re-sent to a model) as the story's source
of truth:

- **Conflicts are surfaced, never silently resolved.** If chapter 12 asserts a
  canon fact under the same id as one chapter 3 already established, and the
  text differs, chapter 3's fact wins and the disagreement is recorded in
  ``ContinuityUpdateResult.conflicts`` — the same "record, don't raise" shape
  ``core.prompting.composer`` uses for bible conflicts, chosen for the same
  reason: one bad extraction must not fail an entire chapter merge.
- **Idempotent.** Re-running the same ``ChapterCompletion`` against the memory
  it already produced must return byte-identical output with zero new
  conflicts, so a retried extraction job (or a chapter re-summarized after an
  unrelated edit) never double-books a fact or reopens a resolved thread.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.storage.json_files import utc_now

from .continuity import (
    MAX_SUMMARY_CHARS,
    CanonFact,
    ChapterSummary,
    ContinuityMemory,
    ResolvedThread,
    TimelineFact,
    UnresolvedThread,
)


class ProposedCanonFact(BaseModel):
    """A canon fact a completed chapter contributes, before it is merged."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    fact: str = Field(min_length=1)


class ProposedTimelineFact(BaseModel):
    """A timeline fact a completed chapter contributes, before it is merged."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ProposedThread(BaseModel):
    """A foreshadowed thread a completed chapter introduces, before merge."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ChapterCompletion(BaseModel):
    """Everything one completed chapter contributes to continuity memory."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    canon_facts: list[ProposedCanonFact] = Field(default_factory=list)
    timeline_facts: list[ProposedTimelineFact] = Field(default_factory=list)
    threads_introduced: list[ProposedThread] = Field(default_factory=list)
    # ids of UnresolvedThread entries this chapter pays off. A plain list of
    # ids (not ProposedThread) because resolving a thread references an
    # identity that must already exist; it does not restate content.
    threads_resolved: list[str] = Field(default_factory=list)


class ContinuityUpdateResult(BaseModel):
    """The updated memory plus any conflicts the merge chose not to silence."""

    model_config = ConfigDict(extra="forbid")

    memory: ContinuityMemory
    conflicts: list[str] = Field(default_factory=list)


def update_continuity_memory(
    memory: ContinuityMemory | None,
    completion: ChapterCompletion,
    *,
    story_id: str | None = None,
    memory_id: str | None = None,
    now: datetime | None = None,
) -> ContinuityUpdateResult:
    """Return ``memory`` updated with ``completion``, or a new record if ``memory`` is ``None``.

    Pure: neither ``memory`` nor ``completion`` is mutated, so a caller can
    compare before/after or discard a merge it does not like — the same
    contract ``apply_text_result`` uses for ``StoryDocument``.
    """

    timestamp = now if now is not None else utc_now()
    conflicts: list[str] = []

    if memory is None:
        if not story_id:
            raise ValueError(
                "story_id is required to start continuity memory for a story "
                "with no existing ContinuityMemory record"
            )
        resolved_story_id = story_id
        resolved_memory_id = memory_id or f"continuity_{story_id}"
        chapter_summaries: list[ChapterSummary] = []
        canon_facts: list[CanonFact] = []
        timeline_facts: list[TimelineFact] = []
        unresolved_threads: list[UnresolvedThread] = []
        resolved_threads: list[ResolvedThread] = []
        created_at = timestamp
    else:
        if story_id is not None and story_id != memory.story_id:
            raise ValueError(
                f"completion targets story_id {story_id!r} but memory "
                f"{memory.id!r} belongs to story_id {memory.story_id!r}"
            )
        resolved_story_id = memory.story_id
        resolved_memory_id = memory.id
        chapter_summaries = list(memory.chapter_summaries)
        canon_facts = list(memory.canon_facts)
        timeline_facts = list(memory.timeline_facts)
        unresolved_threads = list(memory.unresolved_threads)
        resolved_threads = list(memory.resolved_threads)
        created_at = memory.created_at

    chapter_summaries = _upsert_chapter_summary(chapter_summaries, completion)

    canon_facts, fact_conflicts = _merge_canon_facts(canon_facts, completion)
    conflicts.extend(fact_conflicts)

    timeline_facts, timeline_conflicts = _merge_timeline_facts(
        timeline_facts, completion
    )
    conflicts.extend(timeline_conflicts)

    unresolved_threads, introduce_conflicts = _merge_threads_introduced(
        unresolved_threads, resolved_threads, completion
    )
    conflicts.extend(introduce_conflicts)

    unresolved_threads, resolved_threads, resolve_conflicts = (
        _apply_thread_resolutions(unresolved_threads, resolved_threads, completion)
    )
    conflicts.extend(resolve_conflicts)

    # Lists are stored in the same canonical order the ``ordered_*`` accessors
    # compute, rather than raw append order: two calls that merge the same
    # facts (in any order, across any number of ChapterCompletions) must
    # produce a byte-identical record, not just an equivalent one under
    # sorting. That is what makes the round-trip idempotency check below
    # a plain equality comparison rather than a set comparison.
    chapter_summaries = sorted(
        chapter_summaries, key=lambda item: (item.order, item.chapter_id)
    )
    canon_facts = sorted(canon_facts, key=lambda item: (item.order, item.id))
    timeline_facts = sorted(timeline_facts, key=lambda item: (item.order, item.id))
    unresolved_threads = sorted(
        unresolved_threads, key=lambda item: (item.order, item.id)
    )
    resolved_threads = sorted(resolved_threads, key=lambda item: (item.order, item.id))

    # Derived, not tracked separately: the memory's "as of" pointer is always
    # the highest-order chapter it has a summary for, so processing chapters
    # out of order (or re-processing one) can never leave it stale or make it
    # jump backwards.
    as_of_chapter_id = chapter_summaries[-1].chapter_id if chapter_summaries else None

    updated_memory = ContinuityMemory(
        id=resolved_memory_id,
        story_id=resolved_story_id,
        as_of_chapter_id=as_of_chapter_id,
        chapter_summaries=chapter_summaries,
        canon_facts=canon_facts,
        timeline_facts=timeline_facts,
        unresolved_threads=unresolved_threads,
        resolved_threads=resolved_threads,
        created_at=created_at,
        updated_at=timestamp,
    )
    return ContinuityUpdateResult(memory=updated_memory, conflicts=conflicts)


def _next_order(items: list) -> int:
    if not items:
        return 0
    return max(item.order for item in items) + 1


def _upsert_chapter_summary(
    existing: list[ChapterSummary],
    completion: ChapterCompletion,
) -> list[ChapterSummary]:
    # Re-summarizing a chapter (e.g. after a prose edit) must replace its
    # entry, not accumulate a second one under the same chapter_id — the
    # schema's uniqueness rule on chapter_id would reject a plain append.
    kept = [item for item in existing if item.chapter_id != completion.chapter_id]
    kept.append(
        ChapterSummary(
            chapter_id=completion.chapter_id,
            order=completion.order,
            summary=completion.summary,
        )
    )
    return kept


def _merge_canon_facts(
    existing: list[CanonFact],
    completion: ChapterCompletion,
) -> tuple[list[CanonFact], list[str]]:
    conflicts: list[str] = []
    result = list(existing)
    by_id = {item.id: item for item in existing}
    order = _next_order(existing)

    for proposed in completion.canon_facts:
        current = by_id.get(proposed.id)
        if current is None:
            new_fact = CanonFact(
                id=proposed.id,
                subject=proposed.subject,
                fact=proposed.fact,
                order=order,
                source_chapter_id=completion.chapter_id,
            )
            result.append(new_fact)
            by_id[proposed.id] = new_fact
            order += 1
            continue
        if current.subject == proposed.subject and current.fact == proposed.fact:
            # Identical resubmission (e.g. a retried extraction) — idempotent
            # no-op, not a conflict.
            continue
        conflicts.append(
            f"canon_fact {proposed.id!r} from chapter {completion.chapter_id!r} "
            f"({proposed.fact!r}) conflicts with the existing fact "
            f"({current.fact!r}, established in chapter "
            f"{current.source_chapter_id!r}); kept the existing fact"
        )

    return result, conflicts


def _merge_timeline_facts(
    existing: list[TimelineFact],
    completion: ChapterCompletion,
) -> tuple[list[TimelineFact], list[str]]:
    conflicts: list[str] = []
    result = list(existing)
    by_id = {item.id: item for item in existing}
    order = _next_order(existing)

    for proposed in completion.timeline_facts:
        current = by_id.get(proposed.id)
        if current is None:
            new_fact = TimelineFact(
                id=proposed.id,
                description=proposed.description,
                order=order,
                source_chapter_id=completion.chapter_id,
            )
            result.append(new_fact)
            by_id[proposed.id] = new_fact
            order += 1
            continue
        if current.description == proposed.description:
            continue
        conflicts.append(
            f"timeline_fact {proposed.id!r} from chapter {completion.chapter_id!r} "
            f"({proposed.description!r}) conflicts with the existing fact "
            f"({current.description!r}, established in chapter "
            f"{current.source_chapter_id!r}); kept the existing fact"
        )

    return result, conflicts


def _merge_threads_introduced(
    existing: list[UnresolvedThread],
    resolved: list[ResolvedThread],
    completion: ChapterCompletion,
) -> tuple[list[UnresolvedThread], list[str]]:
    conflicts: list[str] = []
    result = list(existing)
    unresolved_by_id = {item.id: item for item in existing}
    resolved_by_id = {item.id: item for item in resolved}
    order = _next_order(existing)

    for proposed in completion.threads_introduced:
        already_resolved = resolved_by_id.get(proposed.id)
        if already_resolved is not None:
            if already_resolved.description == proposed.description:
                # Re-introducing a thread that this same completion (or an
                # earlier one) already resolved — idempotent no-op, and not
                # reopened: resolution is a one-way transition.
                continue
            conflicts.append(
                f"thread {proposed.id!r} from chapter {completion.chapter_id!r} "
                f"conflicts with an already-resolved thread of the same id "
                f"(resolved in chapter {already_resolved.resolved_chapter_id!r}); "
                "not reopened"
            )
            continue

        current = unresolved_by_id.get(proposed.id)
        if current is None:
            new_thread = UnresolvedThread(
                id=proposed.id,
                description=proposed.description,
                order=order,
                introduced_chapter_id=completion.chapter_id,
            )
            result.append(new_thread)
            unresolved_by_id[proposed.id] = new_thread
            order += 1
            continue
        if current.description == proposed.description:
            continue
        conflicts.append(
            f"thread {proposed.id!r} from chapter {completion.chapter_id!r} "
            f"({proposed.description!r}) conflicts with the existing open "
            f"thread ({current.description!r}, introduced in chapter "
            f"{current.introduced_chapter_id!r}); kept the existing thread"
        )

    return result, conflicts


def _apply_thread_resolutions(
    unresolved: list[UnresolvedThread],
    resolved: list[ResolvedThread],
    completion: ChapterCompletion,
) -> tuple[list[UnresolvedThread], list[ResolvedThread], list[str]]:
    conflicts: list[str] = []
    remaining = list(unresolved)
    newly_resolved = list(resolved)
    resolved_ids = {item.id for item in resolved}

    for thread_id in completion.threads_resolved:
        thread = next((item for item in remaining if item.id == thread_id), None)
        if thread is not None:
            remaining = [item for item in remaining if item.id != thread_id]
            newly_resolved.append(
                ResolvedThread(
                    id=thread.id,
                    description=thread.description,
                    order=thread.order,
                    introduced_chapter_id=thread.introduced_chapter_id,
                    resolved_chapter_id=completion.chapter_id,
                )
            )
            resolved_ids.add(thread.id)
            continue
        if thread_id in resolved_ids:
            # Already resolved (e.g. a retried completion) — idempotent no-op.
            continue
        conflicts.append(
            f"chapter {completion.chapter_id!r} resolves thread {thread_id!r}, "
            "which is not an open or previously resolved thread"
        )

    return remaining, newly_resolved, conflicts


__all__ = [
    "ChapterCompletion",
    "ContinuityUpdateResult",
    "ProposedCanonFact",
    "ProposedThread",
    "ProposedTimelineFact",
    "update_continuity_memory",
]
