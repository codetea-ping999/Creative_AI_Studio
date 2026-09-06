"""Deterministic execution-ownership and lifecycle boundary regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3
from threading import Barrier, Event

import pytest

from core.jobs import CancellationRegistry, EventBus, JobQueue, JobRunner, JobService
from core.jobs.schemas import JobRecord
from core.jobs.statuses import JOB_STATUSES, is_valid_transition
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRecordDecodeError, JobRepository
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
    original_claim = runners[1]._claim_job

    def delayed_loser_claim(job_id):
        assert generating.wait(5)
        return original_claim(job_id)

    monkeypatch.setattr(runners[1], "_claim_job", delayed_loser_claim)
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


def test_claim_reread_failure_resolves_owned_job_and_continues_consumer(
    tmp_path, monkeypatch,
):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    first = seed_job(repository, job_id="job_first")
    second = seed_job(repository, job_id="job_second")
    queue.enqueue(first.id)
    queue.enqueue(second.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))
    original_get = repository.get
    first_job_reads = {"count": 0}

    def fail_claim_reread_once(job_id):
        if job_id == first.id:
            first_job_reads["count"] += 1
            if first_job_reads["count"] == 2:
                raise RuntimeError("injected post-CAS reread failure")
        return original_get(job_id)

    monkeypatch.setattr(repository, "get", fail_claim_reread_once)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_forever, stop_event=stopped)
        assert stopped.wait(5), "consumer did not process the next job"
        stopped.set()
        future.result(timeout=5)

    assert repository.get(first.id).status == "failed"
    assert repository.get(second.id).status == "succeeded"
    assert generator.calls == 1


def test_claim_cas_failure_is_not_resolved_as_owned_job(tmp_path, monkeypatch):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    job = seed_job(repository)
    queue.enqueue(job.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": FakeGenerator()}))

    def fail_before_cas(*args, **kwargs):
        raise sqlite3.OperationalError("injected pre-commit CAS failure")

    monkeypatch.setattr(repository, "transition_if_status", fail_before_cas)
    with pytest.raises(sqlite3.OperationalError, match="pre-commit CAS failure"):
        runner.run_once()

    assert repository.get(job.id).status == "queued"


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


def test_context_construction_failure_resolves_claim_and_releases_registration(
    tmp_path, monkeypatch,
):
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
    assert runner.run_once().status == "failed"
    assert repository.get(job.id).status == "failed"

    cancellation.request_cancel(job.id)
    assert not cancellation.is_cancelled(job.id)


@pytest.mark.parametrize("failure_point", ["cancel_read", "publish"])
def test_claim_setup_failure_does_not_strand_job_or_consumer(
    tmp_path, monkeypatch, failure_point,
):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    cancellation = CancellationRegistry()
    stopped = Event()
    calls = {"count": 0}

    def generate(_context):
        calls["count"] += 1
        if calls["count"] == 1:
            stopped.set()

    generator = FakeGenerator(generate)
    first = seed_job(repository, job_id="job_first")
    second = seed_job(repository, job_id="job_second")
    queue.enqueue(first.id)
    queue.enqueue(second.id)
    runner = JobRunner(
        repository,
        queue,
        GeneratorRegistry({"image": generator}),
        cancellation_registry=cancellation,
    )

    if failure_point == "cancel_read":
        original = runner._is_cancelled
        raised = {"value": False}

        def fail_once(job_id):
            if not raised["value"]:
                raised["value"] = True
                raise RuntimeError("injected persisted cancellation read failure")
            return original(job_id)

        monkeypatch.setattr(runner, "_is_cancelled", fail_once)
    else:
        original = runner._publish
        raised = {"value": False}

        def fail_once(event_type, payload):
            if event_type == "job_preparing" and not raised["value"]:
                raised["value"] = True
                raise RuntimeError("injected claim publish failure")
            return original(event_type, payload)

        monkeypatch.setattr(runner, "_publish", fail_once)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_forever, stop_event=stopped)
        assert stopped.wait(5), "consumer did not process the next job"
        stopped.set()
        future.result(timeout=5)

    assert repository.get(first.id).status == "failed"
    assert repository.get(second.id).status == "succeeded"
    assert calls["count"] == 1


def test_failure_recording_error_does_not_kill_consumer(tmp_path, monkeypatch):
    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    first = seed_job(repository, job_id="job_first")
    second = seed_job(repository, job_id="job_second")
    queue.enqueue(first.id)
    queue.enqueue(second.id)
    service = JobService(repository, queue)
    runner = JobRunner(
        repository,
        queue,
        GeneratorRegistry({"image": generator}),
        job_service=service,
    )
    original_begin_context = runner._begin_context
    setup_calls = {"count": 0}

    def fail_setup_once(*args, **kwargs):
        setup_calls["count"] += 1
        if setup_calls["count"] == 1:
            raise RuntimeError("injected setup failure")
        return original_begin_context(*args, **kwargs)

    monkeypatch.setattr(runner, "_begin_context", fail_setup_once)
    original_mark_failed = service.mark_failed
    calls = {"count": 0}

    def fail_recording(job_id, message):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected failure recording error")
        return original_mark_failed(job_id, message)

    monkeypatch.setattr(service, "mark_failed", fail_recording)
    # The first job is left preparing because persistence itself failed; the
    # key contract here is that the loop still consumes and completes the next
    # job rather than terminating.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_forever, stop_event=stopped)
        assert stopped.wait(5)
        stopped.set()
        future.result(timeout=5)

    assert repository.get(first.id).status == "preparing"
    assert repository.get(second.id).status == "succeeded"


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


# --- Post-#395 audit: P1/P2 lifecycle-gap regressions --------------------
#
# Found by an exact-HEAD adversarial audit of the merged #395 job-lifecycle
# hardening. Both gaps sit *after* execution ownership is already
# established -- the `claimed` boundary #395 introduced is correct and is
# deliberately not touched here.


def test_cancel_winning_after_postprocessing_resolves_to_a_terminal_state(
    tmp_path, monkeypatch,
):
    """P1: cancel_requested must never survive a lost mark_succeeded() CAS.

    Reproduces, deterministically: generation finishes -> running ->
    postprocessing commits -> a cancel lands in that exact instant
    (postprocessing -> cancel_requested) -> mark_succeeded()'s CAS (which
    only ever commits from postprocessing) loses. Before the fix,
    process_job() returned mark_succeeded()'s stale cancel_requested read
    as-is and the `finally` block tore down cancellation ownership, leaving
    the job non-terminal with no owner left to ever resolve it. The
    contract (statuses.py: cancel_requested -> cancelled|failed, never back
    to active, never succeeded) requires this to land on a terminal state.
    """

    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    cancellation = CancellationRegistry()
    service = JobService(repository, queue, cancellation_registry=cancellation)
    job = seed_job(repository)
    queue.enqueue(job.id)
    generator = FakeGenerator()
    runner = JobRunner(
        repository, queue, GeneratorRegistry({"image": generator}),
        job_service=service, cancellation_registry=cancellation,
    )

    original_update_if_status = repository.update_if_status

    def race_cancel_the_instant_postprocessing_commits(job_id, expected_statuses, **kwargs):
        result = original_update_if_status(job_id, expected_statuses, **kwargs)
        # Target only the runner's own running -> postprocessing transition
        # (see JobRunner._update_status) so mark_succeeded()'s own
        # postprocessing -> succeeded CAS just below is left untouched.
        if (
            result is not None
            and kwargs.get("status") == "postprocessing"
            and expected_statuses == ("running",)
        ):
            assert service.cancel_job(job_id).status == "cancel_requested"
        return result

    monkeypatch.setattr(
        repository, "update_if_status", race_cancel_the_instant_postprocessing_commits
    )

    result = runner.run_once()

    assert result is not None
    assert result.status == "cancelled"
    assert repository.get(job.id).status == "cancelled"
    assert generator.calls == 1
    # Ownership must actually be released -- not just the return value.
    assert not cancellation.is_cancelled(job.id)


def test_pre_claim_repository_read_failure_under_run_forever_does_not_lose_the_job(
    tmp_path, monkeypatch,
):
    """P2 (Case B): a pre-claim `repository.get()` failure must not orphan
    the durable `queued` row once `job_queue.dequeue()` has already popped
    it out of the in-memory queue.
    """

    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    job = seed_job(repository, job_id="job_a")
    queue.enqueue(job.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    original_get = repository.get
    reads = {"count": 0}

    def fail_first_pre_claim_read(job_id):
        if job_id == job.id:
            reads["count"] += 1
            if reads["count"] == 1:
                raise sqlite3.OperationalError("injected pre-claim read failure")
        return original_get(job_id)

    monkeypatch.setattr(repository, "get", fail_first_pre_claim_read)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            # Stop the loop regardless of outcome -- otherwise a failed
            # assertion below leaves `run_forever` spinning forever and the
            # executor's own context-manager shutdown (which joins it)
            # hangs the test run instead of reporting the failure.
            stopped.set()
        future.result(timeout=5)
        assert processed, "job_a was lost instead of being redelivered"

    assert repository.get(job.id).status == "succeeded"
    assert generator.calls == 1


def test_pre_commit_cas_failure_under_run_forever_does_not_lose_the_job(
    tmp_path, monkeypatch,
):
    """P2 (Case C): a pre-commit `transition_if_status()` failure under
    `run_forever` must not leave a `queued` row with no queue entry left to
    ever redeliver it. `test_claim_cas_failure_is_not_resolved_as_owned_job`
    above proves the single `run_once()` call raises and leaves the row
    `queued`; this proves the *loop* actually recovers the job rather than
    silently losing it forever.
    """

    repository = JobRepository(tmp_path / "jobs.db")
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    job = seed_job(repository, job_id="job_a")
    queue.enqueue(job.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    original_transition = repository.transition_if_status
    attempts = {"count": 0}

    def fail_first_claim_commit(job_id, expected_statuses, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("injected pre-commit CAS failure")
        return original_transition(job_id, expected_statuses, **kwargs)

    monkeypatch.setattr(repository, "transition_if_status", fail_first_claim_commit)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, "job_a was lost instead of being redelivered"

    assert repository.get(job.id).status == "succeeded"
    assert generator.calls == 1


def test_pre_claim_failure_requeues_into_the_same_lane_not_lost_or_misrouted(
    tmp_path, monkeypatch,
):
    """A pre-claim failure's requeue must preserve original lane semantics:
    it must land back in the exact lane it was dequeued from, not the
    default/first lane, and the durable row must stay untouched (`queued`).
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue(lanes=("heavy", "light"))
    job = seed_job(repository, job_id="job_a")
    queue.enqueue(job.id, lane="light")
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": FakeGenerator()}))

    def fail_read(job_id):
        raise sqlite3.OperationalError("injected pre-claim read failure")

    monkeypatch.setattr(repository, "get", fail_read)
    with pytest.raises(sqlite3.OperationalError):
        runner.run_once(lane="light")

    assert queue.size(lane="light") == 1
    assert queue.size(lane="heavy") == 0
    assert JobRepository(db_path).get(job.id).status == "queued"


