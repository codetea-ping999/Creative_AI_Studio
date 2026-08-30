# Cross-agent harness worker policy (v1)

This is the contract `scripts/agent_broker.py` and `scripts/claude_code_mcp_bridge.py`
implement in code. If you change either script's behavior, update this file in the same
change -- it is the thing to review against, not the other way around.

## 1. Fallback triggers on three conditions only

The broker hands a task to `fallback_agent` **only** if the primary agent's run:

1. exited non-zero (or launch itself failed, or it timed out), **and**
2. left the dedicated worktree clean (no uncommitted changes, tracked or untracked), **and**
3. its combined stdout/stderr matched one of exactly three categories:
   `quota_exceeded`, `auth_failed`, `service_unavailable`
   (see `classify_transient_failure` in `scripts/_cross_agent_common.py`).

A task that ran and failed for an ordinary reason -- a bug in the agent's own work, a
failing test it couldn't fix, a semantic misunderstanding -- is **not** eligible for
fallback. The other agent starting fresh on the same prompt is not a retry strategy this
harness implements; it is reported as `status: "failed"` and left for a human to read.

The keyword lists behind this classification are a heuristic, not an exhaustive parser of
either CLI's error surface. Extend them from observed false negatives, not by guessing
broadly -- a classification that's too eager silently turns "the task failed" into "let's
try the other agent," which is exactly the behavior this rule exists to prevent.

## 2. Every task runs in a fresh, dedicated linked worktree

`agent_broker.py run` always creates a brand new `git worktree add -b agent-broker/<task_id>`
off the caller-supplied `base_ref` before doing anything else. It never runs a task directly
in `repo_path`, and it never reuses a worktree across tasks. This removes an entire class of
caller error (accidentally pointing two tasks, or a task and a human, at the same dirty
tree) instead of just documenting "please use a clean worktree" and trusting it.

`claude_code_mcp_bridge.py`'s `delegate_to_claude_code` tool does not create worktrees
itself (the caller -- Codex -- already has one from its own session); instead it *validates*
one on every call: `mode="write"` is refused unless `cwd`'s `.git` is a **file** (a linked
worktree), never a directory (the main checkout). This check is code, not a docstring --
see `is_linked_worktree()` in `scripts/_cross_agent_common.py`.

## 3. The broker never touches git history on the agent's behalf

No `git add`, `git commit`, `git push`, `git merge`, or conflict resolution is ever run by
`agent_broker.py` or `claude_code_mcp_bridge.py` themselves. Whatever an agent commits (or
doesn't) during its own run is exactly what ends up in the worktree. A human (or the
delegating session, if it's the one reviewing the result) decides what to do with it next.

## 4. A dirty worktree after a failure blocks; it never falls back

If the primary agent stops -- crash, timeout, or manual cancellation -- while the worktree
already has changes in it, the result is `status: "blocked"`, not a fallback attempt. A
second agent starting fresh against a half-finished, uncommitted tree is more likely to
misread or overwrite that work than to safely continue it. `blocked` worktrees are never
pruned; `worktree_path` in the result always points at them.

## 5. Fallback is a single hop

If `fallback_agent` also fails, the broker does not look for a third option (there isn't
one) -- it reports `status: "failed"` with `fallback_triggered: true` and
`fallback_reason` set to whatever triggered the original handoff, plus the fallback
attempt's own `error_message`.

## 6. No implicit switch to API-key billing

Both CLIs are invoked with a sanitized environment (`sanitized_child_env()` in
`_cross_agent_common.py`) that strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`OPENAI_API_KEY`, cloud-provider credential variables, and Bedrock/Vertex opt-in flags
before spawning `claude` or `codex`. Both tools can then only authenticate with whatever
interactive (subscription) login is already active on the machine. If neither is logged in,
the run fails with an auth error -- the harness does not paper over that by falling through
to metered API access.

## 7. Recursive delegation is capped at one hop, and only best-effort

Every `claude`/`codex` child process is launched with `CROSS_AGENT_DELEGATION_DEPTH`
incremented by one. Both the bridge and the broker refuse outright if they see that counter
already at or above 1 (`incoming_delegation_depth()` / `MAX_DELEGATION_DEPTH` in
`_cross_agent_common.py`). Every delegated run's system prompt is also appended with an
explicit instruction not to re-delegate (`DELEGATION_GUARD_PROMPT`).

**Honest limitation:** the depth counter only works because it's an environment variable
that ordinary child-process spawning inherits. It is not a sandboxed guarantee. A delegated
Claude Code session that shells out to `codex` directly (e.g. via the `codex` plugin's own
`codex-companion.mjs`, not through `claude_code_mcp_bridge.py`) carries the incremented env
var forward through that chain, but if some future integration spawns a sibling process
that does not inherit the parent's environment, the counter would silently reset. Treat this
as defense-in-depth alongside the prompt-level instruction, not as a hard boundary. Do not
build anything on top of this harness that assumes recursion is cryptographically impossible.

## 8. `doctor` is informational only; `run` always re-checks live

`agent_broker.py doctor` is a cheap health/auth snapshot for a human or a calling script to
read before deciding whether to invoke `run` at all. `run` never trusts a prior `doctor`
result -- every task performs its own live attempt and classifies whatever actually happens.
