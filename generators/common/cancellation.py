"""Shared helper for probing cancellation without poisoning a healthy runtime.

PR4b P2-A: `GenerationContext.is_cancelled()` calls the `is_cancelled`
callable `JobRunner._begin_context()` wired in -- in production this reads
`JobRepository.get(job_id)` (see `JobRunner._is_cancelled()`), real I/O that
can raise (a transient DB/connection failure, for example). A pre-use probe
call made from *inside* an active `with model_service.acquire_runtime(...)`
block, before any runtime mutation/inference, is external bookkeeping, not a
runtime fault -- if it raises there, letting that exception propagate
through the `with` block would make `RuntimeHandle.__exit__()` mark a
perfectly healthy, never-touched runtime `INVALID` (see that method's own
docstring: it invalidates unconditionally whenever `exc_type is not None`,
with no way to distinguish the cause).

`safe_is_cancelled()` isolates exactly that one call so a caller can decide
what to do with the failure *after* its lease has already exited cleanly,
without ever broad-catching whatever comes after it (runtime mutation,
provider/inference calls, ...), which must keep failing loudly and
conservatively invalidating as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.jobs.context import GenerationContext


def safe_is_cancelled(
    context: "GenerationContext | None",
) -> tuple[bool, Exception | None]:
    """Probe `context.is_cancelled()`, isolating a bookkeeping failure.

    Returns `(cancelled, probe_error)`. Exactly one call is made to
    `context.is_cancelled()` (none at all if `context` is `None`, in which
    case `(False, None)` is returned): if it raises, that exception is
    captured and returned as `probe_error` with `cancelled` left `False`;
    otherwise its bool result is returned as `cancelled` with `probe_error`
    `None`. Never raises itself. Callers must still skip runtime
    mutation/inference on `cancelled or probe_error is not None`, let the
    active lease exit normally, and only then act on whichever of the two
    is set -- raising `GenerationCancelled` for `cancelled`, or re-raising
    `probe_error` verbatim so the original external exception is what a
    caller ultimately observes.
    """

    if context is None:
        return False, None
    try:
        return context.is_cancelled(), None
    except Exception as exc:  # noqa: BLE001 -- deliberately narrow to this one call
        return False, exc


__all__ = ["safe_is_cancelled"]
