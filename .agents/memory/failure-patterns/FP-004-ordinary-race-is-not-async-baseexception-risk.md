---
schema_version: 1
id: FP-004
type: failure-pattern
status: validated
scope: [concurrency, cancellation, risk-classification, runtime-lifecycle]
created_at: 2026-09-13
last_verified_at: 2026-09-13
confidence: high
evidence:
  - "PR #415 Codex review history and final head 6ef7f833b4b7723c95d89e6cc0967a6964c679b3"
  - "commit 6ef7f833b4b7723c95d89e6cc0967a6964c679b3 explicitly separates an ordinary scheduling race from three accepted async-BaseException windows"
validated_by: [codex-adversarial-review, deterministic-regression]
supersedes: []
superseded_by: []
review_after: 2027-03-13
---

# Ordinary scheduling races and async BaseException windows are different risk classes

## Trigger

A concurrency finding is dismissed as an asynchronous-signal or instruction-boundary edge case without first proving that ordinary thread scheduling cannot reach it.

## Failed assumption

All narrow race windows have the same operational reachability. In practice, an ordinary scheduler-reachable interleaving is materially different from interruption that requires asynchronous `BaseException` delivery at a specific instruction boundary.

## Required invariant

Classify reachability before accepting residual risk. If ordinary scheduling can reproduce the unsafe state, treat it as a normal concurrency defect. Reserve the async-interruption class for windows that require asynchronous `BaseException`/signal delivery and are not reachable through normal scheduling or cooperative cancellation.

## Proof

Try to construct a deterministic ordinary-thread interleaving using real synchronization. Only after that fails for principled reasons should the risk be classified as async-interruption-only.

## Known occurrence

PR #415 initially had three accepted async-`BaseException` residual windows. A later invalidation/release finding looked similarly narrow but was reproducible through ordinary scheduling, so it remained a merge blocker and was fixed in commit `6ef7f833...`.

## Limits

This classification is implementation-sensitive and must be revisited when cancellation, signal handling, runtime ownership, or interpreter assumptions change.