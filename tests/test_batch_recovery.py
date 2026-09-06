"""PR3: deterministic regressions for Batch child-id stability, cancellation
intent, lock-safe mutation, and terminal-child reconciliation.

No sleep-based waits: concurrency scenarios use `threading.Barrier`/`Event`,
matching `tests/test_story_concurrency.py`'s own established pattern for
`StoryRepository.mutate()`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread

import pytest

from core.batches import BatchReconciliationOutcome, BatchRepository, BatchService
from core.batches.schemas import BatchSpec
from core.jobs import EventBus, JobQueue, JobRunner, JobService
from core.schemas import GenerationResult
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
