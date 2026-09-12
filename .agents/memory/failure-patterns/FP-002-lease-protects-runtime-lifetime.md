---
schema_version: 1
id: FP-002
type: failure-pattern
status: validated
scope: [concurrency, runtime-lifecycle, cache, cleanup]
created_at: 2026-09-13
last_verified_at: 2026-09-13
confidence: high
evidence:
  - "PR #415, final head 6ef7f833b4b7723c95d89e6cc0967a6964c679b3"
  - "tests/test_runtime_safety_core.py::test_pinned_eviction_returns_busy_without_touching_the_pinned_entry"
  - "tests/test_runtime_safety_core.py::test_unload_model_refuses_a_leased_runtime_via_any_identifier"
validated_by: [codex-adversarial-review, deterministic-regression]
supersedes: []
superseded_by: []
review_after: null
---

# A live lease protects runtime lifetime

## Trigger

Eviction, replacement, unload, offload, or cleanup can run while another caller still has a usable reference to the runtime.

## Failed assumption

A local Python/object reference is not a lifetime contract. Cleanup can invalidate device state, provider state, files, buffers, or other external resources while the object remains reachable.

## Required invariant

While `lease_count > 0`, destructive lifecycle operations for that runtime are forbidden. Execution ownership implies a live lease. Release execution ownership before decrementing the final lease.

## Proof

Attempt eviction/unload/replacement while a lease is active and prove the operation is refused without cleanup side effects. Then prove the same operation can proceed after the lease is released.

## Known occurrence

PR #415 added explicit lease/pin accounting because published runtimes could otherwise be selected for destructive lifecycle work while still in use.

## Limits

A lease protects runtime lifetime, not arbitrary unrelated resources. Separate ownership must exist for anything whose lifetime is not identical to the runtime entry.