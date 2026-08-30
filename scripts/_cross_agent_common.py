"""Shared invariants for the Claude Code <-> Codex cross-agent harness.

Both ``claude_code_mcp_bridge.py`` (Codex -> Claude Code, live MCP tool) and
``agent_broker.py`` (standalone headless task runner with fallback) enforce
the same security-relevant rules against the same primitive (spawning a
``claude`` or ``codex`` CLI process against a git worktree). Keeping that
logic in one module means a fix here fixes both callers at once, instead of
each carrying its own copy that can drift out of sync -- see
docs/cross-agent-harness.md for the incident that motivated this.
"""

from __future__ import annotations

import os
from pathlib import Path

# Stripped from every child `claude`/`codex` process env so a delegated run
# can only authenticate with the operator's already-logged-in interactive
# session -- never an implicit fall-back to API-key or cloud-provider
# billing.
STRIP_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_API_KEY",
)

DEPTH_ENV_VAR = "CROSS_AGENT_DELEGATION_DEPTH"
MAX_DELEGATION_DEPTH = 1

DELEGATION_GUARD_PROMPT = (
    "You are running as a delegated sub-task in the Claude Code / Codex "
    "cross-agent harness, not as an interactive session. Do not invoke "
    "/codex:* commands, the codex plugin, the claude_code MCP tool, or "
    "agent_broker.py to hand this task back out -- complete it yourself "
    "with your own tools, or stop and report why you cannot."
)


def sanitized_child_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of the environment with billing/credential vars removed."""

    env = dict(base_env if base_env is not None else os.environ)
    for key in STRIP_ENV_VARS:
        env.pop(key, None)
    return env


def incoming_delegation_depth(env: dict[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    try:
        return int(source.get(DEPTH_ENV_VAR, "0") or "0")
    except ValueError:
        return 0


def is_linked_worktree(path: Path) -> bool:
    """True if `path` is a linked git worktree (.git is a file, not a dir).

    The main checkout's `.git` is a directory; every `git worktree add`
    target gets a `.git` *file* containing a `gitdir:` pointer instead. That
    distinction is what lets write-capable calls refuse the main checkout
    without needing a hardcoded allowlist of worktree paths.
    """

    return (path / ".git").is_file()


def is_any_git_dir(path: Path) -> bool:
    return (path / ".git").exists()


# Heuristic, best-effort classification of a failed CLI invocation into one
# of the three conditions the harness is allowed to fall back on. Anything
# that doesn't match is treated as an ordinary task failure -- NOT eligible
# for fallback, per worker-policy.md's "never retry a clean failure on the
# other agent" rule. Keyword lists are intentionally conservative; extend
# them as real false negatives are observed rather than guessing broadly.
_QUOTA_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "quota",
    "429",
    "too many requests",
    "usage cap",
    "exceeded your",
)
_AUTH_MARKERS = (
    "not logged in",
    "not authenticated",
    "unauthorized",
    "401",
    "please run",
    "invalid api key",
    "authentication failed",
    "auth failed",
    "login required",
)
_SERVICE_DOWN_MARKERS = (
    "503",
    "502",
    "529",
    "bad gateway",
    "service unavailable",
    "overloaded",
    "connection refused",
    "econnrefused",
    "network error",
    "timed out",
    "timeout",
)


def classify_transient_failure(exit_code: int, stdout: str, stderr: str) -> str | None:
    """Return 'quota_exceeded' / 'auth_failed' / 'service_unavailable', or None.

    None means: this failure is not one of the harness's three recognized
    fallback triggers, so the caller must NOT hand off to the other agent --
    it should surface the failure as-is.
    """

    if exit_code == 0:
        return None
    haystack = f"{stdout}\n{stderr}".lower()
    if any(marker in haystack for marker in _AUTH_MARKERS):
        return "auth_failed"
    if any(marker in haystack for marker in _QUOTA_MARKERS):
        return "quota_exceeded"
    if any(marker in haystack for marker in _SERVICE_DOWN_MARKERS):
        return "service_unavailable"
    return None


__all__ = [
    "STRIP_ENV_VARS",
    "DEPTH_ENV_VAR",
    "MAX_DELEGATION_DEPTH",
    "DELEGATION_GUARD_PROMPT",
    "sanitized_child_env",
    "incoming_delegation_depth",
    "is_linked_worktree",
    "is_any_git_dir",
    "classify_transient_failure",
]