# --- Codex exact-HEAD review on 876327b8: bound retries for permanent ----
# pre-claim failures.
#
# The requeue-on-pre-claim-failure fix above is correct for a *transient*
# failure (a SQLite operational error) but, on its own, would requeue a
# *permanent* one (a job row whose persisted `request_json` can never be
# deserialized/validated -- old schema, corrupted bytes) forever: every
# redelivery hits the exact same bytes and fails identically, so the queue
# item would cycle dequeue -> failure -> requeue -> dequeue... forever,
# starving every job behind it in that lane and spamming the log without
# bound. `json.JSONDecodeError` and `pydantic.ValidationError` are both
# `ValueError` subclasses and are the only two realistic sources of a
# permanent pre-claim failure (`JobRepository._row_to_record`'s `json.loads`
# and `GenerationRequest`/`GenerationResult.model_validate`) -- a
# `sqlite3.Error` (locked, busy, disk I/O) carries no information about the
# row's content and is the only realistic *transient* source, so the two
# populations are cleanly separated by `isinstance(exc, ValueError)`.


def test_permanent_pre_claim_failure_does_not_retry_forever_and_lets_the_next_job_run(
    tmp_path,
):
    """Case 2: a poison job (permanently undeserializable request_json) must
    not be retried forever, must not starve a healthy job queued behind it,
    and must land in an observable terminal state -- checked via a raw
    SQLite read, since `JobRepository.get()` on this exact row raises the
    same `json.JSONDecodeError` every time by construction.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")

    with sqlite3.connect(db_path) as raw_connection:
        raw_connection.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison.id),
        )
        raw_connection.commit()

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, (
            "job_healthy was never processed -- the poison job likely "
            "starved the worker with infinite retries"
        )

    # The poison job must be gone from the queue exactly once (never
    # requeued) and the healthy job must have actually run.
    assert queue.size() == 0
    assert generator.calls == 1

    raw_status = sqlite3.connect(db_path).execute(
        "SELECT status FROM jobs WHERE id = ?", (poison.id,)
    ).fetchone()[0]
    assert raw_status == "failed"
    # repository.get() on the poison row still raises identically (now
    # normalized to JobRecordDecodeError -- see the Codex follow-up round
    # below) -- proves the quarantine path never attempted (or depended on)
    # reading the corrupted payload back.
    with pytest.raises(JobRecordDecodeError):
        repository.get(poison.id)

    assert repository.get(healthy.id).status == "succeeded"


def test_permanent_pre_claim_failure_does_not_requeue_and_other_lanes_are_unaffected(
    tmp_path,
):
    """Case 3: the multi-lane requeue contract must hold for both outcomes --
    a transient failure still goes back to its own lane (covered by
    `test_pre_claim_failure_requeues_into_the_same_lane_not_lost_or_misrouted`
    above); a permanent one must not be requeued into *any* lane, and must
    not block a healthy job already queued in a different lane.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue(lanes=("heavy", "light"))
    poison = seed_job(repository, job_id="job_poison")
    other_lane_job = seed_job(repository, job_id="job_other_lane")

    with sqlite3.connect(db_path) as raw_connection:
        raw_connection.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            ("{not valid json", poison.id),
        )
        raw_connection.commit()

    queue.enqueue(poison.id, lane="light")
    queue.enqueue(other_lane_job.id, lane="heavy")
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": FakeGenerator()}))

    with pytest.raises(JobRecordDecodeError):
        runner.run_once(lane="light")

    assert queue.size(lane="light") == 0, "poison job must not be requeued"
    assert queue.size(lane="heavy") == 1, "the other lane must be untouched"

    result = runner.run_once(lane="heavy")
    assert result is not None
    assert result.status == "succeeded"


