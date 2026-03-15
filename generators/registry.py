"""Registry for media-type-specific generators."""

from __future__ import annotations

from generators.base import BaseGenerator


class GeneratorRegistry:
    """Resolve a generator from a media type."""

    def __init__(self, generators: dict[str, BaseGenerator] | None = None) -> None:
        self._generators: dict[str, BaseGenerator] = dict(generators or {})

    def register(self, media_type: str, generator: BaseGenerator) -> None:
        self._generators[media_type] = generator

    def get(self, media_type: str) -> BaseGenerator:
        try:
            return self._generators[media_type]
        except KeyError as exc:
            raise ValueError(f"No generator registered for media type {media_type!r}.") from exc


__all__ = ["GeneratorRegistry"]
