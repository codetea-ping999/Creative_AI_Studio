#!/usr/bin/env python3
"""Minimal MCP server exposing Claude Code as a tool Codex can call.

Registered with Codex via:
    codex mcp add claude_code -- python3 /path/to/claude_code_mcp_bridge.py

Speaks MCP over stdio (newline-delimited JSON-RPC 2.0; no dependency on the
``mcp`` package so this runs with a bare Python 3 interpreter). Exposes one
tool, ``delegate_to_claude_code``, that shells out to the local ``claude``
CLI in non-interactive (``-p``) mode.

Safety invariants (see docs/cross-agent-harness.md and
.agents/protocol/v1/worker-policy.md):
- Never touches API-key billing: known API-key / cloud-credential env vars
  are stripped from the child process so ``claude`` can only authenticate
  with the operator's existing interactive (subscription) login.
- ``mode="write"`` is refused unless ``cwd`` is a *linked* git worktree
  (``.git`` is a file, not a directory) -- the main checkout is never a
  valid write target for a delegated task.
- Recursive delegation is capped at one hop via an env-var depth counter.
  This is best-effort (it relies on the child process chain preserving
  environment variables) and is *not* a hard sandbox -- see
  worker-policy.md for the honest limitation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cross_agent_common import (  # noqa: E402
    DELEGATION_GUARD_PROMPT,
    DEPTH_ENV_VAR,
    MAX_DELEGATION_DEPTH,
    incoming_delegation_depth,
    is_any_git_dir,
    is_linked_worktree,
    sanitized_child_env,
)

SERVER_NAME = "claude-code-bridge"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

_DEFAULT_TIMEOUT_SECONDS = 1800

_TOOL_SCHEMA = {
    "name": "delegate_to_claude_code",
    "description": (
        "Delegate a task to Claude Code (non-interactive) in a specific "
        "git worktree. Use mode='read_only' for review/analysis/planning "
        "(no file writes attempted); use mode='write' only when `cwd` is "
        "already a dedicated linked worktree you want Claude Code to edit."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task text to hand to Claude Code.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Absolute path to the git worktree Claude Code should "
                    "operate in. Must already exist. For mode='write' it "
                    "must be a linked worktree, not the main checkout."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["read_only", "write"],
                "default": "read_only",
                "description": (
                    "'read_only' runs Claude Code in plan mode (no edits "
                    "applied). 'write' allows file edits, gated on `cwd` "
                    "being a dedicated linked worktree."
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional model override (e.g. 'sonnet', 'opus').",
            },
        },
        "required": ["prompt", "cwd"],
    },
}


def _build_command(prompt: str, cwd: Path, mode: str, model: str | None) -> list[str]:
    claude_bin = shutil.which("claude") or "claude"
    command = [
        claude_bin,
        "-p",
        prompt,
        "--add-dir",
        str(cwd),
        "--output-format",
        "json",
        "--permission-mode",
        "plan" if mode == "read_only" else "acceptEdits",
        "--append-system-prompt",
        DELEGATION_GUARD_PROMPT,
    ]
    if model:
        command += ["--model", model]
    return command


def _run_claude_code(arguments: dict[str, Any]) -> dict[str, Any]:
    prompt = arguments.get("prompt")
    cwd_raw = arguments.get("cwd")
    mode = arguments.get("mode", "read_only")
    model = arguments.get("model")

    if not isinstance(prompt, str) or not prompt.strip():
        return _error_result("`prompt` is required and must be non-empty.")
    if not isinstance(cwd_raw, str) or not cwd_raw.strip():
        return _error_result("`cwd` is required and must be an absolute path.")
    if mode not in ("read_only", "write"):
        return _error_result("`mode` must be 'read_only' or 'write'.")

    incoming_depth = incoming_delegation_depth()
    if incoming_depth >= MAX_DELEGATION_DEPTH:
        return _error_result(
            "Recursive delegation blocked: this bridge invocation is "
            f"already at depth {incoming_depth} (max {MAX_DELEGATION_DEPTH}). "
            "Complete the task without a further cross-agent hop."
        )

    cwd = Path(cwd_raw).expanduser()
    if not cwd.is_absolute():
        return _error_result("`cwd` must be an absolute path.")
    if not cwd.is_dir():
        return _error_result(f"`cwd` does not exist or is not a directory: {cwd}")
    if not is_any_git_dir(cwd):
        return _error_result(f"`cwd` is not a git working tree: {cwd}")
    if mode == "write" and not is_linked_worktree(cwd):
        return _error_result(
            "mode='write' requires `cwd` to be a dedicated linked worktree "
            "(git worktree add ...), not the main checkout. Create one "
            "first and pass its path."
        )
    if shutil.which("claude") is None:
        return _error_result("`claude` CLI not found on PATH.")

    child_env = sanitized_child_env()
    child_env[DEPTH_ENV_VAR] = str(incoming_depth + 1)

    command = _build_command(prompt, cwd, mode, model)
    timeout_seconds = float(
        os.environ.get("CLAUDE_CODE_BRIDGE_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
    )

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error_result(f"claude -p timed out after {timeout_seconds:.0f}s in {cwd}.")
    except OSError as exc:
        return _error_result(f"Failed to launch claude CLI: {exc}")

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-2000:]
        return _error_result(f"claude -p exited with status {completed.returncode}.\n{stderr_tail}")

    stdout = (completed.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        # Fall back to raw text if --output-format json ever changes shape.
        return {"content": [{"type": "text", "text": stdout}], "isError": False}

    result_text = payload.get("result") or json.dumps(payload)
    is_error = bool(payload.get("is_error", False))
    summary_lines = [result_text]
    session_id = payload.get("session_id")
    total_cost = payload.get("total_cost_usd")
    if session_id or total_cost is not None:
        summary_lines.append(f"\n[session_id={session_id} total_cost_usd={total_cost}]")
    return {
        "content": [{"type": "text", "text": "".join(summary_lines)}],
        "isError": is_error,
    }


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": [_TOOL_SCHEMA]})
    if method == "tools/call":
        tool_name = params.get("name")
        if tool_name != _TOOL_SCHEMA["name"]:
            return _error_response(request_id, -32602, f"Unknown tool: {tool_name}")
        arguments = params.get("arguments") or {}
        return _response(request_id, _run_claude_code(arguments))

    if request_id is None:
        return None  # unhandled notification; nothing to reply with
    return _error_response(request_id, -32601, f"Method not found: {method}")


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[claude_code_mcp_bridge] bad JSON-RPC line: {exc}", file=sys.stderr)
            continue

        try:
            response = _handle_request(request)
        except Exception as exc:  # noqa: BLE001 - never crash the bridge on a bad call
            response = _error_response(request.get("id"), -32603, f"Internal error: {exc}")

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
