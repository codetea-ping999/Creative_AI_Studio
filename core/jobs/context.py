"""Cooperative progress reporting and cancellation handed to generators."""

from __future__ import annotations

import time
from typing import Callable


class GenerationCancelled(Exception):
    """Raised by a generator when it observes a cancellation request mid-run.

    JobRunner treats this as a successful cancellation, not a failure: it must
    not call ``JobService.mark_failed`` when this propagates out of
    ``generate()``.
    """


class GenerationContext:
    """Handle passed to a generator so it can report progress and poll for cancellation.

    ``report_progress`` takes a fraction of the generator's own work
    (0.0-1.0); the caller supplies ``on_progress`` to translate that into the
    job's overall progress range, so a generator never needs to know how its
    work maps onto job-level status. Updates are throttled so a per-step
    callback does not flood the job repository with writes.
    """

    def __init__(
        self,
        *,
        is_cancelled: Callable[[], bool],
        on_progress: Callable[[float], None] | None = None,
        min_interval_seconds: float = 0.5,
        min_progress_delta: float = 0.01,
    ) -> None:
        self._is_cancelled = is_cancelled
        self._on_progress = on_progress
        self._min_interval_seconds = min_interval_seconds
        self._min_progress_delta = min_progress_delta
        self._last_reported: float | None = None
        self._last_reported_at: float | None = None

    def report_progress(self, fraction: float) -> None:
        if self._on_progress is None:
            return
        fraction = max(0.0, min(1.0, fraction))
        now = time.monotonic()
        if self._last_reported is not None:
            delta = abs(fraction - self._last_reported)
            elapsed = now - (self._last_reported_at or 0.0)
            if delta < self._min_progress_delta and elapsed < self._min_interval_seconds:
                return
        self._last_reported = fraction
        self._last_reported_at = now
        self._on_progress(fraction)

    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise GenerationCancelled()


__all__ = ["GenerationCancelled", "GenerationContext"]
