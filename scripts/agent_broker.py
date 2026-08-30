#!/usr/bin/env python3
"""Headless task runner for the Claude Code <-> Codex cross-agent harness.

Usage:
    venv/bin/python scripts/agent_broker.py --json doctor
    venv/bin/python scripts/agent_broker.py --json run --task-file /absolute/path/to/task.json

`doctor` reports whether `claude` and `codex` are on PATH and authenticated,
without running any task.

`run` executes exactly one task (see .agents/protocol/v1/task.schema.json):
it creates a fresh dedicated git worktree, runs `primary_agent` in it, and
falls back to `fallback_agent` ONLY if the primary run fails with a
recognized quota / auth / service-unavailable signal AND left the worktree
clean. Every other outcome (ordinary task failure, or a dirty worktree left
behind) is reported as-is with no second hop -- see
.agents/protocol/v1/worker-policy.md for the full rationale.

The broker never runs `git add`/`commit`/`push` itself, never deletes a
worktree that has any changes in it, and never lets either CLI fall back to
API-key billing (env vars are stripped the same way claude_code_mcp_bridge.py
strips them, via the shared `_cross_agent_common` module).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cross_agent_common import (  # noqa: E402
    DELEGATION_GUARD_PROMPT,
    DEPTH_ENV_VAR,
    MAX_DELEGATION_DEPTH,
    classify_transient_failure,
    incoming_delegation_depth,
    is_any_git_dir,
    sanitized_child_env,
)

AGENTS = ("claude", "codex")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _other_agent(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


@dataclass
class AgentRunOutcome:
    exit_code: int
    stdout: str
    stderr: str
    output_text: str
    timed_out: bool = False
    launch_error: str | None = None


@dataclass
class TaskResult:
    task_id: str
    status: str  # "success" | "blocked" | "failed"
    agent_used: str
    fallback_triggered: bool
    fallback_reason: str | None
    worktree_path: str | None
    branch: str | None
    dirty: bool
    output_text: str
    error_message: str | None
    started_at: str
    ended_at: str
    duration_seconds: float
    attempts: list[dict[str, Any]] = field(default_factory=list)


def check_binary(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    if path is None:
        return {"available": False, "path": None, "authenticated": None, "detail": "not on PATH"}
    try:
        version = subprocess.run(
            [name, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
        detail = (version.stdout or version.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": True,
            "path": path,
            "authenticated": None,
            "detail": f"--version failed: {exc}",
        }

    authenticated: bool | None = None
    if name == "codex":
        try:
            login = subprocess.run(
                [name, "login", "status"], capture_output=True, text=True, timeout=15, check=False
            )
            authenticated = login.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            authenticated = None
    # `claude` has no cheap non-interactive "am I logged in" check that
    # doesn't itself spend a turn; leave authenticated=None and let `run`'s
    # own preflight (the first real invocation) surface an auth failure.
    return {"available": True, "path": path, "authenticated": authenticated, "detail": detail}


def doctor() -> dict[str, Any]:
    return {agent: check_binary(agent) for agent in AGENTS}


def _create_worktree(repo_path: Path, base_ref: str, task_id: str) -> tuple[Path, str]:
    if not is_any_git_dir(repo_path):
        raise ValueError(f"repo_path is not a git working tree: {repo_path}")
    branch = f"agent-broker/{task_id}"
    worktree_root = Path(tempfile.gettempdir()) / "agent-broker-worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / task_id
    if worktree_path.exists():
        raise ValueError(f"worktree path already exists, refusing to reuse it: {worktree_path}")
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), base_ref],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree_path, branch


def _worktree_is_dirty(worktree_path: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def _maybe_prune_clean_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Remove the worktree only if the agent made zero changes in it.

    A dirty worktree is left in place unconditionally -- it may be the only
    record of work a failed/blocked run produced, and only a human should
    decide whether to keep or discard it.
    """

    if _worktree_is_dirty(worktree_path):
        return
    subprocess.run(
        ["git", "worktree", "remove", str(worktree_path)],
        cwd=str(repo_path),
        check=False,
        capture_output=True,
        text=True,
    )


