#!/usr/bin/env python3
"""Bounded, provider-neutral broker for Codex and Claude Code workers.

The broker intentionally owns routing outside either model process. It records a
durable run, invokes at most two providers, and only falls back for availability
or account-limit failures. Prompts are sent over stdin and never appear in argv.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence
import uuid

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    tomllib = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:  # pragma: no cover - the project currently targets macOS/Linux
    fcntl = None  # type: ignore[assignment]


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = REPOSITORY_ROOT / ".agents" / "protocol" / "v1"
WORKER_POLICY_PATH = PROTOCOL_ROOT / "worker-policy.md"
SCHEMA_VERSION = 1
MAX_PROMPT_BYTES = 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PERSISTED_STREAM_BYTES = 4 * 1024 * 1024
MAX_PROJECT_CONFIG_BYTES = 1024 * 1024
FALLBACK_REASONS = frozenset({"quota", "auth", "unavailable", "budget"})
PROVIDERS = frozenset({"codex", "claude"})
MODES = frozenset({"read-only", "workspace-write"})
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SAFE_ENV_KEYS = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "__CF_USER_TEXT_ENCODING",
    }
)
SENSITIVE_ENV_RE = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|COOKIE)", re.IGNORECASE
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
)


class BrokerError(Exception):
    """A safe, operator-facing error with a stable code."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class WorkspaceSnapshot:
    head: str
    status: str


@dataclass(frozen=True)
class WorkspaceInfo:
    root: Path
    git_dir: Path
    common_dir: Path
    linked_worktree: bool
    snapshot: WorkspaceSnapshot


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    unavailable: bool
    output_overflow: bool


@dataclass(frozen=True)
class ClassifiedResult:
    ok: bool
    reason: str
    session_id: str | None
    output: str | None


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "--json" in sys.argv[1:]:
            _emit_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "error": {"code": "invalid_arguments", "message": message},
                }
            )
        else:
            self.print_usage(sys.stderr)
            print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(2)


