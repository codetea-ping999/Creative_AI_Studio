"""PR3: deterministic regressions for `core.jobs.startup_recovery`.

No sleep-based waits anywhere here: `run_startup_recovery()` is a plain
synchronous function, so every scenario below is set up by seeding SQLite
rows directly (via `JobRepository`/raw `sqlite3`), calling it once, and
asserting on the resulting rows -- no threads, no timing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3

import pytest

from core.assets import AssetRepository
from core.batches import BatchRepository, BatchService
from core.jobs import EventBus, JobQueue, JobRunner, JobService
from core.jobs.completion import CompletionConverger, CompletionOutcome
from core.jobs.schemas import JobRecord
from core.jobs.startup_recovery import run_startup_recovery
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRecordDecodeError, JobRepository
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


class FailingGenerator(BaseGenerator):
    def __init__(self, message):
        self.calls = 0
        self._message = message

    def validate_request(self, request):
        pass

    def prepare(self, request):
        pass

    def cleanup(self, request):
        pass

    def generate(self, request, context=None):
        self.calls += 1
        raise RuntimeError(self._message)


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


def _build_services(tmp_path, generator=None):
    db_path = tmp_path / "jobs.db"
    job_repository = JobRepository(db_path)
    job_queue = JobQueue()
    event_bus = EventBus()
    generator = generator if generator is not None else FakeGenerator()
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
        job_repository, asset_repository, batch_service=batch_service,
        job_service=job_service,
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


# --- PR3 exact-HEAD audit, fourth round, finding 1: treat malformed batch
# scans as unsafe for requeue -----------------------------------------------


def test_startup_treats_a_malformed_batch_scan_as_unsafe_for_generic_requeue(
    tmp_path, caplog
):
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch_repository = services["batch_repository"]
    job_repository = services["job_repository"]

    batch_a = batch_service.create_batch(
        BatchSpec(name="malformed", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    poison_job_id = batch_a.items[0].job_id
    batch_b = batch_service.create_batch(
        BatchSpec(name="healthy", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    healthy_job_id = batch_b.items[0].job_id

    # Durable intent persisted before the file becomes malformed --
    # simulating "corruption after cancellation_requested was saved but
    # before cancel_job() ran", exactly as the finding describes.
    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_repository.mutate(batch_a.id, _mark_cancellation_requested_only)
    assert job_repository.get(poison_job_id).status == "queued"

    # Simulate a fresh restart: the in-memory queue starts empty.
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    # Save the last-known-good content (cancellation_requested=True) before
    # corrupting it -- used to "repair" the file later in this same test.
    batch_file = tmp_path / "batches" / f"{batch_a.id}.json"
    good_content = batch_file.read_text(encoding="utf-8")
    batch_file.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="core.batches.service"):
        report = run_startup_recovery(
            job_repository, services["job_service"], services["completion_converger"],
            batch_service=batch_service,
        )

    assert report.batch_cancellation_scan_was_fully_reliable is False
    assert report.queued_enqueue_skipped_due_to_unreliable_batch_scan is True
    assert poison_job_id not in report.requeued

    # Observability follow-up (adversarial self-review): the malformed
    # batch's own id must be named in a warning, not just silently
    # suppress the whole generic sweep with no trace of why.
    assert any(
        batch_a.id in record.getMessage() for record in caplog.records
    )

    # The poisoned batch's child never becomes worker-visible; the
    # healthy batch's own child still recovers via its own per-batch
    # resume (independent of the suppressed generic sweep).
    _drain_queue(services)
    assert services["generator"].calls == 1
    assert job_repository.get(poison_job_id).status == "queued"  # untouched
    assert job_repository.get(healthy_job_id).status == "succeeded"

    # Repair the batch file -- the next recovery pass observes and applies
    # the cancellation intent that was there all along.
    batch_file.write_text(good_content, encoding="utf-8")

    second = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert second.batch_cancellation_scan_was_fully_reliable is True
    assert job_repository.get(poison_job_id).status in ("cancel_requested", "cancelled")


# --- PR3 exact-HEAD audit, eighth round, finding 1: treat directory scan
# errors as unreliable ---------------------------------------------------


def test_startup_treats_a_batch_directory_enumeration_failure_as_unreliable(
    tmp_path, monkeypatch
):
    """`Path.glob()` internally uses `os.scandir()`, which can itself
    raise a transient `OSError` while enumerating the batch directory (a
    temporary permission, mount, or I/O failure) -- but `glob()` silently
    swallows exactly that and yields zero entries, indistinguishable from
    "this directory is simply empty."

    A batch whose durable `cancellation_requested=True` was persisted
    before the batch DIRECTORY itself (not just one file in it) becomes
    transiently unreadable must not have that intent silently missed --
    the generic queued-job sweep must stay suppressed until the directory
    is readable again (PR3 exact-HEAD audit, eighth round, finding 1).
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch_a = batch_service.create_batch(
        BatchSpec(name="hidden-by-scan-failure", media_type="image", model_id="fake",
                   prompt="x", limit=1)
    )
    poison_job_id = batch_a.items[0].job_id
    batch_b = batch_service.create_batch(
        BatchSpec(name="healthy", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    healthy_job_id = batch_b.items[0].job_id

    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    services["batch_repository"].mutate(batch_a.id, _mark_cancellation_requested_only)
    assert job_repository.get(poison_job_id).status == "queued"

    # Simulate a fresh restart: the in-memory queue starts empty.
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    # Persists for the entire pass -- both `resume_pending_cancellations()`
    # and `resume_current_stage_for_all_batches()` (its own separate
    # bounded-retry scan) call `list_all_tolerant()` independently, so a
    # failure that only fired once could be "used up" by whichever of the
    # two happens to scan first, letting the other see a healthy
    # directory. A real directory-level outage (a mount hiccup, a
    # permission change) does not clear itself mid-attempt.
    batch_dir = tmp_path / "batches"
    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if os.fspath(path) == os.fspath(batch_dir):
            raise OSError("injected: transient directory enumeration failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)

    report = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert report.batch_cancellation_scan_was_fully_reliable is False
    assert report.queued_enqueue_skipped_due_to_unreliable_batch_scan is True
    assert poison_job_id not in report.requeued

    # Neither batch's child becomes worker-visible this pass -- the
    # entire directory was invisible to this scan, not just batch_a's
    # own file, so batch_b's own per-batch resume path never even saw
    # batch_b either.
    _drain_queue(services)
    assert services["generator"].calls == 0
    assert job_repository.get(poison_job_id).status == "queued"  # untouched
    assert job_repository.get(healthy_job_id).status == "queued"  # untouched

    monkeypatch.undo()  # the directory becomes readable again

    second = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert second.batch_cancellation_scan_was_fully_reliable is True
    assert job_repository.get(poison_job_id).status in ("cancel_requested", "cancelled")
    _drain_queue(services)
    assert job_repository.get(healthy_job_id).status == "succeeded"


# --- PR3 exact-HEAD audit, fourth round, finding 2: reflect quarantined
# child status in its batch ---------------------------------------------


def test_startup_reflects_a_quarantined_terminal_status_into_its_batch_item(tmp_path):
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(name="quarantine-me", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()  # simulate a fresh restart's empty queue

    # Corrupt the persisted payload so the row can no longer be decoded.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()

    # Startup's own step 1 quarantines it: raw status -> "failed", payload
    # deliberately left broken.
    run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert _raw_status(services["db_path"], job_id) == "failed"
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job_id)

    # The batch item reflects that quarantined terminal outcome instead of
    # staying stuck at its stale pre-quarantine "queued"/"pending" status.
    refreshed = batch_service.get_batch(batch.id)
    assert refreshed is not None
    assert refreshed.items[0].status == "failed"
    assert refreshed.status == "failed"

    # Repeated reconciliation is idempotent.
    refreshed_again = batch_service.get_batch(batch.id)
    assert refreshed_again.items[0].status == "failed"
    assert refreshed_again.status == "failed"


# --- PR3 exact-HEAD audit, eighth round, finding 4: reconcile batches
# after poison quarantine -------------------------------------------------


def test_startup_advances_a_batch_stage_after_a_successful_poison_quarantine(
    tmp_path,
):
    """A successful poison quarantine transitions a row directly via a
    raw SQL CAS -- it publishes no terminal event and runs no completion/
    Batch reconciliation of its own. If that row was the LAST nonterminal
    item in its stage, and a sibling item in the same stage already
    reached `completion_state="done"`, the owning Batch must still get
    the same convergence opportunity (including a stage-advance attempt)
    an ordinary terminal transition receives -- not just the plain,
    non-advancing `reconcile()` startup's own step 5 already applies to
    every batch (PR3 exact-HEAD audit, eighth round, finding 4).
    """

    from core.batches.schemas import Axis, AxisValue, BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="poison-advance", media_type="image", model_id="fake", prompt="x",
            axes=[
                Axis(
                    name="variant",
                    values=[
                        AxisValue(label="v1", patch={"prompt": "winner"}),
                        AxisValue(label="v2", patch={"prompt": "poisoned"}),
                    ],
                )
            ],
            stages=[{"name": "probe"}, {"name": "refine"}],
        )
    )
    winner_job_id, poison_job_id = (item.job_id for item in batch.items)
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    # Sibling winner: drive directly to a genuine succeeded outcome, then
    # let it fully converge (completion_state="done") -- exactly the
    # "sibling winner already completion-done" the finding describes.
    job_repository.update_status(winner_job_id, "preparing")
    job_repository.update_status(winner_job_id, "running")
    job_repository.update_status(winner_job_id, "postprocessing")
    job_repository.update(
        winner_job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=winner_job_id, status="succeeded", outputs=["a.png"]),
    )
    services["completion_converger"].converge_job(winner_job_id)
    assert job_repository.get(winner_job_id).completion_state == "done"

    # Poison child: still genuinely "queued" -- the last nonterminal item
    # in this stage. Corrupt its payload so list_tolerant() can no
    # longer decode it, exactly the crash window startup's own poison
    # scan exists to close.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison_job_id),
        )
        raw.commit()

    report = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert report.poison_rows[poison_job_id] == "failed"
    assert _raw_status(services["db_path"], poison_job_id) == "failed"

    # The Batch must have advanced past its old "probe" stage -- not
    # stuck "running" on it forever -- and materialized its "refine"
    # stage from the winning sibling.
    advanced = batch_service.get_batch(batch.id)
    assert advanced.stage_index == 1
    refine_items = [item for item in advanced.items if item.stage_index == 1]
    assert len(refine_items) == 1
    assert refine_items[0].job_id is not None
    assert job_repository.get(refine_items[0].job_id) is not None

    # Repeated startup recovery is idempotent -- does not re-advance or
    # duplicate the refine stage's own child.
    run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )
    settled = batch_service.get_batch(batch.id)
    assert settled.stage_index == 1
    assert len([item for item in settled.items if item.stage_index == 1]) == 1


