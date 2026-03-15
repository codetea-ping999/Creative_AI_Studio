"""Lightweight in-memory event bus used by the initial job pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobEvent(BaseModel):
    """Published job lifecycle event."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    """Simple event collector stub that can be replaced later."""

    def __init__(self) -> None:
        self._events: list[JobEvent] = []

    def publish(self, event_type: str, payload: dict[str, Any]) -> JobEvent:
        event = JobEvent(type=event_type, payload=payload)
        self._events.append(event)
        return event

    def list_events(self) -> list[JobEvent]:
        return list(self._events)


__all__ = ["EventBus", "JobEvent"]
