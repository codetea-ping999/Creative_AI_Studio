---
schema_version: 1
id: FP-005
type: failure-pattern
status: validated
scope: [concurrency, state-machine, runtime-lifecycle, handoff]
created_at: 2026-09-13
last_verified_at: 2026-09-13
confidence: high
evidence:
  - "commit 6ef7f833b4b7723c95d89e6cc0967a6964c679b3"
  - "tests/test_runtime_safety_core.py::test_racing_invalidation_is_published_before_e_can_be_released"
validated_by: [codex-adversarial-review, deterministic-3-thread-regression]
supersedes: []
superseded_by: []
review_after: null
---

# Publish unsafe state before releasing ownership

## Trigger

A caller knows a shared resource is unsafe, but the state marking it unsafe is published only after or behind a guard that another cleanup/exit path can win first.

## Failed assumption

It is enough to record invalidity eventually. Another waiter can acquire released execution ownership in the gap and observe the resource as still safe.

## Required invariant

When a resource is known unsafe for new users, publish that unsafe state before any path can release the ownership/exclusion primitive that allows a waiter to proceed.

## Proof

Use a deterministic multi-party regression that pauses the competing release path, starts invalidation, and proves the unsafe state is visible before execution ownership can be released to a waiter.

## Known occurrence

PR #415 final convergence moved `mark_invalid()` before `_release_guard`. The regression `test_racing_invalidation_is_published_before_e_can_be_released` reproduces the three-thread interleaving that previously allowed a waiter to revalidate a known-unsafe runtime.

## Limits

The exact state name and lock differ by subsystem. The reusable rule is about publication-before-handoff, not about `INVALID`, `_release_guard`, or runtime caches specifically.