def test_quarantine_reflection_preserves_a_real_pre_existing_error_message(tmp_path):
    """A child job that legitimately failed (with a real, specific
    `error_message` already recorded) and only *later* becomes undecodable
    for an unrelated reason must keep that real diagnostic message.

    `get_raw_status()` deliberately reads only the `status` column, never
    `error_message` -- it cannot tell "this row was PR #397-quarantined
    with no real message" apart from "this row failed normally, with a
    real message, and only later became undecodable". Unconditionally
    overwriting `item.error_message` with the generic "(quarantined)"
    placeholder would destroy real diagnostic information that is still
    sitting in the DB (PR3 exact-HEAD audit, fourth round, adversarial
    self-review of this round's own finding-2 fix).
    """
    from core.batches.schemas import BatchSpec

    real_message = "injected: out of disk space while writing output"
    services = _build_services(tmp_path, generator=FailingGenerator(real_message))
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(name="legit-failure", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id

    # The job genuinely fails (not via quarantine) and the batch reconciles
    # normally, capturing the real error message.
    _drain_queue(services)
    assert job_repository.get(job_id).status == "failed"
    assert job_repository.get(job_id).error_message == real_message

    reconciled = batch_service.get_batch(batch.id)
    assert reconciled.items[0].status == "failed"
    assert reconciled.items[0].error_message == real_message

    # Only *afterwards* -- unrelated to the original failure -- does the
    # row's payload become undecodable.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()
    assert _raw_status(services["db_path"], job_id) == "failed"
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job_id)

    # The real error message must survive -- not be replaced by the
    # generic quarantine placeholder.
    refreshed = batch_service.get_batch(batch.id)
    assert refreshed.items[0].status == "failed"
    assert refreshed.items[0].error_message == real_message


def test_quarantine_reflection_recovers_the_real_message_on_the_very_first_reconcile(
    tmp_path,
):
    """Even on the *first* reconcile pass ever to observe a row after it
    became undecodable -- so the batch item's own cached `error_message`
    is still its pristine, never-populated default -- the real message
    already sitting in the job's DB row must still be recovered, not
    replaced by the generic placeholder.

    Simulates a job reaching a real terminal failure through a path that
    never notified this batch at all (e.g. a crash between persisting the
    terminal row and the in-memory event bus delivering it -- exactly the
    class of gap `BatchService`'s own docstring already describes for
    `reconcile_child_job()`), with the row's payload becoming undecodable
    before any reconcile ever ran while it was still readable.

    Found via adversarial review of this round's own first attempt at
    this fix: that version fell back to the batch item's own possibly-
    stale *cached* `error_message` (a falsy check) rather than reading
    the DB's authoritative `error_message` column directly -- which
    happens to work once a normal reconcile has already run at least
    once while the row was still decodable (see the test right above
    this one), but loses the real message entirely on a first-ever
    reconcile like this one.
    """
    from core.batches.schemas import BatchSpec

    real_message = "injected: model checkpoint failed to load"
    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(name="legit-failure-no-event", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()  # never actually run by JobRunner

    # The job reaches a real terminal failure with a real message, but
    # through a path that never notifies this batch (no event bus
    # delivery) -- and its payload becomes undecodable before any
    # reconcile ever observes it. `item.error_message` is therefore still
    # its pristine, never-populated default when the batch is first
    # reconciled below.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET status = ?, error_message = ?, request_json = ? "
            "WHERE id = ?",
            ("failed", real_message, "{not valid json", job_id),
        )
        raw.commit()
    assert _raw_status(services["db_path"], job_id) == "failed"
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job_id)

    # The real message -- read directly from the DB's own
    # `error_message` column, never from the batch item's own
    # never-populated cache -- must be recovered here.
    refreshed = batch_service.get_batch(batch.id)
    assert refreshed.items[0].status == "failed"
    assert refreshed.items[0].error_message == real_message


def test_quarantine_reflection_preserves_a_legitimately_empty_error_message(tmp_path):
    """An `error_message` column that was legitimately recorded as an
    empty string (a generator that raised a message-less exception --
    `JobRunner` persists `error_message=str(exc)`, which is `""` for
    `raise RuntimeError()`) is still a real, recorded outcome and must
    not be replaced by the generic quarantine placeholder.

    Found via adversarial review of this round's own fix: naively
    treating "falsy" (`get_raw_error_message() or placeholder`) as
    equivalent to "never recorded" (`None`) would conflate the two,
    since `""` is also falsy -- the fix must check `is not None`
    specifically.
    """
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path, generator=FailingGenerator(""))
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(name="legit-empty-message", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id

    _drain_queue(services)
    assert job_repository.get(job_id).status == "failed"
    assert job_repository.get(job_id).error_message == ""

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job_id)

    # The legitimately-empty message must survive as `""`, not be
    # replaced by the generic placeholder text.
    refreshed = batch_service.get_batch(batch.id)
    assert refreshed.items[0].status == "failed"
    assert refreshed.items[0].error_message == ""


# --- PR3 exact-HEAD audit, fifth round, finding 4: validate raw error text
# before saving the batch -----------------------------------------------------


