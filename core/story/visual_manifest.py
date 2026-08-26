"""Deterministic scene-to-visual request manifest.

Turns a ``StoryDocument`` into an ordered list of self-contained visual
generation requests, one per scene, before any media-strategy decision (still
vs. video, which model, etc. — see parent issue #65) is made.

The manifest deliberately snapshots everything a request needs rather than
storing references to look up later: the composed prompt text, the bible
entries that contributed to it (with their ``updated_at`` as a lightweight
version marker), and the reference asset ids. A ``PromptComposer`` and
``BibleRepository`` are both mutable — a bible entry can be edited after a
scene was composed — so a manifest that stored only ``bible_refs`` and asked
downstream code to re-resolve them later could silently reproduce a different
prompt than the one actually reviewed. Freezing the resolved values into the
manifest at build time is what makes the same story/bible state always produce
the same manifest, and makes every request's provenance fully inspectable
without touching either mutable system again.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.bible import BIBLE_KINDS
from core.prompting.composer import ComposedPrompt

from .schemas import StoryDocument

DEFAULT_ASPECT_RATIO = "16:9"


def _visual_request_id(story_id: str, scene_id: str) -> str:
    # Derived only from the ids that already uniquely identify the scene, so
    # the id is stable across rebuilds (no randomness) and rebuilding the same
    # story never mints a second id for the same scene.
    digest = hashlib.sha1(f"{story_id}:{scene_id}".encode("utf-8")).hexdigest()[:24]
    return f"visreq_{digest}"


class BibleSnapshotRef(BaseModel):
    """One resolved bible entry, frozen at the moment a scene was composed.

    ``version`` is the entry's ``updated_at`` (isoformat) at composition time:
    the bible system has no separate version counter, and the timestamp is
    already the thing that changes exactly when the entry's content does.
    """

    model_config = ConfigDict(extra="forbid")

    bible_id: str = Field(min_length=1)
    kind: str
    name: str
    version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_kind(self) -> "BibleSnapshotRef":
        if self.kind not in BIBLE_KINDS:
            raise ValueError(
                f"bible snapshot for {self.bible_id!r} has unknown kind "
                f"{self.kind!r}; expected one of {', '.join(BIBLE_KINDS)}"
            )
        return self


class SceneVisualRequest(BaseModel):
    """One deterministic visual-generation request derived from one scene."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    prompt: str
    negative_prompt: str | None = None
    visual_intent: str = ""
    duration_seconds: float = Field(default=4.0, ge=0)
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    character_refs: list[str] = Field(default_factory=list)
    location_refs: list[str] = Field(default_factory=list)
    style_refs: list[str] = Field(default_factory=list)
    bible_snapshot: list[BibleSnapshotRef] = Field(default_factory=list)
    reference_asset_ids: list[str] = Field(default_factory=list)
    seed: int | None = None
    conflicts: list[str] = Field(default_factory=list)


class SceneVisualManifest(BaseModel):
    """The ordered, deterministic set of visual requests for one story."""

    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1)
    requests: list[SceneVisualRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_requests(self) -> "SceneVisualManifest":
        seen_scene_ids: set[str] = set()
        seen_ids: set[str] = set()
        for request in self.requests:
            if request.story_id != self.story_id:
                raise ValueError(
                    f"visual request {request.id!r} has story_id "
                    f"{request.story_id!r}, which does not match manifest "
                    f"story_id {self.story_id!r}; a manifest may only carry "
                    "requests for the story it was built from."
                )
            if request.scene_id in seen_scene_ids:
                raise ValueError(
                    f"scene {request.scene_id!r} has more than one visual "
                    "request in this manifest; every request must trace back "
                    "to exactly one source scene."
                )
            seen_scene_ids.add(request.scene_id)
            if request.id in seen_ids:
                raise ValueError(
                    f"duplicate visual request id {request.id!r}; request ids "
                    "must be unique within a manifest."
                )
            seen_ids.add(request.id)
        return self

    def requests_in_order(self) -> list[SceneVisualRequest]:
        """Return requests sorted by scene ``order``, breaking ties by id.

        Ties are broken by id (rather than left to insertion/list order) so
        that building the same manifest twice always serializes identically.
        """

        return sorted(self.requests, key=lambda request: (request.order, request.id))


def build_visual_manifest(
    story: StoryDocument,
    composed_prompts: dict[str, ComposedPrompt],
    bible_snapshots: dict[str, list[BibleSnapshotRef]] | None = None,
    *,
    visual_intents: dict[str, str] | None = None,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
) -> SceneVisualManifest:
    """Build the deterministic scene-to-visual manifest for ``story``.

    ``composed_prompts`` and ``bible_snapshots`` are keyed by scene id and are
    expected to already have been produced (e.g. by ``PromptComposer`` against
    a ``BibleRepository``); this function only assembles and freezes what it is
    given, so it never has to reach back into either mutable system to
    reproduce a result. Rebuilding from the same story and the same composed
    inputs always yields the same manifest, because every id here is derived
    rather than random.
    """

    bible_snapshots = bible_snapshots or {}
    visual_intents = visual_intents or {}

    requests: list[SceneVisualRequest] = []
    for scene in story.scenes_in_order():
        composed = composed_prompts.get(scene.id)
        if composed is None:
            raise ValueError(
                f"scene {scene.id!r} in story {story.id!r} has no composed "
                "prompt; every scene must be composed before it can enter the "
                "visual manifest"
            )
        snapshot = list(bible_snapshots.get(scene.id, []))
        requests.append(
            SceneVisualRequest(
                id=_visual_request_id(story.id, scene.id),
                story_id=story.id,
                scene_id=scene.id,
                order=scene.order,
                prompt=composed.prompt,
                negative_prompt=composed.negative_prompt,
                visual_intent=visual_intents.get(scene.id, ""),
                duration_seconds=scene.duration_seconds,
                aspect_ratio=aspect_ratio,
                character_refs=[
                    ref.bible_id for ref in snapshot if ref.kind == "character"
                ],
                location_refs=[
                    ref.bible_id for ref in snapshot if ref.kind == "location"
                ],
                style_refs=[ref.bible_id for ref in snapshot if ref.kind == "style"],
                bible_snapshot=snapshot,
                reference_asset_ids=list(composed.reference_asset_ids),
                seed=composed.seed,
                conflicts=list(composed.conflicts),
            )
        )
    return SceneVisualManifest(story_id=story.id, requests=requests)


__all__ = [
    "DEFAULT_ASPECT_RATIO",
    "BibleSnapshotRef",
    "SceneVisualManifest",
    "SceneVisualRequest",
    "build_visual_manifest",
]
