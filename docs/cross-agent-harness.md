# Cross-agent harness: Claude Code ⇄ Codex

This document covers the bidirectional integration between Claude Code and OpenAI's
Codex CLI on this machine, plus the standalone fallback broker that can run a task
headlessly on whichever of the two is actually available right now.

This is a different thing from [`docs/agent-harness.md`](agent-harness.md), which governs
same-model (Claude Code) subagent fan-out for `issue-fleet`. This document is about two
different vendors' CLIs calling each other, and about surviving one of them being out of
quota, logged out, or down.

## Why this exists

Two problems, one piece of infrastructure:

1. **Play to each tool's strengths.** Sometimes the task at hand is better suited to
   whichever agent isn't currently driving -- a second opinion, a different model's
   review pass, or a capability one CLI has that the other doesn't.
2. **Don't stop working just because one side is out of runway.** A Claude Code session
   that hits its usage limit, or a Codex session with an expired login, shouldn't block
   the work if the other one is available and the task doesn't strictly require the
   session that's stuck.

Both are handled without ever falling back to metered API-key billing implicitly -- see
rule 6 in [worker-policy.md](../.agents/protocol/v1/worker-policy.md). This harness makes
agents cover for each other; it does not make Anthropic/OpenAI usage cheaper or unlimited.

## Part 1: Claude Code → Codex (interactive)

This direction uses OpenAI's own official plugin, not a custom integration.

- Plugin: `codex@openai-codex`, installed from `github:openai/codex-plugin-cc`.
- Check it's enabled for the worktree you're in: `claude plugin list`. Plugin
  enablement is scoped per project *path* -- enabling it in one worktree does not
  enable it in another. Enable with:
  ```bash
  claude plugin enable codex@openai-codex -s project
  ```
- Requires the `codex` CLI on `PATH` and logged in. Run `/codex:setup` inside a Claude
  Code session to check/install/log in. On this machine the actual binary is the one
  bundled with the ChatGPT desktop app (`/Applications/ChatGPT.app/Contents/Resources/codex`);
  `/opt/homebrew/bin/codex` is a symlink to it kept on `PATH` for exactly this purpose.
- Commands: `/codex:review`, `/codex:transfer` (hand the current session to a resumable
  Codex thread), `/codex:status`, `/codex:cancel`, `/codex:rescue`, `/codex:result`,
  `/codex:adversarial-review`. Run `/codex:setup --help`-equivalent by reading
  the plugin's own `commands/*.md` if you need exact semantics.
- The plugin's own delegation script (`codex-companion.mjs`) already passes an explicit
  `--sandbox read-only`/`workspace-write` on every Codex invocation it makes -- it does
  not rely on `~/.codex/config.toml`'s global sandbox/approval defaults, so it stays safe
  even though those global defaults are permissive (see Part 3).

## Part 2: Codex → Claude Code (interactive)

There is no official equivalent in this direction, so this repo ships one:
[`scripts/claude_code_mcp_bridge.py`](../scripts/claude_code_mcp_bridge.py), a
dependency-free MCP server (stdio, hand-rolled JSON-RPC -- no `mcp` package needed) that
exposes a single tool, `delegate_to_claude_code`, wrapping `claude -p`.

Registered with Codex via:

```bash
codex mcp add claude_code -- python3 /absolute/path/to/scripts/claude_code_mcp_bridge.py
```

`codex mcp add` always writes to the **global** `~/.codex/config.toml`, so once
registered it's available to Codex in every project, not just this one. That also means
the registered path must keep working after this repo's current worktree is torn down --
if you registered it against a throwaway worktree path, re-run the command above against
the main checkout's `scripts/claude_code_mcp_bridge.py` once this branch merges.

Tool contract (see the script's own docstring and `_TOOL_SCHEMA` for the source of
truth):

- `prompt` (required), `cwd` (required, absolute path to a git worktree),
  `mode` (`read_only` default, or `write`), `model` (optional override).
- `mode="write"` is refused unless `cwd` is a *linked* worktree (`.git` is a file, not a
  directory) -- the main checkout is never a valid write target.
- The child `claude` process has API-key/cloud-credential env vars stripped (rule 6) and
  a recursion-depth guard applied (rule 7) -- see `_cross_agent_common.py`, shared with
  `agent_broker.py` so this logic exists in exactly one place.

## Part 3: `.codex/config.toml` project-local sandbox override

`~/.codex/config.toml` (global, every project on this machine) has
`approval_policy = "never"` and `sandbox_mode = "danger-full-access"`. That default is
left alone deliberately -- changing it would affect every other project on this machine,
not just this one.

[`.codex/config.toml`](../.codex/config.toml) (this repo only) overrides it to
`approval_policy = "on-request"` / `sandbox_mode = "workspace-write"` for any Codex
session run from inside this repo, interactive or not. Verify with:

```bash
codex doctor   # run from inside this repo
```

Both the plugin (Part 1) and this repo's own scripts (Parts 2 and 4) pass explicit
`--sandbox`/`--approve-for-me` flags per invocation anyway, which always win over either
config file -- this project-local override is a safety net for plain interactive `codex`
usage started from this repo, not the only thing standing between Codex and
`danger-full-access`.

## Part 4: `agent_broker.py` -- headless task runner with fallback

For unattended/scripted use (not an interactive Claude Code or Codex session), use
[`scripts/agent_broker.py`](../scripts/agent_broker.py):

```bash
venv/bin/python scripts/agent_broker.py --json doctor
venv/bin/python scripts/agent_broker.py --json run --task-file /absolute/path/to/task.json
```

`doctor` is a health/auth snapshot; `run` executes exactly one task per
[`.agents/protocol/v1/task.schema.json`](../.agents/protocol/v1/task.schema.json) and
prints a result per
[`.agents/protocol/v1/result.schema.json`](../.agents/protocol/v1/result.schema.json).
The full set of invariants (when fallback is and isn't allowed, why a dirty worktree
blocks instead of falling back, why fallback is a single hop, the recursion-depth
caveat) is in [`.agents/protocol/v1/worker-policy.md`](../.agents/protocol/v1/worker-policy.md)
-- read that before changing `agent_broker.py`'s behavior.

Minimal task file:

```json
{
  "task_id": "example-001",
  "repo_path": "/absolute/path/to/a/checkout/of/this/repo",
  "prompt": "Describe the task here.",
  "primary_agent": "claude",
  "fallback_agent": "codex",
  "mode": "read_only"
}
```

`primary_agent`/`fallback_agent` are `"claude"` or `"codex"`; `fallback_agent` can also be
`"none"` to disable fallback for a specific task. `mode: "write"` allows file edits inside
the dedicated worktree the broker creates for the task; `mode: "read_only"` runs Claude in
plan mode and Codex in a read-only sandbox.

The broker never stages, commits, or pushes anything itself. On success with zero changes
it prunes the (empty) dedicated worktree; on success with changes, on `blocked`, or on
`failed`, the worktree and its branch (`agent-broker/<task_id>`) are left in place for a
human to inspect with an ordinary `git -C <worktree_path> diff` / `git log`.

## What this harness does not do

- It does not make either subscription's usage limit larger -- it only lets work continue
  on the other CLI when one is stuck, and only for the three conditions in worker-policy.md
  rule 1.
- It does not resolve merge conflicts, run tests, or decide whether a delegated agent's
  work is good -- that's still a human's job (or the delegating session's, if a human is
  reviewing its output).
- It does not sandbox recursion cryptographically -- see worker-policy.md rule 7's honest
  limitation before relying on the depth guard for anything safety-critical.