def test_quarantine_reflection_survives_a_non_text_raw_error_message(tmp_path):
    """An undecodable terminal Job whose raw `error_message` column holds
    non-text data (e.g. an invalid-UTF-8 `BLOB`, which SQLite's TEXT
    affinity does not reject) must not break Batch reconciliation or its
    JSON serialization.

    Before this fix, the raw value was assigned straight into
    `BatchItem.error_message` (a `str | None` field) with no type check;
    a non-`str` value would only fail much later, at
    `BatchRecord.model_dump(mode="json")` -- aborting the very
    reconciliation pass that was trying to recover this row, and (via
    `run_startup_recovery()`'s own batch reconcile pass) capable of
    repeating on every future restart until the row was fixed by hand
    (PR3 exact-HEAD audit, fifth round, finding 4).
    """
    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(name="blob-message", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()  # never actually run by JobRunner

    invalid_utf8_blob = b"\xff\xfe\x00not valid utf-8"
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET status = ?, error_message = ?, request_json = ? "
            "WHERE id = ?",
            ("failed", invalid_utf8_blob, "{not valid json", job_id),
        )
        raw.commit()
    assert _raw_status(services["db_path"], job_id) == "failed"
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job_id)

    # Must not raise -- neither on reconciliation nor on the JSON
    # serialization `batch_repository.mutate()` performs to persist it.
    refreshed = batch_service.get_batch(batch.id)

    assert refreshed.items[0].status == "failed"
    assert isinstance(refreshed.items[0].error_message, str)
    assert refreshed.items[0].error_message != invalid_utf8_blob

    # The Batch record on disk is valid JSON -- serialization genuinely
    # succeeded, not merely avoided raising in-memory.
    batch_file = tmp_path / "batches" / f"{batch.id}.json"
    json.loads(batch_file.read_text(encoding="utf-8"))

    # Idempotent on repeat, and startup recovery as a whole does not abort.
    report = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )
    assert report is not None
    assert batch_service.get_batch(batch.id).items[0].status == "failed"


# --- PR3 exact-HEAD audit, seventh round, finding 4: keep failed poison
# quarantines scheduled for retry ---------------------------------------------


def test_transient_quarantine_failure_is_retried_and_resolved_in_process(
    tmp_path, monkeypatch
):
    """A poison row whose startup quarantine *write* itself fails
    transiently must not be stranded for the rest of this process's
    lifetime -- its raw status stays `queued`, invisible to every other
    retry mechanism (`list_tolerant()` can't decode it, `list_terminal_
    pending_completion()` only sees terminal rows). It must be retained
    as a retry candidate and resolved by a later `run_retry_loop()` tick,
    reusing the exact same `quarantine_poison_row_safely()` primitive
    startup recovery itself uses (PR3 exact-HEAD audit, seventh round,
    finding 4).
    """

    services = _build_services(tmp_path)
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    job = _seed(job_repository, "queued", "job_poison")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", job.id)
        )
        raw.commit()
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job.id)

    # The quarantine *write* itself (not the read) fails transiently, on
    # this first attempt only.
    real_transition = job_repository.transition_if_status
    attempts = {"count": 0}

    def flaky_transition(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("injected: transient write failure")
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(job_repository, "transition_if_status", flaky_transition)

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
    )

    assert report.poison_rows[job.id] == "transient_write_failure"
    assert job.id in completion_converger._poison_retry_candidates
    assert _raw_status(services["db_path"], job.id) == "queued"  # unresolved so far

    monkeypatch.undo()  # storage recovers

    # One retry-loop tick, deterministically: stop_event is set as a side
    # effect of this tick's own list_terminal_pending_completion() call
    # (there is nothing pending -- this job's raw status is still
    # "queued", not terminal -- so the poison-retry block below still
    # runs this same tick regardless), so Event.wait() returns
    # immediately with no real sleep.
    from threading import Event
    stop_event = Event()
    original_list_pending = job_repository.list_terminal_pending_completion

    def list_pending_then_stop():
        result = original_list_pending()
        stop_event.set()
        return result

    monkeypatch.setattr(
        job_repository, "list_terminal_pending_completion", list_pending_then_stop
    )

    completion_converger.run_retry_loop(stop_event=stop_event, poll_interval_seconds=0)

    assert job.id not in completion_converger._poison_retry_candidates
    assert _raw_status(services["db_path"], job.id) == "failed"

    _drain_queue(services)
    assert services["generator"].calls == 0  # never re-run


def test_poison_retry_candidate_stays_scheduled_while_still_transient(
    tmp_path, monkeypatch
):
    """A retry attempt that *also* fails transiently must leave the job id
    scheduled for yet another attempt, not silently drop it.
    """

    services = _build_services(tmp_path)
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    job = _seed(job_repository, "queued", "job_poison")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", job.id)
        )
        raw.commit()

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: still transient")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient)

    run_startup_recovery(job_repository, services["job_service"], completion_converger)
    assert job.id in completion_converger._poison_retry_candidates

    from threading import Event
    stop_event = Event()
    original_list_pending = job_repository.list_terminal_pending_completion

    def list_pending_then_stop():
        result = original_list_pending()
        stop_event.set()
        return result

    monkeypatch.setattr(
        job_repository, "list_terminal_pending_completion", list_pending_then_stop
    )

    completion_converger.run_retry_loop(stop_event=stop_event, poll_interval_seconds=0)

    # Still transient -- still scheduled, not dropped.
    assert job.id in completion_converger._poison_retry_candidates
    assert _raw_status(services["db_path"], job.id) == "queued"


def test_poison_retry_never_blocks_healthy_completion_retry_in_the_same_tick(
    tmp_path, monkeypatch
):
    """A permanently-stuck poison retry candidate must not prevent an
    unrelated, genuinely healthy completion-pending job from converging
    in the same `run_retry_loop()` tick.
    """

    services = _build_services(tmp_path)
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    poison_job = _seed(job_repository, "queued", "job_poison")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            ("not-a-timestamp", poison_job.id),
        )
        raw.commit()

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: still transient")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient)
    run_startup_recovery(job_repository, services["job_service"], completion_converger)
    assert poison_job.id in completion_converger._poison_retry_candidates
    monkeypatch.undo()

    # A healthy, unrelated job that genuinely needs completion convergence.
    healthy_job = _seed(
        job_repository, "succeeded",
        "job_healthy",
        result=GenerationResult(job_id="job_healthy", status="succeeded", outputs=["a.png"]),
    )
    assert job_repository.get(healthy_job.id).completion_state == "pending"

    from threading import Event
    stop_event = Event()
    convergence_calls = {"count": 0}
    original_converge_job = completion_converger.converge_job

    def converge_job_then_stop(job_id, **kwargs):
        result = original_converge_job(job_id, **kwargs)
        convergence_calls["count"] += 1
        stop_event.set()
        return result

    monkeypatch.setattr(completion_converger, "converge_job", converge_job_then_stop)

    # Re-inject the transient failure so the poison retry in this tick
    # fails again too -- proving it does not block the healthy job.
    def always_transient_again(*args, **kwargs):
        raise sqlite3.OperationalError("injected: still transient")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient_again)

    completion_converger.run_retry_loop(stop_event=stop_event, poll_interval_seconds=0)

    assert convergence_calls["count"] == 1
    assert job_repository.get(healthy_job.id).completion_state == "done"
    # The poison candidate is unaffected either way -- still scheduled.
    assert poison_job.id in completion_converger._poison_retry_candidates


# --- PR3 exact-HEAD audit, eighth round, finding 5: revalidate poison rows
# before retrying quarantine ---------------------------------------------


def _one_retry_tick(job_repository, completion_converger):
    """Run exactly one `run_retry_loop()` tick, deterministically -- the
    stop_event is set as a side effect of this tick's own
    `list_terminal_pending_completion()` call, so `Event.wait()` returns
    immediately with no real sleep.
    """

    from threading import Event

    stop_event = Event()
    original_list_pending = job_repository.list_terminal_pending_completion

    def list_pending_then_stop():
        result = original_list_pending()
        stop_event.set()
        return result

    original_attr = job_repository.list_terminal_pending_completion
    job_repository.list_terminal_pending_completion = list_pending_then_stop
    try:
        completion_converger.run_retry_loop(stop_event=stop_event, poll_interval_seconds=0)
    finally:
        job_repository.list_terminal_pending_completion = original_attr