def _corrupt_request_json(db_path, job_id: str, broken_payload: str) -> None:
    with sqlite3.connect(db_path) as raw_connection:
        raw_connection.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?",
            (broken_payload, job_id),
        )
        raw_connection.commit()


def _raw_status(db_path, job_id: str) -> str:
    return sqlite3.connect(db_path).execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]


# --- Second Codex exact-HEAD review round, on 5e7e9465: two further ------
# pre-claim edge cases in the classify-and-quarantine fix above.


def test_recursion_error_from_deeply_nested_json_is_treated_as_permanent(tmp_path):
    """Finding 3: a RecursionError from json.loads() is a permanent, not a
    transient, pre-claim failure -- the prior isinstance(exc, ValueError)
    classifier missed it (RecursionError subclasses RuntimeError, not
    ValueError), so it fell through to the transient branch and would have
    been requeued forever. Reproduced via the real code path: a genuinely
    deep JSON array, through the actual json.loads() call inside
    JobRepository._row_to_record() -- not a monkeypatched substitute.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")

    # Deep enough to overflow the C json decoder's own stack guard --
    # verified to raise RecursionError cleanly (no interpreter crash) in
    # well under a second; sys.getrecursionlimit() is irrelevant here since
    # the C accelerator's stack usage, not Python frame count, trips first.
    depth = 2_000_000
    _corrupt_request_json(db_path, poison.id, "[" * depth + "]" * depth)

    with pytest.raises(RecursionError):
        json.loads("[" * depth + "]" * depth)  # confirms the premise directly
    with pytest.raises(JobRecordDecodeError):
        repository.get(poison.id)

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, "job_healthy was never processed"

    assert queue.size() == 0, "the poison job must not still be queued for retry"
    assert generator.calls == 1
    assert _raw_status(db_path, poison.id) == "failed"
    assert repository.get(healthy.id).status == "succeeded"


def test_transient_quarantine_write_failure_requeues_instead_of_losing_the_poison_job(
    tmp_path, monkeypatch,
):
    """Finding 2: if the quarantine write itself hits a transient failure
    (e.g. a SQLite lock), the poison job must be requeued rather than left
    `queued` in the database with no queue entry -- exactly the lost-job
    condition this whole fix exists to prevent. Once the quarantine write
    is retried and succeeds, it must still converge to `failed`, and a
    healthy job queued behind it must still be processed.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator()
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")
    _corrupt_request_json(db_path, poison.id, "{not valid json")

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    original_transition = repository.transition_if_status
    quarantine_attempts = {"count": 0}

    def fail_first_quarantine_write(job_id, expected_statuses, **kwargs):
        if job_id != poison.id or kwargs.get("status") != "failed":
            return original_transition(job_id, expected_statuses, **kwargs)
        quarantine_attempts["count"] += 1
        if quarantine_attempts["count"] == 1:
            raise sqlite3.OperationalError(
                "injected transient quarantine write failure"
            )
        result = original_transition(job_id, expected_statuses, **kwargs)
        # This is the second (successful) quarantine attempt. By
        # construction it can only run after job_healthy -- queued behind
        # the poison job's first, requeued-to-tail attempt -- has already
        # been processed, since run_forever() is strictly sequential.
        stopped.set()
        return result

    monkeypatch.setattr(repository, "transition_if_status", fail_first_quarantine_write)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            converged = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert converged, (
            "the poison job's quarantine was never retried to a successful "
            "conclusion -- it was likely lost after the first write failure"
        )

    assert quarantine_attempts["count"] == 2
    assert queue.size() == 0
    assert _raw_status(db_path, poison.id) == "failed"
    assert repository.get(healthy.id).status == "succeeded"


