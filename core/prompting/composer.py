"""Deterministic prompt composition from bible entries and axis values.

Two properties matter more than cleverness here:

- **Determinism.** The same spec must always produce the same prompt string.
  Reproducibility is a project rule, and a batch that silently reorders its own
  fragments cannot be compared across runs.
- **Auditability.** Every fragment records where it came from, so an unexpected
  attribute in an image can be traced back to the entry that introduced it
  instead of being guessed at.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.bible import BibleEntry, BibleRepository

# Short quality tails per template. They go last because they are the least
# specific part of the prompt and should not compete with the subject.
TEMPLATE_TAILS: dict[str, str] = {
    "image": "highly detailed, sharp focus",
    "logo": "vector logo, flat, clean edges, plain background, centered",
    "thumbnail": "high contrast, bold readable composition, clear focal point",
    "video": "cinematic lighting, consistent framing",
    "text": "",
}

TEMPLATE_NEGATIVE_TAILS: dict[str, str] = {
    "image": "",
    "logo": "photograph, 3d render, busy background, gradient mesh, watermark, text artifacts",
    "thumbnail": "cluttered, illegible text, low contrast",
    "video": "flicker, warped geometry",
    "text": "",
}


class PromptSpec(BaseModel):
    """Declarative description of what a prompt should be built from."""

    model_config = ConfigDict(extra="forbid")

    base_prompt: str = ""
    negative_prompt: str | None = None
    bible_refs: list[str] = Field(default_factory=list)
    axis_values: dict[str, Any] = Field(default_factory=dict)
    template: str = "image"
    extra_fragments: list[str] = Field(default_factory=list)
    seed: int | None = None


class ComposedPrompt(BaseModel):
    """The resolved prompt plus everything needed to reproduce and audit it."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    negative_prompt: str | None
    seed: int | None
    lora: dict[str, Any] | None
    reference_asset_ids: list[str] = Field(default_factory=list)
    palette: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    applied: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class PromptComposer:
    """Compose prompts from a spec, resolving bible references when available."""

    def __init__(self, bible_repository: BibleRepository | None = None) -> None:
        self.bible_repository = bible_repository

    def compose(self, spec: PromptSpec) -> ComposedPrompt:
        applied: list[dict[str, Any]] = []
        conflicts: list[str] = []

        positive_fragments: list[str] = []
        negative_fragments: list[str] = []
        attributes: dict[str, str] = {}
        attribute_owners: dict[str, str] = {}
        locked_attributes: dict[str, str] = {}
        reference_asset_ids: list[str] = []
        palette: list[str] = []
        lora: dict[str, Any] | None = None
        lora_owner: str | None = None
        seed = spec.seed
        seed_owner: str | None = None

        if spec.base_prompt.strip():
            positive_fragments.append(spec.base_prompt.strip())
            applied.append({"source": "base", "fragment": spec.base_prompt.strip()})
        if spec.negative_prompt and spec.negative_prompt.strip():
            negative_fragments.append(spec.negative_prompt.strip())

        for entry in self._resolve_entries(spec.bible_refs, conflicts):
            source = f"bible:{entry.id}"
            if entry.prompt_fragment.strip():
                positive_fragments.append(entry.prompt_fragment.strip())
            if entry.tokens:
                positive_fragments.extend(
                    token.strip() for token in entry.tokens if token.strip()
                )
            if entry.negative_fragment.strip():
                negative_fragments.append(entry.negative_fragment.strip())

            for slot, value in sorted(entry.attributes.items()):
                self._assign_attribute(
                    slot,
                    str(value),
                    source=source,
                    owner_name=entry.name,
                    attributes=attributes,
                    attribute_owners=attribute_owners,
                    locked_attributes=locked_attributes,
                    conflicts=conflicts,
                    is_locked=slot in entry.locked_fields,
                )

            for asset_id in entry.reference_asset_ids:
                if asset_id not in reference_asset_ids:
                    reference_asset_ids.append(asset_id)
            for color in entry.palette:
                if color not in palette:
                    palette.append(color)

            if entry.lora:
                if lora is None:
                    lora = dict(entry.lora)
                    lora_owner = entry.name
                elif str(entry.lora.get("path")) != str(lora.get("path")):
                    # The image pipeline exposes a single adapter slot, so a
                    # second LoRA cannot be honoured; say so instead of silently
                    # dropping it.
                    conflicts.append(
                        f"LoRA conflict: keeping {lora_owner!r} and ignoring "
                        f"{entry.name!r}; only one LoRA can be applied per generation"
                    )

            entry_seed = entry.locked_seed
            if entry_seed is not None:
                if seed_owner is None:
                    seed = entry_seed
                    seed_owner = entry.name
                    applied.append(
                        {
                            "source": source,
                            "kind": "seed_lock",
                            "seed": entry_seed,
                        }
                    )
                elif entry_seed != seed:
                    # First listed wins: bible_refs order is the user's stated
                    # priority, and picking the smaller/larger seed would be an
                    # arbitrary rule that changes output when refs are reordered.
                    conflicts.append(
                        f"Seed lock conflict: keeping seed {seed} from "
                        f"{seed_owner!r} and ignoring {entry_seed} from {entry.name!r}"
                    )

            applied.append(
                {
                    "source": source,
                    "kind": entry.kind,
                    "name": entry.name,
                    "fragment": entry.prompt_fragment.strip(),
                }
            )

        for axis_name in sorted(spec.axis_values):
            patch = spec.axis_values[axis_name]
            fragment, negative, axis_attributes, axis_palette = _normalize_axis_patch(
                patch
            )
            if fragment:
                positive_fragments.append(fragment)
            if negative:
                negative_fragments.append(negative)
            for color in axis_palette:
                if color not in palette:
                    palette.append(color)
            for slot, value in sorted(axis_attributes.items()):
                self._assign_attribute(
                    slot,
                    str(value),
                    source=f"axis:{axis_name}",
                    owner_name=axis_name,
                    attributes=attributes,
                    attribute_owners=attribute_owners,
                    locked_attributes=locked_attributes,
                    conflicts=conflicts,
                    is_locked=False,
                )
            applied.append(
                {"source": f"axis:{axis_name}", "kind": "axis", "fragment": fragment}
            )

        for fragment in spec.extra_fragments:
            if fragment.strip():
                positive_fragments.append(fragment.strip())
                applied.append({"source": "extra", "fragment": fragment.strip()})

        # Attributes are emitted after entry and axis fragments so that a locked
        # attribute is stated close to the quality tail, where it is least likely
        # to be diluted by a later contradicting phrase.
        for slot in sorted(attributes):
            positive_fragments.append(f"{slot}: {attributes[slot]}")

        tail = TEMPLATE_TAILS.get(spec.template, TEMPLATE_TAILS["image"])
        if tail:
            positive_fragments.append(tail)
            applied.append({"source": "template", "fragment": tail})
        negative_tail = TEMPLATE_NEGATIVE_TAILS.get(spec.template, "")
        if negative_tail:
            negative_fragments.append(negative_tail)

        prompt = _join_fragments(positive_fragments)
        negative_prompt = _join_fragments(negative_fragments) or None

        return ComposedPrompt(
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            lora=lora,
            reference_asset_ids=reference_asset_ids,
            palette=palette,
            attributes=attributes,
            applied=applied,
            conflicts=conflicts,
        )

    def _resolve_entries(
        self,
        bible_refs: list[str],
        conflicts: list[str],
    ) -> list[BibleEntry]:
        if not bible_refs:
            return []
        if self.bible_repository is None:
            if bible_refs:
                conflicts.append(
                    "bible references were given but no bible repository is configured"
                )
            return []

        entries: list[BibleEntry] = []
        for entry_id in bible_refs:
            entry = self.bible_repository.get(entry_id)
            if entry is None:
                # A stale reference must not kill a 30-item batch, so it degrades
                # to a warning and the rest of the composition proceeds.
                conflicts.append(f"unknown bible entry: {entry_id}")
                continue
            entries.append(entry)

        # Stable sort by kind rank keeps subject-defining entries first while
        # preserving the caller's order inside each kind.
        return sorted(entries, key=lambda entry: entry.composition_rank())

    def _assign_attribute(
        self,
        slot: str,
        value: str,
        *,
        source: str,
        owner_name: str,
        attributes: dict[str, str],
        attribute_owners: dict[str, str],
        locked_attributes: dict[str, str],
        conflicts: list[str],
        is_locked: bool,
    ) -> None:
        if slot in locked_attributes and locked_attributes[slot] != value:
            conflicts.append(
                f"locked attribute {slot!r} kept as "
                f"{locked_attributes[slot]!r} from {attribute_owners[slot]!r}; "
                f"ignored {value!r} from {owner_name!r}"
            )
            return

        attributes[slot] = value
        attribute_owners[slot] = owner_name
        if is_locked:
            locked_attributes[slot] = value