def test_poison_retry_revalidates_and_resumes_normal_processing_for_a_repaired_row(
    tmp_path, monkeypatch
):
    """A poison-retry candidate that turns out to have been repaired (an
    operator or concurrent process fixed the payload) while its raw
    status is still genuinely `queued` must NOT be quarantined -- the
    stale original decode exception must not be blindly reused against a
    now-perfectly-valid row. It must instead be dropped as a candidate
    and handed back to the existing, safe `enqueue_job()` path so it
    resumes ordinary queued processing (PR3 exact-HEAD audit, eighth
    round, finding 5).
    """

    services = _build_services(tmp_path)
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    job = _seed(job_repository, "queued", "job_repaired")
    good_created_at = job.created_at
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", job.id)
        )
        raw.commit()
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job.id)

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient)

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
    )
    assert report.poison_rows[job.id] == "transient_write_failure"
    assert job.id in completion_converger._poison_retry_candidates
    monkeypatch.undo()

    # An operator repairs the payload while status remains queued --
    # simulating a concurrent fix landing before this candidate's own
    # retry tick runs.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (good_created_at.isoformat(), job.id),
        )
        raw.commit()
    assert job_repository.get(job.id).status == "queued"

    _one_retry_tick(job_repository, completion_converger)

    # NOT quarantined -- still genuinely queued, never touched.
    assert job_repository.get(job.id).status == "queued"
    assert job.id not in completion_converger._poison_retry_candidates

    # Handed back to normal queue processing, not left un-enqueued forever
    # -- proven by actually draining the queue and watching it run, not
    # just peeking (JobQueue exposes no non-destructive peek), matching
    # `_drain_queue()`'s own "prove it via the real runner" convention.
    _drain_queue(services)
    assert services["generator"].calls == 1
    assert job_repository.get(job.id).status == "succeeded"


def test_poison_retry_still_quarantines_a_row_that_remains_genuinely_poison(
    tmp_path, monkeypatch
):
    """The revalidation step must not accidentally block a genuinely
    still-poison row from being quarantined -- it only ever short-circuits
    for a row that decodes cleanly right now.
    """

    services = _build_services(tmp_path)
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    job = _seed(job_repository, "queued", "job_still_poison")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", job.id)
        )
        raw.commit()

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient)
    run_startup_recovery(job_repository, services["job_service"], completion_converger)
    assert job.id in completion_converger._poison_retry_candidates
    monkeypatch.undo()  # only the write mechanism recovers -- the payload stays corrupt

    _one_retry_tick(job_repository, completion_converger)

    assert job.id not in completion_converger._poison_retry_candidates
    assert _raw_status(services["db_path"], job.id) == "failed"
    _drain_queue(services)
    assert services["generator"].calls == 0


def test_poison_retry_keeps_the_candidate_scheduled_on_a_transient_revalidation_read(
    tmp_path, monkeypatch
):
    """A transient repository-level read failure *during revalidation
    itself* (distinct from a decode failure) must not be misread as
    either "repaired" or "still poison" -- the candidate must simply
    stay scheduled for a later attempt.
    """

    services = _build_services(tmp_path)
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    job = _seed(job_repository, "queued", "job_flaky_revalidate")
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", job.id)
        )
        raw.commit()

    def always_transient_write(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient_write)
    run_startup_recovery(job_repository, services["job_service"], completion_converger)
    assert job.id in completion_converger._poison_retry_candidates
    monkeypatch.undo()

    # This tick's own revalidation read (job_repository.get()) hits a
    # transient repository-level failure -- not a decode failure.
    real_get = job_repository.get

    def flaky_get(job_id_arg):
        if job_id_arg == job.id:
            raise sqlite3.OperationalError("injected: transient revalidation read failure")
        return real_get(job_id_arg)

    monkeypatch.setattr(job_repository, "get", flaky_get)

    _one_retry_tick(job_repository, completion_converger)

    # Neither quarantined nor resumed -- genuinely uncertain this tick,
    # so it stays scheduled rather than guessing either way.
    assert job.id in completion_converger._poison_retry_candidates
    assert _raw_status(services["db_path"], job.id) == "queued"

    monkeypatch.undo()  # the revalidation read recovers too

    _one_retry_tick(job_repository, completion_converger)

    assert job.id not in completion_converger._poison_retry_candidates
    assert _raw_status(services["db_path"], job.id) == "failed"


# --- PR3 exact-HEAD audit, eighth round, adversarial follow-up to findings
# 1 and 4 --------------------------------------------------------------


def test_poison_retry_advances_a_batch_stage_after_a_successful_quarantine_via_retry_loop(
    tmp_path, monkeypatch
):
    """A poison quarantine that succeeds via the *runtime retry loop*
    (`_retry_poison_quarantine_candidates()`), not the startup pass, must
    give its owning Batch the identical stage-advance opportunity
    `run_startup_recovery()`'s own quarantine attempts already get
    (finding 4). Without this, a Batch whose last poison child happened
    to resolve here instead of at startup would stay stuck on its old
    stage for the rest of the process's life -- exactly the gap an
    adversarial self-review of this round's own finding-4 fix surfaced,
    since finding 4's `QUARANTINE_OUTCOMES_NEEDING_BATCH_CONVERGENCE`
    follow-up was originally wired only into `run_startup_recovery()`'s
    step 1, not into this sibling call site of the exact same
    `quarantine_poison_row_safely()` primitive.
    """

    from core.batches.schemas import Axis, AxisValue, BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="poison-advance-via-retry-loop",
            media_type="image",
            model_id="fake",
            prompt="x",
            axes=[
                Axis(
                    name="variant",
                    values=[
                        AxisValue(label="v1", patch={"prompt": "winner"}),
                        AxisValue(label="v2", patch={"prompt": "poisoned"}),
                    ],
                )
            ],
            stages=[{"name": "probe"}, {"name": "refine"}],
        )
    )
    winner_job_id, poison_job_id = (item.job_id for item in batch.items)
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    job_repository.update_status(winner_job_id, "preparing")
    job_repository.update_status(winner_job_id, "running")
    job_repository.update_status(winner_job_id, "postprocessing")
    job_repository.update(
        winner_job_id,
        status="succeeded",
        progress=1.0,
        result=GenerationResult(job_id=winner_job_id, status="succeeded", outputs=["a.png"]),
    )
    completion_converger.converge_job(winner_job_id)
    assert job_repository.get(winner_job_id).completion_state == "done"

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison_job_id),
        )
        raw.commit()

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient)

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )
    assert report.poison_rows[poison_job_id] == "transient_write_failure"
    assert poison_job_id in completion_converger._poison_retry_candidates
    # Quarantine has not actually succeeded yet -- the Batch must not
    # have advanced.
    assert batch_service.get_batch(batch.id).stage_index == 0
    monkeypatch.undo()

    _one_retry_tick(job_repository, completion_converger)

    assert _raw_status(services["db_path"], poison_job_id) == "failed"
    assert poison_job_id not in completion_converger._poison_retry_candidates

    advanced = batch_service.get_batch(batch.id)
    assert advanced.stage_index == 1
    refine_items = [item for item in advanced.items if item.stage_index == 1]
    assert len(refine_items) == 1
    assert refine_items[0].job_id is not None
    assert job_repository.get(refine_items[0].job_id) is not None


