"""Abstract generator contract shared across media types."""

from __future__ import annotations

from abc import ABC, abstractmethod
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
            return self.generate(request, context)
        finally:
            self.cleanup(request)

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
