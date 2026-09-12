---
schema_version: 1
id: FP-001
type: failure-pattern
status: validated
scope: [concurrency, runtime-lifecycle, core/models]
created_at: 2026-09-13
last_verified_at: 2026-09-13
confidence: high
evidence:
  - "PR #415, final head 6ef7f833b4b7723c95d89e6cc0967a6964c679b3"
  - "tests/test_runtime_safety_core.py::test_same_canonical_runtime_execution_is_mutually_exclusive"
validated_by: [codex-adversarial-review, deterministic-regression]
supersedes: []
superseded_by: []
review_after: null
---

# Load lock is not execution lock

## Trigger

Code uses a per-model or per-resource lock to serialize construction/loading and later assumes the same lock protects active use.

## Failed assumption

Preventing duplicate loads does not prevent two callers from concurrently executing against the same published runtime.

## Required invariant

Construction ownership and execution ownership are separate concerns. A shared runtime that is not proven reentrant needs explicit execution exclusion for the full use interval.

## Proof

Use deterministic concurrent callers and prove that a second caller cannot enter execution while the first owns the runtime. Prefer real lock contention, `Event`, or `Barrier`; do not infer exclusivity from timing.

## Known occurrence

PR #415 introduced a per-entry execution lock after audit showed the pre-existing load lock protected loading but not runtime execution.

## Limits

This does not require exclusive execution for runtimes that have an independently proven concurrency contract. The burden is on that contract, not on the load lock.