def test_startup_step_5_batch_reconcile_pass_reports_a_directory_scan_failure_as_unreliable(
    tmp_path, monkeypatch
):
    """`run_startup_recovery()` step 5's own backstop reconcile pass --
    covering a Batch whose children were already fully converged by an
    earlier pass but whose own on-disk record was never re-read since --
    used `BatchService.list_batches()`, which is built on `list_all()`'s
    plain `Path.glob()` scan. That scan's internal `os.scandir()`
    silently swallows a directory-level `OSError` and yields zero
    entries, indistinguishable from "no batches exist" -- the same
    finding-1 bug, on a second call site (PR3 exact-HEAD audit, eighth
    round, adversarial follow-up). Fixed by switching step 5 to
    `list_batches_tolerant()` and recording the outcome on
    `report.batch_reconcile_scan_was_fully_reliable` rather than treating
    an unreliable scan as "nothing needed reconciling."
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]

    batch = batch_service.create_batch(
        BatchSpec(name="missed-by-step-4", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    job_repository.update_status(job_id, "preparing")
    job_repository.update_status(job_id, "running")
    job_repository.update_status(job_id, "postprocessing")
    job_repository.update(
        job_id,
        status="succeeded",
        progress=1.0,
        result=GenerationResult(job_id=job_id, status="succeeded", outputs=["a.png"]),
    )
    services["completion_converger"].converge_job(job_id)
    assert job_repository.get(job_id).completion_state == "done"

    batch_dir = tmp_path / "batches"
    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if os.fspath(path) == os.fspath(batch_dir):
            raise OSError("injected: transient directory enumeration failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)

    report = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )

    assert report.batch_reconcile_scan_was_fully_reliable is False

    monkeypatch.undo()

    second = run_startup_recovery(
        job_repository, services["job_service"], services["completion_converger"],
        batch_service=batch_service,
    )
    assert second.batch_reconcile_scan_was_fully_reliable is True


# --- PR3 exact-HEAD audit, ninth round, finding 1: recheck Batch
# cancellation before re-enqueuing repaired jobs -------------------------


def _register_poison_batch_child_candidate(services, job_id):
    """Corrupt `job_id`'s row and, via one `run_startup_recovery()` pass
    with a transient quarantine-write failure injected, register it as a
    poison-retry candidate -- the exact same setup round eight's own
    poison-retry tests use, factored out since finding 1's two new tests
    both need it as their starting point.
    """

    job_repository = services["job_repository"]
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", job_id)
        )
        raw.commit()
    with pytest.raises(JobRecordDecodeError):
        job_repository.get(job_id)

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    from unittest.mock import patch

    with patch.object(job_repository, "transition_if_status", always_transient):
        run_startup_recovery(
            job_repository, services["job_service"], services["completion_converger"],
            batch_service=services["batch_service"],
        )
    assert job_id in services["completion_converger"]._poison_retry_candidates


def _repair_job_created_at(services, job_id, good_created_at):
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (good_created_at.isoformat(), job_id),
        )
        raw.commit()


def test_poison_retry_reauthorizes_a_repaired_child_against_durable_cancellation(
    tmp_path,
):
    """A poison-retry candidate that turns out to be repaired must not be
    handed straight to `JobService.enqueue_job()` if it is a Batch
    child: that would bypass the durable-cancellation authorization
    boundary `BatchRepository.run_exclusive()`/`_authorize_and_expose()`
    already established, making the child worker-visible again even
    though its owning Batch's `cancellation_requested` is durably set
    (PR3 exact-HEAD audit, ninth round, finding 1) -- exactly the
    scenario a startup cancellation sweep could not itself apply to this
    child, since that scan only ever sees decodable rows.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch_repository = services["batch_repository"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="cancel-repair", media_type="image", model_id="fake", prompt="x",
            limit=1,
        )
    )
    job_id = batch.items[0].job_id
    good_created_at = job_repository.get(job_id).created_at
    services["job_queue"].dequeue()

    _register_poison_batch_child_candidate(services, job_id)

    # The Batch's cancellation intent becomes durable while its child was
    # still poison -- exactly why a startup cancellation sweep could not
    # itself terminalize/expose this child.
    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_repository.mutate(batch.id, _mark_cancellation_requested_only)

    # An operator repairs the payload while raw status stays queued.
    _repair_job_created_at(services, job_id, good_created_at)
    assert job_repository.get(job_id).status == "queued"

    _one_retry_tick(job_repository, completion_converger)

    # Never exposed to a worker -- Batch cancellation wins.
    _drain_queue(services)
    assert services["generator"].calls == 0
    assert job_repository.get(job_id).status == "cancelled"
    # A definitive outcome ("cancelled" is not "uncertain") -- the
    # candidate resolves, it does not stay scheduled forever.
    assert job_id not in completion_converger._poison_retry_candidates

    # Idempotent: a second tick (nothing left to do) changes nothing.
    _one_retry_tick(job_repository, completion_converger)
    _drain_queue(services)
    assert services["generator"].calls == 0
    assert job_repository.get(job_id).status == "cancelled"