def _run_claude(
    prompt: str, worktree_path: Path, mode: str, model: str | None, timeout_seconds: float
) -> AgentRunOutcome:
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return AgentRunOutcome(1, "", "", "", launch_error="`claude` not found on PATH")
    command = [
        claude_bin,
        "-p",
        prompt,
        "--add-dir",
        str(worktree_path),
        "--output-format",
        "json",
        "--permission-mode",
        "plan" if mode == "read_only" else "acceptEdits",
        "--append-system-prompt",
        DELEGATION_GUARD_PROMPT,
    ]
    if model:
        command += ["--model", model]
    env = sanitized_child_env()
    env[DEPTH_ENV_VAR] = str(incoming_delegation_depth() + 1)
    try:
        completed = subprocess.run(
            command,
            cwd=str(worktree_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AgentRunOutcome(1, "", "", "", timed_out=True)

    output_text = completed.stdout
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        output_text = payload.get("result", completed.stdout)
    except json.JSONDecodeError:
        pass
    return AgentRunOutcome(completed.returncode, completed.stdout, completed.stderr, output_text)


def _run_codex(
    prompt: str, worktree_path: Path, mode: str, model: str | None, timeout_seconds: float
) -> AgentRunOutcome:
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        return AgentRunOutcome(1, "", "", "", launch_error="`codex` not found on PATH")
    with tempfile.NamedTemporaryFile(
        prefix="agent-broker-codex-", suffix=".txt", delete=False
    ) as last_message_file:
        last_message_path = Path(last_message_file.name)
    command = [
        codex_bin,
        "exec",
        "--cd",
        str(worktree_path),
        "--sandbox",
        "read-only" if mode == "read_only" else "workspace-write",
        "--output-last-message",
        str(last_message_path),
        # Explicit --sandbox/--approve-for-me above always win over config
        # file values, so this only drops the user's personal MCP servers
        # (Linear, Figma, ...) and plugins for this headless run -- avoids
        # their unrelated auth-failure noise on stderr being misread as a
        # quota/auth signal for the task itself by classify_transient_failure.
        "--ignore-user-config",
    ]
    if mode == "write":
        # `codex exec` has no interactive approval loop to answer; without
        # this, anything the workspace-write sandbox would otherwise block
        # just fails instead of being auto-reviewed and allowed.
        command += ["--approve-for-me"]
    if model:
        command += ["--model", model]
    command.append(f"{DELEGATION_GUARD_PROMPT}\n\n{prompt}")
    env = sanitized_child_env()
    env[DEPTH_ENV_VAR] = str(incoming_delegation_depth() + 1)
    try:
        completed = subprocess.run(
            command,
            cwd=str(worktree_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        last_message_path.unlink(missing_ok=True)
        return AgentRunOutcome(1, "", "", "", timed_out=True)

    output_text = ""
    if last_message_path.exists():
        output_text = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
        last_message_path.unlink(missing_ok=True)
    if not output_text:
        output_text = completed.stdout
    return AgentRunOutcome(completed.returncode, completed.stdout, completed.stderr, output_text)


_RUNNERS = {"claude": _run_claude, "codex": _run_codex}


def run_task(task: dict[str, Any]) -> TaskResult:
    task_id = task["task_id"]
    repo_path = Path(task["repo_path"]).expanduser().resolve()
    base_ref = task.get("base_ref", "HEAD")
    prompt = task["prompt"]
    primary_agent = task["primary_agent"]
    fallback_agent = task.get("fallback_agent", _other_agent(primary_agent))
    mode = task.get("mode", "write")
    timeout_seconds = float(task.get("timeout_seconds", 3600))
    primary_model = task.get("primary_model")
    fallback_model = task.get("fallback_model")

    started_at = _now_iso()
    incoming_depth = incoming_delegation_depth()
    if incoming_depth >= MAX_DELEGATION_DEPTH:
        return TaskResult(
            task_id=task_id,
            status="blocked",
            agent_used=primary_agent,
            fallback_triggered=False,
            fallback_reason=None,
            worktree_path=None,
            branch=None,
            dirty=False,
            output_text="",
            error_message=(
                f"Recursive delegation blocked: already at depth {incoming_depth} "
                f"(max {MAX_DELEGATION_DEPTH})."
            ),
            started_at=started_at,
            ended_at=_now_iso(),
            duration_seconds=0.0,
        )

    try:
        worktree_path, branch = _create_worktree(repo_path, base_ref, task_id)
    except (ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        return TaskResult(
            task_id=task_id,
            status="failed",
            agent_used=primary_agent,
            fallback_triggered=False,
            fallback_reason=None,
            worktree_path=None,
            branch=None,
            dirty=False,
            output_text="",
            error_message=f"Failed to create dedicated worktree: {detail}",
            started_at=started_at,
            ended_at=_now_iso(),
            duration_seconds=0.0,
        )

    attempts: list[dict[str, Any]] = []

    def _attempt(agent: str, model: str | None) -> AgentRunOutcome:
        outcome = _RUNNERS[agent](prompt, worktree_path, mode, model, timeout_seconds)
        attempts.append(
            {
                "agent": agent,
                "exit_code": outcome.exit_code,
                "timed_out": outcome.timed_out,
                "launch_error": outcome.launch_error,
                "dirty_after": _worktree_is_dirty(worktree_path),
            }
        )
        return outcome

    def _failure_message(
        agent: str, outcome: AgentRunOutcome, *, unrecognized: bool = False
    ) -> str:
        if outcome.launch_error:
            return outcome.launch_error
        if outcome.timed_out:
            return f"{agent} timed out after {timeout_seconds:.0f}s"
        suffix = " (not a recognized quota/auth/service failure)" if unrecognized else ""
        return f"{agent} exited with status {outcome.exit_code}{suffix}"

    agent_used = primary_agent
    fallback_triggered = False
    fallback_reason: str | None = None

    primary_outcome = _attempt(primary_agent, primary_model)
    if (
        primary_outcome.launch_error is None
        and not primary_outcome.timed_out
        and primary_outcome.exit_code == 0
    ):
        final_status = "success"
        final_output = primary_outcome.output_text
        final_error: str | None = None
    elif _worktree_is_dirty(worktree_path):
        final_status = "blocked"
        final_output = primary_outcome.output_text
        final_error = (
            f"{primary_agent} failed and left uncommitted changes in {worktree_path}; "
            "not falling back to avoid a second agent compounding on unreviewed work."
        )
    else:
        reason = (
            None
            if primary_outcome.timed_out
            else classify_transient_failure(
                primary_outcome.exit_code, primary_outcome.stdout, primary_outcome.stderr
            )
        )
        can_fall_back = reason is not None and fallback_agent not in ("none", primary_agent)
        if not can_fall_back:
            final_status = "failed"
            final_output = primary_outcome.output_text
            final_error = _failure_message(
                primary_agent, primary_outcome, unrecognized=reason is None
            )
        else:
            fallback_triggered = True
            fallback_reason = reason
            fallback_outcome = _attempt(fallback_agent, fallback_model)
            agent_used = fallback_agent
            if (
                fallback_outcome.launch_error is None
                and not fallback_outcome.timed_out
                and fallback_outcome.exit_code == 0
            ):
                final_status = "success"
                final_output = fallback_outcome.output_text
                final_error = None
            else:
                final_status = "failed"
                final_output = fallback_outcome.output_text
                final_error = _failure_message(fallback_agent, fallback_outcome)

    ended_at = _now_iso()
    dirty = _worktree_is_dirty(worktree_path)
    duration = (
        datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
    ).total_seconds()

    if final_status == "success" and not dirty:
        _maybe_prune_clean_worktree(repo_path, worktree_path)

    return TaskResult(
        task_id=task_id,
        status=final_status,
        agent_used=agent_used,
        fallback_triggered=fallback_triggered,
        fallback_reason=fallback_reason,
        worktree_path=str(worktree_path) if worktree_path.exists() else None,
        branch=branch,
        dirty=dirty,
        output_text=final_output,
        error_message=final_error,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        attempts=attempts,
    )


def _load_task(task_file: Path) -> dict[str, Any]:
    task = json.loads(task_file.read_text(encoding="utf-8"))
    required = ("task_id", "repo_path", "prompt", "primary_agent")
    missing = [key for key in required if key not in task]
    if missing:
        raise ValueError(f"task file missing required field(s): {', '.join(missing)}")
    if task["primary_agent"] not in AGENTS:
        raise ValueError(f"primary_agent must be one of {AGENTS}")
    if "fallback_agent" in task and task["fallback_agent"] not in (*AGENTS, "none"):
        raise ValueError(f"fallback_agent must be one of {(*AGENTS, 'none')}")
    if "task_id" in task and not task["task_id"]:
        task["task_id"] = uuid.uuid4().hex
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Check claude/codex CLI availability and auth.")
    run_parser = subparsers.add_parser("run", help="Execute one task from a task file.")
    run_parser.add_argument("--task-file", required=True, type=Path)

    args = parser.parse_args()

    if args.command == "doctor":
        result: Any = doctor()
    elif args.command == "run":
        try:
            task = _load_task(args.task_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Invalid task file: {exc}", file=sys.stderr)
            sys.exit(2)
        outcome = run_task(task)
        result = asdict(outcome)
    else:  # pragma: no cover - argparse enforces valid subcommands
        parser.error("unknown command")
        return

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result)

    if args.command == "run" and result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
