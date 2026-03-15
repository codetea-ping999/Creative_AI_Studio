"""In-memory FIFO queue for pending job ids."""

from __future__ import annotations

from collections import deque


class JobQueue:
    """Minimal single-process FIFO queue."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()

    def enqueue(self, job_id: str) -> None:
        self._items.append(job_id)

    def dequeue(self) -> str | None:
        if not self._items:
            return None
        return self._items.popleft()

    def size(self) -> int:
        return len(self._items)


__all__ = ["JobQueue"]