def test_poison_retry_still_authorizes_a_repaired_child_of_a_healthy_batch(
    tmp_path,
):
    """The new Batch-authorization step for a repaired queued job must
    not block a perfectly healthy (not cancelling) Batch child -- it is
    authorized and enqueued exactly once, and runs to completion exactly
    like an ordinary queued job would (PR3 exact-HEAD audit, ninth
    round, finding 1's own required "healthy Batch child" case).
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="healthy-repair", media_type="image", model_id="fake", prompt="x",
            limit=1,
        )
    )
    job_id = batch.items[0].job_id
    good_created_at = job_repository.get(job_id).created_at
    services["job_queue"].dequeue()

    _register_poison_batch_child_candidate(services, job_id)

    # No cancellation this time -- the Batch is perfectly healthy.
    _repair_job_created_at(services, job_id, good_created_at)
    assert job_repository.get(job_id).status == "queued"

    _one_retry_tick(job_repository, completion_converger)

    assert job_id not in completion_converger._poison_retry_candidates
    _drain_queue(services)
    assert services["generator"].calls == 1
    assert job_repository.get(job_id).status == "succeeded"


# --- PR3 exact-HEAD audit, ninth round, finding 3: retain poison
# candidates when Batch reconciliation is retryable -----------------------


def test_poison_retry_keeps_the_candidate_when_batch_reconciliation_is_retryable(
    tmp_path, monkeypatch
):
    """A poison row whose quarantine succeeds via the runtime retry loop
    must not be dropped as a candidate if the follow-up Batch
    reconciliation itself comes back `RETRYABLE_FAILURE` (the owning
    Batch could not be read just now) -- a terminalized-but-still-
    undecodable row has no other path back to Batch convergence: it is
    invisible to `list_terminal_pending_completion()` (still undecodable
    as a whole `JobRecord`) and published no terminal event of its own
    (PR3 exact-HEAD audit, ninth round, finding 3). Dropping the
    candidate here would strand the owning Batch's convergence
    indefinitely, exactly the class of gap finding 4 of the eighth round
    closed for the *startup* quarantine path -- this is the analogous
    gap for a *successful* quarantine whose own Batch-convergence
    follow-up itself fails transiently.
    """

    from core.batches.schemas import Axis, AxisValue, BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="poison-reconcile-retryable", media_type="image", model_id="fake",
            prompt="x",
            axes=[
                Axis(
                    name="variant",
                    values=[
                        AxisValue(label="v1", patch={"prompt": "winner"}),
                        AxisValue(label="v2", patch={"prompt": "poisoned"}),
                    ],
                )
            ],
            stages=[{"name": "probe"}, {"name": "refine"}],
        )
    )
    winner_job_id, poison_job_id = (item.job_id for item in batch.items)
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    job_repository.update_status(winner_job_id, "preparing")
    job_repository.update_status(winner_job_id, "running")
    job_repository.update_status(winner_job_id, "postprocessing")
    job_repository.update(
        winner_job_id,
        status="succeeded",
        progress=1.0,
        result=GenerationResult(job_id=winner_job_id, status="succeeded", outputs=["a.png"]),
    )
    completion_converger.converge_job(winner_job_id)
    assert job_repository.get(winner_job_id).completion_state == "done"

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison_job_id),
        )
        raw.commit()

    def always_transient_write(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient_write)
    run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )
    assert poison_job_id in completion_converger._poison_retry_candidates
    monkeypatch.undo()  # quarantine writes recover; the payload stays corrupt

    # The follow-up Batch reconciliation itself now fails transiently --
    # distinct from the quarantine write, which just succeeded above.
    real_reconcile_child_job = batch_service.reconcile_child_job

    def flaky_reconcile_child_job(job_id_arg):
        if job_id_arg == poison_job_id:
            from core.batches.service import BatchReconciliationOutcome

            return None, BatchReconciliationOutcome.RETRYABLE_FAILURE
        return real_reconcile_child_job(job_id_arg)

    monkeypatch.setattr(batch_service, "reconcile_child_job", flaky_reconcile_child_job)

    _one_retry_tick(job_repository, completion_converger)

    # Quarantine itself succeeded (raw status is terminal)...
    assert _raw_status(services["db_path"], poison_job_id) == "failed"
    # ...but the candidate must stay scheduled: Batch convergence for
    # this row is not yet definitive.
    assert poison_job_id in completion_converger._poison_retry_candidates
    # No stage advance yet either -- reconciliation never actually ran.
    assert batch_service.get_batch(batch.id).stage_index == 0

    monkeypatch.undo()  # Batch reconciliation recovers too

    _one_retry_tick(job_repository, completion_converger)

    # Now definitive -- reconciliation actually ran and the candidate
    # resolves.
    assert poison_job_id not in completion_converger._poison_retry_candidates
    advanced = batch_service.get_batch(batch.id)
    assert advanced.stage_index == 1
    refine_items = [item for item in advanced.items if item.stage_index == 1]
    assert len(refine_items) == 1
    assert refine_items[0].job_id is not None
    assert job_repository.get(refine_items[0].job_id) is not None

    # Idempotent repeat.
    _one_retry_tick(job_repository, completion_converger)
    settled = batch_service.get_batch(batch.id)
    assert settled.stage_index == 1
    assert len([item for item in settled.items if item.stage_index == 1]) == 1


# --- PR3 exact-HEAD audit, ninth round, adversarial follow-up to finding
# 3: retain poison candidates when the *startup* Batch-convergence
# follow-up is retryable ---------------------------------------------------


def test_startup_registers_a_poison_retry_candidate_when_batch_reconciliation_is_retryable(
    tmp_path, monkeypatch
):
    """`run_startup_recovery()`'s own step-1 quarantine-success Batch-
    convergence follow-up (added eighth round, finding 4) discarded
    `reconcile_child_job()`'s outcome unconditionally -- the identical
    bug this round's finding 3 fixed for the *runtime retry loop*'s
    sibling call site, just not applied here (found via this round's
    own required adversarial self-review, same failure boundary as
    finding 3). A `RETRYABLE_FAILURE` outcome at startup (the owning
    Batch could not be read just now) must not be silently lost: the
    row's payload stays undecodable (only its `status` column was ever
    flipped by the quarantine CAS), so without an explicit retry
    candidate it is invisible to every other retry mechanism and the
    owning Batch would stay stuck on its old stage forever. Fixed by
    registering it as a poison-retry candidate on `RETRYABLE_FAILURE`,
    reusing the exact same mechanism the eighth round already built for
    a quarantine *write* failure.
    """

    from core.batches.schemas import Axis, AxisValue, BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="startup-poison-reconcile-retryable", media_type="image",
            model_id="fake", prompt="x",
            axes=[
                Axis(
                    name="variant",
                    values=[
                        AxisValue(label="v1", patch={"prompt": "winner"}),
                        AxisValue(label="v2", patch={"prompt": "poisoned"}),
                    ],
                )
            ],
            stages=[{"name": "probe"}, {"name": "refine"}],
        )
    )
    winner_job_id, poison_job_id = (item.job_id for item in batch.items)
    services["job_queue"].dequeue()
    services["job_queue"].dequeue()

    job_repository.update_status(winner_job_id, "preparing")
    job_repository.update_status(winner_job_id, "running")
    job_repository.update_status(winner_job_id, "postprocessing")
    job_repository.update(
        winner_job_id,
        status="succeeded",
        progress=1.0,
        result=GenerationResult(job_id=winner_job_id, status="succeeded", outputs=["a.png"]),
    )
    completion_converger.converge_job(winner_job_id)
    assert job_repository.get(winner_job_id).completion_state == "done"

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison_job_id),
        )
        raw.commit()

    # Quarantine itself succeeds; the follow-up Batch reconciliation
    # fails transiently right at the moment startup tries it.
    real_reconcile_child_job = batch_service.reconcile_child_job

    def flaky_reconcile_child_job(job_id_arg):
        if job_id_arg == poison_job_id:
            from core.batches.service import BatchReconciliationOutcome

            return None, BatchReconciliationOutcome.RETRYABLE_FAILURE
        return real_reconcile_child_job(job_id_arg)

    monkeypatch.setattr(batch_service, "reconcile_child_job", flaky_reconcile_child_job)

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )

    assert report.poison_rows[poison_job_id] == "failed"
    assert _raw_status(services["db_path"], poison_job_id) == "failed"
    # Must not be silently lost -- Batch reconciliation for this row
    # never actually happened yet.
    assert poison_job_id in completion_converger._poison_retry_candidates
    assert batch_service.get_batch(batch.id).stage_index == 0

    monkeypatch.undo()  # Batch reconciliation recovers

    _one_retry_tick(job_repository, completion_converger)

    assert poison_job_id not in completion_converger._poison_retry_candidates
    advanced = batch_service.get_batch(batch.id)
    assert advanced.stage_index == 1
    refine_items = [item for item in advanced.items if item.stage_index == 1]
    assert len(refine_items) == 1
    assert refine_items[0].job_id is not None
    assert job_repository.get(refine_items[0].job_id) is not None


# --- PR3 exact-HEAD audit, tenth round, finding 2: isolate exceptions
# from quarantine reconciliation -------------------------------------------


def test_startup_isolates_a_batch_reconciliation_exception_per_job(
    tmp_path, monkeypatch
):
    """A poison row's quarantine-success Batch-convergence follow-up
    (`batch_service.reconcile_child_job()`) can itself raise -- not just
    return `RETRYABLE_FAILURE` -- for example a transient `OSError` from
    `BatchRepository.save()` while persisting the recomputed Batch
    state. Before this round, such an exception was completely
    unguarded at this call site and would propagate straight out of
    `run_startup_recovery()`, aborting the entire startup pass over one
    already-terminal poison row's Batch-convergence follow-up (PR3
    exact-HEAD audit, tenth round, finding 2). A healthy, unrelated
    job's own recovery must not be blocked by it.
    """

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    from core.batches.schemas import BatchSpec

    batch = batch_service.create_batch(
        BatchSpec(name="poison-a", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    poison_job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    healthy_job = _seed(job_repository, "queued", "job_healthy_b")

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison_job_id),
        )
        raw.commit()

    real_reconcile_child_job = batch_service.reconcile_child_job

    def flaky_reconcile_child_job(job_id_arg):
        if job_id_arg == poison_job_id:
            raise OSError("injected: transient Batch save failure")
        return real_reconcile_child_job(job_id_arg)

    monkeypatch.setattr(batch_service, "reconcile_child_job", flaky_reconcile_child_job)

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )

    # startup did not raise globally -- reaching this line proves it.
    assert report.poison_rows[poison_job_id] == "failed"
    assert _raw_status(services["db_path"], poison_job_id) == "failed"
    # Registered for a later retry since Batch convergence never
    # actually completed.
    assert poison_job_id in completion_converger._poison_retry_candidates
    # Job B's own recovery was not blocked by Job A's exception.
    assert healthy_job.id in report.requeued

    monkeypatch.undo()  # storage repairs

    _drain_queue(services)
    assert services["generator"].calls == 1  # only Job B ever ran
    assert job_repository.get(healthy_job.id).status == "succeeded"
    assert _raw_status(services["db_path"], poison_job_id) == "failed"

    # Runtime retry later completes Batch reconciliation for A.
    _one_retry_tick(job_repository, completion_converger)

    assert poison_job_id not in completion_converger._poison_retry_candidates
    settled = batch_service.get_batch(batch.id)
    assert settled.status == "failed"


# --- PR3 exact-HEAD audit, tenth round, finding 3: resolve repaired
# non-queued poison candidates ---------------------------------------------


@pytest.mark.parametrize(
    "active_status,expected_final_status",
    [
        ("preparing", "failed"),
        ("running", "failed"),
        ("postprocessing", "failed"),
        ("cancel_requested", "cancelled"),
    ],
)
def test_poison_retry_resolves_a_repaired_non_queued_candidate(
    tmp_path, active_status, expected_final_status
):
    """A poison-retry candidate that turns out to decode cleanly, but
    whose CURRENT status is not `queued` (an operator/concurrent repair
    can land while the row is `preparing`/`running`/`postprocessing`/
    `cancel_requested`), must not simply be dropped: this process is not
    the worker that owned an active job before whatever crash/restart
    left it undecodable, so it is finalized `failed` (process_
    interrupted), never resumed or re-run; a `cancel_requested` row is
    finalized `cancelled` (PR3 exact-HEAD audit, tenth round, finding
    3). Reuses `run_startup_recovery()` step 3's own exact
    classification -- `INTERRUPTED_JOB_STATUSES`/`PROCESS_INTERRUPTED_
    REASON`.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="repaired-active", media_type="image", model_id="fake", prompt="x",
            limit=1,
        )
    )
    job_id = batch.items[0].job_id
    good_created_at = job_repository.get(job_id).created_at
    services["job_queue"].dequeue()

    _register_poison_batch_child_candidate(services, job_id)

    # An operator repairs the payload, but the row's raw status is left
    # at an active/cancel_requested value -- e.g. exactly where it was
    # when a crash first made it undecodable.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute("UPDATE jobs SET status = ? WHERE id = ?", (active_status, job_id))
        raw.commit()
    _repair_job_created_at(services, job_id, good_created_at)
    assert job_repository.get(job_id).status == active_status

    _one_retry_tick(job_repository, completion_converger)

    # Definitively transitioned -- never left active/cancel_requested,
    # never resumed by this process, never re-run.
    assert job_repository.get(job_id).status == expected_final_status
    assert job_id not in completion_converger._poison_retry_candidates
    _drain_queue(services)
    assert services["generator"].calls == 0

    # Batch reconciliation opportunity preserved: it is now a plain,
    # fully decodable terminal JobRecord, so the exact same
    # `converge_job()` path an ordinary terminal job takes (which the
    # runtime retry loop's own `list_terminal_pending_completion()` pass
    # would reach it through on its own) converges it normally.
    assert completion_converger.converge_job(job_id) == CompletionOutcome.DONE
    assert job_repository.get(job_id).completion_state == "done"
    settled = batch_service.get_batch(batch.id)
    assert settled.status == expected_final_status


