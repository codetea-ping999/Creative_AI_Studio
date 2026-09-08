"""PR3: deterministic regressions for Batch child-id stability, cancellation
intent, lock-safe mutation, and terminal-child reconciliation.

No sleep-based waits: concurrency scenarios use `threading.Barrier`/`Event`,
matching `tests/test_story_concurrency.py`'s own established pattern for
`StoryRepository.mutate()`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
from threading import Barrier, Event, Thread

import pytest

from core.batches import (
    BatchReconciliationOutcome,
    BatchRepository,
    BatchService,
    BatchStageMaterializationError,
)
from core.batches.schemas import Axis, AxisValue, BatchSpec
from core.jobs import EventBus, JobQueue, JobRunner, JobService
from core.jobs.schemas import JobRecord
from core.reference_capabilities import UnsupportedReferenceError
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRepository
from generators.base import BaseGenerator
from generators.registry import GeneratorRegistry


class _CountingGenerator(BaseGenerator):
    def __init__(self):
        self.calls = 0

    def validate_request(self, request):
        pass

    def prepare(self, request):
        pass

    def cleanup(self, request):
        pass

    def generate(self, request, context=None):
        self.calls += 1
        return GenerationResult(job_id="fake", status="succeeded", outputs=["a.png"])


def _build(tmp_path):
    job_repository = JobRepository(tmp_path / "jobs.db")
    job_queue = JobQueue()
    event_bus = EventBus()
    job_service = JobService(job_repository, job_queue, event_bus)
    batch_repository = BatchRepository(tmp_path / "batches")
    batch_service = BatchService(batch_repository, job_service, job_repository, event_bus=event_bus)
    return job_repository, job_queue, job_service, batch_repository, batch_service


def _drain_queue(job_runner) -> None:
    while job_runner.run_once() is not None:
        pass


def _spec(**overrides):
    fields = dict(name="sweep", media_type="image", model_id="fake", prompt="x", limit=1)
    fields.update(overrides)
    return BatchSpec(**fields)


# --- Case 15/16: stable child id persisted before the Job row exists ------


def test_child_job_id_is_persisted_even_when_job_creation_then_fails(tmp_path, monkeypatch):
    _job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: job row creation never completes")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)

    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())

    records = batch_repository.list_all()
    assert len(records) == 1
    persisted_job_id = records[0].items[0].job_id
    # The id was durably assigned even though every attempt to actually
    # create the job row failed -- "crash after id persist, before job
    # created" is resumable because the id already exists on disk.
    assert persisted_job_id is not None
    assert job_service.job_repository.get(persisted_job_id) is None


def test_crash_after_id_persist_resumes_with_the_same_id(tmp_path, monkeypatch):
    job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: job row creation never completes")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    batch_id = batch_repository.list_all()[0].id
    persisted_job_id = batch_repository.get(batch_id).items[0].job_id

    monkeypatch.undo()  # restore the real create_or_reuse_job_without_enqueue
    resumed = batch_service._enqueue_stage(batch_id, stage_index=0)

    assert resumed.items[0].job_id == persisted_job_id
    assert job_repository.get(persisted_job_id) is not None
    assert job_repository.get(persisted_job_id).status == "queued"


def test_crash_after_job_created_before_enqueue_reuses_the_same_job(tmp_path, monkeypatch):
    """"Job row created, process crash, not enqueued" -- create_or_reuse_job's
    reuse branch must not create a second job; the general startup
    "re-enqueue every queued job" step (not batch-specific) is what puts an
    already-created-but-never-enqueued row back in the in-memory queue."""

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(_spec())
    job_id = batch.items[0].job_id
    job_queue.dequeue()  # simulate "never enqueued" by discarding it

    # Re-running the exact same enqueue step (as recovery would) must reuse
    # the existing row, not create a duplicate.
    resumed = batch_service._enqueue_stage(batch.id, stage_index=0)

    assert resumed.items[0].job_id == job_id
    matching_jobs = [job for job in job_repository.list() if job.id == job_id]
    assert len(matching_jobs) == 1


# --- Case 18: duplicate reconciliation never duplicates a child job -------


def test_duplicate_reconciliation_creates_no_duplicate_child_job(tmp_path):
    job_repository, _job_queue, _job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(_spec())
    job_id = batch.items[0].job_id

    for _ in range(5):
        batch_service._enqueue_stage(batch.id, stage_index=0)
        batch_service.reconcile_child_job(job_id)

    matching_jobs = [job for job in job_repository.list() if job.id == job_id]
    assert len(matching_jobs) == 1
    assert len(batch_repository.get(batch.id).items) == 1


# --- Case 19/20: cancellation intent persisted before child cancel --------


def test_cancellation_intent_is_persisted_before_any_child_is_cancelled(tmp_path, monkeypatch):
    job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(_spec(limit=2, axes=[]))
    # Force two items so there is a child to observe being cancelled after
    # the intent lands.
    batch = batch_service.create_batch(_spec())
    job_id = batch.items[0].job_id

    order = []
    original_cancel_job = job_service.cancel_job

    def spy_cancel_job(target_job_id):
        # By the time any child is actually told to cancel, the durable
        # intent must already be on disk.
        assert batch_repository.get(batch.id).cancellation_requested is True
        order.append("cancel_job")
        return original_cancel_job(target_job_id)

    monkeypatch.setattr(job_service, "cancel_job", spy_cancel_job)

    batch_service.cancel(batch.id)

    assert order == ["cancel_job"]
    assert job_repository.get(job_id).status in ("cancel_requested", "cancelled")


def test_cancel_intent_after_restart_suppresses_the_next_stage(tmp_path):
    job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(
        _spec(stages=[{"name": "probe"}, {"name": "refine"}])
    )
    job_id = batch.items[0].job_id

    batch_service.cancel(batch.id)
    # Simulate the job actually finishing (e.g. it was already postprocessing
    # when cancel() ran) so _try_advance_in_place's "all current items
    # terminal" precondition would otherwise be satisfied.
    job_repository.update_status(job_id, "cancelled")

    # "Restart": a fresh BatchService instance over the same on-disk state,
    # re-running reconciliation/advance exactly as startup recovery would.
    _jr, _jq, job_service2, _br, batch_service2 = _build(tmp_path)
    batch_service2.batch_repository = batch_repository  # same on-disk records
    advanced = batch_service2.advance(batch.id)

    assert advanced.cancellation_requested is True
    assert advanced.stage_index == 0  # never advanced to "refine"
    assert not any(item.stage_index == 1 for item in advanced.items)


# --- Case 21: lock acquisition -> fresh read -> no stale overwrite --------


def test_concurrent_batch_writers_do_not_lose_either_change(tmp_path):
    _job_repository, _job_queue, _job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(_spec())
    other_batch = batch_service.create_batch(_spec())

    barrier = Barrier(2)
    errors: list[BaseException] = []

    def _promote():
        try:
            barrier.wait(timeout=5)
            batch_service.promote(batch.id, batch.items[0].id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _cancel_other():
        try:
            barrier.wait(timeout=5)
            batch_service.cancel(other_batch.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_promote), executor.submit(_cancel_other)]
        for future in futures:
            future.result(timeout=5)

    assert errors == []
    assert batch_repository.get(batch.id).items[0].promoted is True
    assert batch_repository.get(other_batch.id).cancellation_requested is True


def test_concurrent_same_batch_writers_do_not_clobber_each_other(tmp_path):
    """Two concurrent mutations against the *same* batch: a fresh-read-under-
    lock design must serialize them so both are reflected, never one
    silently overwriting the other (the exact-HEAD audit's finding).

    Confirmed red against a lock-free `mutate()` by forcing both threads'
    reads to interleave via a barrier inserted into `BatchRepository.get()`
    before either saved: exactly one promotion survived, the other was
    silently lost. That same forced interleaving cannot be kept once the
    real lock is restored -- the two `get()` calls can no longer ever be
    concurrent by construction, so forcing them to wait on each other would
    deadlock instead of proving anything -- so the permanent version below
    relies on the lock itself to correctly serialize two genuinely
    concurrent callers, which is the actual contract being protected.
    """

    _job_repository, _job_queue, _job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(
        _spec(
            limit=2,
            axes=[
                {
                    "name": "variant",
                    "values": [{"label": "a"}, {"label": "b"}],
                }
            ],
        )
    )
    assert len(batch.items) == 2
    first_item_id = batch.items[0].id
    second_item_id = batch.items[1].id

    barrier = Barrier(2)
    errors: list[BaseException] = []

    def _promote(item_id):
        def _call():
            try:
                barrier.wait(timeout=5)
                batch_service.promote(batch.id, item_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        return _call

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_promote(first_item_id)),
            executor.submit(_promote(second_item_id)),
        ]
        for future in futures:
            future.result(timeout=5)

    assert errors == []
    refreshed = batch_repository.get(batch.id)
    promoted_ids = {item.id for item in refreshed.items if item.promoted}
    assert promoted_ids == {first_item_id, second_item_id}


# --- Case 22: terminal child with a lost event still advances -------------


# --- PR3 exact-HEAD audit P1-3: cancel converges an id with no Job row ----


def test_cancel_terminalizes_a_stable_child_id_whose_job_row_was_never_created(
    tmp_path, monkeypatch
):
    job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: job row creation never completes")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    persisted_job_id = batch_repository.get(batch_id).items[0].job_id
    assert persisted_job_id is not None
    assert job_repository.get(persisted_job_id) is None  # row never created

    refreshed = batch_service.cancel(batch_id)

    assert refreshed.items[0].job_id == persisted_job_id
    assert refreshed.items[0].status == "cancelled"
    assert refreshed.status == "cancelled"

    # Repeated cancel/startup recovery must stay terminal, never regress
    # back to "pending forever".
    refreshed_again = batch_service.cancel(batch_id)
    assert refreshed_again.items[0].status == "cancelled"
    assert refreshed_again.status == "cancelled"


# --- PR3 exact-HEAD audit, seventh round, finding 3: preserve terminal
# items when their Job row is missing -----------------------------------------


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "cancelled"])
def test_cancel_never_rewrites_a_terminal_items_outcome_when_its_job_row_is_missing(
    tmp_path, terminal_status
):
    """A Batch item that already reached a terminal outcome, and still
    holds the exact stable `job_id` it was assigned, must never have that
    real outcome overwritten to "cancelled" just because its Job row is
    later missing from SQLite (a DB restore, a manual delete, ...) while
    the Batch JSON survives.

    The branch this guards was added for a *nonterminal* item ("stable id
    persisted, Job row never created" -- see the previous test) and is
    unconditional on the item's own status: before this fix, calling
    `cancel()` on a batch with an already-`succeeded`/`failed` item whose
    row happened to be missing would silently destroy that real,
    already-concluded result, which the surviving Batch JSON is the only
    remaining record of (PR3 exact-HEAD audit, seventh round, finding 3).
    Cancelling already-terminal work must always be a no-op.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    batch = batch_service.create_batch(_spec())
    job_id = batch.items[0].job_id
    job_queue.dequeue()  # drain the original create_batch() enqueue

    def _mark_terminal(record):
        record.items[0].status = terminal_status
        return record

    batch_repository.mutate(batch.id, _mark_terminal)

    with sqlite3.connect(tmp_path / "jobs.db") as raw:
        raw.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        raw.commit()
    assert job_repository.get(job_id) is None

    refreshed = batch_service.cancel(batch.id)

    assert refreshed.items[0].job_id == job_id  # stable id preserved as-is
    assert refreshed.items[0].status == terminal_status  # never rewritten
    assert refreshed.status == terminal_status  # aggregate follows suit
    assert job_repository.get(job_id) is None  # never recreated
    assert job_queue.dequeue() is None  # never enqueued

    _drain_queue(job_runner)
    assert generator.calls == 0

    # Idempotent on repeat.
    refreshed_again = batch_service.cancel(batch.id)
    assert refreshed_again.items[0].status == terminal_status
    assert refreshed_again.status == terminal_status


# --- PR3 exact-HEAD audit P1-2: cancellation is rechecked per child -------


def test_enqueue_stage_stops_materializing_children_once_cancellation_lands(
    tmp_path, monkeypatch
):
    """Deterministic race: a `cancel()` landing *during* `_enqueue_stage()`'s
    per-item creation loop (after its own persisted-record check let the
    loop start) must stop the loop from materializing any further child --
    not just fail to cancel the ones already created.
    """

    job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before any child row is created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(
            _spec(
                limit=2,
                axes=[{"name": "variant", "values": [{"label": "a"}, {"label": "b"}]}],
            )
        )
    monkeypatch.undo()

    batch = batch_repository.list_all()[0]
    assert len(batch.items) == 2
    first_id, second_id = batch.items[0].job_id, batch.items[1].job_id
    assert first_id is not None and second_id is not None
    assert job_repository.get(first_id) is None
    assert job_repository.get(second_id) is None

    original_create_or_reuse = job_service.create_or_reuse_job_without_enqueue
    calls: list[str] = []

    def racing_create_or_reuse(job_id, request, project_id=None):
        calls.append(job_id)
        result = original_create_or_reuse(job_id, request, project_id=project_id)
        # Simulate a concurrent cancel() landing in the exact window PR3
        # exact-HEAD audit finding P1-2 identifies: after the persisted-
        # record check that let this loop start, but before the *next*
        # item's own create_or_reuse_job_without_enqueue() call.
        batch_service.cancel(batch.id)
        return result

    monkeypatch.setattr(
        job_service, "create_or_reuse_job_without_enqueue", racing_create_or_reuse
    )

    batch_service._enqueue_stage(batch.id, stage_index=0)

    # Exactly one child was materialized -- the recheck must stop the loop
    # before the second item is ever created, not just skip cancelling it
    # afterward.
    assert calls == [first_id]
    assert job_repository.get(first_id) is not None
    assert job_repository.get(second_id) is None

    refreshed = batch_repository.get(batch.id)
    assert refreshed.cancellation_requested is True
    # The item that was never materialized converges to cancelled (P1-3),
    # closing the loop: nothing is left running for a cancelled batch, and
    # nothing is left permanently pending either.
    second_item = next(item for item in refreshed.items if item.job_id == second_id)
    assert second_item.status == "cancelled"


# --- PR3 exact-HEAD audit, second round, P1-1: keep new children off the
# queue until cancellation is rechecked ------------------------------------


def test_batch_never_lets_a_worker_claim_a_child_created_for_a_cancelled_batch(
    tmp_path, monkeypatch
):
    """Deterministic two-thread race (`threading.Event`, no sleep): durable
    cancellation lands exactly between a child's Job row being materialized
    and it ever being exposed to a worker via `enqueue_job()`. A real
    `JobRunner` proves the worker can never see -- let alone claim or run --
    that row; the old combined create-and-enqueue call could let a worker
    claim it before the post-create cancel signal ever landed.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    persisted_job_id = batch_repository.get(batch_id).items[0].job_id
    assert persisted_job_id is not None
    assert job_repository.get(persisted_job_id) is None

    original = job_service.create_or_reuse_job_without_enqueue
    row_materialized = Event()
    cancellation_landed = Event()

    def racing(job_id, request, project_id=None):
        result = original(job_id, request, project_id=project_id)
        # The row now exists in the database, but _enqueue_stage() has not
        # yet called enqueue_job() on it -- no worker can see it yet.
        row_materialized.set()
        assert cancellation_landed.wait(timeout=5)
        return result

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", racing)

    def _cancel_once_row_exists():
        assert row_materialized.wait(timeout=5)
        batch_service.cancel(batch_id)
        cancellation_landed.set()

    canceller = Thread(target=_cancel_once_row_exists)
    canceller.start()
    batch_service._enqueue_stage(batch_id, stage_index=0)
    canceller.join(timeout=5)

    # The row was created, but must never have been exposed to a worker.
    assert job_repository.get(persisted_job_id) is not None
    assert job_queue.dequeue() is None

    # Draining the (empty) queue with a real runner proves no generation
    # ever happened -- not merely that cancel_job() was eventually called.
    _drain_queue(job_runner)
    assert generator.calls == 0

    refreshed = batch_repository.get(batch_id)
    assert refreshed.cancellation_requested is True
    assert refreshed.items[0].status in ("cancel_requested", "cancelled")


def test_terminal_child_with_no_event_still_reconciles_via_direct_call(tmp_path):
    job_repository, _job_queue, _job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(_spec())
    job_id = batch.items[0].job_id

    # Finish the job without ever publishing a job_succeeded event -- the
    # scenario a lost/never-subscribed event represents.
    job_repository.update_status(job_id, "preparing")
    job_repository.update_status(job_id, "running")
    job_repository.update_status(job_id, "postprocessing")
    job_repository.update(
        job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=job_id, status="succeeded", outputs=["a.png"]),
    )

    assert batch_repository.get(batch.id).status == "running"  # stale, no event ever fired

    refreshed, outcome = batch_service.reconcile_child_job(job_id)

    assert outcome == BatchReconciliationOutcome.RECONCILED
    assert refreshed.status == "succeeded"
    assert refreshed.aggregate.succeeded == 1


# --- PR3 exact-HEAD audit, second round, P1-3: skip terminal items when
# resuming legacy batches ---------------------------------------------------


def test_startup_resume_never_reassigns_an_id_to_a_legacy_cancelled_item(tmp_path):
    """A pre-PR3 ("legacy") batch record has no `cancellation_requested`
    field on disk -- pydantic defaults it to `False` on load, exactly like a
    batch that was never cancelled. The old `cancel()` implementation
    persisted a cancelled item as `job_id=None`, `status="cancelled"`
    directly, with nothing else recording that intent. Resuming such a
    batch must never mint a fresh id (and materialize/enqueue a new Job)
    for that already-terminalized item.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(_spec())
    job_queue.dequeue()  # drain the original create_batch() enqueue

    def _simulate_legacy_cancelled_item(record):
        # This is exactly the on-disk shape a pre-PR3 cancel() left behind:
        # no durable batch-level intent, just the item's own status set
        # directly, with its job_id cleared.
        record.cancellation_requested = False
        record.items[0].job_id = None
        record.items[0].status = "cancelled"
        return record

    legacy = batch_repository.mutate(batch.id, _simulate_legacy_cancelled_item)
    assert legacy.cancellation_requested is False
    assert legacy.items[0].job_id is None
    assert legacy.items[0].status == "cancelled"
    rows_before = len(job_repository.list())

    for _ in range(3):
        batch_service.resume_current_stage_for_all_batches()

    resumed = batch_repository.get(batch.id)
    assert resumed.items[0].job_id is None
    assert resumed.items[0].status == "cancelled"
    assert len(job_repository.list()) == rows_before  # no new row was ever created
    assert job_queue.dequeue() is None  # nothing new was ever enqueued


# --- PR3 exact-HEAD audit, fifth round, finding 2: skip terminal items
# before recreating missing job rows ------------------------------------------


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "cancelled"])
def test_resume_never_recreates_a_missing_job_row_for_a_terminal_item(
    tmp_path, terminal_status
):
    """A Batch item that already reached a terminal outcome, and still
    holds the exact stable `job_id` it was assigned, must never have its
    Job row re-materialized just because that row is later missing from
    SQLite (a DB restore, a manual delete, ...) while the Batch JSON
    survives.

    Before this fix, `_enqueue_stage()`'s phase-two loop only skipped an
    item lacking a `job_id` entirely (the pre-PR3 legacy-cancelled case);
    an item that already has a stable id but whose row is now missing
    still entered `create_or_reuse_job_without_enqueue()`, which happily
    creates a brand-new `queued` row under that same id and goes on to
    authorize and enqueue it -- silently resurrecting and rerunning a
    generation that had already concluded (PR3 exact-HEAD audit, fifth
    round, finding 2).
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    batch = batch_service.create_batch(_spec())
    job_id = batch.items[0].job_id
    job_queue.dequeue()  # drain the original create_batch() enqueue

    def _mark_terminal(record):
        record.items[0].status = terminal_status
        return record

    batch_repository.mutate(batch.id, _mark_terminal)

    with sqlite3.connect(tmp_path / "jobs.db") as raw:
        raw.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        raw.commit()
    assert job_repository.get(job_id) is None

    for _ in range(3):
        batch_service.resume_current_stage_for_all_batches()

    resumed = batch_repository.get(batch.id)
    assert resumed.items[0].job_id == job_id  # stable id preserved as-is
    assert resumed.items[0].status == terminal_status
    assert job_repository.get(job_id) is None  # never recreated
    assert job_queue.dequeue() is None  # never enqueued

    _drain_queue(job_runner)
    assert generator.calls == 0


# --- PR3 exact-HEAD audit, third round, P1-1: re-enqueue a reused queued
# child during runtime recovery ----------------------------------------------


def test_runtime_retry_safely_reenqueues_a_reused_queued_child(tmp_path, monkeypatch):
    """A reused row that is still genuinely `queued` (because an earlier
    `enqueue_job()` call raised *after* materialization) must be safely
    (re-)enqueued on a later `_enqueue_stage()` call -- not silently
    skipped just because this call did not create the row itself. This is
    a *runtime* retry, not a restart: the same `BatchService`/`JobQueue`
    instances, exactly as `reconcile_child_job()`'s own completion-retry
    path would re-drive `_enqueue_stage()`.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    original_enqueue_job = job_service.enqueue_job
    calls = {"count": 0}

    def flaky_enqueue_job(job_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("injected: enqueue_job fails after materialization")
        return original_enqueue_job(job_id)

    monkeypatch.setattr(job_service, "enqueue_job", flaky_enqueue_job)

    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())

    batch_id = batch_repository.list_all()[0].id
    job_id = batch_repository.get(batch_id).items[0].job_id
    assert job_id is not None
    materialized = job_repository.get(job_id)
    assert materialized is not None
    assert materialized.status == "queued"
    assert job_queue.dequeue() is None  # never actually reached the queue

    resumed = batch_service._enqueue_stage(batch_id, stage_index=0)

    assert resumed is not None

    matching_rows = [job for job in job_repository.list() if job.id == job_id]
    assert len(matching_rows) == 1  # same stable id, never duplicated

    # Draining with a real runner is itself the proof the job reached the
    # queue this time (a dequeue-then-check would consume the only copy
    # before the runner ever saw it).
    _drain_queue(job_runner)
    assert generator.calls == 1  # exactly one execution, no duplicate delivery
    assert job_repository.get(job_id).status == "succeeded"


