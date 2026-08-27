"""Independent per-lane worker threads for `JobRunner` (#181).

`core/jobs/lanes.py` (#179) defines *which* lane a job kind belongs to, and
`core/jobs/queue.py` (#180) keeps each lane's pending job ids separate once
routed. Neither one runs anything -- before this module, exactly one thread
called `JobRunner.run_forever()` for the whole process (see
`apps/api/main.py`), so a long run of heavy image/video/music jobs still
starved a quick text/TTS job behind it in practice: the single worker only
ever dequeues one lane at a time.

`WorkerPool` is the piece that actually gives each configured lane its own
worker thread(s), so a heavy job in flight never blocks a light job from
being picked up. It is intentionally the only thing in the lane stack that
touches `threading.Thread` -- `JobRunner` stays lane-agnostic (it already
accepts an optional `lane` argument, added by #180, that this module is the
first caller to actually use for concurrent per-lane dispatch).
"""

from __future__ import annotations

import logging
from threading import Event, Thread
from typing import Callable

from .lanes import LaneConfig
from .runner import JobRunner

logger = logging.getLogger(__name__)

# Matches `JobRunner.run_forever`'s own default poll cadence (see
# `core/jobs/runner.py`) so a `WorkerPool` worker is not observably slower to
# notice a newly queued job than the pre-#181 single-thread runner was.
_DEFAULT_POLL_INTERVAL_SECONDS = 0.1

# After a worker observes an exception from `run_once`, it waits this long
# before polling its lane again instead of retrying immediately. A worker
# whose lane is persistently failing (e.g. a generator that always raises
# before `JobRunner.process_job` can convert the failure into `mark_failed`)
# must not spin a CPU core at 100% retrying the same failure forever.
_DEFAULT_ERROR_BACKOFF_SECONDS = 0.5

_DEFAULT_STOP_JOIN_TIMEOUT_SECONDS = 2.0


