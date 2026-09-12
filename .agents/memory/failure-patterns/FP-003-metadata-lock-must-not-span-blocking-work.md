---
schema_version: 1
id: FP-003
type: failure-pattern
status: validated
scope: [concurrency, lock-ordering, cache, cleanup]
created_at: 2026-09-13
last_verified_at: 2026-09-13
confidence: high
evidence:
  - "PR #415, final head 6ef7f833b4b7723c95d89e6cc0967a6964c679b3"
  - "core/models/cache.py and core/models/service.py at 6ef7f833b4b7723c95d89e6cc0967a6964c679b3"
validated_by: [codex-adversarial-review, deterministic-regression-suite]
supersedes: []
superseded_by: []
review_after: null
---

# Metadata lock must not span blocking or re-entrant work

## Trigger

A metadata/state lock is held while code waits for another lock/resource or calls loader, inference, provider, cleanup, callback, or other code that can block or re-enter the subsystem.

## Failed assumption

Making a larger operation "atomic" by holding the metadata lock across slow work can create lock cycles, global stalls, and cleanup re-entry deadlocks.

## Required invariant

Keep metadata transactions short and state-only. Do not wait for admission/load/execution resources and do not run loader, inference, provider, cleanup, or callbacks while holding the metadata lock.

## Proof

Document the blocking-lock order separately from metadata transactions. Tests should force contention/re-entry boundaries and prove progress without relying on sleeps.

## Known occurrence

PR #415 separated the short metadata lock (`M`) from process admission (`G`), per-id load (`L`), per-entry execution (`E`), loader work, and cleanup.

## Limits

This is a subsystem-level rule, not a claim that every lock must be tiny. A different lock may legitimately protect a blocking operation if its ownership contract proves a cycle cannot form.