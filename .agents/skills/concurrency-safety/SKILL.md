---
name: concurrency-safety
description: Use when changing shared runtime ownership, caching, leases, lock ordering, admission, cleanup, cancellation, or concurrent lifecycle/state-machine code.
---

# Concurrency Safety

Use this procedure before and during bounded concurrency-sensitive implementation. It operationalizes validated memories `FP-001` through `FP-005`; it does not replace the primary code/tests/ADRs that govern the subsystem.

## Load the relevant memory

Read:

- `.agents/memory/failure-patterns/FP-001-load-lock-is-not-execution-lock.md`
- `.agents/memory/failure-patterns/FP-002-lease-protects-runtime-lifetime.md`
- `.agents/memory/failure-patterns/FP-003-metadata-lock-must-not-span-blocking-work.md`
- `.agents/memory/failure-patterns/FP-004-ordinary-race-is-not-async-baseexception-risk.md`
- `.agents/memory/failure-patterns/FP-005-publish-unsafe-state-before-releasing-ownership.md`

If any memory conflicts with current code, tests, or an accepted specification, stop treating that memory as authoritative and escalate the contradiction.

## Before editing

1. Write down the resource/state ownership model: what is loaded, published, leased, executing, invalid, retiring, and cleaned up.
2. List every blocking primitive and its acquisition order. Separate short metadata transactions from blocking locks/resources.
3. Identify destructive operations and state which ownership/lease condition forbids each one.
4. Identify every handoff point where ownership is released to a waiter and state what safety state must already be visible before that handoff.
5. Classify cancellation/interruption mechanisms. Do not conflate cooperative cancellation, ordinary thread scheduling, and asynchronous `BaseException`/signal delivery.

## Implementation invariants

- Do not use a load/construction lock as proof of execution exclusivity.
- Do not evict, unload, replace, offload, or clean up a runtime while a protecting lease is live.
- Do not hold a metadata/state lock while waiting for admission/load/execution resources or while running loader, inference, provider, cleanup, callback, or re-entrant code.
- Publish known-unsafe state before releasing the ownership/exclusion primitive that permits another caller to proceed.
- Release resources exactly once on ordinary exceptions and cancellation paths. Do not infer that a cancellation request proves synchronous work has ended.

## Deterministic proof

For every concurrency claim that matters to correctness:

1. Construct the interleaving with `threading.Event`, `Barrier`, real lock contention, or another explicit synchronization point.
2. Prove the competing caller is genuinely blocked at the intended resource.
3. Assert state only after the relevant thread/event boundary is deterministic.
4. Join spawned threads and clean test synchronization helpers in `finally` paths so a failing assertion cannot hang the suite.
5. Do not use `sleep()` as the proof of ordering or mutual exclusion.

When evaluating a narrow race, first attempt to reproduce it with ordinary scheduling. Classify it as async-interruption-only only when ordinary scheduling cannot reach it for a principled reason.

## Escalate instead of improvising

Stop bounded implementation and request an architectural decision when any of these occurs:

- a new invariant is required;
- ownership boundaries across multiple subsystems must change;
- the proposed fix weakens an approved contract;
- deterministic RED evidence cannot be constructed for a claimed ordinary race;
- the same completion/safety condition fails twice after fixes;
- persistence, migration, auth, destructive behavior, or cancellation semantics require a new contract.

## Completion evidence

A concurrency-sensitive change is not complete merely because tests pass once. Report the exact HEAD, deterministic regression names, relevant invariant(s), residual risks classified by reachability, and the verification commands/results. Keep accepted residual risk explicit rather than hiding it in a generic known-issues bucket.