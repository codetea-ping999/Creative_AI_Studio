"""Control-flow tests for scripts/agent_broker.py's fallback decision logic.

These exercise `run_task` against a throwaway local git repo with the real
`claude`/`codex` subprocess runners monkeypatched out -- no network calls,
no spend. What's under test is the state machine: success / blocked /
failed / fallback-triggered, and that a dirty worktree after a primary
failure always wins over falling back.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_broker  # noqa: E402
from _cross_agent_common import DEPTH_ENV_VAR  # noqa: E402


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def unique_task_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup_worktree_root():
    yield
    root = Path(agent_broker.tempfile.gettempdir()) / "agent-broker-worktrees"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _outcome(
    exit_code=0, stdout="", stderr="", output_text="ok", timed_out=False, launch_error=None
):
    return agent_broker.AgentRunOutcome(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        output_text=output_text,
        timed_out=timed_out,
        launch_error=launch_error,
    )


def _make_runner(outcome, *, side_effect=None):
    def runner(prompt, worktree_path, mode, model, timeout_seconds):
        if side_effect is not None:
            side_effect(worktree_path)
        return outcome

    return runner


def _write_dirty_file(worktree_path: Path) -> None:
    (worktree_path / "untracked.txt").write_text("wip\n", encoding="utf-8")


def test_success_prunes_clean_worktree(source_repo, unique_task_id, monkeypatch):
    monkeypatch.setitem(
        agent_broker._RUNNERS, "claude", _make_runner(_outcome(exit_code=0, output_text="done"))
    )
    task = {
        "task_id": unique_task_id,
        "repo_path": str(source_repo),
        "prompt": "say hi",
        "primary_agent": "claude",
        "mode": "read_only",
    }
    result = agent_broker.run_task(task)
    assert result.status == "success"
    assert result.agent_used == "claude"
    assert result.fallback_triggered is False
    assert result.dirty is False
    assert result.worktree_path is None  # pruned since nothing changed


def test_dirty_failure_is_blocked_not_fallback(source_repo, unique_task_id, monkeypatch):
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "claude",
        _make_runner(_outcome(exit_code=1, stderr="boom"), side_effect=_write_dirty_file),
    )
    codex_called = []
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "codex",
        _make_runner(_outcome(exit_code=0), side_effect=lambda p: codex_called.append(p)),
    )
    task = {
        "task_id": unique_task_id,
        "repo_path": str(source_repo),
        "prompt": "edit something",
        "primary_agent": "claude",
        "mode": "write",
    }
    result = agent_broker.run_task(task)
    assert result.status == "blocked"
    assert result.fallback_triggered is False
    assert codex_called == []  # fallback must never run against a dirty worktree
    assert result.dirty is True
    assert result.worktree_path is not None
    assert Path(result.worktree_path).exists()


def test_unrecognized_failure_does_not_fall_back(source_repo, unique_task_id, monkeypatch):
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "claude",
        _make_runner(_outcome(exit_code=1, stderr="some ordinary bug")),
    )
    codex_called = []
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "codex",
        _make_runner(_outcome(exit_code=0), side_effect=lambda p: codex_called.append(p)),
    )
    task = {
        "task_id": unique_task_id,
        "repo_path": str(source_repo),
        "prompt": "do something",
        "primary_agent": "claude",
        "mode": "read_only",
    }
    result = agent_broker.run_task(task)
    assert result.status == "failed"
    assert result.fallback_triggered is False
    assert codex_called == []


def test_quota_failure_falls_back_once_and_succeeds(source_repo, unique_task_id, monkeypatch):
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "claude",
        _make_runner(_outcome(exit_code=1, stderr="Error: usage limit reached for this account")),
    )
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "codex",
        _make_runner(_outcome(exit_code=0, output_text="handled by codex")),
    )
    task = {
        "task_id": unique_task_id,
        "repo_path": str(source_repo),
        "prompt": "do something",
        "primary_agent": "claude",
        "mode": "read_only",
    }
    result = agent_broker.run_task(task)
    assert result.status == "success"
    assert result.agent_used == "codex"
    assert result.fallback_triggered is True
    assert result.fallback_reason == "quota_exceeded"
    assert result.output_text == "handled by codex"


def test_fallback_agent_none_skips_second_hop(source_repo, unique_task_id, monkeypatch):
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "claude",
        _make_runner(_outcome(exit_code=1, stderr="rate limit exceeded")),
    )
    codex_called = []
    monkeypatch.setitem(
        agent_broker._RUNNERS,
        "codex",
        _make_runner(_outcome(exit_code=0), side_effect=lambda p: codex_called.append(p)),
    )
    task = {
        "task_id": unique_task_id,
        "repo_path": str(source_repo),
        "prompt": "do something",
        "primary_agent": "claude",
        "fallback_agent": "none",
        "mode": "read_only",
    }
    result = agent_broker.run_task(task)
    assert result.status == "failed"
    assert result.fallback_triggered is False
    assert codex_called == []


def test_recursion_depth_guard_blocks_before_worktree(source_repo, unique_task_id, monkeypatch):
    monkeypatch.setenv(DEPTH_ENV_VAR, "1")
    task = {
        "task_id": unique_task_id,
        "repo_path": str(source_repo),
        "prompt": "do something",
        "primary_agent": "claude",
    }
    result = agent_broker.run_task(task)
    assert result.status == "blocked"
    assert result.worktree_path is None


def test_load_task_rejects_missing_fields(tmp_path):
    task_file = tmp_path / "task.json"
    task_file.write_text('{"task_id": "x"}', encoding="utf-8")
    with pytest.raises(ValueError):
        agent_broker._load_task(task_file)


def test_load_task_rejects_unknown_agent(tmp_path):
    task_file = tmp_path / "task.json"
    task_file.write_text(
        '{"task_id": "x", "repo_path": "/tmp", "prompt": "p", "primary_agent": "gpt5"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        agent_broker._load_task(task_file)
