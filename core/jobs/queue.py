"""In-memory FIFO queue for pending job ids, with optional lane routing.

`core/jobs/lanes.py` (#179) defines *which* lane a job kind belongs to
(`assign_lane`); this module is the queue that actually keeps each lane's
pending job ids separate once assigned (#180). A `JobQueue` constructed with
no `lanes` argument behaves exactly as it did before lanes existed -- a
single FIFO of job ids, addressed with no lane argument at all -- so every
existing caller (`JobService`, `JobRunner`, `bootstrap/factories.py`, tests)
keeps working unchanged. Passing `lanes=("heavy", "light")` (for example)
switches it into routed mode, where each lane is an independent FIFO deque
and every method requires an explicit `lane` argument -- there is no
"guess which lane" fallback, in keeping with `assign_lane`'s own refusal to
guess an unconfigured lane.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from threading import Lock

# Name of the single implicit lane used when `JobQueue` is constructed with
# no `lanes` argument. Never observable by a zero-arg caller (`enqueue(id)`,
# `dequeue()`, `size()` all resolve to it implicitly) -- it only shows up in
# `lane_names` for introspection.
_DEFAULT_LANE = "default"


class JobQueue:
    """FIFO queue for pending job ids, optionally partitioned into lanes.

    Safe for multiple concurrent producers/consumers: every operation holds a
    single lock across its check-then-mutate steps, so two callers racing to
    dequeue (or to enqueue the same job id into two different lanes) cannot
    observe or create an inconsistent state.
    """

    def __init__(self, lanes: Sequence[str] | None = None) -> None:
        lane_names = tuple(lanes) if lanes else (_DEFAULT_LANE,)
        if not lane_names:
            raise ValueError("JobQueue requires at least one lane.")
        if len(set(lane_names)) != len(lane_names):
            raise ValueError(f"JobQueue lane names must be unique; got {lane_names}.")

        self._lane_names = lane_names
        self._queues: dict[str, deque[str]] = {name: deque() for name in lane_names}
        # Tracks which lane currently holds each pending job id. This is what
        # makes "a job cannot be enqueued into two lanes" an enforced
        # invariant rather than just a convention: enqueue consults it before
        # ever appending to a deque, and dequeue clears it, so a job id is
        # associated with at most one lane at any moment.
        self._job_lane: dict[str, str] = {}
        self._lock = Lock()

    @property
    def lane_names(self) -> tuple[str, ...]:
        """Configured lane names, in construction order."""

        return self._lane_names

    @property
    def is_single_lane(self) -> bool:
        """True when this queue has exactly one lane (the pre-#180 shape)."""

        return len(self._lane_names) == 1

    def enqueue(self, job_id: str, lane: str | None = None) -> None:
        """Append `job_id` to `lane` (or the sole lane, if only one exists).

        Idempotent for a job id already pending in that same lane -- calling
        it twice does not duplicate the entry. Raises `ValueError` if
        `job_id` is already pending in a *different* lane: a job is assigned
        to exactly one lane (#180's core invariant), so re-routing it would
        be a bug in the caller, not something to silently accept.
        """

        with self._lock:
            target = self._resolve_lane(lane)
            existing_lane = self._job_lane.get(job_id)
            if existing_lane is not None:
                if existing_lane != target:
                    raise ValueError(
                        f"Job {job_id!r} is already queued in lane {existing_lane!r}; "
                        f"refusing to also enqueue it into lane {target!r}."
                    )
                # Already pending in this lane -- enqueue is idempotent.
                return
            self._job_lane[job_id] = target
            self._queues[target].append(job_id)

    def dequeue(self, lane: str | None = None) -> str | None:
        """Pop and return the next job id from `lane`, or None if it is empty."""

        with self._lock:
            target = self._resolve_lane(lane)
            queue = self._queues[target]
            if not queue:
                return None
            job_id = queue.popleft()
            self._job_lane.pop(job_id, None)
            return job_id

    def size(self, lane: str | None = None) -> int:
        """Return the pending count for `lane`, or the total across all lanes.

        `lane=None` on a single-lane queue returns that lane's size (the
        pre-#180 behavior). `lane=None` on a multi-lane queue returns the sum
        across every lane -- a read-only introspection convenience, not a
        routing decision, so it is not subject to the "no guessing" rule
        `enqueue`/`dequeue` follow.
        """

        with self._lock:
            if lane is None and not self.is_single_lane:
                return sum(len(queue) for queue in self._queues.values())
            target = self._resolve_lane(lane)
            return len(self._queues[target])

    def _resolve_lane(self, lane: str | None) -> str:
        if lane is None:
            if not self.is_single_lane:
                raise ValueError(
                    f"lane must be specified for a multi-lane JobQueue; "
                    f"configured lanes are {self._lane_names}."
                )
            return self._lane_names[0]
        if lane not in self._queues:
            raise ValueError(
                f"Unknown lane {lane!r}; configured lanes are {self._lane_names}."
            )
        return lane


__all__ = ["JobQueue"]