# --- PR3 exact-HEAD audit, third round, P1-2: cancellation check and queue
# exposure are atomic ---------------------------------------------------------


def test_cancellation_and_enqueue_exposure_are_mutually_exclusive(tmp_path, monkeypatch):
    """Deterministic proof (via `threading.Event`, no sleep) that the
    authorize-and-expose step and `cancel()`'s own durable-intent mutation
    cannot interleave: force the canceller thread to attempt its mutation
    *while* the main thread is inside the atomic authorize-and-expose
    critical section, and confirm it genuinely could not have completed
    before that section finishes and releases the lock.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    job_id = batch_repository.get(batch_id).items[0].job_id
    assert job_repository.get(job_id) is None

    inside_critical_section = Event()
    cancel_attempted = Event()
    observed_cancellation_state = []
    original_enqueue_job = job_service.enqueue_job

    def paused_enqueue_job(job_id_arg):
        inside_critical_section.set()
        # Give the canceller thread every opportunity to interleave -- if
        # the exclusion boundary were not real (the pre-fix code), this is
        # exactly the window its own race let cancel() slip through.
        cancel_attempted.wait(timeout=0.5)
        current = batch_repository.get(batch_id)
        observed_cancellation_state.append(
            current.cancellation_requested if current is not None else None
        )
        return original_enqueue_job(job_id_arg)

    monkeypatch.setattr(job_service, "enqueue_job", paused_enqueue_job)

    def _attempt_cancel():
        assert inside_critical_section.wait(timeout=5)
        batch_service.cancel(batch_id)
        cancel_attempted.set()

    canceller = Thread(target=_attempt_cancel)
    canceller.start()
    batch_service._enqueue_stage(batch_id, stage_index=0)
    canceller.join(timeout=5)

    # The read taken from inside the same critical section as the enqueue
    # itself must never observe a cancellation that landed *during* that
    # section -- proving the two operations are genuinely mutually
    # exclusive, not just separately (and non-atomically) rechecked.
    assert observed_cancellation_state == [False]
    # This exact job legitimately started before any cancellation intent
    # existed -- expected and correct; cancel()'s own cooperative
    # cancel_job() loop (which ran right after, once unblocked by our lock
    # release) is what handles it from here.
    assert job_queue.dequeue() == job_id


# --- PR3 exact-HEAD audit, third round, P1-3: treat an unreadable
# cancellation recheck as unsafe ---------------------------------------------


def test_unreadable_authorize_recheck_never_enqueues_the_child(tmp_path, monkeypatch):
    """Materialization succeeds, but the atomic authorize-and-expose step's
    own diagnosed read hits a transient failure -- must be treated the
    same as "possibly cancelled", never as "confirmed not cancelled".
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    job_id = batch_repository.get(batch_id).items[0].job_id
    assert job_repository.get(job_id) is None

    # Materialization itself uses the plain (non-diagnosed) `_try_load()`
    # path (via `mutate()` -> `get()`); only the authorize step's own
    # `get_or_diagnose()` -> `_try_load_diagnosed()` read is injected here,
    # so the per-item loop genuinely reaches the authorize step this time.
    original_try_load_diagnosed = batch_repository._try_load_diagnosed

    def flaky_try_load_diagnosed(batch_file):
        if batch_file.stem == batch_id:
            return None, True
        return original_try_load_diagnosed(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load_diagnosed", flaky_try_load_diagnosed)

    batch_service._enqueue_stage(batch_id, stage_index=0)

    materialized = job_repository.get(job_id)
    assert materialized is not None  # materialization itself succeeded
    assert materialized.status == "queued"
    assert job_queue.dequeue() is None  # never exposed -- uncertainty is not "not cancelled"

    _drain_queue(job_runner)
    assert generator.calls == 0

    monkeypatch.undo()  # storage "recovers"

    batch_service._enqueue_stage(batch_id, stage_index=0)

    assert job_queue.dequeue() == job_id  # now safely (re-)enqueued


# --- PR3 exact-HEAD audit, fifth round, adversarial self-review: a
# malformed (not merely transiently unreadable) batch file must be just as
# "uncertain" to `get_or_diagnose()`, never "confirmed absent" -----------------


def test_malformed_authorize_recheck_never_cancels_or_enqueues_the_child(
    tmp_path, monkeypatch
):
    """`get_or_diagnose()` is the single-file lookup `_authorize_and_
    expose()` uses to decide whether a materialized-but-not-yet-exposed
    child may become worker-visible. Before this fix, a batch file that
    is genuinely malformed (not merely hit by a transient `OSError`) was
    reported identically to a *confirmed-deleted* batch -- both as
    `(None, False)` -- so a live batch whose file happened to be
    malformed right now would have this child's row *cancelled*
    (`decision == "absent"`), not merely left unexposed for a later
    retry, exactly as if the batch had genuinely been deleted (PR3
    exact-HEAD audit, fifth round, adversarial self-review).
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    job_id = batch_repository.get(batch_id).items[0].job_id
    assert job_repository.get(job_id) is None

    # Materialization itself uses the plain (non-diagnosed) `_try_load()`
    # path (via `mutate()` -> `get()`); only the authorize step's own
    # `get_or_diagnose()` -> `_try_load_diagnosed()` read is injected
    # here, simulating deterministically malformed content (not an
    # `OSError`) -- `transient_failure=False`, matching exactly what
    # `_try_load_diagnosed()` itself returns for bad JSON/schema content.
    original_try_load_diagnosed = batch_repository._try_load_diagnosed

    def flaky_malformed(batch_file):
        if batch_file.stem == batch_id:
            return None, False
        return original_try_load_diagnosed(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load_diagnosed", flaky_malformed)

    batch_service._enqueue_stage(batch_id, stage_index=0)

    materialized = job_repository.get(job_id)
    assert materialized is not None  # materialization itself succeeded
    # Must stay exactly as materialized -- never cancelled (the "absent"
    # bug) and never enqueued (uncertainty is not "not cancelled").
    assert materialized.status == "queued"
    assert job_queue.dequeue() is None

    _drain_queue(job_runner)
    assert generator.calls == 0

    monkeypatch.undo()  # the file is repaired

    batch_service._enqueue_stage(batch_id, stage_index=0)

    assert job_queue.dequeue() == job_id  # now safely (re-)enqueued


# --- PR3 exact-HEAD audit, eighth round, finding 2: distinguish stat
# failures from confirmed absence ---------------------------------------------


def test_get_or_diagnose_treats_a_stat_failure_as_uncertain_not_absent(
    tmp_path, monkeypatch
):
    """`get_or_diagnose()` now calls the batch file's own `.stat()`
    directly and must distinguish a confirmed `FileNotFoundError` from
    any other transient `OSError` (a permission hiccup, a mount
    timeout) -- the latter must be reported as uncertain, never as
    confirmed absence, or a live batch could be wrongly treated as
    deleted (PR3 exact-HEAD audit, eighth round, finding 2).

    Injects the failure by patching `pathlib.Path.stat` itself (the
    exact method `get_or_diagnose()` calls), rather than the lower-level
    `os.stat()` free function: `Path.exists()`/`Path.stat()`'s internal
    routing to `os.stat()` is a CPython-version-specific implementation
    detail (confirmed to differ between the local 3.14 environment and
    CI's Python 3.10 runtime -- an `os.stat()`-level patch does not
    reliably intercept every version's call path), while `Path.stat`
    itself is the stable, version-portable interception point that
    matches what the fixed production code actually calls.

    A direct unit test of `get_or_diagnose()` itself, not through
    `_enqueue_stage()`: that method's own earlier "cheap, lock-free
    pre-check" (`self.batch_repository.get(batch_id)`) also stats the
    exact same file, so a `Path.stat`-level injection broad enough to
    catch every batch file would trip that unrelated call too,
    confounding the test -- guarded against below by raising only for
    this exact target path and delegating every other path to the real
    `Path.stat`.
    """

    from pathlib import Path

    _job_repository, _job_queue, _job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    batch = batch_service.create_batch(_spec())

    target_file = batch_repository.batch_dir / f"{batch.id}.json"
    real_path_stat = Path.stat

    def flaky_path_stat(self, *args, **kwargs):
        if self == target_file:
            raise OSError("injected: transient stat failure")
        return real_path_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_path_stat)

    record, uncertain = batch_repository.get_or_diagnose(batch.id)

    assert record is None
    assert uncertain is True  # NOT confirmed absent -- must stay retryable

    monkeypatch.undo()  # the storage hiccup clears

    record_after, uncertain_after = batch_repository.get_or_diagnose(batch.id)
    assert record_after is not None
    assert record_after.id == batch.id
    assert uncertain_after is False


# --- PR3 exact-HEAD audit, third round, P1-6: propagate manual
# stage-enqueue failures -------------------------------------------------


def test_manual_advance_raises_when_stage_materialization_cannot_be_confirmed(
    tmp_path, monkeypatch
):
    job_repository, _job_queue, job_service, batch_repository, batch_service = _build(tmp_path)
    batch = batch_service.create_batch(
        _spec(stages=[{"name": "probe"}, {"name": "refine"}])
    )
    job_id = batch.items[0].job_id
    job_repository.update_status(job_id, "preparing")
    job_repository.update_status(job_id, "running")
    job_repository.update_status(job_id, "postprocessing")
    job_repository.update(
        job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=job_id, status="succeeded", outputs=["a.png"]),
    )

    # advance()'s own stage-advance mutation and _enqueue_stage()'s
    # materialization both read this batch via the same `_try_load()`
    # (via `get()`) path -- to let the *advance* persist and only the
    # *following* materialization fail, count calls and only start
    # failing from the second one for this exact batch.
    original_try_load = batch_repository._try_load
    call_count = {"n": 0}

    def flaky_try_load(batch_file):
        if batch_file.stem == batch.id:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return None
        return original_try_load(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load", flaky_try_load)

    with pytest.raises(BatchStageMaterializationError):
        batch_service.advance(batch.id)

    monkeypatch.undo()

    # The advance itself really did persist despite the injected failure.
    advanced_only = batch_repository.get(batch.id)
    assert advanced_only.stage_index == 1
    refine_item = next(item for item in advanced_only.items if item.stage_index == 1)
    assert refine_item.job_id is None  # never materialized while the read was flaky

    # Repair + retry converges to the same stable stage/child, no
    # duplicates.
    second = batch_service.advance(batch.id)

    assert second.stage_index == 1
    refine_item_after = next(item for item in second.items if item.stage_index == 1)
    assert refine_item_after.job_id is not None
    assert job_repository.get(refine_item_after.job_id) is not None
    assert len([item for item in second.items if item.stage_index == 1]) == 1


# --- PR3 exact-HEAD audit, third round, follow-up (found via adversarial
# review): cancel() survives a poisoned sibling child -----------------------


def test_cancel_survives_a_poisoned_sibling_child_and_still_cancels_the_others(
    tmp_path,
):
    """`cancel()`'s own child-row read (inside `_apply_cancellation_intent`)
    needs the same `JobRecordDecodeError` guard `_recompute()`/
    `_enqueue_stage()` already got this round -- otherwise one poisoned
    sibling anywhere in the batch aborts the *entire* `cancel()` call,
    including durably persisting `cancellation_requested` and cancelling
    every *other*, healthy item. Also reachable from
    `resume_pending_cancellations()` -> `run_startup_recovery()`, which
    would otherwise crash the whole application's startup on every future
    restart until the poisoned row is fixed by hand.
    """

    job_repository, _job_queue, _job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    batch = batch_service.create_batch(
        _spec(
            limit=2,
            axes=[{"name": "variant", "values": [{"label": "a"}, {"label": "b"}]}],
        )
    )
    assert len(batch.items) == 2
    healthy_job_id = batch.items[0].job_id
    poison_job_id = batch.items[1].job_id

    with sqlite3.connect(tmp_path / "jobs.db") as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison_job_id),
        )
        raw.commit()

    # This must not raise -- the whole point of the fix.
    refreshed = batch_service.cancel(batch.id)

    assert refreshed is not None
    assert refreshed.cancellation_requested is True
    # The healthy sibling is still cancelled despite the poisoned one.
    assert job_repository.get(healthy_job_id).status == "cancelled"
    healthy_item = next(item for item in refreshed.items if item.job_id == healthy_job_id)
    assert healthy_item.status in ("cancel_requested", "cancelled")

    # Repeated cancellation (as resume_pending_cancellations() would do on
    # every restart) keeps converging, never crashing again.
    refreshed_again = batch_service.cancel(batch.id)
    assert refreshed_again is not None
    assert refreshed_again.cancellation_requested is True


# --- PR3 exact-HEAD audit, fourth round, finding 3: isolate permanent
# materialization failures during startup ------------------------------------


def test_startup_isolates_a_permanent_unsupported_reference_failure_per_batch(
    tmp_path, monkeypatch
):
    """Case A: model/reference capabilities changed across a restart, so
    materializing a batch item's persisted-but-not-yet-created stable id
    now raises `UnsupportedReferenceError`. One such permanently-broken
    batch must not abort startup recovery for every other batch.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    broken_batch_id = batch_repository.list_all()[0].id
    broken_job_id = batch_repository.get(broken_batch_id).items[0].job_id
    assert job_repository.get(broken_job_id) is None

    healthy_batch = batch_service.create_batch(_spec())
    healthy_job_id = healthy_batch.items[0].job_id
    job_queue.dequeue()  # simulate a fresh restart's empty in-memory queue

    def always_unsupported(*args, **kwargs):
        raise UnsupportedReferenceError(
            "injected: capability no longer supported after restart"
        )

    monkeypatch.setattr(job_service, "validate_references", always_unsupported)

    # This must not raise -- the whole point of the fix. (The healthy
    # batch's own row already exists, so its create-or-reuse never calls
    # validate_references() at all -- only the broken batch's does.)
    resumed = batch_service.resume_current_stage_for_all_batches()

    assert job_repository.get(broken_job_id) is None  # never materialized
    broken_refreshed = batch_repository.get(broken_batch_id)
    assert broken_refreshed.status == "failed"
    assert broken_refreshed.advance_error is not None
    assert any(record.id == healthy_batch.id for record in resumed)

    monkeypatch.undo()
    _drain_queue(job_runner)
    assert generator.calls == 1  # only the healthy job ran
    assert job_repository.get(healthy_job_id).status == "succeeded"

    # Repeated startup does not crash again -- the same permanent failure
    # is re-observed and re-isolated, never escaping.
    monkeypatch.setattr(job_service, "validate_references", always_unsupported)
    resumed_again = batch_service.resume_current_stage_for_all_batches()
    assert resumed_again is not None
    assert batch_repository.get(broken_batch_id).status == "failed"


def test_startup_isolates_a_permanent_stable_id_request_mismatch_per_batch(
    tmp_path, monkeypatch
):
    """Case B: a Job row already exists under a batch item's stable id, but
    with different content than the batch currently expects. One such
    permanently-broken batch must not abort startup recovery for every
    other batch.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    mismatched_batch_id = batch_repository.list_all()[0].id
    stable_job_id = batch_repository.get(mismatched_batch_id).items[0].job_id
    assert job_repository.get(stable_job_id) is None

    healthy_batch = batch_service.create_batch(_spec())
    healthy_job_id = healthy_batch.items[0].job_id
    job_queue.dequeue()  # simulate a fresh restart's empty in-memory queue

    # Under the exact same stable id, a Job row now exists with different
    # content than the batch item currently expects.
    different_request = GenerationRequest(
        media_type="image", prompt="a completely different prompt", model_id="fake"
    )
    now = datetime.now(timezone.utc)
    job_repository.create(
        JobRecord(
            id=stable_job_id,
            status="queued",
            media_type="image",
            request=different_request,
            created_at=now,
            updated_at=now,
        )
    )

    # This must not raise -- the whole point of the fix.
    resumed = batch_service.resume_current_stage_for_all_batches()

    mismatched_refreshed = batch_repository.get(mismatched_batch_id)
    assert mismatched_refreshed.status == "failed"
    assert mismatched_refreshed.advance_error is not None
    assert any(record.id == healthy_batch.id for record in resumed)

    _drain_queue(job_runner)
    assert generator.calls == 1  # only the healthy job ran
    assert job_repository.get(healthy_job_id).status == "succeeded"

    # Repeated startup does not crash again.
    resumed_again = batch_service.resume_current_stage_for_all_batches()
    assert resumed_again is not None
    assert batch_repository.get(mismatched_batch_id).status == "failed"


# --- PR3 exact-HEAD audit, fourth round, adversarial self-review of finding
# 3: a materialization failure must be clearable once retried successfully --


def test_a_recovered_materialization_failure_does_not_permanently_fail_a_single_stage_batch(
    tmp_path, monkeypatch
):
    """A batch whose stage materialization fails once (e.g. a transient
    validation error at startup) and then succeeds on a later retry must
    converge to its real outcome, not stay permanently reported as
    ``failed``.

    `_spec()` has no ``stages`` override, so `BatchSpec.resolved_stages()`
    defaults to exactly one stage -- the common case, and the one where
    `_try_advance_in_place()`'s own `advance_error = None` clear line is
    structurally unreachable (there is no *next* stage to advance into).
    Without a dedicated clearing path, `_persist_stage_materialization_
    failure()` marking this single-stage batch failed once would leave it
    misreported as failed forever, even after the underlying job goes on
    to succeed (found via adversarial review of this round's own finding-3
    fix, before this test existed to guard it).
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    job_id = batch_repository.get(batch_id).items[0].job_id
    assert job_repository.get(job_id) is None

    def always_unsupported(*args, **kwargs):
        raise UnsupportedReferenceError("injected: transient at this restart only")

    monkeypatch.setattr(job_service, "validate_references", always_unsupported)
    resumed = batch_service.resume_current_stage_for_all_batches()
    assert not any(record.id == batch_id for record in resumed)

    failed_once = batch_repository.get(batch_id)
    assert failed_once.status == "failed"
    assert failed_once.advance_error is not None
    monkeypatch.undo()

    # The condition that caused the failure is gone -- a later restart's
    # retry succeeds this time.
    resumed_again = batch_service.resume_current_stage_for_all_batches()
    assert any(record.id == batch_id for record in resumed_again)

    _drain_queue(job_runner)
    assert generator.calls == 1
    assert job_repository.get(job_id).status == "succeeded"

    converged = batch_service.get_batch(batch_id)
    assert converged.status == "succeeded"
    assert converged.advance_error is None

    # Idempotent: reconciling again changes nothing further.
    assert batch_service.get_batch(batch_id).status == "succeeded"


def test_a_retry_that_never_reaches_the_still_broken_item_does_not_clear_its_marker(
    tmp_path, monkeypatch
):
    """A stage-materialization retry that `break`s early for an unrelated,
    transient reason -- before ever reaching the specific item whose
    permanent failure was previously persisted -- must not clear that
    marker.

    Uses a 3-item single-stage batch (a single ``Axis`` with 3 values):
    item 1 materializes fine, item 2's materialization is permanently
    broken, item 3 is never reached this pass (`_enqueue_stage()` exits
    via the uncaught exception as soon as item 2 raises). On a later
    retry, item 2's own problem is resolved -- but a fresh, *unrelated*
    transient condition (simulated here as `_authorize_and_expose()`
    reporting "unreadable" for item 1's already-materialized row) makes
    the loop `break` right after item 1, before item 2 is ever
    reattempted this pass. Without gating the clear on the loop
    completing every item without an early exit,
    `_clear_stale_materialization_failure()` would wrongly wipe item 2's
    marker even though its actual problem was never re-tested this pass
    (found via adversarial review of this round's own Fix A, before this
    test existed to guard it).
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    spec = BatchSpec(
        name="multi-item",
        media_type="image",
        model_id="fake",
        prompt="x",
        axes=[
            Axis(
                name="variant",
                values=[
                    AxisValue(label="v1", patch={"prompt": "item one"}),
                    AxisValue(label="v2", patch={"prompt": "item two"}),
                    AxisValue(label="v3", patch={"prompt": "item three"}),
                ],
            )
        ],
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before any row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(spec)
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    items = batch_repository.get(batch_id).items
    item1_id, item2_id, item3_id = (item.job_id for item in items)
    for job_id in (item1_id, item2_id, item3_id):
        assert job_repository.get(job_id) is None

    real_create_or_reuse = job_service.create_or_reuse_job_without_enqueue

    def fail_only_item2(job_id, request, **kwargs):
        if job_id == item2_id:
            raise UnsupportedReferenceError("injected: item 2 permanently unsupported")
        return real_create_or_reuse(job_id, request, **kwargs)

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", fail_only_item2)

    # First resume: item 1 materializes fine, item 2 raises (item 3 is
    # never even reached this pass) -- the caller persists the failure.
    resumed = batch_service.resume_current_stage_for_all_batches()
    assert not any(record.id == batch_id for record in resumed)
    assert job_repository.get(item1_id) is not None
    assert job_repository.get(item2_id) is None
    assert job_repository.get(item3_id) is None

    failed_once = batch_repository.get(batch_id)
    assert failed_once.status == "failed"
    assert failed_once.advance_error is not None
    monkeypatch.undo()

    # Item 2's own problem is now resolved -- but an unrelated, transient
    # condition makes this retry break right after item 1, before item 2
    # is ever reattempted.
    real_authorize = batch_service._authorize_and_expose

    def force_unreadable_for_item1(batch_id_arg, job_id_arg, job_status_arg):
        if job_id_arg == item1_id:
            return "unreadable"
        return real_authorize(batch_id_arg, job_id_arg, job_status_arg)

    monkeypatch.setattr(batch_service, "_authorize_and_expose", force_unreadable_for_item1)
    resumed_early_break = batch_service.resume_current_stage_for_all_batches()
    # This batch must NOT be reported as resumed: `_authorize_and_expose()`
    # returning "unreadable" for item 1 is a genuinely ambiguous, retryable
    # outcome, not a confirmed one -- exactly the case PR3 exact-HEAD
    # audit, seventh round, finding 1 fixed (`_enqueue_stage()` used to
    # let its own separate, later `reconcile()` read stand in for "this
    # stage's materialization is confirmed complete" even after an
    # ambiguous early exit). The monkeypatch here is permanent (not a
    # fires-once counter), so every one of this call's bounded retry
    # attempts hits the same "unreadable" outcome and the batch ends this
    # call still unresolved.
    assert not any(record.id == batch_id for record in resumed_early_break)
    monkeypatch.undo()

    # The marker must survive: item 2's actual problem was never
    # re-tested this pass, so its previously-persisted failure is still
    # exactly as unresolved as before.
    assert job_repository.get(item2_id) is None
    still_failed = batch_repository.get(batch_id)
    assert still_failed.status == "failed"
    assert still_failed.advance_error is not None

    # A genuinely clean retry (no early break, item 2 reattempted and
    # succeeding) does clear it.
    resumed_clean = batch_service.resume_current_stage_for_all_batches()
    assert any(record.id == batch_id for record in resumed_clean)

    _drain_queue(job_runner)
    assert generator.calls == 3
    for job_id in (item1_id, item2_id, item3_id):
        assert job_repository.get(job_id).status == "succeeded"

    converged = batch_service.get_batch(batch_id)
    assert converged.status == "succeeded"
    assert converged.advance_error is None


# --- PR3 exact-HEAD audit, fifth round, finding 3: retry batches omitted
# from the startup resume scan ------------------------------------------------


def test_startup_retries_a_batch_transiently_omitted_from_the_resume_scan(
    tmp_path, monkeypatch
):
    """A batch in the supported "stable id persisted, Job row not yet
    created" crash window must not be permanently stuck just because the
    very first startup scan attempt hits a transient `OSError` reading
    its file.

    Before this fix, `resume_current_stage_for_all_batches()` used the
    failure-suppressing `list_all()`, which silently omits a file it
    could not read this pass -- the batch was never even seen, let alone
    resumed, and nothing distinguished that from "there is genuinely
    nothing left to resume." With no Job row yet, no terminal event, and
    no runtime retry trigger, such a batch would stay stuck until the
    next restart or manual intervention (PR3 exact-HEAD audit, fifth
    round, finding 3). The fix's bounded immediate re-scan recovers it
    within this same call, without a new background mechanism.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_a_id = batch_repository.list_all()[0].id
    job_a_id = batch_repository.get(batch_a_id).items[0].job_id
    assert job_repository.get(job_a_id) is None

    batch_b = batch_service.create_batch(_spec())
    job_b_id = batch_b.items[0].job_id
    job_queue.dequeue()  # simulate a fresh restart's empty in-memory queue

    original_try_load_diagnosed = batch_repository._try_load_diagnosed
    read_attempts = {"count": 0}

    def flaky_once_for_batch_a(batch_file):
        if batch_file.stem == batch_a_id and read_attempts["count"] == 0:
            read_attempts["count"] += 1
            return None, True  # simulate a transient OSError, first read only
        return original_try_load_diagnosed(batch_file)

    monkeypatch.setattr(
        batch_repository, "_try_load_diagnosed", flaky_once_for_batch_a
    )

    resumed = batch_service.resume_current_stage_for_all_batches()

    # Batch A must not be silently treated as processed on the attempt
    # that missed it -- but the bounded in-call retry recovers it within
    # this same startup pass, alongside Batch B.
    assert any(record.id == batch_a_id for record in resumed)
    assert any(record.id == batch_b.id for record in resumed)
    assert read_attempts["count"] == 1  # the injected failure fired exactly once
    materialized = job_repository.get(job_a_id)
    assert materialized is not None
    assert materialized.status == "queued"

    monkeypatch.undo()
    _drain_queue(job_runner)
    assert generator.calls == 2
    assert job_repository.get(job_a_id).status == "succeeded"
    assert job_repository.get(job_b_id).status == "succeeded"

    # Repeated startup recovery never duplicates the row.
    batch_service.resume_current_stage_for_all_batches()
    assert len([job for job in job_repository.list() if job.id == job_a_id]) == 1


def test_startup_reports_a_malformed_batch_as_unresumable_without_retrying_it(
    tmp_path,
):
    """Unlike a transient read failure, a genuinely malformed batch file
    must not be retried by the bounded in-call scan (its content will
    not change on its own) -- it is reported and left unresumed, while
    an unrelated healthy batch still resumes normally.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    batch_a = batch_service.create_batch(_spec())
    job_a_id = batch_a.items[0].job_id
    batch_b = batch_service.create_batch(_spec())
    job_b_id = batch_b.items[0].job_id
    job_queue.dequeue()
    job_queue.dequeue()

    batch_file = tmp_path / "batches" / f"{batch_a.id}.json"
    batch_file.write_text("{not valid json", encoding="utf-8")

    resumed = batch_service.resume_current_stage_for_all_batches()

    assert not any(record.id == batch_a.id for record in resumed)
    assert any(record.id == batch_b.id for record in resumed)

    _drain_queue(job_runner)
    assert generator.calls == 1
    assert job_repository.get(job_a_id).status == "queued"  # untouched
    assert job_repository.get(job_b_id).status == "succeeded"


# --- PR3 exact-HEAD audit, sixth round, finding 1: retry batches whose
# own enqueue reread fails -----------------------------------------------


def test_startup_retries_a_batch_whose_own_enqueue_stage_reread_fails(
    tmp_path, monkeypatch, caplog
):
    """The outer directory scan (`list_all_tolerant()`) can succeed in
    full while `_enqueue_stage()`'s own, *separate* internal reread
    (`mutate()`'s own `get()` -> `_try_load()`) hits a transient failure
    for one specific batch. That batch must not be marked processed on
    this attempt merely because `scan_was_fully_reliable` is `True` --
    unlike the outer scan's own read failure (see the test above), the
    previous fix only retried when the *scan itself* was unreliable,
    unconditionally adding a batch to `processed_ids` the moment its own
    `_enqueue_stage()` call returned (PR3 exact-HEAD audit, sixth round,
    finding 1). In the supported "stable id persisted, Job row not yet
    created" crash window, a batch stuck this way has no Job row, no
    terminal event, and no runtime retry trigger to ever notice it again
    before the next restart.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before the row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(_spec())
    monkeypatch.undo()

    batch_a_id = batch_repository.list_all()[0].id
    job_a_id = batch_repository.get(batch_a_id).items[0].job_id
    assert job_repository.get(job_a_id) is None

    batch_b = batch_service.create_batch(_spec())
    job_b_id = batch_b.items[0].job_id
    job_queue.dequeue()  # simulate a fresh restart's empty in-memory queue

    # `_try_load()` (used by `get()` -> `mutate()`), not
    # `_try_load_diagnosed()` (used by `list_all_tolerant()`) -- the
    # outer scan reads Batch A's file just fine this attempt; only
    # `_enqueue_stage()`'s own phase-1 `mutate()` reread fails, and only
    # once.
    original_try_load = batch_repository._try_load
    read_attempts = {"count": 0}

    def flaky_once_for_batch_a(batch_file):
        if batch_file.stem == batch_a_id and read_attempts["count"] == 0:
            read_attempts["count"] += 1
            return None  # what _try_load() returns after catching an OSError
        return original_try_load(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load", flaky_once_for_batch_a)

    with caplog.at_level("WARNING", logger="core.batches.service"):
        resumed = batch_service.resume_current_stage_for_all_batches()

    # Batch A's own materialization reread failed this attempt even
    # though the outer scan succeeded -- it must not be silently marked
    # processed; the bounded retry cycle recovers it within this same
    # call, alongside Batch B.
    assert any(record.id == batch_a_id for record in resumed)
    assert any(record.id == batch_b.id for record in resumed)
    assert read_attempts["count"] == 1  # the injected failure fired exactly once

    # Observability follow-up (adversarial self-review): the unresolved
    # batch's own id must be named in a warning, mirroring the sibling
    # malformed-file warning in the same method -- not just a generic
    # "something is unreliable" message.
    assert any(batch_a_id in record.getMessage() for record in caplog.records)
    materialized = job_repository.get(job_a_id)
    assert materialized is not None
    assert materialized.status == "queued"

    monkeypatch.undo()
    _drain_queue(job_runner)
    assert generator.calls == 2
    assert job_repository.get(job_a_id).status == "succeeded"
    assert job_repository.get(job_b_id).status == "succeeded"

    # Repeated startup recovery never duplicates the row.
    batch_service.resume_current_stage_for_all_batches()
    assert len([job for job in job_repository.list() if job.id == job_a_id]) == 1


# --- PR3 exact-HEAD audit, seventh round, finding 1: propagate early exits
# from stage materialization --------------------------------------------------


def test_enqueue_stage_does_not_report_success_after_an_unreadable_early_exit(
    tmp_path, monkeypatch
):
    """`_enqueue_stage()` must not let its own final `reconcile()` read --
    a wholly separate read, taken moments after an earlier item's
    ambiguous ("unreadable") outcome -- stand in for "this stage's
    materialization is confirmed complete."

    2-item single stage: item 1 materializes and is exposed to the queue
    successfully; item 2's own `_authorize_and_expose()` call reports
    "unreadable" (a transient pre-exposure Batch reread failure, not a
    confirmed cancellation or deletion). Before this fix, the loop simply
    broke and the function still returned whatever the final
    `reconcile()` call happened to read -- often a perfectly normal
    `BatchRecord`, silently reported as full success to every caller
    (`reconcile_child_job()` -> `RECONCILED` -> completion marked done;
    `resume_current_stage_for_all_batches()` -> marked processed) even
    though item 2 was never actually exposed to a worker (PR3 exact-HEAD
    audit, seventh round, finding 1).
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    spec = BatchSpec(
        name="two-item", media_type="image", model_id="fake", prompt="x",
        axes=[
            Axis(
                name="variant",
                values=[
                    AxisValue(label="v1", patch={"prompt": "item one"}),
                    AxisValue(label="v2", patch={"prompt": "item two"}),
                ],
            )
        ],
    )

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash before any row is ever created")

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(spec)
    monkeypatch.undo()

    batch_id = batch_repository.list_all()[0].id
    item1_id, item2_id = (item.job_id for item in batch_repository.get(batch_id).items)
    for job_id in (item1_id, item2_id):
        assert job_repository.get(job_id) is None

    real_authorize = batch_service._authorize_and_expose

    def force_unreadable_for_item2(batch_id_arg, job_id_arg, job_status_arg):
        if job_id_arg == item2_id:
            return "unreadable"
        return real_authorize(batch_id_arg, job_id_arg, job_status_arg)

    monkeypatch.setattr(batch_service, "_authorize_and_expose", force_unreadable_for_item2)

    result = batch_service._enqueue_stage(batch_id, stage_index=0)

    assert result is None  # not reported as normal success
    assert job_repository.get(item1_id) is not None
    assert job_repository.get(item1_id).status == "queued"
    assert job_repository.get(item2_id) is not None  # materialized...
    assert job_queue.size() == 1  # ...but never exposed to a worker

    monkeypatch.undo()  # storage/authorize "recovers"

    # A caller (startup resume) treating the prior `None` as "ambiguous,
    # diagnose" -- never as confirmed complete -- retries within the same
    # bounded cycle, exposing item 2 exactly once without re-materializing
    # or re-exposing item 1 as a duplicate (JobQueue.enqueue()'s own
    # idempotency for an id already pending in its lane).
    resumed = batch_service.resume_current_stage_for_all_batches()
    assert any(record.id == batch_id for record in resumed)
    assert job_queue.size() == 2

    _drain_queue(job_runner)
    assert generator.calls == 2
    assert job_repository.get(item1_id).status == "succeeded"
    assert job_repository.get(item2_id).status == "succeeded"
    assert len(job_repository.list()) == 2  # no duplicate rows


def test_reconcile_child_job_treats_an_unreadable_early_exit_as_retryable(
    tmp_path, monkeypatch
):
    """The same ambiguous early exit, observed through `reconcile_child_
    job()` (the completion-convergence caller) instead of a direct
    `_enqueue_stage()` call: completion must stay retryable, never be
    reported as `RECONCILED` (which `CompletionConverger` would otherwise
    mark `completion_state="done"`, permanently excluding the batch's
    still-unexposed item from every future retry).
    """

    from core.batches import BatchReconciliationOutcome

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    spec = BatchSpec(
        name="two-stage-single-item", media_type="image", model_id="fake", prompt="x",
        limit=1, stages=[{"name": "probe"}, {"name": "refine"}],
    )
    batch = batch_service.create_batch(spec)
    probe_job_id = batch.items[0].job_id
    job_queue.dequeue()  # drain the original create_batch() enqueue of the probe job
    job_repository.update_status(probe_job_id, "preparing")
    job_repository.update_status(probe_job_id, "running")
    job_repository.update_status(probe_job_id, "postprocessing")
    job_repository.update(
        probe_job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=probe_job_id, status="succeeded", outputs=["a.png"]),
    )

    # The stage-advance mutation itself persists normally; only the
    # *following* `_enqueue_stage()` call for the new "refine" stage hits
    # the ambiguous "unreadable" outcome for its own single item.
    def force_unreadable(batch_id_arg, job_id_arg, job_status_arg):
        return "unreadable"

    monkeypatch.setattr(batch_service, "_authorize_and_expose", force_unreadable)

    _record, outcome = batch_service.reconcile_child_job(probe_job_id)

    assert outcome == BatchReconciliationOutcome.RETRYABLE_FAILURE
    advanced = batch_repository.get(batch.id)
    assert advanced.stage_index == 1  # the advance itself did persist
    refine_item = next(item for item in advanced.items if item.stage_index == 1)
    assert refine_item.job_id is not None  # materialized...
    assert job_queue.size() == 0  # ...but never exposed

    monkeypatch.undo()

    _record2, outcome2 = batch_service.reconcile_child_job(probe_job_id)
    assert outcome2 == BatchReconciliationOutcome.RECONCILED
    assert job_queue.size() == 1

    _drain_queue(job_runner)
    assert generator.calls == 1
    assert job_repository.get(refine_item.job_id).status == "succeeded"


def test_create_batch_retries_an_ambiguous_early_exit_within_the_same_call(
    tmp_path, monkeypatch
):
    """`create_batch()` has no later runtime pass of its own that will
    ever revisit a batch stuck by an ambiguous early exit during its
    initial materialization (an ordinary `reconcile()` poll never calls
    `_enqueue_stage()`; `resume_current_stage_for_all_batches()` only
    ever runs once, at startup) -- found via adversarial review of this
    round's own finding-1 fix, whose first version silently returned an
    apparently-normal-looking, but actually incompletely-materialized,
    batch straight to the API caller with no error and no retry signal.

    2-item single stage: item 2's own `_authorize_and_expose()` call
    reports "unreadable" on the FIRST attempt only. `create_batch()`'s
    own small, bounded immediate retry resolves this within the same
    call -- both items end up materialized and exposed, without ever
    needing a restart or an external caller to notice anything was
    wrong.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )
    generator = _CountingGenerator()
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        job_service=job_service,
    )

    spec = BatchSpec(
        name="two-item", media_type="image", model_id="fake", prompt="x",
        axes=[
            Axis(
                name="variant",
                values=[
                    AxisValue(label="v1", patch={"prompt": "item one"}),
                    AxisValue(label="v2", patch={"prompt": "item two"}),
                ],
            )
        ],
    )

    # item2's stable id isn't known until phase 1 assigns it inside
    # create_batch() itself -- capture it via the request content
    # (unique prompt) at the point create_or_reuse_job_without_enqueue()
    # is called for it, which always happens strictly before
    # _authorize_and_expose() for that same item in the same iteration.
    item2_id_holder: dict = {}
    real_create_or_reuse = job_service.create_or_reuse_job_without_enqueue

    def capture_item2_id(job_id_arg, request_arg, **kwargs):
        if request_arg.prompt == "item two":
            item2_id_holder["id"] = job_id_arg
        return real_create_or_reuse(job_id_arg, request_arg, **kwargs)

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", capture_item2_id)

    real_authorize = batch_service._authorize_and_expose
    attempts = {"count": 0}

    def flaky_once_for_item2(batch_id_arg, job_id_arg, job_status_arg):
        if job_id_arg == item2_id_holder.get("id") and attempts["count"] == 0:
            attempts["count"] += 1
            return "unreadable"
        return real_authorize(batch_id_arg, job_id_arg, job_status_arg)

    monkeypatch.setattr(batch_service, "_authorize_and_expose", flaky_once_for_item2)

    batch = batch_service.create_batch(spec)

    assert attempts["count"] == 1  # the injected failure fired exactly once
    item1_id, item2_id = (item.job_id for item in batch.items)
    assert item2_id == item2_id_holder["id"]
    assert job_repository.get(item1_id).status == "queued"
    assert job_repository.get(item2_id).status == "queued"
    assert job_queue.size() == 2  # both ended up exposed within this one call

    _drain_queue(job_runner)
    assert generator.calls == 2
    assert job_repository.get(item1_id).status == "succeeded"
    assert job_repository.get(item2_id).status == "succeeded"
    assert len(job_repository.list()) == 2  # no duplicate rows


def test_create_batch_falls_back_honestly_when_retries_are_exhausted(
    tmp_path, monkeypatch, caplog
):
    """If the ambiguous early exit never resolves within `create_batch()`'s
    own bounded retry attempts, it must still return a valid `BatchRecord`
    (never raise or hang) -- the already-materialized item stays exposed,
    the still-ambiguous one is left for a later explicit resume, and the
    situation is at least logged rather than silently swallowed.
    """

    job_repository, job_queue, job_service, batch_repository, batch_service = _build(
        tmp_path
    )

    spec = BatchSpec(
        name="two-item-stuck", media_type="image", model_id="fake", prompt="x",
        axes=[
            Axis(
                name="variant",
                values=[
                    AxisValue(label="v1", patch={"prompt": "item one"}),
                    AxisValue(label="v2", patch={"prompt": "item two"}),
                ],
            )
        ],
    )

    item2_id_holder: dict = {}
    real_create_or_reuse = job_service.create_or_reuse_job_without_enqueue

    def capture_item2_id(job_id_arg, request_arg, **kwargs):
        if request_arg.prompt == "item two":
            item2_id_holder["id"] = job_id_arg
        return real_create_or_reuse(job_id_arg, request_arg, **kwargs)

    monkeypatch.setattr(job_service, "create_or_reuse_job_without_enqueue", capture_item2_id)

    real_authorize = batch_service._authorize_and_expose

    def always_unreadable_for_item2(batch_id_arg, job_id_arg, job_status_arg):
        if job_id_arg == item2_id_holder.get("id"):
            return "unreadable"
        return real_authorize(batch_id_arg, job_id_arg, job_status_arg)

    monkeypatch.setattr(batch_service, "_authorize_and_expose", always_unreadable_for_item2)

    with caplog.at_level("WARNING", logger="core.batches.service"):
        batch = batch_service.create_batch(spec)

    assert batch is not None
    item1_id, item2_id = (item.job_id for item in batch.items)
    assert item2_id == item2_id_holder["id"]
    assert job_repository.get(item1_id).status == "queued"
    assert job_queue.size() == 1  # only item 1 ever got exposed
    assert any(batch.id in record.getMessage() for record in caplog.records)

    monkeypatch.undo()

    # A later explicit resume (e.g. the next startup) still recovers it.
    resumed = batch_service.resume_current_stage_for_all_batches()
    assert any(record.id == batch.id for record in resumed)
    assert job_queue.size() == 2
