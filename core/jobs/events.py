"""Lightweight in-memory event bus used by the job pipeline."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
from threading import Lock
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DEFAULT_MAX_EVENTS = 1000

EventSubscriber = Callable[["JobEvent"], None]


class JobEvent(BaseModel):
    """Published job lifecycle event."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    """In-process pub/sub with a bounded event log.

    The log is bounded because a long-lived studio process publishes an event per
    job transition; an unbounded list would grow for the lifetime of the process.
    Subscribers are invoked synchronously on the publishing thread (usually a job
    runner lane), so a failing subscriber must never break job execution.
    """

    def __init__(self, *, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least 1.")
        self._events: deque[JobEvent] = deque(maxlen=max_events)
        self._subscribers: list[EventSubscriber] = []
        self._lock = Lock()

    def publish(self, event_type: str, payload: dict[str, Any]) -> JobEvent:
        event = JobEvent(type=event_type, payload=payload)
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception(
                    "Event subscriber failed for event %s; continuing.",
                    event.type,
                )
        return event

    def subscribe(self, subscriber: EventSubscriber) -> EventSubscriber:
        with self._lock:
            if subscriber not in self._subscribers:
                self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def list_events(self) -> list[JobEvent]:
        with self._lock:
            return list(self._events)


__all__ = ["DEFAULT_MAX_EVENTS", "EventBus", "EventSubscriber", "JobEvent"]
