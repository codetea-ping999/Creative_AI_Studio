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

## Desktop shell — verification gate

When any change touches `apps/desktop/`, `apps/api` CORS in `apps/api/main.py` /
`tests/test_api_extensions.py`, or `apps/web/src/runtime.ts` / `studioClient.ts`,
follow `docs/desktop/desktop-shell-runbook.md` in addition to the backend and
frontend gates. Minimum, before calling it complete:

- run the boundary guard: `python3 apps/desktop/scripts/check_no_backend_spawn.py`;
- `cd apps/desktop/src-tauri && cargo check --all-targets && cargo test --all-targets`;
- build the bundles: `(cd apps/desktop/src-tauri && cargo tauri build)`;
- after a full build, run the packaged-app smoke:
  `./scripts/desktop_smoke.sh` (build-tree binary only — never `open`
  `/Applications`, which may resolve to the same-bundle-id old install).

Desktop smoke verifies default 8000, CORS read/preflight/JSON write, the
`API_PORT=8123` non-default flow through root `.env`, and second-instance focus.

Hard invariant: the Rust shell must never spawn Python/FastAPI, initialize CUDA,
or load model runtimes. Do not put the probe words (python/fastapi/uvicorn/
cuda/torch/spawn) in Rust code outside comments/strings. Do not touch `models/`.
A stale root `.env` can divert the non-default-port smoke; the script restores it.