def test_quarantine_write_failure_requeues_into_the_same_lane(tmp_path, monkeypatch):
    """Finding 2, multi-lane: a quarantine write failure's requeue must
    follow the same "exact lane it came from" contract as any other
    transient pre-claim failure, and must not touch a different lane's job.
    The row must remain genuinely `queued` (not silently marked `failed`)
    since the quarantine write never actually committed.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue(lanes=("heavy", "light"))
    poison = seed_job(repository, job_id="job_poison")
    other_lane_job = seed_job(repository, job_id="job_other_lane")
    _corrupt_request_json(db_path, poison.id, "{not valid json")

    queue.enqueue(poison.id, lane="light")
    queue.enqueue(other_lane_job.id, lane="heavy")
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": FakeGenerator()}))

    def always_fail_quarantine_write(job_id, expected_statuses, **kwargs):
        raise sqlite3.OperationalError("injected transient quarantine write failure")

    monkeypatch.setattr(repository, "transition_if_status", always_fail_quarantine_write)

    with pytest.raises(JobRecordDecodeError):
        runner.run_once(lane="light")

    assert queue.size(lane="light") == 1, (
        "must be requeued into its own lane, not lost, when the quarantine "
        "write itself fails"
    )
    assert queue.size(lane="heavy") == 1, "the other lane must be untouched"
    assert _raw_status(db_path, poison.id) == "queued", (
        "the row must remain queued -- the quarantine write never committed"
    )


def _corrupt_column(db_path, job_id: str, column: str, value) -> None:
    with sqlite3.connect(db_path) as raw_connection:
        raw_connection.execute(
            f"UPDATE jobs SET {column} = ? WHERE id = ?",  # noqa: S608 -- column is a fixed literal from this test file, never external input
            (value, job_id),
        )
        raw_connection.commit()


# --- Third Codex exact-HEAD review round, on 5eae5ad8: permanent row- -----
# reconstruction failures beyond the request/result payload itself.
#
# `_row_to_record()` previously only wrapped the payload JSON decode/
# validate steps; a malformed `created_at`/`updated_at` timestamp
# (`datetime.fromisoformat` raising `ValueError`) or a `JobRecord`-level
# validation failure (an invalid persisted `status`/`media_type`, an
# out-of-range `progress`) raised *outside* that boundary, propagated as a
# raw `ValueError`, and would have been misclassified transient by
# `_is_permanent_pre_claim_failure` -- reproducing the exact poison-job
# infinite-retry regression the payload-only fix already closed, just for a
# different part of the row.


def test_malformed_created_at_timestamp_is_a_permanent_persisted_row_failure(
    tmp_path,
):
    """Case 1: a malformed `created_at` has nothing to do with request_json
    (which is left completely valid here) -- it must still be classified as
    a permanent, never-retry failure, proven through a real `run_forever`
    loop, not just a repository-level exception-type check.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")

    _corrupt_column(db_path, poison.id, "created_at", "not-a-date")
    with pytest.raises(JobRecordDecodeError):
        repository.get(poison.id)

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, (
            "job_healthy was never processed -- the malformed-timestamp "
            "poison job likely starved the worker with infinite retries"
        )

    assert queue.size() == 0, "the poison job must not still be queued for retry"
    assert generator.calls == 1
    assert _raw_status(db_path, poison.id) == "failed"
    assert repository.get(healthy.id).status == "succeeded"


