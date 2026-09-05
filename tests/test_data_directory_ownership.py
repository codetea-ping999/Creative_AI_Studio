"""OS ownership must survive a stale lock file, but never a dead owner."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
from threading import Event

import pytest


LOCK_SCRIPT = """
import sys
from core.storage.ownership import DataDirectoryOwnership, DataDirectoryInUseError
try:
    owner = DataDirectoryOwnership(sys.argv[1])
    owner.acquire()
except DataDirectoryInUseError:
    print('busy', flush=True)
    sys.exit(23)
print('acquired', flush=True)
if len(sys.argv) > 2:
    sys.stdin.read()
owner.release()
"""


def child_env():
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}


@pytest.mark.parametrize("termination", ["terminate", "kill"])
def test_second_process_is_excluded_until_owner_exits(tmp_path, termination):
    owner = subprocess.Popen(
        [sys.executable, "-c", LOCK_SCRIPT, str(tmp_path), "hold"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=child_env(), cwd=tmp_path,
    )
    try:
        assert owner.stdout.readline().strip() == "acquired"
        contender = subprocess.run(
            [sys.executable, "-c", LOCK_SCRIPT, str(tmp_path / ".")],
            capture_output=True, text=True, env=child_env(), cwd=tmp_path, timeout=10,
        )
        assert contender.returncode == 23, contender.stderr
        assert contender.stdout.strip() == "busy"
        getattr(owner, termination)()
        owner.wait(timeout=10)
        successor = subprocess.run(
            [sys.executable, "-c", LOCK_SCRIPT, str(tmp_path)],
            capture_output=True, text=True, env=child_env(), cwd=tmp_path, timeout=10,
        )
        assert successor.returncode == 0, successor.stderr
        assert successor.stdout.strip() == "acquired"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)


def test_independent_directories_and_explicit_release(tmp_path):
    from core.storage.ownership import DataDirectoryOwnership, DataDirectoryInUseError

    first = DataDirectoryOwnership(tmp_path / "a")
    other = DataDirectoryOwnership(tmp_path / "b")
    contender = DataDirectoryOwnership(tmp_path / "a")
    first.acquire()
    other.acquire()
    try:
        with pytest.raises(DataDirectoryInUseError):
            contender.acquire()
        first.release()
        contender.acquire()
        contender.release()
        first.release()  # Safe cleanup after an earlier release.
    finally:
        first.release()
        other.release()
        contender.release()


def test_api_owns_directory_before_startup_sync_and_does_not_recover(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from bootstrap import create_application_services
    from core.storage.ownership import DataDirectoryInUseError
    from core.jobs.statuses import JOB_STATUSES
    from tests.test_job_hardening import seed_job

    services = create_application_services(
        db_path=tmp_path / "jobs.db", output_dir=tmp_path / "outputs" / "images",
    )
    jobs = [seed_job(services.job_repository, status, "job_" + status)
            for status in JOB_STATUSES]
    sync_calls = []
    monkeypatch.setattr(services.asset_repository, "sync_jobs", lambda jobs: sync_calls.append(1))
    first = create_app(services, start_job_runner=False)
    second = create_app(services, start_job_runner=False)
    with TestClient(first):
        with pytest.raises(DataDirectoryInUseError), TestClient(second):
            pass
        assert len(sync_calls) == 1
        assert [services.job_repository.get(job.id) for job in jobs] == jobs
        assert services.job_queue.dequeue() is None
    with TestClient(second):
        assert len(sync_calls) == 2


def test_startup_sync_failure_releases_ownership(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from bootstrap import create_application_services
    from core.storage.ownership import DataDirectoryOwnership

    services = create_application_services(
        db_path=tmp_path / "jobs.db", output_dir=tmp_path / "outputs" / "images",
    )

    def fail_sync(jobs):
        raise OSError("injected startup failure")

    monkeypatch.setattr(services.asset_repository, "sync_jobs", fail_sync)
    with pytest.raises(OSError, match="injected startup failure"):
        with TestClient(create_app(services, start_job_runner=False)):
            pass
    successor = DataDirectoryOwnership(tmp_path)
    successor.acquire()
    successor.release()


def test_shutdown_timeout_retains_ownership_until_worker_exits(tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from bootstrap import create_application_services
    from core.storage.ownership import DataDirectoryInUseError, DataDirectoryOwnership

    services = create_application_services(
        db_path=tmp_path / "jobs.db", output_dir=tmp_path / "outputs" / "images",
    )
    entered, finish, released = Event(), Event(), Event()
    original_release = DataDirectoryOwnership.release

    def observed_release(self):
        held = self._fd is not None
        original_release(self)
        if held:
            released.set()

    def blocked_worker(**kwargs):
        entered.set()
        assert finish.wait(10)

    monkeypatch.setattr(DataDirectoryOwnership, "release", observed_release)
    monkeypatch.setattr(services.job_runner, "run_forever", blocked_worker)
    app = create_app(services)
    contender = DataDirectoryOwnership(tmp_path)
    try:
        with TestClient(app):
            assert entered.wait(5)
            # Exiting lifespan requests stop, but the worker is still busy.
        assert app.state.job_runner_thread.is_alive()
        with pytest.raises(DataDirectoryInUseError):
            contender.acquire()
        assert not released.is_set()
        assert "retaining data-directory ownership" in caplog.text
    finally:
        finish.set()
        app.state.job_runner_thread.join(timeout=5)
        contender.release()
    assert released.wait(5)
    contender.acquire()
    contender.release()


def test_directory_alias_cannot_bypass_ownership(tmp_path):
    from core.storage.ownership import DataDirectoryInUseError, DataDirectoryOwnership

    directory = tmp_path / "real"
    owner = DataDirectoryOwnership(directory)
    owner.acquire()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    contender = DataDirectoryOwnership(alias)
    try:
        with pytest.raises(DataDirectoryInUseError):
            contender.acquire()
    finally:
        contender.release()
        owner.release()
