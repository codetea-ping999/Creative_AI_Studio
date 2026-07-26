"""Creative Bible: reusable work settings that keep generations consistent.

A bible entry is the unit of continuity. Instead of retyping "long black straight
hair, purple eyes, beige cardigan" into every prompt and drifting a little each
time, the description lives in one record that the prompt composer expands
deterministically, together with the LoRA, reference images, and seed policy that
belong to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any
import uuid

from core.storage.json_files import ensure_utc, utc_now, write_json_atomic

BIBLE_KINDS: tuple[str, ...] = ("character", "style", "brand", "location", "prop")

# Composition order by kind. Subject-defining entries come first so that style
# and tone modifiers read as modifiers, which is how diffusion prompts weight
# earlier tokens more heavily.
KIND_COMPOSITION_ORDER: tuple[str, ...] = (
    "character",
    "prop",
    "location",
    "style",
    "brand",
)

SEED_MODES: tuple[str, ...] = ("locked", "free")


@dataclass(slots=True)
class BibleEntry:
    """One reusable setting: a character, a style, a brand, a place, a prop."""

    id: str
    kind: str
    name: str
    project_id: str | None = None
    summary: str = ""
    prompt_fragment: str = ""
    negative_fragment: str = ""
    tokens: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    lora: dict[str, Any] | None = None
    reference_asset_ids: list[str] = field(default_factory=list)
    seed_policy: dict[str, Any] = field(default_factory=dict)
    palette: list[str] = field(default_factory=list)
    tone_and_manner: dict[str, Any] = field(default_factory=dict)
    locked_fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in BIBLE_KINDS:
            raise ValueError(
                f"Unknown bible kind {self.kind!r}; "
                f"expected one of {', '.join(BIBLE_KINDS)}"
            )
        if not self.name.strip():
            raise ValueError("Bible entry name must not be empty.")
        seed_mode = self.seed_policy.get("mode")
        if seed_mode is not None and seed_mode not in SEED_MODES:
            raise ValueError(
                f"Unknown seed policy mode {seed_mode!r}; "
                f"expected one of {', '.join(SEED_MODES)}"
            )
        if self.created_at is None:
            self.created_at = utc_now()
        if self.updated_at is None:
            self.updated_at = utc_now()

    @property
    def locked_seed(self) -> int | None:
        """Return the pinned seed, or None when this entry does not pin one."""

        if self.seed_policy.get("mode") != "locked":
            return None
        seed = self.seed_policy.get("seed")
        return int(seed) if isinstance(seed, (int, float)) else None

    def composition_rank(self) -> int:
        try:
            return KIND_COMPOSITION_ORDER.index(self.kind)
        except ValueError:  # pragma: no cover - guarded by __post_init__
            return len(KIND_COMPOSITION_ORDER)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "project_id": self.project_id,
            "summary": self.summary,
            "prompt_fragment": self.prompt_fragment,
            "negative_fragment": self.negative_fragment,
            "tokens": list(self.tokens),
            "attributes": dict(self.attributes),
            "lora": dict(self.lora) if self.lora else None,
            "reference_asset_ids": list(self.reference_asset_ids),
            "seed_policy": dict(self.seed_policy),
            "palette": list(self.palette),
            "tone_and_manner": dict(self.tone_and_manner),
            "locked_fields": list(self.locked_fields),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


_MUTABLE_FIELDS: tuple[str, ...] = (
    "kind",
    "name",
    "project_id",
    "summary",
    "prompt_fragment",
    "negative_fragment",
    "tokens",
    "attributes",
    "lora",
    "reference_asset_ids",
    "seed_policy",
    "palette",
    "tone_and_manner",
    "locked_fields",
    "metadata",
)


class BibleRepository:
    """Persist and manage bible entries on disk."""

    def __init__(self, bible_dir: str | Path = "data/bible") -> None:
        self.bible_dir = Path(bible_dir)
        self.bible_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        kind: str,
        name: str,
        project_id: str | None = None,
        **fields: Any,
    ) -> BibleEntry:
        unknown_fields = set(fields) - set(_MUTABLE_FIELDS)
        if unknown_fields:
            raise ValueError(
                f"Unknown bible fields: {', '.join(sorted(unknown_fields))}"
            )

        entry = BibleEntry(
            id=f"bible_{uuid.uuid4().hex}",
            kind=kind,
            name=name,
            project_id=project_id,
            **fields,
        )
        self._save(entry)
        return entry

    def update(self, entry_id: str, **fields: Any) -> BibleEntry | None:
        entry = self.get(entry_id)
        if entry is None:
            return None

        unknown_fields = set(fields) - set(_MUTABLE_FIELDS)
        if unknown_fields:
            raise ValueError(
                f"Unknown bible fields: {', '.join(sorted(unknown_fields))}"
            )

        before = entry.to_dict()
        for name, value in fields.items():
            setattr(entry, name, value)
        # Re-run validation so an update cannot smuggle in an invalid kind or
        # seed policy that create() would have rejected.
        entry.__post_init__()

        after = entry.to_dict()
        if {k: v for k, v in before.items() if k != "updated_at"} == {
            k: v for k, v in after.items() if k != "updated_at"
        }:
            return self.get(entry_id)

        entry.updated_at = utc_now()
        self._save(entry)
        return entry

    def get(self, entry_id: str) -> BibleEntry | None:
        entry_file = self.bible_dir / f"{entry_id}.json"
        if not entry_file.exists():
            return None
        return self._try_load(entry_file)

    def get_many(self, entry_ids: list[str]) -> list[BibleEntry]:
        """Resolve ids in the given order, silently skipping unknown ones.

        Order is preserved because prompt composition treats reference order as
        meaningful; the caller decides how to report the misses.
        """

        resolved: list[BibleEntry] = []
        for entry_id in entry_ids:
            entry = self.get(entry_id)
            if entry is not None:
                resolved.append(entry)
        return resolved

    def list_all(
        self,
        *,
        kind: str | None = None,
        project_id: str | None = None,
        query_text: str | None = None,
    ) -> list[BibleEntry]:
        normalized_query = query_text.strip().lower() if query_text else None
        entries: list[BibleEntry] = []
        for entry_file in sorted(self.bible_dir.glob("*.json")):
            entry = self._try_load(entry_file)
            if entry is None:
                continue
            if kind and entry.kind != kind:
                continue
            if project_id and entry.project_id != project_id:
                continue
            if normalized_query and normalized_query not in self._haystack(entry):
                continue
            entries.append(entry)

        entries.sort(key=lambda item: item.updated_at, reverse=True)
        return entries

    def delete(self, entry_id: str) -> bool:
        entry_file = self.bible_dir / f"{entry_id}.json"
        if entry_file.exists():
            entry_file.unlink()
            return True
        return False

    def _haystack(self, entry: BibleEntry) -> str:
        return " ".join(
            [
                entry.name,
                entry.kind,
                entry.summary,
                entry.prompt_fragment,
                " ".join(entry.tokens),
                json.dumps(entry.attributes, ensure_ascii=True, sort_keys=True),
            ]
        ).lower()

    def _save(self, entry: BibleEntry) -> None:
        write_json_atomic(self.bible_dir / f"{entry.id}.json", entry.to_dict())

    def _load(self, entry_file: Path) -> BibleEntry:
        data = json.loads(entry_file.read_text(encoding="utf-8"))
        return BibleEntry(
            id=data["id"],
            kind=data["kind"],
            name=data["name"],
            project_id=data.get("project_id"),
            summary=data.get("summary", ""),
            prompt_fragment=data.get("prompt_fragment", ""),
            negative_fragment=data.get("negative_fragment", ""),
            tokens=list(data.get("tokens", [])),
            attributes=dict(data.get("attributes", {})),
            lora=dict(data["lora"]) if data.get("lora") else None,
            reference_asset_ids=list(data.get("reference_asset_ids", [])),
            seed_policy=dict(data.get("seed_policy", {})),
            palette=list(data.get("palette", [])),
            tone_and_manner=dict(data.get("tone_and_manner", {})),
            locked_fields=list(data.get("locked_fields", [])),
            metadata=dict(data.get("metadata", {})),
            created_at=ensure_utc(datetime.fromisoformat(data["created_at"])),
            updated_at=ensure_utc(datetime.fromisoformat(data["updated_at"])),
        )

    def _try_load(self, entry_file: Path) -> BibleEntry | None:
        try:
            return self._load(entry_file)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None


__all__ = [
    "BIBLE_KINDS",
    "KIND_COMPOSITION_ORDER",
    "SEED_MODES",
    "BibleEntry",
    "BibleRepository",
]