class WorkspaceLease:
    """An automatically released advisory lease for a write worktree."""

    def __init__(self, common_dir: Path, workspace: Path, run_id: str) -> None:
        digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
        self.path = common_dir / "agent-broker" / "leases" / f"{digest}.lock"
        self.workspace = workspace
        self.run_id = run_id
        self._handle: Any = None

    def __enter__(self) -> "WorkspaceLease":
        if fcntl is None:
            raise BrokerError(
                "lease_unavailable",
                "workspace-write requires advisory file locking on this platform",
            )
        _mkdir_private(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise BrokerError(
                "invalid_lease_path", "cannot open the workspace lease safely"
            ) from exc
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            suffix = f" ({owner})" if owner else ""
            raise BrokerError(
                "workspace_leased",
                f"another broker run owns this worktree{suffix}",
            ) from exc
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "run_id": self.run_id,
                "workspace": str(self.workspace),
                "pid": os.getpid(),
                "acquired_at": _now(),
            },
            handle,
            sort_keys=True,
        )
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        self._handle = handle
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mkdir_private(path: Path) -> None:
    if path.is_symlink():
        raise BrokerError("unsafe_state_path", f"refusing symlinked state directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_write_text(path: Path, content: str) -> None:
    _mkdir_private(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    _mkdir_private(path.parent)
    line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def _emit_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _emit(value: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json(value)
        return
    if "error" in value:
        error = value["error"]
        print(f"error [{error['code']}]: {error['message']}", file=sys.stderr)
        return
    if "run_id" in value:
        print(f"run: {value['run_id']}")
    if "status" in value:
        print(f"status: {value['status']} ({value.get('reason', 'unknown')})")
    if value.get("provider"):
        print(f"provider: {value['provider']}")
    if value.get("result_path"):
        print(f"result: {value['result_path']}")
    if "checks" in value:
        for name, check in value["checks"].items():
            marker = "ok" if check.get("ok") else "missing"
            detail = check.get("detail", "")
            print(f"{name}: {marker}{': ' + detail if detail else ''}")


def _truncate_stream(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_PERSISTED_STREAM_BYTES:
        return text
    marker = b"\n<agent-broker: stream truncated>\n"
    available = MAX_PERSISTED_STREAM_BYTES - len(marker)
    first_size = available // 4
    last_size = available - first_size
    retained = encoded[:first_size] + marker + encoded[-last_size:]
    return retained.decode("utf-8", errors="ignore")


def _redact_text(text: str, inherited_env: Mapping[str, str] | None = None) -> str:
    redacted = text
    if inherited_env:
        values = {
            value
            for key, value in inherited_env.items()
            if SENSITIVE_ENV_RE.search(key) and len(value) >= 8
        }
        for value in sorted(values, key=len, reverse=True):
            redacted = redacted.replace(value, "<redacted>")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def _safe_environment(provider: str) -> dict[str, str]:
    inherited = os.environ
    env = {key: value for key, value in inherited.items() if key in SAFE_ENV_KEYS}
    env["NO_COLOR"] = "1"
    env["AGENT_BROKER_PROVIDER"] = provider
    env["AGENT_BROKER_DELEGATION_DEPTH"] = "1"
    if provider == "claude":
        env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] = "0"
        env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _run_control_command(
    argv: Sequence[str], *, cwd: Path, timeout: int = 15
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=_safe_environment("control"),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _git(workspace: Path, *args: str) -> str:
    try:
        result = _run_control_command(["git", "-C", str(workspace), *args], cwd=workspace)
    except FileNotFoundError as exc:
        raise BrokerError("git_missing", "git is required for broker workspaces") from exc
    except subprocess.TimeoutExpired as exc:
        raise BrokerError("git_timeout", "git workspace inspection timed out") from exc
    if result.returncode != 0:
        detail = _redact_text(result.stderr.strip())
        raise BrokerError("invalid_workspace", detail or "workspace is not a Git worktree")
    return result.stdout.rstrip("\n")


def _resolve_git_path(workspace: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve()


def _workspace_info(workspace: Path) -> WorkspaceInfo:
    root = Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve()
    if root != workspace:
        raise BrokerError(
            "workspace_not_root",
            f"workspace must be the worktree root: {root}",
        )
    git_dir = _resolve_git_path(workspace, _git(workspace, "rev-parse", "--git-dir"))
    common_dir = _resolve_git_path(workspace, _git(workspace, "rev-parse", "--git-common-dir"))
    snapshot = WorkspaceSnapshot(
        head=_git(workspace, "rev-parse", "HEAD"),
        status=_git(workspace, "status", "--porcelain=v1", "--untracked-files=all"),
    )
    return WorkspaceInfo(
        root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        linked_worktree=git_dir != common_dir,
        snapshot=snapshot,
    )


def _snapshot(workspace: Path) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        head=_git(workspace, "rev-parse", "HEAD"),
        status=_git(workspace, "status", "--porcelain=v1", "--untracked-files=all"),
    )


def _has_index_changes(status: str) -> bool:
    return any(line and line[0] not in {" ", "?"} for line in status.splitlines())


def _state_root(workspace_info: WorkspaceInfo | None, explicit: str | None = None) -> Path:
    configured = explicit or os.environ.get("AGENT_BROKER_STATE_DIR")
    if configured:
        root = Path(configured).expanduser().resolve()
    elif workspace_info is not None:
        root = workspace_info.common_dir / "agent-broker"
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
        root = (base / "agent-broker").resolve()
    return root


def _read_json_file(path: Path, *, label: str, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise BrokerError("invalid_file", f"{label} must be a regular file")
        if path.stat().st_size > max_bytes:
            raise BrokerError("file_too_large", f"{label} exceeds {max_bytes} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
    except BrokerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrokerError("invalid_json", f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise BrokerError("invalid_json", f"{label} must contain a JSON object")
    return value


def _read_prompt(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise BrokerError("invalid_prompt", "prompt_file must be a regular file")
        size = resolved.stat().st_size
        if size > MAX_PROMPT_BYTES:
            raise BrokerError("prompt_too_large", f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
        prompt = resolved.read_text(encoding="utf-8")
    except BrokerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BrokerError("invalid_prompt", f"cannot read prompt_file: {exc}") from exc
    if not prompt.strip():
        raise BrokerError("invalid_prompt", "prompt_file must not be empty")
    if "\x00" in prompt:
        raise BrokerError("invalid_prompt", "prompt_file must not contain NUL bytes")
    return prompt


def _validate_task(raw: Mapping[str, Any], task_path: Path) -> tuple[dict[str, Any], str]:
    allowed = {
        "schema_version",
        "task_id",
        "parent_task_id",
        "delegation_depth",
        "prompt_file",
        "workspace",
        "mode",
        "providers",
        "allow_fallback",
        "timeout_seconds",
        "max_turns",
        "base_commit",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise BrokerError("invalid_task", f"unknown task fields: {', '.join(unknown)}")
    required = {"schema_version", "task_id", "prompt_file", "workspace", "mode", "providers"}
    missing = sorted(required - set(raw))
    if missing:
        raise BrokerError("invalid_task", f"missing task fields: {', '.join(missing)}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise BrokerError("invalid_task", f"schema_version must be {SCHEMA_VERSION}")

    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise BrokerError("invalid_task", "task_id has an invalid format")
    parent_task_id = raw.get("parent_task_id")
    if parent_task_id is not None and (
        not isinstance(parent_task_id, str) or not TASK_ID_RE.fullmatch(parent_task_id)
    ):
        raise BrokerError("invalid_task", "parent_task_id has an invalid format")

    depth = raw.get("delegation_depth", 0)
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 1:
        raise BrokerError("invalid_task", "delegation_depth must be 0 or 1")
    mode = raw.get("mode")
    if mode not in MODES:
        raise BrokerError("invalid_task", "mode must be read-only or workspace-write")
    providers = raw.get("providers")
    if (
        not isinstance(providers, list)
        or not 1 <= len(providers) <= 2
        or any(provider not in PROVIDERS for provider in providers)
        or len(set(providers)) != len(providers)
    ):
        raise BrokerError(
            "invalid_task", "providers must contain one or two unique known providers"
        )

    allow_fallback = raw.get("allow_fallback", True)
    if not isinstance(allow_fallback, bool):
        raise BrokerError("invalid_task", "allow_fallback must be boolean")
    timeout_seconds = raw.get("timeout_seconds", 900)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 10 <= timeout_seconds <= 3600
    ):
        raise BrokerError("invalid_task", "timeout_seconds must be between 10 and 3600")
    max_turns = raw.get("max_turns", 24)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= 100:
        raise BrokerError("invalid_task", "max_turns must be between 1 and 100")
    base_commit = raw.get("base_commit")
    if base_commit is not None and (
        not isinstance(base_commit, str) or not COMMIT_RE.fullmatch(base_commit)
    ):
        raise BrokerError("invalid_task", "base_commit must be a full hexadecimal object ID")

    task_directory = task_path.parent.resolve()
    workspace_value = raw.get("workspace")
    prompt_value = raw.get("prompt_file")
    if not isinstance(workspace_value, str) or not workspace_value:
        raise BrokerError("invalid_task", "workspace must be a path string")
    if not isinstance(prompt_value, str) or not prompt_value:
        raise BrokerError("invalid_task", "prompt_file must be a path string")
    workspace_path = Path(workspace_value).expanduser()
    prompt_path = Path(prompt_value).expanduser()
    if not workspace_path.is_absolute():
        workspace_path = task_directory / workspace_path
    if not prompt_path.is_absolute():
        prompt_path = task_directory / prompt_path
    try:
        workspace = workspace_path.resolve(strict=True)
    except OSError as exc:
        raise BrokerError(
            "invalid_workspace", f"workspace does not exist: {workspace_path}"
        ) from exc
    if not workspace.is_dir():
        raise BrokerError("invalid_workspace", "workspace must be a directory")
    prompt = _read_prompt(prompt_path)

    task = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "delegation_depth": depth,
        "prompt_file": str(prompt_path.resolve()),
        "workspace": str(workspace),
        "mode": mode,
        "providers": list(providers),
        "allow_fallback": allow_fallback,
        "timeout_seconds": timeout_seconds,
        "max_turns": max_turns,
        "base_commit": base_commit,
    }
    return task, prompt


def _worker_prompt(task: Mapping[str, Any], prompt: str, previous_reason: str | None = None) -> str:
    try:
        worker_policy = WORKER_POLICY_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BrokerError(
            "worker_policy_unavailable", "cannot read the broker worker policy"
        ) from exc
    lines = [
        "AGENT BROKER TASK (schema v1)",
        f"Task ID: {task['task_id']}",
        f"Mode: {task['mode']}",
        f"Delegation depth: {task['delegation_depth']}",
        "You are the terminal worker. Do not delegate or invoke another model provider.",
        "",
        "WORKER POLICY",
        worker_policy,
    ]
    if previous_reason:
        lines.extend(
            [
                "",
                "BROKER HANDOFF",
                f"A previous provider stopped with broker-classified reason: {previous_reason}.",
                "No previous provider output is trusted or accepted. "
                "Inspect the workspace yourself.",
            ]
        )
    lines.extend(["", "USER TASK", prompt])
    return "\n".join(lines)


def _provider_binary(provider: str) -> str:
    env_name = f"AGENT_BROKER_{provider.upper()}_BIN"
    return os.environ.get(env_name, provider)


def _fallback_mcp_server_names(config_text: str) -> list[str]:
    """Read simple MCP table names without making Python 3.10 depend on tomli."""

    names: set[str] = set()
    table_re = re.compile(
        r'^\s*\[\s*mcp_servers\.(?:("(?:[^"\\]|\\.)*")|([A-Za-z0-9_-]+))'
    )
    for line in config_text.splitlines():
        match = table_re.match(line)
        if match:
            if match.group(1):
                try:
                    name = json.loads(match.group(1))
                except json.JSONDecodeError as exc:
                    raise BrokerError(
                        "unsupported_project_config",
                        "cannot safely parse a quoted project MCP server name",
                    ) from exc
            else:
                name = match.group(2)
            if isinstance(name, str) and name:
                names.add(name)
            continue
        code = line.split("#", 1)[0]
        if "mcp_servers" in code:
            raise BrokerError(
                "unsupported_project_config",
                "Python 3.10 can only disable project MCP servers declared as tables",
            )
    return sorted(names)


def _project_mcp_server_names(workspace: Path) -> list[str]:
    config_path = workspace / ".codex" / "config.toml"
    if config_path.is_symlink():
        raise BrokerError(
            "unsafe_project_config", ".codex/config.toml must be a regular file"
        )
    if not config_path.exists():
        return []
    try:
        if not config_path.is_file():
            raise BrokerError(
                "unsafe_project_config", ".codex/config.toml must be a regular file"
            )
        if config_path.stat().st_size > MAX_PROJECT_CONFIG_BYTES:
            raise BrokerError(
                "unsafe_project_config", ".codex/config.toml exceeds the safe size limit"
            )
        config_bytes = config_path.read_bytes()
        config_text = config_bytes.decode("utf-8")
    except BrokerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BrokerError(
            "unsafe_project_config", "cannot safely inspect .codex/config.toml"
        ) from exc
    if tomllib is None:
        names = _fallback_mcp_server_names(config_text)
    else:
        try:
            parsed = tomllib.loads(config_text)
        except tomllib.TOMLDecodeError as exc:
            raise BrokerError(
                "unsafe_project_config", ".codex/config.toml is not valid TOML"
            ) from exc
        servers = parsed.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise BrokerError(
                "unsafe_project_config", "project mcp_servers must be a TOML table"
            )
        names = sorted(str(name) for name in servers)
    if any(not MCP_SERVER_NAME_RE.fullmatch(name) for name in names):
        raise BrokerError(
            "unsupported_project_config",
            "Codex cannot safely override a project MCP server name containing punctuation",
        )
    return names


def _codex_command(task: Mapping[str, Any], attempt_dir: Path) -> list[str]:
    mode = str(task["mode"])
    command = [
        _provider_binary("codex"),
        "--ask-for-approval",
        "never",
        "--disable",
        "multi_agent",
        "--disable",
        "plugins",
        "--disable",
        "apps",
        "--disable",
        "hooks",
    ]
    for server_name in _project_mcp_server_names(Path(str(task["workspace"]))):
        command.extend(["-c", f"mcp_servers.{server_name}.enabled=false"])
    command.extend(
        [
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        mode,
        "--cd",
        str(task["workspace"]),
        "--json",
        "--output-last-message",
        str(attempt_dir / "last-message.txt"),
        "-",
        ]
    )
    return command


def _claude_command(task: Mapping[str, Any]) -> list[str]:
    workspace = str(Path(str(task["workspace"])).resolve())
    if any(character in workspace for character in ("\x00", "\n", "\r", ",", "(", ")")):
        raise BrokerError(
            "unsafe_workspace_path",
            "Claude permission rules cannot safely represent this workspace path",
        )
    tool_names = ["Read", "Glob", "Grep"]
    if task["mode"] == "workspace-write":
        tool_names.extend(["Edit", "Write"])
    tools = ",".join(tool_names)
    workspace_glob = f"{workspace}/**"
    allowed = ",".join(f"{tool}({workspace_glob})" for tool in tool_names)
    denied = (
        "Agent,Task,TaskOutput,TaskStop,SendMessage,TeamCreate,TeamDelete,Bash,NotebookEdit,mcp__*"
    )
    return [
        _provider_binary("claude"),
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-chrome",
        "--append-system-prompt-file",
        str(WORKER_POLICY_PATH),
        "--permission-mode",
        "dontAsk",
        "--tools",
        tools,
        "--allowedTools",
        allowed,
        "--disallowedTools",
        denied,
        "--max-turns",
        str(task["max_turns"]),
        "--output-format",
        "stream-json",
        "--verbose",
        "--print",
    ]


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    def send(sig: int) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, sig)
            else:  # pragma: no cover
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    send(signal.SIGINT)
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    send(signal.SIGTERM)
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    send(signal.SIGKILL)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _invoke(
    argv: Sequence[str], *, cwd: Path, prompt: str, timeout: int, provider: str
) -> ProcessResult:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=_safe_environment(provider),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError:
        return ProcessResult(
            exit_code=None,
            stdout="",
            stderr=f"{provider} executable is unavailable",
            timed_out=False,
            unavailable=True,
            output_overflow=False,
        )

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    capture_lock = threading.Lock()
    output_overflow = threading.Event()
    total_bytes = 0

    def read_stream(name: str, stream: Any) -> None:
        nonlocal total_bytes
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                with capture_lock:
                    remaining = max(0, MAX_CAPTURE_BYTES - total_bytes)
                    captured[name].extend(chunk[:remaining])
                    total_bytes += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        output_overflow.set()
        except (OSError, ValueError):
            pass

    def write_prompt() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    writer = threading.Thread(target=write_prompt, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()

    timed_out = False
    started = time.monotonic()
    try:
        while process.poll() is None:
            if output_overflow.is_set():
                _terminate_process(process)
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                _terminate_process(process)
                break
            time.sleep(0.01)
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_process(process)
        writer.join(timeout=1)
        for thread in readers:
            thread.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    return ProcessResult(
        exit_code=process.returncode,
        stdout=bytes(captured["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(captured["stderr"]).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        unavailable=False,
        output_overflow=output_overflow.is_set(),
    )


def _json_lines(text: str) -> tuple[list[dict[str, Any]], bool]:
    events: list[dict[str, Any]] = []
    malformed = False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(value, dict):
            malformed = True
            continue
        events.append(value)
    return events, malformed


def _reason_from_text(text: str, *, exit_code: int | None = None) -> str:
    lowered = text.casefold()
    if any(
        phrase in lowered
        for phrase in (
            "usagelimitexceeded",
            "usage limit",
            "rate limit",
            "rate_limit",
            "quota exceeded",
            "quota_exceeded",
            "credit balance",
            "credits exhausted",
            "hit your limit",
        )
    ):
        return "quota"
    if any(
        phrase in lowered
        for phrase in (
            "authentication failed",
            "not authenticated",
            "login required",
            "please log in",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
        )
    ):
        return "auth"
    if any(
        phrase in lowered
        for phrase in (
            "max budget",
            "budget exceeded",
            "error_max_budget",
            "spending limit",
        )
    ):
        return "budget"
    if any(
        phrase in lowered
        for phrase in (
            "max turns",
            "maximum turns",
            "error_max_turns",
        )
    ):
        return "turn_limit"
    if any(
        phrase in lowered
        for phrase in (
            "service unavailable",
            "temporarily unavailable",
            "overloaded",
            "connection refused",
            "connection reset",
            "network is unreachable",
            "failed to connect",
            "executable is unavailable",
        )
    ) or exit_code in {126, 127}:
        return "unavailable"
    return "worker_error"


def _codex_failure_reason(
    events: Sequence[Mapping[str, Any]], *, exit_code: int | None
) -> str:
    """Classify only stable fields from Codex failure envelopes."""

    values: list[str] = []
    for event in events:
        if event.get("type") not in {"turn.failed", "error"}:
            continue
        sources = [event]
        error = event.get("error")
        if isinstance(error, Mapping):
            sources.append(error)
        for source in sources:
            for field in ("codexErrorInfo", "codex_error_info", "code", "type"):
                value = source.get(field)
                if isinstance(value, str):
                    values.append(re.sub(r"[^a-z0-9]", "", value.casefold()))
    quota_codes = {
        "usagelimitexceeded",
        "ratelimitexceeded",
        "insufficientquota",
        "creditsexhausted",
    }
    auth_codes = {
        "authenticationfailed",
        "notauthenticated",
        "unauthorized",
        "invalidapikey",
    }
    budget_codes = {"budgetexceeded", "maxbudget", "spendinglimitexceeded"}
    unavailable_codes = {
        "serviceunavailable",
        "temporarilyunavailable",
        "overloaded",
        "connectionfailed",
    }
    if any(value in quota_codes for value in values):
        return "quota"
    if any(value in auth_codes for value in values):
        return "auth"
    if any(value in budget_codes for value in values):
        return "budget"
    if any(value in unavailable_codes for value in values) or exit_code in {126, 127}:
        return "unavailable"
    return "worker_error"


def _classify_codex(process: ProcessResult, attempt_dir: Path) -> ClassifiedResult:
    if process.timed_out:
        return ClassifiedResult(False, "timeout", None, None)
    if process.unavailable:
        return ClassifiedResult(False, "unavailable", None, None)
    if process.output_overflow:
        return ClassifiedResult(False, "protocol_error", None, None)
    events, malformed = _json_lines(process.stdout)
    session_id: str | None = None
    completed = False
    failed = False
    for event in events:
        event_type = event.get("type")
        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]
        elif event_type == "turn.completed":
            completed = True
        elif event_type in {"turn.failed", "error"}:
            failed = True
    if process.exit_code != 0 or failed:
        return ClassifiedResult(
            False,
            _codex_failure_reason(events, exit_code=process.exit_code),
            session_id,
            None,
        )
    if malformed or not events or not completed:
        return ClassifiedResult(False, "protocol_error", session_id, None)
    message_path = attempt_dir / "last-message.txt"
    try:
        if message_path.is_symlink() or not message_path.is_file():
            return ClassifiedResult(False, "protocol_error", session_id, None)
        if message_path.stat().st_size > MAX_PERSISTED_STREAM_BYTES:
            return ClassifiedResult(False, "protocol_error", session_id, None)
        output = message_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ClassifiedResult(False, "protocol_error", session_id, None)
    return ClassifiedResult(True, "success", session_id, output)


def _classify_claude(process: ProcessResult) -> ClassifiedResult:
    if process.timed_out:
        return ClassifiedResult(False, "timeout", None, None)
    if process.unavailable:
        return ClassifiedResult(False, "unavailable", None, None)
    if process.output_overflow:
        return ClassifiedResult(False, "protocol_error", None, None)
    events, malformed = _json_lines(process.stdout)
    result_event: dict[str, Any] | None = None
    for event in events:
        if event.get("type") == "result":
            result_event = event
    session_id = None
    if result_event is not None and isinstance(result_event.get("session_id"), str):
        session_id = result_event["session_id"]
    if process.exit_code != 0 or result_event is None or result_event.get("is_error") is True:
        subtype = str(result_event.get("subtype", "")) if result_event else ""
        if subtype.startswith("error_max_turns"):
            reason = "turn_limit"
        elif subtype.startswith("error_max_budget"):
            reason = "budget"
        elif result_event is not None and isinstance(result_event.get("result"), str):
            reason = _reason_from_text(
                result_event["result"], exit_code=process.exit_code
            )
        elif process.exit_code in {126, 127}:
            reason = "unavailable"
        else:
            reason = "worker_error"
        return ClassifiedResult(
            False,
            reason,
            session_id,
            None,
        )
    if malformed or result_event.get("subtype") != "success":
        if result_event and str(result_event.get("subtype", "")).startswith("error_max_turns"):
            reason = "turn_limit"
        elif result_event and str(result_event.get("subtype", "")).startswith("error_max_budget"):
            reason = "budget"
        else:
            reason = "protocol_error"
        return ClassifiedResult(False, reason, session_id, None)
    output_value = result_event.get("structured_output", result_event.get("result", ""))
    if isinstance(output_value, str):
        output = output_value
    else:
        output = json.dumps(output_value, indent=2, sort_keys=True)
    return ClassifiedResult(True, "success", session_id, output)


def _attempt(
    provider: str,
    task: Mapping[str, Any],
    prompt: str,
    run_dir: Path,
    attempt_number: int,
) -> tuple[dict[str, Any], ClassifiedResult]:
    attempt_dir = run_dir / "attempts" / f"{attempt_number:02d}-{provider}"
    _mkdir_private(attempt_dir)
    stdout_path = attempt_dir / "stdout.jsonl"
    stderr_path = attempt_dir / "stderr.log"
    command = _codex_command(task, attempt_dir) if provider == "codex" else _claude_command(task)
    started_at = _now()
    process = _invoke(
        command,
        cwd=Path(str(task["workspace"])),
        prompt=prompt,
        timeout=int(task["timeout_seconds"]),
        provider=provider,
    )
    inherited_env = dict(os.environ)
    clean_stdout = _truncate_stream(_redact_text(process.stdout, inherited_env))
    clean_stderr = _truncate_stream(_redact_text(process.stderr, inherited_env))
    _atomic_write_text(stdout_path, clean_stdout)
    _atomic_write_text(stderr_path, clean_stderr)
    classified = (
        _classify_codex(process, attempt_dir) if provider == "codex" else _classify_claude(process)
    )
    if classified.output is not None:
        classified = ClassifiedResult(
            classified.ok,
            classified.reason,
            classified.session_id,
            _truncate_stream(_redact_text(classified.output, inherited_env)),
        )
    finished_at = _now()
    attempt = {
        "attempt": attempt_number,
        "provider": provider,
        "status": "completed" if classified.ok else "failed",
        "reason": classified.reason,
        "exit_code": process.exit_code,
        "session_id": classified.session_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    return attempt, classified


def _run_task(task_path: Path, *, explicit_state_dir: str | None) -> dict[str, Any]:
    raw = _read_json_file(task_path, label="task file", max_bytes=MAX_PROMPT_BYTES)
    task, prompt = _validate_task(raw, task_path)
    workspace = Path(task["workspace"])
    info = _workspace_info(workspace)
    if (
        task["base_commit"] is not None
        and info.snapshot.head.casefold() != task["base_commit"].casefold()
    ):
        raise BrokerError(
            "base_commit_mismatch",
            f"workspace HEAD {info.snapshot.head} does not match task base_commit",
        )
    task["base_commit"] = info.snapshot.head
    if "codex" in task["providers"]:
        _project_mcp_server_names(workspace)
    if task["mode"] == "workspace-write":
        if not info.linked_worktree:
            raise BrokerError(
                "write_requires_linked_worktree",
                "workspace-write is allowed only in a dedicated linked Git worktree",
            )
        if info.snapshot.status:
            raise BrokerError(
                "write_requires_clean_worktree",
                "workspace-write requires a clean worktree before the first provider starts",
            )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{task['task_id']}-{uuid.uuid4().hex[:8]}"
    state_root = _state_root(info, explicit_state_dir)
    if state_root.is_relative_to(workspace) and not state_root.is_relative_to(info.git_dir):
        raise BrokerError(
            "unsafe_state_path",
            "state root must be outside the worktree (the Git directory is allowed)",
        )
    _mkdir_private(state_root)
    baseline = info.snapshot
    lease = WorkspaceLease(info.common_dir, workspace, run_id)
    lease.__enter__()
    if _snapshot(workspace) != baseline:
        lease.__exit__(None, None, None)
        raise BrokerError(
            "workspace_changed_before_start",
            "workspace changed while the broker was acquiring its lease",
        )
    setup_complete = False
    try:
        runs_root = state_root / "runs"
        _mkdir_private(runs_root)
        run_dir = runs_root / run_id
        run_dir.mkdir(mode=0o700)
        result_path = run_dir / "result.json"
        status_path = run_dir / "status.json"
        events_path = run_dir / "events.jsonl"
        stored_prompt_path = run_dir / "prompt.txt"
        _atomic_write_text(stored_prompt_path, prompt)
        stored_task = dict(task)
        stored_task["prompt_file"] = str(stored_prompt_path)
        _atomic_write_json(run_dir / "task.json", stored_task)
        initial_status = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "run_id": run_id,
            "task_id": task["task_id"],
            "status": "running",
            "reason": None,
            "provider": None,
            "workspace": str(workspace),
            "mode": task["mode"],
            "base_commit": baseline.head,
            "attempts": [],
            "result_path": str(result_path),
            "updated_at": _now(),
        }
        _atomic_write_json(status_path, initial_status)
        _append_event(
            events_path,
            {
                "type": "run.started",
                "at": _now(),
                "run_id": run_id,
                "base_commit": baseline.head,
            },
        )

        providers = list(task["providers"])
        if not task["allow_fallback"]:
            providers = providers[:1]
        attempts: list[dict[str, Any]] = []
        final_classified: ClassifiedResult | None = None
        final_provider: str | None = None
        previous_reason: str | None = None
        setup_complete = True
    finally:
        if not setup_complete:
            lease.__exit__(None, None, None)

    try:
        for attempt_number, provider in enumerate(providers, start=1):
            _append_event(
                events_path,
                {
                    "type": "attempt.started",
                    "at": _now(),
                    "attempt": attempt_number,
                    "provider": provider,
                },
            )
            status_value = dict(initial_status)
            status_value.update(
                {
                    "provider": provider,
                    "attempts": attempts,
                    "updated_at": _now(),
                }
            )
            _atomic_write_json(status_path, status_value)
            attempt, classified = _attempt(
                provider,
                task,
                _worker_prompt(task, prompt, previous_reason),
                run_dir,
                attempt_number,
            )

            after = _snapshot(workspace)
            changed = after != baseline
            if task["mode"] == "read-only" and changed:
                classified = ClassifiedResult(
                    False, "workspace_changed", classified.session_id, None
                )
                attempt["status"] = "failed"
                attempt["reason"] = "workspace_changed"
            elif (
                task["mode"] == "workspace-write"
                and classified.ok
                and (after.head != baseline.head or _has_index_changes(after.status))
            ):
                classified = ClassifiedResult(
                    False, "workspace_changed", classified.session_id, None
                )
                attempt["status"] = "failed"
                attempt["reason"] = "workspace_changed"
            elif task["mode"] == "workspace-write" and not classified.ok and changed:
                classified = ClassifiedResult(
                    False, "workspace_changed", classified.session_id, None
                )
                attempt["status"] = "failed"
                attempt["reason"] = "workspace_changed"

            attempts.append(attempt)
            final_classified = classified
            final_provider = provider
            _append_event(
                events_path,
                {
                    "type": "attempt.finished",
                    "at": _now(),
                    "attempt": attempt_number,
                    "provider": provider,
                    "status": attempt["status"],
                    "reason": attempt["reason"],
                },
            )
            if classified.ok:
                break
            if task["mode"] == "workspace-write":
                break
            if classified.reason not in FALLBACK_REASONS:
                break
            if changed:
                break
            previous_reason = classified.reason
    finally:
        lease.__exit__(None, None, None)

    if final_classified is None:
        raise BrokerError("worker_error", "no provider attempt was executed", exit_code=1)
    if final_classified.ok:
        status = "completed"
    elif final_classified.reason == "workspace_changed":
        status = "blocked"
    else:
        status = "failed"
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": final_classified.ok,
        "run_id": run_id,
        "task_id": task["task_id"],
        "status": status,
        "reason": final_classified.reason,
        "provider": final_provider,
        "session_id": final_classified.session_id,
        "workspace": str(workspace),
        "mode": task["mode"],
        "base_commit": baseline.head,
        "output": final_classified.output,
        "attempts": attempts,
        "result_path": str(result_path),
    }
    _atomic_write_json(result_path, result)
    _atomic_write_json(status_path, result)
    _append_event(
        events_path,
        {
            "type": "run.finished",
            "at": _now(),
            "run_id": run_id,
            "base_commit": baseline.head,
            "status": status,
            "reason": final_classified.reason,
        },
    )
    return result


def _probe(name: str, argv: Sequence[str], cwd: Path) -> dict[str, Any]:
    executable = shutil.which(argv[0], path=os.environ.get("PATH"))
    if executable is None:
        return {"ok": False, "detail": "not found", "path": None}
    try:
        result = _run_control_command(argv, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": _redact_text(str(exc)), "path": executable}
    raw_detail = (result.stdout or result.stderr).strip()
    detail: str
    try:
        parsed = json.loads(raw_detail)
    except json.JSONDecodeError:
        lines = raw_detail.splitlines()
        detail = lines[0] if lines else f"exit {result.returncode}"
    else:
        if isinstance(parsed, dict) and "loggedIn" in parsed:
            method = parsed.get("authMethod", "configured auth")
            plan = parsed.get("subscriptionType")
            suffix = f" ({plan})" if isinstance(plan, str) and plan else ""
            detail = (
                f"logged in via {method}{suffix}" if parsed.get("loggedIn") else "not logged in"
            )
        else:
            detail = "JSON response received"
    detail = _redact_text(detail)
    return {"ok": result.returncode == 0, "detail": detail, "path": executable, "name": name}


def _doctor(workspace: Path, *, explicit_state_dir: str | None) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["codex_cli"] = _probe("codex", [_provider_binary("codex"), "--version"], workspace)
    checks["codex_auth"] = _probe(
        "codex auth", [_provider_binary("codex"), "login", "status"], workspace
    )
    checks["codex_claude_mcp"] = _probe(
        "claude_code MCP",
        [_provider_binary("codex"), "mcp", "get", "claude_code"],
        workspace,
    )
    checks["claude_cli"] = _probe("claude", [_provider_binary("claude"), "--version"], workspace)
    checks["claude_auth"] = _probe(
        "claude auth", [_provider_binary("claude"), "auth", "status"], workspace
    )
    checks["claude_codex_plugin"] = _probe(
        "codex plugin",
        [_provider_binary("claude"), "plugin", "details", "codex@openai-codex"],
        workspace,
    )
    info: WorkspaceInfo | None = None
    try:
        resolved = workspace.resolve(strict=True)
        info = _workspace_info(resolved)
        checks["git_workspace"] = {
            "ok": True,
            "detail": "linked worktree" if info.linked_worktree else "main checkout",
            "path": str(info.root),
            "clean": not bool(info.snapshot.status),
        }
    except (OSError, BrokerError) as exc:
        checks["git_workspace"] = {"ok": False, "detail": str(exc), "path": str(workspace)}
    try:
        state_root = _state_root(info, explicit_state_dir)
        _mkdir_private(state_root)
        checks["state_root"] = {
            "ok": True,
            "detail": "private durable state",
            "path": str(state_root),
        }
    except OSError as exc:
        checks["state_root"] = {"ok": False, "detail": str(exc), "path": None}
    checks["worker_policy"] = {
        "ok": WORKER_POLICY_PATH.is_file(),
        "detail": "present" if WORKER_POLICY_PATH.is_file() else "missing",
        "path": str(WORKER_POLICY_PATH),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": all(check.get("ok") for check in checks.values()),
        "command": "doctor",
        "model_invoked": False,
        "checks": checks,
    }


def _status(run_id: str, workspace: Path, *, explicit_state_dir: str | None) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise BrokerError("invalid_run_id", "run_id has an invalid format")
    info: WorkspaceInfo | None
    try:
        info = _workspace_info(workspace.resolve(strict=True))
    except (OSError, BrokerError):
        info = None
    state_root = _state_root(info, explicit_state_dir)
    runs_root = (state_root / "runs").resolve()
    status_path = (runs_root / run_id / "status.json").resolve()
    try:
        status_path.relative_to(runs_root)
    except ValueError as exc:
        raise BrokerError("invalid_run_id", "run_id escapes the run root") from exc
    return _read_json_file(status_path, label="run status")


def _build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="agent-broker",
        description="Run one bounded Codex/Claude task with safe provider fallback.",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON on stdout")
    parser.add_argument(
        "--state-dir",
        help="override durable state root (primarily for tests and recovery)",
    )
    parser.add_argument("--version", action="version", version="agent-broker 1.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="check CLIs, auth, plugin, Git, and state without a model call"
    )
    doctor.add_argument("--workspace", default=os.getcwd(), help="Git workspace to inspect")

    run = subparsers.add_parser("run", help="run a validated task file and persist its result")
    run.add_argument("--task-file", required=True, help="path to a task.v1 JSON file")

    status = subparsers.add_parser("status", help="read the last durable status for a run")
    status.add_argument("run_id", help="run ID returned by run")
    status.add_argument(
        "--workspace", default=os.getcwd(), help="workspace used to resolve the state root"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = _doctor(Path(args.workspace), explicit_state_dir=args.state_dir)
            _emit(result, json_output=args.json)
            return 0 if result["ok"] else 1
        if args.command == "run":
            result = _run_task(
                Path(args.task_file).expanduser().resolve(), explicit_state_dir=args.state_dir
            )
            _emit(result, json_output=args.json)
            return 0 if result["ok"] else 1
        if args.command == "status":
            result = _status(
                args.run_id,
                Path(args.workspace),
                explicit_state_dir=args.state_dir,
            )
            _emit(result, json_output=args.json)
            return 0
        raise BrokerError("invalid_arguments", "unknown command")
    except BrokerError as exc:
        error = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {"code": exc.code, "message": _redact_text(exc.message, os.environ)},
        }
        _emit(error, json_output=args.json)
        return exc.exit_code
    except Exception as exc:  # fail closed without leaking a traceback or credentials
        error = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": _redact_text(f"{type(exc).__name__}: {exc}", os.environ),
            },
        }
        _emit(error, json_output=args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