class WorkerPool:
    """Starts/stops one independent worker thread per configured lane slot.

    Reads `lane_config.concurrency` to decide how many worker threads each
    lane gets (task 1 of #181: "Start the configured worker count for each
    lane"). Every worker thread runs its own dequeue/process loop against
    `job_runner`, scoped to one lane, so a heavy lane full of long-running
    jobs never blocks a light lane's workers from making progress -- they are
    genuinely independent threads polling independent `JobQueue` lanes.

    `lane_config=None` (the default) reproduces the pre-#181 single-thread
    behavior exactly: one worker, dequeuing the queue's implicit lane via
    `run_once(lane=None)`. A single-lane `LaneConfig` (e.g. `heavy:2`, or the
    result of `parse_job_lanes("heavy:1")`) also dequeues with `lane=None`
    rather than the lane's own name -- this mirrors `JobService.enqueue_job`,
    which collapses single-lane configurations onto the queue's implicit lane
    (see `core/jobs/service.py`), so a `WorkerPool` and the `JobService`
    feeding it always agree on which lane name (if any) is actually in play.
    Only a genuinely multi-lane `LaneConfig` causes workers to dequeue by
    explicit lane name, matching the multi-lane `JobQueue` that pairs with it.

    A worker thread that hits an unexpected exception logs it and keeps
    polling its own lane rather than exiting -- one bad iteration must not
    silently turn into a dead consumer with jobs piling up unprocessed behind
    it, and must not affect any other lane's worker threads (each is an
    independent thread with no shared mutable loop state).
    """

    def __init__(
        self,
        job_runner: JobRunner,
        lane_config: LaneConfig | None = None,
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        error_backoff_seconds: float = _DEFAULT_ERROR_BACKOFF_SECONDS,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        self._job_runner = job_runner
        self._lane_config = lane_config
        self._poll_interval_seconds = poll_interval_seconds
        self._error_backoff_seconds = error_backoff_seconds
        # Overridable only so tests can substitute a `Thread` subclass/spy;
        # production callers should never need this.
        self._thread_factory = thread_factory
        self._stop_event: Event | None = None
        self._threads: list[Thread] = []

    @property
    def is_running(self) -> bool:
        """True between a successful `start()` and the matching `stop()`."""

        return self._stop_event is not None

    @property
    def worker_names(self) -> tuple[str, ...]:
        """Thread names of the currently running workers, lane-major order.

        Empty when not running. Exists so callers (chiefly tests) can assert
        on worker count/composition without reaching into private state.
        """

        return tuple(thread.name for thread in self._threads)

    def start(self) -> None:
        """Spawn one daemon thread per configured lane worker slot.

        Raises `RuntimeError` if already running: calling `start()` twice
        without an intervening `stop()` would spin up a second set of
        consumers racing the first over the same lanes, which is exactly the
        "duplicate consumers" failure #181 requires this to prevent.
        """

        if self.is_running:
            raise RuntimeError(
                "WorkerPool.start() called while already running; call stop() "
                "first -- starting twice would create duplicate consumers on "
                "the same lane(s)."
            )

        self._stop_event = Event()
        self._threads = []
        for lane, worker_count in self._lane_worker_slots():
            lane_label = lane if lane is not None else "default"
            for worker_index in range(worker_count):
                thread = self._thread_factory(
                    target=self._run_worker,
                    args=(lane, worker_index),
                    daemon=True,
                    name=f"creative-ai-job-runner-{lane_label}-{worker_index}",
                )
                self._threads.append(thread)
                thread.start()

    def stop(self, *, timeout: float | None = _DEFAULT_STOP_JOIN_TIMEOUT_SECONDS) -> None:
        """Signal every worker to exit its loop and join them.

        A no-op when not running, so callers do not need to track `is_running`
        themselves before calling this (mirrors the old single-thread
        shutdown in `apps/api/main.py`, which tolerated `stop_event is None`).
        A thread that has not stopped within `timeout` is logged by name
        rather than silently dropped -- it is still a daemon thread so it
        cannot block process exit, but a caller relying on "stop() means
        stopped" deserves to know that assumption did not hold.
        """

        stop_event = self._stop_event
        if stop_event is None:
            return

        stop_event.set()
        dangling: list[str] = []
        for thread in self._threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                dangling.append(thread.name)

        self._threads = []
        self._stop_event = None

        if dangling:
            logger.warning(
                "WorkerPool.stop(): %d worker thread(s) did not stop within "
                "%.1fs and were left running (they are daemon threads, so "
                "process exit is not blocked): %s",
                len(dangling),
                timeout or 0.0,
                ", ".join(dangling),
            )

    def __enter__(self) -> "WorkerPool":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _lane_worker_slots(self) -> list[tuple[str | None, int]]:
        """Return `(lane, worker_count)` pairs, one per lane to spawn.

        `lane=None` tells a worker to call `run_once(lane=None)`, i.e. dequeue
        the queue's implicit single lane -- used both when there is no lane
        configuration at all and when the configuration is single-lane (see
        the class docstring for why the latter also uses `lane=None`).
        """

        if self._lane_config is None:
            return [(None, 1)]
        if self._lane_config.is_single_lane:
            sole_lane = self._lane_config.lane_names[0]
            return [(None, self._lane_config.concurrency[sole_lane])]
        return [
            (lane, self._lane_config.concurrency[lane])
            for lane in self._lane_config.lane_names
        ]

    def _run_worker(self, lane: str | None, worker_index: int) -> None:
        stop_event = self._stop_event
        # `start()` always creates `_stop_event` before spawning any thread
        # that could reach this method, so this is a real invariant rather
        # than an optional check.
        assert stop_event is not None
        lane_label = lane if lane is not None else "default"

        while not stop_event.is_set():
            try:
                processed = self._job_runner.run_once(lane=lane)
            except Exception:
                # A failure here is something `JobRunner.process_job` itself
                # could not already turn into a `mark_failed` job (that path
                # is inside its own try/except) -- e.g. a repository error on
                # dequeue. Logging and continuing keeps this worker (and thus
                # its lane) alive instead of leaving jobs stranded behind a
                # dead consumer; other lanes' workers are unaffected because
                # each runs on its own thread with no shared loop state.
                logger.exception(
                    "Worker %d for lane %r failed while processing a job; "
                    "continuing to poll lane %r rather than exiting.",
                    worker_index,
                    lane_label,
                    lane_label,
                )
                stop_event.wait(self._error_backoff_seconds)
                continue

            if processed is None:
                stop_event.wait(self._poll_interval_seconds)


__all__ = ["WorkerPool"]
