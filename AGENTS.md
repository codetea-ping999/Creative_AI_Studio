# Repository Agent Instructions

## Working as one of several parallel agents

When you are a subagent in a fan-out (see `.claude/workflows/issue-fleet.js`),
read `docs/agent-harness.md` first. It defines the verification gate, which files
are reserved for the integrator, and what you must not do (commit, install heavy
dependencies, download weights).

## Delegating to Codex, or being delegated to by Codex

If you are asked to hand a task to Codex, or you are running as a task Codex handed to
you (via the `claude_code` MCP tool or `scripts/agent_broker.py`), read
`docs/cross-agent-harness.md` first. In particular: do not re-delegate further (no
`/codex:*` commands, no `claude_code` MCP calls, no `agent_broker.py`) from inside a
delegated run -- see rule 7 in `.agents/protocol/v1/worker-policy.md`. If you were invoked
with `mode="read_only"`, do not attempt file writes even if you believe they'd help.

## Frontend design source of truth

When changing the web UI, follow:

- `docs/design-system.md`
- `docs/ui-principles.md`
- `docs/design-directions.md`

Do not add a UI library without explicit approval. Reuse the existing React and
CSS implementation and semantic design tokens.

## Required UI review

Before calling a frontend change complete:

- verify spacing, control height, radius, typography, and borders are consistent;
- verify loading, empty, error, disabled, long-text, and large-list behavior;
- verify keyboard focus, labels, ARIA state, and non-color status cues;
- inspect the rendered page at 390px, 768px, 1280px, and 1440px;
- run frontend tests and the production build.

Do not judge visual completion from source code alone.