# --- PR3 exact-HEAD audit, tenth round, finding 4: honor durable
# cancellation before quarantining poison Batch children -------------------


def test_startup_quarantines_a_cancelled_batchs_poison_queued_child_as_cancelled(
    tmp_path,
):
    """A Batch's durable `cancellation_requested` intent must win before
    a raw QUEUED poison child is classified: if the crash window is
    "Batch cancellation intent persisted, then the process crashed
    before this exact child's own cancellation could be applied," and
    this child's payload is independently malformed, quarantining it to
    `failed` (the ordinary poison-queued outcome) instead of `cancelled`
    reports an incorrect terminal Batch status and overrides a
    cancellation intent that already existed before this decision was
    ever made (PR3 exact-HEAD audit, tenth round, finding 4).
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch_repository = services["batch_repository"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="cancelled-poison-queued-child", media_type="image", model_id="fake",
            prompt="x", limit=1,
        )
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    # Durable cancellation intent persisted -- the crash happens before
    # this exact child's own cancellation is ever applied.
    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_repository.mutate(batch.id, _mark_cancellation_requested_only)
    assert job_repository.get(job_id).status == "queued"

    # The child's own payload is independently malformed.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )

    assert report.poison_rows[job_id] == "cancelled"
    assert _raw_status(services["db_path"], job_id) == "cancelled"
    settled = batch_service.get_batch(batch.id)
    assert settled.status == "cancelled"
    assert settled.cancellation_requested is True
    _drain_queue(services)
    assert services["generator"].calls == 0


def test_startup_still_quarantines_a_healthy_batchs_poison_queued_child_as_failed(
    tmp_path,
):
    """The precedence check must not affect a poison queued child whose
    owning Batch is NOT cancelling: it still resolves to the ordinary
    `failed` quarantine outcome, exactly as before this round.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="healthy-poison-queued-child", media_type="image", model_id="fake",
            prompt="x", limit=1,
        )
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )

    assert report.poison_rows[job_id] == "failed"
    assert _raw_status(services["db_path"], job_id) == "failed"


def test_startup_defers_a_poison_queued_child_when_batch_ownership_is_uncertain(
    tmp_path, monkeypatch
):
    """If Batch ownership itself cannot be confirmed right now (a
    transient directory-scan failure), the precedence check must not
    guess "not cancelled" and proceed with the ordinary failed-
    quarantine classification -- it must defer disposition entirely via
    the existing poison-retry mechanism, exactly like a transient
    quarantine write failure already does (PR3 exact-HEAD audit, tenth
    round, finding 4).
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="uncertain-poison-queued-child", media_type="image", model_id="fake",
            prompt="x", limit=1,
        )
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    with sqlite3.connect(services["db_path"]) as raw:
        good_request_json = raw.execute(
            "SELECT request_json FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()

    def flaky_find_by_job_id_or_diagnose(job_id_arg):
        return None, True  # uncertain -- a transient read failure

    monkeypatch.setattr(
        services["batch_repository"],
        "find_by_job_id_or_diagnose",
        flaky_find_by_job_id_or_diagnose,
    )

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )

    assert report.poison_rows[job_id] == "batch_cancellation_uncertain"
    # Never exposed/run, never guessed as "not cancelled" and quarantined
    # to failed.
    assert _raw_status(services["db_path"], job_id) == "queued"
    assert job_id in completion_converger._poison_retry_candidates
    _drain_queue(services)
    assert services["generator"].calls == 0

    monkeypatch.undo()  # Batch ownership becomes readable again

    # An operator also repairs the payload -- storage recovering in
    # full, not just the Batch-ownership read.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", (good_request_json, job_id)
        )
        raw.commit()
    assert job_repository.get(job_id).status == "queued"

    _one_retry_tick(job_repository, completion_converger)

    # Not owned by a cancelling Batch (the batch here never actually
    # requested cancellation) -- resolves through the normal repaired-
    # queued authorization path and runs to completion.
    _drain_queue(services)
    assert services["generator"].calls == 1
    assert job_repository.get(job_id).status == "succeeded"
    assert job_id not in completion_converger._poison_retry_candidates


# --- PR3 exact-HEAD audit, tenth round, adversarial follow-up to
# finding 4: the runtime poison-retry loop's own "still poison" branch
# shared the identical Batch-cancellation-precedence gap ------------------


def test_poison_retry_honors_durable_batch_cancellation_for_a_still_poison_queued_child(
    tmp_path, monkeypatch
):
    """A poison row can still be sitting in `_poison_retry_candidates`
    (because its quarantine *write* failed transiently at startup, or
    Batch ownership was itself uncertain on an earlier attempt) when its
    owning Batch's cancellation becomes durable sometime later, during
    ordinary runtime operation -- not only at startup. Finding 4's
    precedence check was originally added only to `run_startup_
    recovery()`'s step 1; this sibling call site
    (`CompletionConverger._retry_poison_quarantine_candidates()`'s own
    "still poison" branch) shared the identical exposure, found via this
    round's own required adversarial self-review. Blindly quarantining a
    raw QUEUED row to `failed` here would override the Batch's
    now-durable cancellation intent exactly like the original gap did.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch_repository = services["batch_repository"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="runtime-cancelled-poison-queued-child", media_type="image",
            model_id="fake", prompt="x", limit=1,
        )
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    # Registered as a retry candidate via a transient quarantine-write
    # failure at startup -- the Batch is not cancelling yet at this
    # point.
    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", job_id),
        )
        raw.commit()

    def always_transient(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient)
    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )
    assert report.poison_rows[job_id] == "transient_write_failure"
    assert job_id in completion_converger._poison_retry_candidates
    monkeypatch.undo()

    # The Batch's cancellation intent becomes durable only now, during
    # ordinary runtime operation -- the row is still genuinely poison
    # (its payload was never repaired).
    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_repository.mutate(batch.id, _mark_cancellation_requested_only)

    _one_retry_tick(job_repository, completion_converger)

    assert _raw_status(services["db_path"], job_id) == "cancelled"
    settled = batch_service.get_batch(batch.id)
    assert settled.status == "cancelled"
    _drain_queue(services)
    assert services["generator"].calls == 0


# --- PR3 exact-HEAD audit, tenth round, adversarial follow-up to
# finding 2: isolate exceptions from the new precedence-check code
# itself, not just from reconcile_child_job() --------------------------


