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