def test_out_of_range_progress_is_a_permanent_persisted_row_failure(tmp_path):
    """Case 2: request_json and every timestamp are completely valid here --
    only the persisted `progress` value (2.5, outside JobRecord's
    `ge=0.0, le=1.0` constraint) is corrupted, so `JobRecord(...)`'s own
    Pydantic validation is what fails, not payload deserialization. This is
    the row-reconstruction-in-general failure Codex's finding is about,
    distinct from (and in addition to) the payload-only case.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")

    _corrupt_column(db_path, poison.id, "progress", 2.5)
    with pytest.raises(JobRecordDecodeError):
        repository.get(poison.id)

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, (
            "job_healthy was never processed -- the invalid-progress poison "
            "job likely starved the worker with infinite retries"
        )

    assert queue.size() == 0, "the poison job must not still be queued for retry"
    assert generator.calls == 1
    assert _raw_status(db_path, poison.id) == "failed"
    assert repository.get(healthy.id).status == "succeeded"


# --- Fourth Codex exact-HEAD review round, on ce216adb: three further ----
# pre-claim edge cases the row-wide reconstruction fix above still missed.


def test_blob_created_at_is_a_permanent_persisted_row_failure_via_typeerror(
    tmp_path,
):
    """P2-1: SQLite's type affinity is advisory -- a BLOB can end up in a
    TEXT-affinity column despite the schema. `datetime.fromisoformat()`
    reacts to a non-str value with `TypeError`, not `ValueError`, so the
    prior `except (ValueError, RecursionError)` missed it entirely and this
    content-caused, deterministic failure would have been requeued forever.
    Proven through a real `run_forever` loop, not just a repository-level
    exception-type check.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")

    _corrupt_column(db_path, poison.id, "created_at", b"not-a-date-blob")
    with pytest.raises(JobRecordDecodeError):
        repository.get(poison.id)

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, (
            "job_healthy was never processed -- the BLOB-timestamp poison "
            "job likely starved the worker with infinite retries"
        )

    assert queue.size() == 0, "the poison job must not still be queued for retry"
    assert generator.calls == 1
    assert _raw_status(db_path, poison.id) == "failed"
    assert repository.get(healthy.id).status == "succeeded"


