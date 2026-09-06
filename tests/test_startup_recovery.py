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