def test_startup_isolates_a_transient_failure_from_the_cancellation_precedence_check(
    tmp_path, monkeypatch
):
    """Finding 4's new Batch-cancellation-precedence check (`get_raw_
    status()` and the atomic `diagnose_and_transition_cancelled_poison_
    child()`) was itself unguarded when first written -- a transient
    failure from any of those calls would propagate straight out of
    `run_startup_recovery()`'s step-1 loop, aborting the entire startup
    pass, exactly the class of gap finding 2 fixed for `reconcile_
    child_job()` but reintroduced by finding 4's own new code (found via
    this round's own required adversarial self-review). A healthy,
    unrelated job's own recovery must not be blocked by it.
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    batch_repository = services["batch_repository"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="flaky-precedence-check", media_type="image", model_id="fake",
            prompt="x", limit=1,
        )
    )
    job_id = batch.items[0].job_id
    services["job_queue"].dequeue()

    def _mark_cancellation_requested_only(record):
        record.cancellation_requested = True
        return record

    batch_repository.mutate(batch.id, _mark_cancellation_requested_only)

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", ("{not valid json", job_id)
        )
        raw.commit()

    healthy_job = _seed(job_repository, "queued", "job_healthy_precedence")

    real_diagnose = batch_service.diagnose_and_transition_cancelled_poison_child

    def flaky_diagnose(job_id_arg, *, reason):
        if job_id_arg == job_id:
            raise sqlite3.OperationalError("injected: transient Batch read failure")
        return real_diagnose(job_id_arg, reason=reason)

    monkeypatch.setattr(
        batch_service, "diagnose_and_transition_cancelled_poison_child", flaky_diagnose
    )

    report = run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )

    # startup did not raise globally -- reaching this line proves it.
    assert report.poison_rows[job_id] == "transient_write_failure"
    assert job_id in completion_converger._poison_retry_candidates
    # Step 1's own precedence check never guessed a disposition -- but this
    # same startup pass's step 2 (`resume_pending_cancellations()`) reaches
    # this exact child through a wholly separate, unmocked path (`cancel()`'s
    # own `_apply_cancellation_intent` closure, finding 3) and resolves it
    # correctly on its own, independent of step 1's injected failure. This
    # is defense in depth, not a guess: `cancel()` re-derives the same
    # raw-status-is-QUEUED fact directly, under its own lock, rather than
    # trusting step 1's failed attempt.
    assert _raw_status(services["db_path"], job_id) == "cancelled"
    # The healthy, unrelated job's own recovery was not blocked.
    assert healthy_job.id in report.requeued

    monkeypatch.undo()

    _one_retry_tick(job_repository, completion_converger)

    # A later retry tick, now unmocked, finds the row already terminal and
    # simply reflects that (`left_untouched_terminal`) -- it does not need
    # to, and cannot, re-derive a disposition of its own for an already-
    # resolved row.
    assert _raw_status(services["db_path"], job_id) == "cancelled"
    assert job_id not in completion_converger._poison_retry_candidates


# --- PR3 exact-HEAD audit, tenth round, adversarial follow-up to
# finding 3: a lost CAS race must keep a repaired candidate scheduled,
# not silently drop it ------------------------------------------------


def test_poison_retry_keeps_a_repaired_active_candidate_scheduled_if_its_cas_loses_a_race(
    tmp_path,
):
    """`_resolve_repaired_job()` runs from the *live* runtime retry loop
    -- unlike `run_startup_recovery()` step 3, a concurrent, genuinely
    legitimate actor (e.g. an operator's `POST /jobs/{id}/cancel`) can
    race its CAS between the revalidation read and the transition
    attempt. If that race is lost, the candidate must stay scheduled
    (not be silently dropped as "resolved") -- the next tick's own fresh
    revalidation read picks up the row's true current status and
    reclassifies it correctly (PR3 exact-HEAD audit, tenth round,
    adversarial follow-up to finding 3).
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    batch = batch_service.create_batch(
        BatchSpec(
            name="raced-repaired-active", media_type="image", model_id="fake",
            prompt="x", limit=1,
        )
    )
    job_id = batch.items[0].job_id
    good_created_at = job_repository.get(job_id).created_at
    services["job_queue"].dequeue()

    _register_poison_batch_child_candidate(services, job_id)

    with sqlite3.connect(services["db_path"]) as raw:
        raw.execute("UPDATE jobs SET status = ? WHERE id = ?", ("preparing", job_id))
        raw.commit()
    _repair_job_created_at(services, job_id, good_created_at)
    assert job_repository.get(job_id).status == "preparing"

    # Simulate a concurrent operator cancel landing in the exact gap
    # between this tick's revalidation read and its own CAS attempt: the
    # CAS this code issues (preparing -> failed) is made to lose by
    # racing the row to cancel_requested first, via a one-shot wrapper
    # around transition_if_status.
    real_transition = job_repository.transition_if_status
    fired = {"count": 0}

    def race_once_then_real(job_id_arg, expected, **kwargs):
        if job_id_arg == job_id and kwargs.get("status") == "failed" and fired["count"] == 0:
            fired["count"] += 1
            real_transition(job_id_arg, ("preparing",), status="cancel_requested")
            return real_transition(job_id_arg, expected, **kwargs)
        return real_transition(job_id_arg, expected, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(
        job_repository, "transition_if_status", side_effect=race_once_then_real
    ):
        _one_retry_tick(job_repository, completion_converger)

    # The lost race must not be silently dropped -- still scheduled.
    assert job_repository.get(job_id).status == "cancel_requested"
    assert job_id in completion_converger._poison_retry_candidates

    # A later tick, with no more races, resolves it correctly this time.
    _one_retry_tick(job_repository, completion_converger)

    assert job_repository.get(job_id).status == "cancelled"
    assert job_id not in completion_converger._poison_retry_candidates
    _drain_queue(services)
    assert services["generator"].calls == 0


# --- PR3 exact-HEAD audit, tenth round, adversarial follow-up to
# finding 2: isolate exceptions from reconcile_child_job() in the
# runtime poison-retry loop too, not only at startup -----------------


def test_poison_retry_isolates_a_batch_reconciliation_exception_per_candidate(
    tmp_path, monkeypatch
):
    """`CompletionConverger._retry_poison_quarantine_candidates()`'s own
    quarantine-success Batch-convergence follow-up can itself raise --
    not just return `RETRYABLE_FAILURE` -- exactly like the sibling
    startup call site finding 2 fixed. Before this fix, such an
    exception would propagate out of the `for` loop over ALL candidates
    this tick, meaning `self._poison_retry_candidates = still_pending`
    at the end of the method was never reached -- silently reverting
    every OTHER candidate this exact tick already resolved back to
    "still pending," not just failing to advance the one job whose
    follow-up raised (found via this round's own required adversarial
    self-review, same failure boundary as finding 2).
    """

    from core.batches.schemas import BatchSpec

    services = _build_services(tmp_path)
    batch_service = services["batch_service"]
    job_repository = services["job_repository"]
    completion_converger = services["completion_converger"]

    # Job A: will raise from its own Batch-convergence follow-up.
    batch_a = batch_service.create_batch(
        BatchSpec(name="poison-a-raises", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    poison_job_a = batch_a.items[0].job_id
    services["job_queue"].dequeue()

    # Job B: a second, independent poison candidate that must still
    # resolve correctly in the SAME tick, even though A's follow-up
    # raises.
    batch_b = batch_service.create_batch(
        BatchSpec(name="poison-b-resolves", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    poison_job_b = batch_b.items[0].job_id
    services["job_queue"].dequeue()

    def always_transient_write(*args, **kwargs):
        raise sqlite3.OperationalError("injected: transient write failure")

    monkeypatch.setattr(job_repository, "transition_if_status", always_transient_write)

    for job_id in (poison_job_a, poison_job_b):
        with sqlite3.connect(services["db_path"]) as raw:
            raw.execute(
                "UPDATE jobs SET request_json = ? WHERE id = ?",
                ("{not valid json", job_id),
            )
            raw.commit()

    run_startup_recovery(
        job_repository, services["job_service"], completion_converger,
        batch_service=batch_service,
    )
    assert poison_job_a in completion_converger._poison_retry_candidates
    assert poison_job_b in completion_converger._poison_retry_candidates
    monkeypatch.undo()

    real_reconcile_child_job = batch_service.reconcile_child_job

    def flaky_reconcile_child_job(job_id_arg):
        if job_id_arg == poison_job_a:
            raise OSError("injected: transient Batch save failure")
        return real_reconcile_child_job(job_id_arg)

    monkeypatch.setattr(batch_service, "reconcile_child_job", flaky_reconcile_child_job)

    _one_retry_tick(job_repository, completion_converger)

    # A's own follow-up raised -- it stays scheduled, quarantine itself
    # still succeeded.
    assert _raw_status(services["db_path"], poison_job_a) == "failed"
    assert poison_job_a in completion_converger._poison_retry_candidates
    # B must still have resolved in this SAME tick, not been silently
    # reverted to "still pending" by A's exception.
    assert _raw_status(services["db_path"], poison_job_b) == "failed"
    assert poison_job_b not in completion_converger._poison_retry_candidates

    monkeypatch.undo()

    _one_retry_tick(job_repository, completion_converger)
    assert poison_job_a not in completion_converger._poison_retry_candidates
