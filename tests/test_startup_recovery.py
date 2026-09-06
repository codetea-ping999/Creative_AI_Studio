"""PR3: deterministic regressions for `core.jobs.startup_recovery`.

No sleep-based waits anywhere here: `run_startup_recovery()` is a plain
synchronous function, so every scenario below is set up by seeding SQLite
rows directly (via `JobRepository`/raw `sqlite3`), calling it once, and
asserting on the resulting rows -- no threads, no timing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from core.assets import AssetRepository
from core.batches import BatchRepository, BatchService
from core.jobs import EventBus, JobQueue, JobRunner, JobService
from core.jobs.completion import CompletionConverger, CompletionOutcome
from core.jobs.schemas import JobRecord
from core.jobs.startup_recovery import run_startup_recovery
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRepository
from generators.base import BaseGenerator
from generators.registry import GeneratorRegistry


class FakeGenerator(BaseGenerator):
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
        return GenerationResult(job_id="fake", status="succeeded")


def _seed(repository, status, job_id, **overrides):
    now = datetime.now(timezone.utc)
    fields = dict(
        id=job_id,
        status=status,
        media_type="image",
        request=GenerationRequest(media_type="image", prompt="fake", model_id="fake"),
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return repository.create(JobRecord(**fields))


def _build_services(tmp_path):
    db_path = tmp_path / "jobs.db"
    job_repository = JobRepository(db_path)
    job_queue = JobQueue()
    event_bus = EventBus()
    generator = FakeGenerator()
    job_service = JobService(job_repository, job_queue, event_bus)
    job_runner = JobRunner(
        job_repository, job_queue, GeneratorRegistry({"image": generator}),
        event_bus, job_service=job_service,
    )
    asset_repository = AssetRepository(tmp_path / "assets")
    batch_repository = BatchRepository(tmp_path / "batches")
    batch_service = BatchService(
        batch_repository, job_service, job_repository, event_bus=event_bus
    )
    completion_converger = CompletionConverger(
        job_repository, asset_repository, batch_service=batch_service
    )
    return {
        "db_path": db_path,
        "job_repository": job_repository,
        "job_queue": job_queue,
        "job_service": job_service,
        "job_runner": job_runner,
        "generator": generator,
        "batch_service": batch_service,
        "batch_repository": batch_repository,
        "completion_converger": completion_converger,
    }


def _drain_queue(services) -> None:
    """Run every currently-queued job to completion via the real runner --
    used after recovery to prove a job that was *not* re-enqueued can never
    be picked up and regenerated, not just that a mock counter stayed 0."""

    while services["job_runner"].run_once() is not None:
        pass


def _raw_status(db_path, job_id: str) -> str:
    return sqlite3.connect(db_path).execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]


# --- Job recovery (Cases 1-7) ---------------------------------------------


def test_queued_restart_enqueues_the_same_job_id(tmp_path):
    services = _build_services(tmp_path)
    job = _seed(services["job_repository"], "queued", "job_a")

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
    )

    assert report.requeued == [job.id]
    assert services["job_queue"].dequeue() == job.id
    assert services["job_repository"].get(job.id).status == "queued"


@pytest.mark.parametrize("status", ["preparing", "running", "postprocessing"])
def test_active_restart_resolves_to_failed_without_rerunning_the_generator(tmp_path, status):
    services = _build_services(tmp_path)
    job = _seed(services["job_repository"], status, "job_a")

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
    )

    assert report.interrupted_failed == [job.id]
    after = services["job_repository"].get(job.id)
    assert after.status == "failed"
    assert after.error_message is not None
    assert "process_interrupted" in after.error_message
    assert services["job_queue"].dequeue() is None
    _drain_queue(services)  # no-op: proves the queue really is empty
    assert services["generator"].calls == 0


def test_cancel_requested_restart_resolves_to_cancelled(tmp_path):
    services = _build_services(tmp_path)
    job = _seed(services["job_repository"], "cancel_requested", "job_a")

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
    )

    assert report.cancel_requested_cancelled == [job.id]
    assert services["job_repository"].get(job.id).status == "cancelled"
    _drain_queue(services)
    assert services["generator"].calls == 0


def test_succeeded_restart_never_reruns_the_generator(tmp_path):
    services = _build_services(tmp_path)
    job = _seed(
        services["job_repository"], "succeeded", "job_a",
        result=GenerationResult(job_id="job_a", status="succeeded", outputs=["a.png"]),
    )

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
    )

    _drain_queue(services)
    assert services["generator"].calls == 0
    assert report.completion_outcomes.get(job.id) == CompletionOutcome.DONE
    assert services["job_repository"].get(job.id).completion_state == "done"
    assert services["job_repository"].get(job.id).status == "succeeded"  # unchanged


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_restart_never_reruns_the_generator(tmp_path, status):
    services = _build_services(tmp_path)
    job = _seed(services["job_repository"], status, "job_a")

    run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
    )

    _drain_queue(services)
    assert services["generator"].calls == 0
    assert services["job_repository"].get(job.id).status == status
    assert services["job_repository"].get(job.id).completion_state == "done"


# --- Startup isolation (Cases 23-26) --------------------------------------


def test_poison_row_does_not_abort_recovery_of_a_healthy_queued_row(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    poison = _seed(repository, "queued", "job_poison")
    healthy = _seed(repository, "queued", "job_healthy")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute("UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", poison.id))
        raw.commit()

    report = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert healthy.id in report.requeued
    assert services["job_queue"].dequeue() == healthy.id
    assert _raw_status(services["db_path"], poison.id) == "failed"


def test_invalid_raw_status_uses_the_existing_quarantine_repair_path(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    poison = _seed(repository, "queued", "job_poison")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute("UPDATE jobs SET status = ? WHERE id = ?", ("banana", poison.id))
        raw.commit()

    report = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert report.poison_rows.get(poison.id) == "quarantined_invalid_status"
    assert _raw_status(services["db_path"], poison.id) == "failed"


def test_quarantine_transient_write_failure_is_not_treated_as_processed(tmp_path, monkeypatch):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    poison = _seed(repository, "queued", "job_poison")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute("UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", poison.id))
        raw.commit()

    def fail_transition(*args, **kwargs):
        raise sqlite3.OperationalError("injected transient quarantine write failure")

    monkeypatch.setattr(repository, "transition_if_status", fail_transition)

    report = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert report.poison_rows.get(poison.id) == "transient_write_failure"
    # Never marked resolved/handled -- the raw row is untouched (still
    # queued -- transition_if_status was patched to always fail).
    assert _raw_status(services["db_path"], poison.id) == "queued"


def test_malformed_payload_row_does_not_abort_the_complete_scan(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    jobs = [_seed(repository, "queued", f"job_{i}") for i in range(5)]
    poison = jobs[2]
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute("UPDATE jobs SET result_json = ? WHERE id = ?", ("{also not valid", poison.id))
        # A succeeded job needs a result_json to decode too -- give it one so
        # the corruption actually trips json.loads() (queued jobs never read
        # result_json otherwise, since it's NULL).
        raw.execute("UPDATE jobs SET status = ? WHERE id = ?", ("succeeded", poison.id))
        raw.commit()

    report = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert poison.id in report.poison_rows
    for job in jobs:
        if job.id == poison.id:
            continue
        assert job.id in report.requeued


# --- Repeat restart (Cases 27-29) -----------------------------------------


def test_repeated_startup_recovery_passes_are_idempotent(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    queued = _seed(repository, "queued", "job_queued")
    interrupted = _seed(repository, "running", "job_running")
    cancel_requested = _seed(repository, "cancel_requested", "job_cancel_requested")
    succeeded = _seed(
        repository, "succeeded", "job_succeeded",
        result=GenerationResult(job_id="job_succeeded", status="succeeded", outputs=["a.png"]),
    )

    converger = services["completion_converger"]
    run_startup_recovery(repository, services["job_service"], converger)
    services["job_queue"].dequeue()  # drain what the first pass enqueued, without running it
    second = run_startup_recovery(repository, services["job_service"], converger)

    # `queued` is a legitimate queued job -- it is *supposed* to run
    # eventually (once, no matter how many recovery passes re-enqueue it,
    # since re-enqueue is idempotent for an id already pending); only the
    # already-terminal jobs must never reach the generator.
    _drain_queue(services)
    assert services["generator"].calls == 1
    assert repository.get(queued.id).status == "succeeded"
    assert repository.get(interrupted.id).status == "failed"
    assert repository.get(cancel_requested.id).status == "cancelled"
    assert repository.get(succeeded.id).status == "succeeded"
    assert repository.get(succeeded.id).completion_state == "done"
    # Second pass must not re-finalize what the first pass already
    # finalized (the CAS sources no longer match), and must not even
    # re-attempt convergence for a job already completion_state="done"
    # (list_terminal_pending_completion() excludes it at the SQL level).
    assert second.interrupted_failed == []
    assert second.cancel_requested_cancelled == []
    assert succeeded.id not in second.completion_outcomes
    assert (
        services["completion_converger"].converge_job(succeeded.id)
        == CompletionOutcome.SAFE_NOOP
    )


def test_repeated_startup_recovery_never_reruns_the_generator_for_a_succeeded_job(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    job = _seed(
        repository, "succeeded", "job_a",
        result=GenerationResult(job_id="job_a", status="succeeded", outputs=["a.png"]),
    )

    for _ in range(3):
        run_startup_recovery(repository, services["job_service"], services["completion_converger"])

    _drain_queue(services)
    assert services["generator"].calls == 0
    assert repository.get(job.id).status == "succeeded"
    assert repository.get(job.id).completion_state == "done"


# --- PR3 exact-HEAD audit P1-1: resume persisted stages on startup -------


def test_startup_materializes_a_batch_child_row_that_was_never_created(tmp_path, monkeypatch):
    """A crash between `create_batch()` persisting the batch record (and its
    items' stable ids) and phase 2 actually creating the Job row leaves the
    batch permanently `queued` under the old startup-recovery contract --
    nothing in an ordinary reconcile pass creates a missing row. Startup
    recovery must resume/materialize it under the exact same id.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash between id persist and row creation")

    monkeypatch.setattr(services["job_service"], "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(
            BatchSpec(name="crashed", media_type="image", model_id="fake", prompt="x", limit=1)
        )
    batch = batch_service.batch_repository.list_all()[0]
    persisted_job_id = batch.items[0].job_id
    assert persisted_job_id is not None
    assert services["job_repository"].get(persisted_job_id) is None
    monkeypatch.undo()

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert batch.id in report.batches_resumed_current_stage
    materialized = services["job_repository"].get(persisted_job_id)
    assert materialized is not None
    assert materialized.status == "queued"
    assert services["job_queue"].dequeue() == persisted_job_id


def test_repeated_startup_recovery_never_duplicates_a_previously_uncreated_child(
    tmp_path, monkeypatch
):
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]

    def always_fail(*args, **kwargs):
        raise RuntimeError("injected: crash between id persist and row creation")

    monkeypatch.setattr(services["job_service"], "create_or_reuse_job_without_enqueue", always_fail)
    with pytest.raises(RuntimeError, match="injected"):
        batch_service.create_batch(
            BatchSpec(name="crashed", media_type="image", model_id="fake", prompt="x", limit=1)
        )
    batch_id = batch_service.batch_repository.list_all()[0].id
    persisted_job_id = batch_service.batch_repository.get(batch_id).items[0].job_id
    monkeypatch.undo()

    for _ in range(3):
        run_startup_recovery(
            services["job_repository"], services["job_service"],
            services["completion_converger"], batch_service=batch_service,
        )

    matching = [job for job in services["job_repository"].list() if job.id == persisted_job_id]
    assert len(matching) == 1
    refreshed = batch_service.get_batch(batch_id)
    assert refreshed.items[0].job_id == persisted_job_id


