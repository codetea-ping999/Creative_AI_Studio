"""In-process cancellation signaling between the API and the job worker."""

from __future__ import annotations

from threading import Event, Lock


class CancellationRegistry:
    """Tracks cancellation requests for jobs currently being processed.

    The API and the job worker run as threads in the same process (see
    ``apps/api/main.py``), so an in-memory ``Event`` is enough to interrupt a
    blocking generation call without IPC. Entries are created when a job
    starts running and removed once it reaches a terminal state, so the
    registry only ever holds entries for jobs actively being worked on.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: dict[str, Event] = {}

    def begin(self, job_id: str) -> Event:
        with self._lock:
            event = self._events.get(job_id)
            if event is None:
                event = Event()
                self._events[job_id] = event
            return event

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            event = self._events.get(job_id)
        if event is not None:
            event.set()

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            event = self._events.get(job_id)
        return event is not None and event.is_set()

    def end(self, job_id: str, token: Event) -> None:
        """Remove an entry only when the caller still owns its registration."""

        with self._lock:
            if self._events.get(job_id) is token:
                self._events.pop(job_id, None)


__all__ = ["CancellationRegistry"]
