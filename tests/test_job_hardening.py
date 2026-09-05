"""Deterministic execution-ownership and lifecycle boundary regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event

import pytest

from core.jobs import CancellationRegistry, EventBus, JobQueue, JobRunner, JobService
from core.jobs.schemas import JobRecord
from core.jobs.statuses import JOB_STATUSES, is_valid_transition
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRepository
from generators.base import BaseGenerator
from generators.registry import GeneratorRegistry


class FakeGenerator(BaseGenerator):
    def __init__(self, action=lambda context: None):
        self.action = action
        self.calls = 0

    def validate_request(self, request):
        pass

    def prepare(self, request):
        pass

    def cleanup(self, request):
        pass

    def generate(self, request, context=None):
        self.calls += 1
        self.action(context)
        return GenerationResult(job_id="fake", status="succeeded")


def seed_job(repository, status="queued", job_id="job_test"):
    now = datetime.now(timezone.utc)
    return repository.create(JobRecord(
        id=job_id, status=status, media_type="image",
        request=GenerationRequest(media_type="image", prompt="fake", model_id="fake"),
        created_at=now, updated_at=now,
    ))


@pytest.mark.parametrize("source", JOB_STATUSES)
@pytest.mark.parametrize("target", JOB_STATUSES)
@pytest.mark.parametrize("entrypoint", ["update", "cas", "legacy", "service"])
def test_persisted_transitions_follow_contract(tmp_path, source, target, entrypoint):
    repository = JobRepository(tmp_path / "jobs.db")
    before = seed_job(repository, source)
    if entrypoint == "cas":
        result = repository.update_if_status(before.id, (source,), status=target)
    elif entrypoint == "legacy":
        result = repository.update_job_status(before.id, target)
    elif entrypoint == "service":
        result = JobService(repository, JobQueue()).update_status(before.id, target)
    else:
        result = repository.update(before.id, status=target)
    after = repository.get(before.id)
    if is_valid_transition(source, target):
        assert result is not None
        assert after.status == target
    else:
        assert result is None
        assert after == before


@pytest.mark.parametrize("source", JOB_STATUSES)
def test_success_only_commits_from_postprocessing(tmp_path, source):
    repository = JobRepository(tmp_path / "jobs.db")
    before = seed_job(repository, source)
    bus = EventBus()
    service = JobService(repository, JobQueue(), bus)
    service.mark_succeeded(before.id, GenerationResult(job_id=before.id, status="succeeded"))
    after = repository.get(before.id)
    if source == "postprocessing":
        assert after.status == "succeeded"
        assert after.result is not None
        assert [event.type for event in bus.list_events()] == ["job_succeeded"]
    else:
        assert after == before
        assert bus.list_events() == []


def test_duplicate_consumers_have_one_owner_and_loser_preserves_cancellation(
    tmp_path, monkeypatch,
):
    owner_repo = JobRepository(tmp_path / "jobs.db")
    loser_repo = JobRepository(tmp_path / "jobs.db")
    job = seed_job(owner_repo)
    cancellation = CancellationRegistry()
    service = JobService(owner_repo, JobQueue(), cancellation_registry=cancellation)
    both_read = Barrier(2)
    generating = Event()
    release = Event()

    def generate(context):
        generating.set()
        assert release.wait(5)
        assert context.is_cancelled()

    generator = FakeGenerator(generate)
    runners = []
    for repository in (owner_repo, loser_repo):
        # Both consumers observe queued before either attempts the SQLite claim.
        def gated_get(job_id, original=repository.get, first=[True]):
            record = original(job_id)
            if first[0]:
                first[0] = False
                both_read.wait(5)
            return record

        monkeypatch.setattr(repository, "get", gated_get)
        queue = JobQueue()
        queue.enqueue(job.id)
        runners.append(JobRunner(
            repository, queue, GeneratorRegistry({"image": generator}),
            job_service=service, cancellation_registry=cancellation,
        ))
    original_update = runners[1]._update_status

    def delayed_loser_claim(job_id, status, **kwargs):
        if status == "preparing":
            assert generating.wait(5)
        return original_update(job_id, status, **kwargs)

    monkeypatch.setattr(runners[1], "_update_status", delayed_loser_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(runners[0].run_once)
        loser = executor.submit(runners[1].run_once)
        try:
            assert loser.result(timeout=5).status == "running"
            service.cancel_job(job.id)
            assert cancellation.is_cancelled(job.id), "claim loser removed owner's entry"
        finally:
            release.set()
        assert owner.result(timeout=5).status == "cancelled"
    assert generator.calls == 1
    assert not cancellation.is_cancelled(job.id), "owner must release its entry"


def test_cancel_between_claim_and_context_registration(tmp_path, monkeypatch):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    cancellation = CancellationRegistry()
    service = JobService(repository, queue, cancellation_registry=cancellation)
    service.create_job(
        GenerationRequest(media_type="image", prompt="fake", model_id="fake")
    )
    generator = FakeGenerator()
    original_begin = cancellation.begin

    def cancel_before_register(job_id):
        assert repository.get(job_id).status == "preparing"
        assert service.cancel_job(job_id).status == "cancel_requested"
        event = original_begin(job_id)
        assert not event.is_set()  # The in-memory signal was necessarily missed.
        return event

    monkeypatch.setattr(cancellation, "begin", cancel_before_register)
    runner = JobRunner(
        repository, queue, GeneratorRegistry({"image": generator}),
        job_service=service, cancellation_registry=cancellation,
    )
    assert runner.run_once().status == "cancelled"
    assert generator.calls == 0


def test_context_construction_failure_releases_only_its_registration(tmp_path, monkeypatch):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    cancellation = CancellationRegistry()
    job = seed_job(repository)
    queue.enqueue(job.id)
    runner = JobRunner(
        repository,
        queue,
        GeneratorRegistry({"image": FakeGenerator()}),
        cancellation_registry=cancellation,
    )

    def fail_context(*args, **kwargs):
        raise RuntimeError("injected context construction failure")

    monkeypatch.setattr(runner, "_begin_context", fail_context)
    with pytest.raises(RuntimeError, match="context construction failure"):
        runner.run_once()

    cancellation.request_cancel(job.id)
    assert not cancellation.is_cancelled(job.id)


def test_cancel_retries_when_claim_wins_its_cas(tmp_path, monkeypatch):
    repository = JobRepository(tmp_path / "jobs.db")
    job = seed_job(repository)
    original_update = repository.update_if_status

    def claim_before_cancel(job_id, expected_statuses, **kwargs):
        if kwargs.get("status") == "cancelled" and expected_statuses == ("queued",):
            assert original_update(job_id, ("queued",), status="preparing") is not None
        return original_update(job_id, expected_statuses, **kwargs)

    monkeypatch.setattr(repository, "update_if_status", claim_before_cancel)
    service = JobService(repository, JobQueue())
    assert service.cancel_job(job.id).status == "cancel_requested"


def test_post_generation_failure_does_not_kill_consumer(tmp_path, caplog):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    cancellation = CancellationRegistry()
    stopped = Event()
    generator = FakeGenerator()

    class BrokenAssets:
        def sync_job(self, job):
            if job.id == "job_first":
                raise OSError("injected asset storage failure")
            stopped.set()

    service = JobService(repository, queue, asset_repository=BrokenAssets())
    for job_id in ("job_first", "job_second"):
        seed_job(repository, job_id=job_id)
        queue.enqueue(job_id)
    runner = JobRunner(
        repository, queue, GeneratorRegistry({"image": generator}),
        job_service=service, cancellation_registry=cancellation,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_forever, stop_event=stopped)
        try:
            assert stopped.wait(5), "second job was never processed"
        finally:
            stopped.set()
        future.result(timeout=5)
    assert generator.calls == 2
    assert all(job.status == "succeeded" for job in repository.list())
    assert "job_first" in caplog.text
    assert "injected asset storage failure" in caplog.text


@pytest.mark.parametrize(("status", "expected"), [
    ("queued", "requeue_candidate"),
    ("preparing", "interrupted_failure_candidate"),
    ("running", "interrupted_failure_candidate"),
    ("postprocessing", "interrupted_failure_candidate"),
    ("cancel_requested", "cancelled_candidate"),
    ("succeeded", "completion_reconciliation_candidate"),
    ("failed", "terminal_reconciliation_candidate"),
    ("cancelled", "terminal_reconciliation_candidate"),
])
def test_recovery_classification_is_pure(tmp_path, status, expected):
    from core.jobs.recovery import classify_job

    repository = JobRepository(tmp_path / "jobs.db")
    job = seed_job(repository, status)
    before = job.model_dump_json()
    assert classify_job(job) == expected
    assert job.model_dump_json() == before
    assert repository.get(job.id) == job