# --- PR3 exact-HEAD audit P1-6: transient Batch scan failures -------------


def test_startup_does_not_enqueue_queued_jobs_when_the_cancellation_scan_is_unreliable(
    tmp_path, monkeypatch
):
    """A durable `cancellation_requested=True` batch whose file is
    transiently unreadable during `resume_pending_cancellations()`'s scan
    must not let startup recovery re-enqueue *any* queued job this pass --
    the hidden batch's own still-queued child could be exactly the job a
    generic queued-job sweep would otherwise put back to work despite a
    cancellation intent this pass never got the chance to see.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch = batch_service.create_batch(
        BatchSpec(name="cancel-me", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    child_job_id = batch.items[0].job_id
    services["job_queue"].dequeue()  # simulate "never enqueued" (a fresh restart)

    # Durable intent persisted, but the child was never actually told to
    # cancel -- the exact crash window resume_pending_cancellations() exists
    # to close.
    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_service.batch_repository.mutate(batch.id, _mark_cancellation_requested_only)
    assert services["job_repository"].get(child_job_id).status == "queued"

    original_try_load_diagnosed = batch_service.batch_repository._try_load_diagnosed

    def flaky_try_load_diagnosed(batch_file):
        if batch_file.stem == batch.id:
            return None, True  # simulate a transient OSError reading this exact file
        return original_try_load_diagnosed(batch_file)

    monkeypatch.setattr(
        batch_service.batch_repository, "_try_load_diagnosed", flaky_try_load_diagnosed
    )

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert report.batch_cancellation_scan_was_fully_reliable is False
    assert batch.id not in report.batches_resumed_cancelling
    assert report.queued_enqueue_skipped_due_to_unreliable_batch_scan is True
    assert child_job_id not in report.requeued
    assert services["job_queue"].dequeue() is None  # nothing was put back in the queue
    # The row itself is untouched -- still safely resumable later, not lost.
    assert services["job_repository"].get(child_job_id).status == "queued"

    monkeypatch.undo()  # storage "recovers"

    second = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert second.batch_cancellation_scan_was_fully_reliable is True
    assert batch.id in second.batches_resumed_cancelling
    assert services["job_repository"].get(child_job_id).status in (
        "cancel_requested", "cancelled",
    )
    assert services["job_queue"].dequeue() is None  # never enqueued despite recovering


def test_repeated_startup_recovery_keeps_the_same_batch_child_job_id(tmp_path):
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch = services["batch_service"].create_batch(
        BatchSpec(name="steady", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    child_job_id = batch.items[0].job_id
    assert child_job_id is not None

    for _ in range(3):
        run_startup_recovery(
            services["job_repository"], services["job_service"], services["completion_converger"],
            batch_service=services["batch_service"],
        )

    refreshed = services["batch_service"].get_batch(batch.id)
    assert refreshed.items[0].job_id == child_job_id
    assert len([job for job in services["job_repository"].list() if job.id == child_job_id]) == 1


# --- PR3 exact-HEAD audit, third round, P1-4: propagate failures while
# reapplying discovered cancellation -----------------------------------------


def test_startup_treats_a_failed_cancellation_reapplication_as_unreliable(
    tmp_path, monkeypatch
):
    """The tolerant scan itself can successfully observe
    `cancellation_requested=True`, but `cancel()`'s own subsequent
    read/mutate can still hit a transient failure -- that must downgrade
    the overall reliability result too, not just a failure in the initial
    scan.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch_repository = services["batch_repository"]
    batch = batch_service.create_batch(
        BatchSpec(name="cancel-me", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    child_job_id = batch.items[0].job_id
    services["job_queue"].dequeue()  # simulate a fresh restart's empty queue

    # Durable intent persisted directly, without going through cancel()'s
    # own child-cancelling loop -- simulating "discovered by the tolerant
    # scan, but the child was never actually told to cancel yet" (the
    # exact crash window this whole mechanism exists to close).
    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_repository.mutate(batch.id, _mark_cancellation_requested_only)
    assert services["job_repository"].get(child_job_id).status == "queued"

    # The initial tolerant scan (list_all_tolerant() -> _try_load_diagnosed())
    # must still succeed and observe cancellation_requested=True; only
    # cancel()'s own subsequent mutate() -> get() -> _try_load() read is
    # injected to fail here.
    original_try_load = batch_repository._try_load

    def flaky_try_load(batch_file):
        if batch_file.stem == batch.id:
            return None
        return original_try_load(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load", flaky_try_load)

    report = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert report.batch_cancellation_scan_was_fully_reliable is False
    assert report.queued_enqueue_skipped_due_to_unreliable_batch_scan is True
    assert child_job_id not in report.requeued
    assert services["job_queue"].dequeue() is None  # never enqueued
    assert services["job_repository"].get(child_job_id).status == "queued"  # left alone

    monkeypatch.undo()  # storage "recovers"

    second = run_startup_recovery(
        services["job_repository"], services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert second.batch_cancellation_scan_was_fully_reliable is True
    assert services["job_repository"].get(child_job_id).status in (
        "cancel_requested", "cancelled",
    )
    assert services["job_queue"].dequeue() is None  # never enqueued despite recovering


# --- PR3 exact-HEAD audit, third round, P1-5: isolate poisoned batch
# children during the final startup sweep ------------------------------------


def test_startup_batch_sweep_survives_one_poison_child_and_recovers_the_others(tmp_path):
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch_a = batch_service.create_batch(
        BatchSpec(name="poison", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    batch_b = batch_service.create_batch(
        BatchSpec(name="healthy", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    poison_job_id = batch_a.items[0].job_id
    healthy_job_id = batch_b.items[0].job_id
    # Simulate a fresh restart: the in-memory queue starts empty regardless
    # of what create_batch() enqueued a "process" ago -- otherwise the
    # poison job's *original*, pre-corruption queue entry would still be
    # sitting there for `_drain_queue()` to trip over below, which is not
    # what this test is about.
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    # Simulate step 1's own quarantine outcome for a malformed queued Job
    # belonging to a batch: raw status flipped to "failed" (exactly what
    # step 1 already does), but the malformed request_json deliberately
    # left untouched -- exactly what the finding describes as still
    # tripping up an unconditional `_recompute()` read afterward.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET status = ?, request_json = ? WHERE id = ?",
            ("failed", "{not valid json", poison_job_id),
        )
        raw.commit()

    # This must not raise -- the whole point of the fix.
    run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    # Batch B's healthy child was never lost -- it converges normally.
    _drain_queue(services)
    assert job_repository.get(healthy_job_id).status == "succeeded"
    assert (
        services["completion_converger"].converge_job(healthy_job_id)
        == CompletionOutcome.DONE
    )
    healthy_refreshed = batch_service.get_batch(batch_b.id)
    assert healthy_refreshed.status == "succeeded"

    # Batch A survives being read (not aborted) even though its own child
    # cannot currently be decoded -- isolated, not resurrected or crashed.
    poisoned_refreshed = batch_service.get_batch(batch_a.id)
    assert poisoned_refreshed is not None


# --- PR3 exact-HEAD audit, third round, P2-1: preserve startup Asset
# repair for completed jobs --------------------------------------------------


def test_startup_restores_a_deleted_asset_for_an_already_done_succeeded_job(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    job = _seed(
        repository, "succeeded", "job_a",
        result=GenerationResult(job_id="job_a", status="succeeded", outputs=["a.png"]),
    )

    run_startup_recovery(repository, services["job_service"], services["completion_converger"])
    assert repository.get(job.id).completion_state == "done"

    asset_repository = services["completion_converger"].asset_repository
    original_asset = asset_repository.get_primary_by_job(job.id)
    assert original_asset is not None
    asset_path = tmp_path / "assets" / f"{original_asset.id}.json"
    asset_path.unlink()
    assert asset_repository.get(original_asset.id) is None

    second = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert second.assets_repaired >= 1
    restored = asset_repository.get_primary_by_job(job.id)
    assert restored is not None
    assert restored.id == original_asset.id
    # Never re-derived completion_state, never re-ran the generator.
    assert repository.get(job.id).completion_state == "done"
    _drain_queue(services)
    assert services["generator"].calls == 0


def test_startup_repairs_a_malformed_asset_for_an_already_done_succeeded_job(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    job = _seed(
        repository, "succeeded", "job_a",
        result=GenerationResult(job_id="job_a", status="succeeded", outputs=["a.png"]),
    )
    run_startup_recovery(repository, services["job_service"], services["completion_converger"])
    asset_repository = services["completion_converger"].asset_repository
    original_asset = asset_repository.get_primary_by_job(job.id)
    asset_path = tmp_path / "assets" / f"{original_asset.id}.json"
    asset_path.write_text("{not valid json", encoding="utf-8")
    assert asset_repository.get(original_asset.id) is None

    second = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert second.assets_repaired >= 1
    repaired = asset_repository.get_primary_by_job(job.id)
    assert repaired is not None
    assert repaired.id == original_asset.id
    _drain_queue(services)
    assert services["generator"].calls == 0


def test_startup_asset_repair_is_not_aborted_by_one_poison_succeeded_job(tmp_path):
    services = _build_services(tmp_path)
    repository = services["job_repository"]
    healthy = _seed(
        repository, "succeeded", "job_healthy",
        result=GenerationResult(job_id="job_healthy", status="succeeded", outputs=["a.png"]),
    )
    poison = _seed(
        repository, "succeeded", "job_poison",
        result=GenerationResult(job_id="job_poison", status="succeeded", outputs=["b.png"]),
    )
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET result_json = ? WHERE id = ?", ("{also not valid", poison.id)
        )
        raw.commit()

    asset_repository = services["completion_converger"].asset_repository
    report = run_startup_recovery(
        repository, services["job_service"], services["completion_converger"],
    )

    assert report.assets_repaired >= 1
    healthy_asset = asset_repository.get_primary_by_job(healthy.id)
    assert healthy_asset is not None
    _drain_queue(services)
    assert services["generator"].calls == 0
