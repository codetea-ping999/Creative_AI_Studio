---
schema_version: 1
id: REF-PR415-001
type: reflection
status: promoted
source_pr: 415
source_head: 6ef7f833b4b7723c95d89e6cc0967a6964c679b3
source_merge: d6293bb5f5ea3156eb082c3c0d10c185cdd5e6d4
created_at: 2026-09-13
validated_by:
  - codex-adversarial-review
  - deterministic-runtime-safety-regressions
  - merged-main
promotion_targets:
  - FP-001
  - FP-002
  - FP-003
  - FP-004
  - FP-005
  - concurrency-safety
---

# Golden Reflection: PR #415 runtime safety

## Task

PR #415 introduced the PR4a runtime-safety core: explicit runtime entries, leases/pins, execution exclusion, process-wide admission, capacity-before-load behavior, retirement/unload rules, and deterministic concurrency tests. Generator migration and production multi-lane activation were intentionally out of scope.

## What happened

The implementation needed repeated adversarial review because several defects were not syntax or unit-test failures; they were ownership and ordering mistakes that only appeared under carefully constructed interleavings. Codex review rounds found both ordinary scheduler-reachable races and narrower asynchronous `BaseException` instruction-boundary risks.

The final ordinary invalidation race was fixed at exact head `6ef7f833b4b7723c95d89e6cc0967a6964c679b3`. The fix publishes runtime invalidation before attempting `_release_guard`, preventing another exit path from releasing execution ownership while a waiter can still observe the entry as `READY`. A deterministic three-thread regression proves the ordering.

## Evidence

- Merged PR: `#415` — `PR4a: Add runtime lease and admission safety core`.
- Final PR head: `6ef7f833b4b7723c95d89e6cc0967a6964c679b3`.
- Merge commit on main: `d6293bb5f5ea3156eb082c3c0d10c185cdd5e6d4`.
- Deterministic test suite: `tests/test_runtime_safety_core.py`.
- Final regression: `test_racing_invalidation_is_published_before_e_can_be_released`.
- Review provenance: Codex adversarial review drove multiple fixes; the final exact-head Codex re-review was unavailable after review quota exhaustion, so this reflection does not claim an exact-head Codex approval that did not occur.

## Generalized lessons

1. A load/construction lock is not execution exclusion.
2. A live lease is the lifetime contract that blocks destructive cleanup.
3. Metadata/state locks should not span waits, loader work, provider work, cleanup, or callbacks.
4. Ordinary scheduler-reachable races and async-`BaseException` instruction-boundary windows must be classified separately.
5. Unsafe state must be published before ownership/exclusion is released to a waiter.

## What was not promoted

The exact number of review rounds, test totals, coverage percentage, CI run IDs, intermediate SHAs, and PR-specific patch chronology remain episodic evidence. They are useful for audit/history but are not reusable engineering rules.

The three accepted async-`BaseException` residual windows remain implementation-specific residual risks, not universal invariants. They are referenced by FP-004 only to preserve the risk-classification lesson.

## Promotion decision

The five generalized lessons above were promoted to `FP-001` through `FP-005`. Their procedural consequences were consolidated into the provider-neutral `concurrency-safety` skill.

This reflection is the v0.1 Golden Memory Case. Its success criterion is behavioral: PR4b should retrieve and apply these memories/skill and prevent or catch at least one already-known failure class earlier than PR4a did.