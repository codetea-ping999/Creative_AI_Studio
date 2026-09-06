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

STARTUP_SCRIPT = """
import os
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from core.storage.ownership import DataDirectoryInUseError

data_dir = Path(sys.argv[1])
marker = Path(sys.argv[2])
os.environ['DB_PATH'] = str(data_dir / 'jobs.db')
os.environ['OUTPUT_DIR'] = str(data_dir / 'outputs')
import apps.api.main as main_module

original_factory = main_module.create_application_services
def observed_factory():
    marker.write_text('factory-called')
    return original_factory()
main_module.create_application_services = observed_factory

try:
    with TestClient(main_module.create_app(start_job_runner=False)):
        print('started', flush=True)
        sys.stdin.read()
except DataDirectoryInUseError:
    print('busy', flush=True)
    raise SystemExit(23)
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


def test_api_startup_claims_ownership_before_repository_initialization(tmp_path):
    owner_marker = tmp_path / "owner-factory"
    loser_marker = tmp_path / "loser-factory"
    owner = subprocess.Popen(
        [sys.executable, "-c", STARTUP_SCRIPT, str(tmp_path), str(owner_marker)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=child_env(), cwd=tmp_path,
    )
    try:
        assert owner.stdout.readline().strip() == "started"
        assert owner_marker.read_text() == "factory-called"

        loser = subprocess.run(
            [sys.executable, "-c", STARTUP_SCRIPT, str(tmp_path), str(loser_marker)],
            input="", capture_output=True, text=True, env=child_env(), cwd=tmp_path,
            timeout=10,
        )
        assert loser.returncode == 23, loser.stderr
        assert loser.stdout.strip() == "busy"
        assert not loser_marker.exists(), "loser initialized repositories before ownership"
    finally:
        if owner.poll() is None:
            owner.stdin.close()
            owner.wait(timeout=10)

    successor_marker = tmp_path / "successor-factory"
    successor = subprocess.run(
        [sys.executable, "-c", STARTUP_SCRIPT, str(tmp_path), str(successor_marker)],
        input="", capture_output=True, text=True, env=child_env(), cwd=tmp_path,
        timeout=10,
    )
    assert successor.returncode == 0, successor.stderr
    assert successor.stdout.strip() == "started"
    assert successor_marker.read_text() == "factory-called"


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
    # PR3 note: startup recovery (core.jobs.startup_recovery.run_startup_recovery)
    # now *does* mutate/enqueue jobs on the actual owner's startup -- this
    # test's own name predates that (classify_job's pre-PR3 docstring: "the
    # caller must first establish exclusive process ownership ... (a later
    # PR)"). Its real contract -- ownership exclusivity, and startup work
    # running exactly once per successful acquire, never for a failed
    # contender -- is unchanged, so the injection point moves from the old
    # asset-sync hook to the new startup-recovery hook rather than actually
    # running recovery here (which would make "nothing changed" no longer
    # true by design).
    from fastapi.testclient import TestClient
    from apps.api import main as main_module
    from bootstrap import create_application_services
    from core.jobs.startup_recovery import StartupRecoveryReport
    from core.storage.ownership import DataDirectoryInUseError
    from core.jobs.statuses import JOB_STATUSES
    from tests.test_job_hardening import seed_job

    services = create_application_services(
        db_path=tmp_path / "jobs.db", output_dir=tmp_path / "outputs" / "images",
    )
    jobs = [seed_job(services.job_repository, status, "job_" + status)
            for status in JOB_STATUSES]
    sync_calls = []

    def fake_recovery(*args, **kwargs):
        sync_calls.append(1)
        return StartupRecoveryReport()

    monkeypatch.setattr(main_module, "run_startup_recovery", fake_recovery)
    first = main_module.create_app(services, start_job_runner=False)
    second = main_module.create_app(services, start_job_runner=False)
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
    from apps.api import main as main_module
    from bootstrap import create_application_services
    from core.storage.ownership import DataDirectoryOwnership

    services = create_application_services(
        db_path=tmp_path / "jobs.db", output_dir=tmp_path / "outputs" / "images",
    )

    def fail_recovery(*args, **kwargs):
        raise OSError("injected startup failure")

    monkeypatch.setattr(main_module, "run_startup_recovery", fail_recovery)
    with pytest.raises(OSError, match="injected startup failure"):
        with TestClient(main_module.create_app(services, start_job_runner=False)):
            pass
    successor = DataDirectoryOwnership(tmp_path)
    successor.acquire()
    successor.release()


def test_default_service_factory_failure_releases_ownership(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import main as main_module
    from core.storage.ownership import DataDirectoryOwnership

    monkeypatch.setenv("DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))

    def fail_factory():
        raise RuntimeError("injected service construction failure")

    monkeypatch.setattr(main_module, "create_application_services", fail_factory)
    with pytest.raises(RuntimeError, match="service construction failure"):
        with TestClient(main_module.create_app(start_job_runner=False)):
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
