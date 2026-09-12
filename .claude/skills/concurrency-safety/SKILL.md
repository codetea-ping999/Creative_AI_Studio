---
name: concurrency-safety
description: Use for changes involving shared runtime ownership, caching, leases, lock ordering, admission, cleanup, cancellation, or concurrent lifecycle/state-machine code.
---

# Concurrency Safety Adapter

This Claude-facing skill is a thin adapter. The canonical procedure lives at:

`.agents/skills/concurrency-safety/SKILL.md`

Before reasoning about or editing concurrency-sensitive code, read that canonical skill and follow it as the controlling procedure. Then load the failure-pattern memories it references.

Do not copy or restate the canonical invariants here. If this adapter and the canonical skill ever disagree, `.agents/skills/concurrency-safety/SKILL.md` wins.

When the canonical skill requires escalation, stop bounded implementation rather than weakening or inventing a concurrency contract.