def _normalize_axis_patch(
    patch: Any,
) -> tuple[str, str, dict[str, Any], list[str]]:
    """Normalize an axis value into (fragment, negative, attributes, palette)."""

    if patch is None:
        return "", "", {}, []
    if isinstance(patch, str):
        return patch.strip(), "", {}, []
    if isinstance(patch, dict):
        fragment = str(
            patch.get("prompt_fragment") or patch.get("prompt_suffix") or ""
        ).strip()
        negative = str(
            patch.get("negative_fragment") or patch.get("negative_suffix") or ""
        ).strip()
        attributes = patch.get("attributes") or {}
        palette = patch.get("palette") or []
        return (
            fragment,
            negative,
            dict(attributes) if isinstance(attributes, dict) else {},
            [str(color) for color in palette] if isinstance(palette, list) else [],
        )
    return str(patch).strip(), "", {}, []


def _join_fragments(fragments: list[str]) -> str:
    """Join fragments into a comma-separated prompt, dropping duplicate tokens.

    Deduplication is case-insensitive and works at comma-separated token level, so
    stacking a character entry and a style entry that both ask for "masterpiece"
    yields one occurrence rather than a prompt that over-weights it.
    """

    seen: set[str] = set()
    tokens: list[str] = []
    for fragment in fragments:
        for token in fragment.split(","):
            normalized = " ".join(token.split())
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(normalized)
    return ", ".join(tokens)


__all__ = [
    "TEMPLATE_NEGATIVE_TAILS",
    "TEMPLATE_TAILS",
    "ComposedPrompt",
    "PromptComposer",
    "PromptSpec",
]
