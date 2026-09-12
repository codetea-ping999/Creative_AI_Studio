# Agent Cognitive Infrastructure

This directory is the provider-neutral source of truth for shared agent cognitive artifacts. It extends the existing cross-agent protocol without replacing product or domain specifications.

Authority order for a governed behavior:

1. accepted ADRs, explicit domain/API contracts, deterministic tests, and current code;
2. validated cognitive memory that summarizes or operationalizes those primary sources;
3. provider adapters and local/provider-native memory.

If cognitive memory conflicts with a primary source, treat the memory as stale or contradicted until revalidated or superseded.

## Layout

- `protocol/` — existing deterministic cross-agent execution contracts.
- `constitution/` — long-lived agent-engineering principles.
- `memory/` — validated reusable engineering knowledge.
- `reflections/` — evidence-backed candidate lessons and promotion provenance.
- `skills/` — reusable procedures that turn validated knowledge into behavior.

v0.1 retrieval is deterministic. Select knowledge by path, subsystem, task class, failure class, or explicit skill links. Do not load every memory by default.

See `docs/agent-cognitive-infrastructure/architecture-decision.md` for the governing ADR.