def test_invalid_raw_status_is_quarantined_without_losing_the_job(tmp_path):
    """P2-2: a structurally invalid raw `status` (outside `JOB_STATUSES`
    entirely -- external corruption, a truncated write) is correctly
    detected as permanent by `_row_to_record()`, but the ordinary quarantine
    CAS (`expected_statuses=(queued,)`) can never match it, since the row
    was never `queued` to begin with. Treating that CAS miss as "already
    resolved" would discard the queue entry while the row stays stuck in
    the invalid status forever -- proven through a real `run_forever` loop.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    stopped = Event()
    generator = FakeGenerator(lambda _context: stopped.set())
    poison = seed_job(repository, job_id="job_poison")
    healthy = seed_job(repository, job_id="job_healthy")

    _corrupt_column(db_path, poison.id, "status", "banana")
    with pytest.raises(JobRecordDecodeError):
        repository.get(poison.id)

    queue.enqueue(poison.id)
    queue.enqueue(healthy.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": generator}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_forever, stop_event=stopped, poll_interval_seconds=0.01
        )
        try:
            processed = stopped.wait(5)
        finally:
            stopped.set()
        future.result(timeout=5)
        assert processed, (
            "job_healthy was never processed -- the invalid-raw-status "
            "poison job likely starved the worker with infinite retries"
        )

    assert queue.size() == 0, "the poison job must not still be queued for retry"
    assert generator.calls == 1
    assert _raw_status(db_path, poison.id) == "failed"
    poison_after = repository.get(poison.id)
    assert poison_after.error_message is not None
    assert "banana" in poison_after.error_message
    assert repository.get(healthy.id).status == "succeeded"


@pytest.mark.parametrize("status", JOB_STATUSES)
def test_structurally_invalid_status_repair_never_matches_a_valid_status(
    tmp_path, status,
):
    """P2-2: `quarantine_structurally_invalid_status()` is a deliberately
    narrow escape hatch, not a general "rewrite any status" helper -- its
    own WHERE clause must never match a row whose raw status is any of the
    8 canonical `JOB_STATUSES`, including a valid terminal one.
    """

    repository = JobRepository(tmp_path / "jobs.db")
    job = seed_job(repository, status=status)

    repaired = repository.quarantine_structurally_invalid_status(
        job.id, error_message="should never be applied"
    )

    assert repaired is False
    after = repository.get(job.id)
    assert after.status == status
    assert after.progress == job.progress
    assert after.error_message is None


def test_quarantine_persists_the_original_error_message_when_the_row_becomes_readable_again(
    tmp_path,
):
    """P2-3: once quarantine's own atomic UPDATE repairs `progress` back
    into range, `repository.get()` can read the row again -- its
    `error_message` must show why it failed, not `None`, matching
    `JobService.mark_failed()`'s own contract. An event publish alone is
    not a substitute for persistence: nothing replays a past event to a
    caller reading the job later.
    """

    db_path = tmp_path / "jobs.db"
    repository = JobRepository(db_path)
    queue = JobQueue()
    poison = seed_job(repository, job_id="job_poison")
    _corrupt_column(db_path, poison.id, "progress", 2.5)
    queue.enqueue(poison.id)
    runner = JobRunner(repository, queue, GeneratorRegistry({"image": FakeGenerator()}))

    with pytest.raises(JobRecordDecodeError):
        runner.run_once()

    after = repository.get(poison.id)
    assert after.status == "failed"
    assert after.progress == 1.0
    assert after.error_message is not None
    assert "progress" in after.error_message


# --- PR3 exact-HEAD audit finding P2-3: create-or-reuse under a race ------


def test_concurrent_create_or_reuse_job_converges_on_exactly_one_row(tmp_path):
    """Two callers racing to materialize the same stable child job id (a
    terminal event handler racing a completion-retry pass over the same
    Batch item, for instance) must both observe success and leave exactly
    one persisted row -- not one 500 from an unhandled
    `sqlite3.IntegrityError` and not two Job rows under one id.
    """

    job_repository = JobRepository(tmp_path / "jobs.db")
    job_queue = JobQueue()
    event_bus = EventBus()
    job_service = JobService(job_repository, job_queue, event_bus)

    request = GenerationRequest(media_type="image", prompt="shared", model_id="fake")
    job_id = "job_shared_child"
    barrier = Barrier(2)
    results: list[JobRecord] = []
    errors: list[BaseException] = []

    def _create_or_reuse():
        try:
            barrier.wait(timeout=5)
            results.append(job_service.create_or_reuse_job(job_id, request))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_create_or_reuse) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert results[0].id == job_id
    assert results[1].id == job_id
    matching_rows = [job for job in job_repository.list() if job.id == job_id]
    assert len(matching_rows) == 1


def test_create_or_reuse_job_still_raises_on_a_genuine_request_mismatch(tmp_path):
    """The race-safe path (`create_if_absent`) must not silently paper over
    a real content mismatch -- only a losing caller whose request matches
    the winner's exactly may reuse the row.
    """

    job_repository = JobRepository(tmp_path / "jobs.db")
    job_service = JobService(job_repository, JobQueue(), EventBus())
    job_id = "job_mismatch"
    job_service.create_or_reuse_job(
        job_id, GenerationRequest(media_type="image", prompt="first", model_id="fake")
    )

    with pytest.raises(ValueError, match="different request"):
        job_service.create_or_reuse_job(
            job_id, GenerationRequest(media_type="image", prompt="second", model_id="fake")
        )
