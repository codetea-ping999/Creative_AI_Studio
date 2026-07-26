"""Registry for media-type-specific generators."""

from __future__ import annotations

from generators.base import BaseGenerator


class GeneratorRegistry:
    """Resolve a generator from a media type and optional task type.

    Generators are keyed by ``(media_type, task_type)``. A ``task_type`` of
    ``None`` registers the default generator for a media type. Named tasks must
    always match an explicitly registered key so a typo cannot silently run a
    different generator.
    """

    def __init__(self, generators: dict[str, BaseGenerator] | None = None) -> None:
        self._generators: dict[tuple[str, str | None], BaseGenerator] = {
            (media_type, None): generator
            for media_type, generator in (generators or {}).items()
        }

    def register(
        self,
        media_type: str,
        generator: BaseGenerator,
        *,
        task_type: str | None = None,
    ) -> None:
        self._generators[(media_type, task_type)] = generator

    def get(self, media_type: str, task_type: str | None = None) -> BaseGenerator:
        generator = self._generators.get((media_type, task_type))
        if generator is not None:
            return generator

        if task_type is None:
            raise ValueError(f"No generator registered for media type {media_type!r}.")
        raise ValueError(
            f"No generator registered for media type {media_type!r} "
            f"and task type {task_type!r}."
        )

    def registered_keys(self) -> list[tuple[str, str | None]]:
        return list(self._generators)


__all__ = ["GeneratorRegistry"]
