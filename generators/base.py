"""Abstract generator contract shared across media types."""

from __future__ import annotations

from abc import ABC, abstractmethod
import inspect
from typing import TYPE_CHECKING

from core.schemas import GenerationRequest, GenerationResult

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext


class BaseGenerator(ABC):
    """Base lifecycle for all media generators."""

    def run(
        self,
        request: GenerationRequest,
        context: "GenerationContext | None" = None,
    ) -> GenerationResult:
        """Execute the generator lifecycle for a request."""
        self.validate_request(request)
        self.prepare(request)
        try:
            return self._generate_with_optional_context(request, context)
        finally:
            self.cleanup(request)

    def _generate_with_optional_context(
        self,
        request: GenerationRequest,
        context: "GenerationContext | None",
    ) -> GenerationResult:
        """Call both context-aware and legacy context-free generators."""

        generate = self.generate
        try:
            parameters = inspect.signature(generate).parameters
        except (TypeError, ValueError):
            return generate(request, context)

        if "context" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return generate(request, context=context)
        if any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters.values()
        ):
            return generate(request, context)
        return generate(request)  # type: ignore[call-arg]

    @abstractmethod
    def validate_request(self, request: GenerationRequest) -> None:
        """Validate that a request can be processed by this generator."""

    @abstractmethod
    def prepare(self, request: GenerationRequest) -> None:
        """Prepare any local state required before generation starts."""

    @abstractmethod
    def generate(
        self,
        request: GenerationRequest,
        context: "GenerationContext | None" = None,
    ) -> GenerationResult:
        """Generate output for a validated request.

        ``context``, when provided, lets the generator report incremental
        progress and check for a cooperative cancellation request. It is
        optional so generators that do not support step-level feedback keep
        working unchanged.
        """

    @abstractmethod
    def cleanup(self, request: GenerationRequest) -> None:
        """Clean up transient state after generation finishes."""


__all__ = ["BaseGenerator"]
