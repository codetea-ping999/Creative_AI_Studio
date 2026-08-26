"""Job lane configuration and lane assignment rules.

A single FIFO `JobQueue` (see `core/jobs/queue.py`) lets a long run of heavy
jobs (30 image generations, a video render) starve a quick job (a story text
edit, a TTS line) queued behind it. Lanes let those job kinds be routed to
independent queues so light work does not wait behind heavy work.

This module defines the configuration *contract* only: parsing `JOB_LANES`,
and the media/task_type -> lane mapping. It intentionally does not start
worker threads or touch `JobQueue`/`JobRunner` dispatch — that is #180
(routing) and #181 (independent lane workers). See
`docs/multimedia-content-generation-plan.md` §9 for the design this
implements.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

# Canonical lane names used by the default configuration and by
# `resolve_lane`'s media/task_type mapping below. `JOB_LANES` itself accepts
# arbitrary lane names (see `parse_job_lanes`) so an operator can rename or
# add lanes without a code change; these two are just what ships by default.
LANE_HEAVY = "heavy"
LANE_LIGHT = "light"

# Default `JOB_LANES` value: one heavy-lane worker, one light-lane worker.
# This is the value documented in the parent epic (#39) and in
# docs/multimedia-content-generation-plan.md §9.
DEFAULT_JOB_LANES = "heavy:1,light:1"

# (media_type, task_type) pairs that are the *exception* within their
# media type: cheap/CPU-bound work that must not queue behind the heavy
# lane even though its media_type otherwise routes there. Both are
# CPU-bound today (imageio/PIL assembly, template or small-model TTS) and
# are meant to run alongside the next heavy image/video/music job, not
# wait for it.
_LIGHT_TASK_TYPE_OVERRIDES: frozenset[tuple[str, str]] = frozenset(
    {
        ("audio", "text-to-speech"),
        ("video", "assembly"),
    }
)

# Default lane per media_type, used whenever `task_type` is None or is not
# one of the overrides above. This is what "unknown task types" fall back
# to: a new/unrecognized task_type under a heavy media_type (e.g. a future
# audio task type nobody has taught this module about) stays on that media
# type's lane rather than being silently fast-tracked into `light`.
_MEDIA_TYPE_LANES: dict[str, str] = {
    "image": LANE_HEAVY,
    "video": LANE_HEAVY,
    "audio": LANE_HEAVY,
    "text": LANE_LIGHT,
}


@dataclass(frozen=True, slots=True)
class LaneConfig:
    """Parsed `JOB_LANES` configuration: lane name -> worker concurrency."""

    concurrency: dict[str, int]

    @property
    def lane_names(self) -> tuple[str, ...]:
        """Configured lane names, in the order `JOB_LANES` declared them."""

        return tuple(self.concurrency)

    @property
    def is_single_lane(self) -> bool:
        """True when this configuration defines exactly one lane.

        A single-lane `JOB_LANES` (e.g. `heavy:1` or `default:3`) is the
        backward-compatibility mode: every job collapses onto that one lane
        regardless of `resolve_lane`, reproducing the pre-lane single-queue
        behavior exactly. See `assign_lane`.
        """

        return len(self.concurrency) == 1


def parse_job_lanes(raw_value: str | None = None) -> LaneConfig:
    """Parse a `JOB_LANES` string such as ``"heavy:1,light:1"``.

    Reads `os.environ["JOB_LANES"]` (defaulting to `DEFAULT_JOB_LANES`) when
    `raw_value` is not given. Every entry must be ``name:concurrency`` with a
    positive integer concurrency; lane names must be non-empty and unique.

    Raises `ValueError` naming the offending entry and the full configured
    value on any malformed input — an operator's `JOB_LANES` typo must fail
    loudly rather than silently falling back to a default that hides it.
    """

    if raw_value is None:
        raw_value = os.getenv("JOB_LANES", DEFAULT_JOB_LANES)

    normalized = raw_value.strip()
    if not normalized:
        raise ValueError(
            f"JOB_LANES must not be empty; use a lane list such as {DEFAULT_JOB_LANES!r}."
        )

    concurrency: dict[str, int] = {}
    for segment in normalized.split(","):
        entry = segment.strip()
        if not entry:
            raise ValueError(
                f"JOB_LANES={raw_value!r} contains an empty lane entry; expected "
                f"comma-separated name:concurrency pairs such as {DEFAULT_JOB_LANES!r}."
            )
        if ":" not in entry:
            raise ValueError(
                f"JOB_LANES entry {entry!r} is missing ':concurrency' (from "
                f"JOB_LANES={raw_value!r}); expected a form like 'heavy:1'."
            )

        name, _, count_text = entry.partition(":")
        name = name.strip()
        count_text = count_text.strip()

        if not name:
            raise ValueError(
                f"JOB_LANES entry {entry!r} has an empty lane name (from "
                f"JOB_LANES={raw_value!r})."
            )
        if name in concurrency:
            raise ValueError(
                f"JOB_LANES defines lane {name!r} more than once (from "
                f"JOB_LANES={raw_value!r})."
            )
        try:
            count = int(count_text)
        except ValueError as exc:
            raise ValueError(
                f"JOB_LANES lane {name!r} has a non-integer concurrency {count_text!r} "
                f"(from JOB_LANES={raw_value!r})."
            ) from exc
        if count < 1:
            raise ValueError(
                f"JOB_LANES lane {name!r} concurrency must be >= 1; got {count} "
                f"(from JOB_LANES={raw_value!r})."
            )

        concurrency[name] = count

    return LaneConfig(concurrency=concurrency)


def resolve_lane(media_type: str, task_type: str | None = None) -> str:
    """Return the canonical lane (`LANE_HEAVY`/`LANE_LIGHT`) for a job kind.

    Routing is keyed on `(media_type, task_type)`, not `media_type` alone:
    image/video/music route to `heavy`; text/TTS/assembly route to `light`.
    Concretely, `audio` and `video` are `heavy` by default but drop to
    `light` for their known CPU-bound task types (`text-to-speech`,
    `assembly`); an unrecognized or absent `task_type` uses the media type's
    default lane instead of guessing.

    Raises `ValueError` for a `media_type` this module has no mapping for at
    all (as opposed to an unrecognized `task_type`, which is expected and
    falls back by design).
    """

    if task_type is not None and (media_type, task_type) in _LIGHT_TASK_TYPE_OVERRIDES:
        return LANE_LIGHT

    try:
        return _MEDIA_TYPE_LANES[media_type]
    except KeyError:
        known = sorted(_MEDIA_TYPE_LANES)
        raise ValueError(
            f"No lane mapping for media_type={media_type!r}; known media types are {known}."
        ) from None


def assign_lane(media_type: str, task_type: str | None, lanes: LaneConfig) -> str:
    """Return the lane name a job should be enqueued to under `lanes`.

    When `lanes.is_single_lane` is true, every job collapses onto that one
    lane regardless of `resolve_lane` — this is the single-lane
    compatibility mode required by #39, and it reproduces the pre-lane
    single-queue behavior exactly for any one-entry `JOB_LANES` value
    (`heavy:1` alone, `light:1` alone, or a custom name like `default:1`).

    Otherwise the job routes to `resolve_lane(media_type, task_type)`, and
    that lane must be one `lanes` actually defines — a `JOB_LANES` that
    drops the `light` (or `heavy`) lane while still receiving that kind of
    job is a configuration error, not something to silently reroute.
    """

    if lanes.is_single_lane:
        return lanes.lane_names[0]

    lane = resolve_lane(media_type, task_type)
    if lane not in lanes.concurrency:
        configured = sorted(lanes.concurrency)
        raise ValueError(
            f"Lane {lane!r} (resolved for media_type={media_type!r}, "
            f"task_type={task_type!r}) is not defined in JOB_LANES; configured lanes "
            f"are {configured}."
        )
    return lane


__all__ = [
    "DEFAULT_JOB_LANES",
    "LANE_HEAVY",
    "LANE_LIGHT",
    "LaneConfig",
    "assign_lane",
    "parse_job_lanes",
    "resolve_lane",
]
