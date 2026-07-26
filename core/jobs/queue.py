"""In-memory FIFO queue for pending job ids."""

from __future__ import annotations

from collections import deque
from threading import Lock


class JobQueue:
    """Minimal single-process FIFO queue safe for multiple consumers."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        # Guards the check-then-pop in dequeue so multiple runner lanes cannot
        # hand the same job id to two generators.
        self._lock = Lock()

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            self._items.append(job_id)

    def dequeue(self) -> str | None:
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def size(self) -> int:
        with self._lock:
            return len(self._items)


__all__ = ["JobQueue"]
