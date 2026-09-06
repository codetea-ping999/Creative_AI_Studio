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
    # repository.get() on the poison row still raises identically -- proves
    # the quarantine path never attempted (or depended on) reading the
    # corrupted payload back.
    with pytest.raises(json.JSONDecodeError):
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

    with pytest.raises(json.JSONDecodeError):
        runner.run_once(lane="light")

    assert queue.size(lane="light") == 0, "poison job must not be requeued"
    assert queue.size(lane="heavy") == 1, "the other lane must be untouched"

    result = runner.run_once(lane="heavy")
    assert result is not None
    assert result.status == "succeeded"
