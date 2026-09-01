# Repository Agent Instructions

## Working as one of several parallel agents

When you are a subagent in a fan-out (see `.claude/workflows/issue-fleet.js`),
read `docs/agent-harness.md` first. It defines the verification gate, which files
are reserved for the integrator, and what you must not do (commit, install heavy
dependencies, download weights).

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

## Experimental Codex model routing

For non-trivial Codex tasks, follow the `Codex モデル・ルーティングの試行`
section in `docs/cross-agent-harness.md`. Use it only when the runtime exposes
explicit subagent model selection, and never report an effective model that the
runtime did not attest.

## Cross-agent delegation

Follow `docs/cross-agent-harness.md` when delegating between Codex and Claude Code.
Claude Code is available through the project-scoped `claude_code` MCP server, but
starting its `Agent` consumes Claude usage and requires operator approval.

- Keep delegation depth at one; a delegated agent must not delegate back.
- Use `isolation: "worktree"` for every delegated write task.
- Never allow two providers to write to the same worktree concurrently.
- Prefer the other provider for independent verification of high-risk changes.
- Preserve the patch and verification contract in `docs/agent-harness.md`.
