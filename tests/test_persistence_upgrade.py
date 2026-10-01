"""B2: one supported pre-v1 persistence upgrade path, proven against a real fixture.

Contract proven here (and only this): current main opens a data directory
written by main at ``55c4127`` -- the last main commit before PR #408
(``12b55eb``) added ``jobs.completion_state`` / ``jobs.completion_error`` and
the ``idx_jobs_completion_retry`` index -- and startup recovery converges it
without re-running a generator or discarding Story bindings.

The fixture under ``tests/fixtures/persistence_upgrade_55c4127`` was produced
by running that baseline's own ``JobRepository`` / ``AssetRepository`` /
``StoryRepository`` / ``SceneBinder`` (see its README). It is not a general
migration test: no other historical schema is claimed or exercised.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3

import pytest

from bootstrap import create_application_services
from core.jobs import JobRunner
from core.jobs.completion import CompletionOutcome
from core.jobs.startup_recovery import run_startup_recovery
from core.schemas import GenerationResult
from generators.base import BaseGenerator
from generators.registry import GeneratorRegistry

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "persistence_upgrade_55c4127"

BASELINE_COLUMNS = {
    "id",
    "project_id",
    "media_type",
    "status",
    "request_json",
    "result_json",
    "progress",
    "error_message",
    "created_at",
    "updated_at",
}
ADDED_COLUMNS = {"completion_state", "completion_error"}

SUCCEEDED = "job_fx_succeeded"
RUNNING = "job_fx_running"
QUEUED = "job_fx_queued"
ASSET_ID = "asset_e6c220e30f3d37f101bb39e8"


class CountingGenerator(BaseGenerator):
    def __init__(self) -> None:
        self.job_prompts: list[str] = []

    def validate_request(self, request):
        pass

    def prepare(self, request):
        pass

    def cleanup(self, request):
        pass

    def generate(self, request, context=None):
        self.job_prompts.append(request.prompt)
        return GenerationResult(job_id="fake", status="succeeded")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A private copy of the baseline data directory (jobs.db, assets/, stories/)."""

    root = tmp_path / "data"
    root.mkdir()
    connection = sqlite3.connect(root / "jobs.db")
    try:
        connection.executescript((FIXTURE_DIR / "jobs.sql").read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    shutil.copytree(FIXTURE_DIR / "assets", root / "assets")
    shutil.copytree(FIXTURE_DIR / "stories", root / "stories")
    return root


def _columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}


def _indexes(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        # sqlite_autoindex_* is the PRIMARY KEY's implicit index, not an added one.
        return {
            row[1]
            for row in connection.execute("PRAGMA index_list(jobs)")
            if not row[1].startswith("sqlite_autoindex_")
        }


def _raw_rows(db_path: Path) -> dict[str, tuple]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]: row
            for row in connection.execute(
                "SELECT id, status, request_json, result_json, progress, error_message, "
                "created_at, completion_state, completion_error FROM jobs ORDER BY id"
            )
        }


def _start(data_dir: Path, tmp_path: Path):
    services = create_application_services(
        db_path=data_dir / "jobs.db", output_dir=tmp_path / "outputs"
    )
    report = run_startup_recovery(
        services.job_repository,
        services.job_service,
        services.completion_converger,
        batch_service=services.batch_service,
    )
    return services, report


def test_fixture_really_is_the_pre_completion_baseline(data_dir):
    # Guards the premise: if this fails the fixture no longer represents 55c4127.
    assert _columns(data_dir / "jobs.db") == BASELINE_COLUMNS
    assert _indexes(data_dir / "jobs.db") == set()


def test_baseline_data_directory_upgrades_and_recovers(data_dir, tmp_path):
    db_path = data_dir / "jobs.db"
    baseline_story = (data_dir / "stories" / "story_fixture.json").read_text(encoding="utf-8")
    baseline_asset = (data_dir / "assets" / f"{ASSET_ID}.json").read_bytes()

    services, report = _start(data_dir, tmp_path)
    repository = services.job_repository

    # 2. additive columns/indexes created; nothing dropped.
    assert _columns(db_path) == BASELINE_COLUMNS | ADDED_COLUMNS
    assert "idx_jobs_completion_retry" in _indexes(db_path)

    # 1. old rows remain readable through the current repository, request/result intact.
    succeeded = repository.get(SUCCEEDED)
    assert succeeded is not None and succeeded.status == "succeeded"
    assert succeeded.result is not None
    assert succeeded.result.outputs == ["outputs/images/job_fx_succeeded.png"]
    assert succeeded.request.prompt == "a lighthouse at dawn"
    assert repository.get(RUNNING).request.prompt == "a harbor at noon"
    assert repository.get(QUEUED).request.prompt == "a storm at night"
    assert {job.id for job in repository.list()} == {SUCCEEDED, RUNNING, QUEUED}

    # 3. queued job re-enqueued under its existing id, still queued, exactly once.
    assert report.requeued == [QUEUED]
    assert repository.get(QUEUED).status == "queued"
    assert services.job_queue.dequeue() == QUEUED
    assert services.job_queue.dequeue() is None

    # 4. interrupted in-flight job converges to failed; not resumed, not requeued.
    assert report.interrupted_failed == [RUNNING]
    interrupted = repository.get(RUNNING)
    assert interrupted.status == "failed"
    assert "process_interrupted" in (interrupted.error_message or "")

    # 5. succeeded job untouched (status/result byte-identical) and completion converged.
    assert report.completion_outcomes.get(SUCCEEDED) == CompletionOutcome.DONE
    assert repository.get(SUCCEEDED).completion_state == "done"
    raw = _raw_rows(db_path)[SUCCEEDED]
    assert raw[1] == "succeeded"
    assert raw[3] == (
        '{"error_message": null, "job_id": "job_fx_succeeded", "metadata": {"seed": 7}, '
        '"outputs": ["outputs/images/job_fx_succeeded.png"], "previews": [], "status": "succeeded"}'
    )

    # 6/7. Story loads; existing scene asset ids/bindings preserved; asset record intact.
    story = services.story_repository.get("story_fixture")
    assert story is not None
    scenes = {scene.id: scene for scene in story.scenes}
    assert scenes["scene_1"].asset_ids == {"visual": ASSET_ID}
    assert scenes["scene_1"].job_ids == [SUCCEEDED]
    assert scenes["scene_2"].asset_ids == {} and scenes["scene_2"].job_ids == []
    assert scenes["scene_3"].asset_ids == {} and scenes["scene_3"].job_ids == []
    assert services.asset_repository.get(ASSET_ID) is not None
    assert services.asset_repository.get_primary_by_job(SUCCEEDED).id == ASSET_ID
    # Nothing rewrote the baseline-shaped Story/Asset files.
    story_file = data_dir / "stories" / "story_fixture.json"
    assert story_file.read_text(encoding="utf-8") == baseline_story
    assert (data_dir / "assets" / f"{ASSET_ID}.json").read_bytes() == baseline_asset


def test_generators_run_only_for_the_queued_job(data_dir, tmp_path):
    services, _ = _start(data_dir, tmp_path)
    generator = CountingGenerator()
    runner = JobRunner(
        services.job_repository,
        services.job_queue,
        GeneratorRegistry({"image": generator}),
        services.event_bus,
        job_service=services.job_service,
    )
    while runner.run_once() is not None:
        pass

    # Only the re-enqueued queued job runs; succeeded/interrupted jobs are never regenerated.
    assert generator.job_prompts == ["a storm at night"]
    assert services.job_repository.get(SUCCEEDED).status == "succeeded"
    assert services.job_repository.get(RUNNING).status == "failed"


def test_upgrade_and_recovery_are_idempotent(data_dir, tmp_path):
    db_path = data_dir / "jobs.db"
    first_services, _ = _start(data_dir, tmp_path)
    first_rows = _raw_rows(db_path)
    first_columns = _columns(db_path)
    first_indexes = _indexes(db_path)
    first_story = first_services.story_repository.get("story_fixture").model_dump()

    # A second full process start over the already-upgraded directory.
    second_services, second_report = _start(data_dir, tmp_path)

    assert _columns(db_path) == first_columns
    assert _indexes(db_path) == first_indexes
    second_rows = _raw_rows(db_path)
    # Same rows, same state (updated_at is deliberately not selected; nothing else moves).
    assert second_rows == first_rows
    assert second_services.story_repository.get("story_fixture").model_dump() == first_story
    # Nothing new is failed/cancelled the second time; the already-failed job is not re-processed.
    assert second_report.interrupted_failed == []
    assert second_report.cancel_requested_cancelled == []
    assert second_report.poison_rows == {}
    # The queued job is the only thing re-enqueued (queue is in-memory, so it is per-process).
    assert second_report.requeued == [QUEUED]
    assert second_services.job_queue.dequeue() == QUEUED
    assert second_services.job_queue.dequeue() is None
