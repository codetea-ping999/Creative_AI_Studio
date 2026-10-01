# AI Development Operating Model

Creative AI Studio uses AI agents as a layered engineering organization rather than treating every model as an interchangeable coder.

The goal is to preserve AI development speed while keeping architecture, release risk, and final accountability legible to a human operator.

## Roles

### Human operator — Owner / Release Manager

The human operator owns:

- product scope and release scope;
- Stable / Preview / Experimental classification;
- final risk acceptance;
- merge and release decisions;
- exceptions to the rules in this document.

The operator should not need to read every generated line of code. Agent reports must compress work into decision-relevant evidence.

### OpenCode — Delivery / implementation lane

OpenCode is the default execution lane for bounded engineering work:

- issue implementation;
- tests and regression tests;
- bug reproduction and minimal fixes;
- local refactors with stable contracts;
- lint/type/build fixes;
- documentation maintenance;
- dependency and release-checklist chores;
- CI failure investigation.

OpenCode is an execution surface, not a fixed model identity. Prefer the cheapest/local model that reliably satisfies the task and verification gate.

OpenCode workers must not silently make product or architecture decisions. If a task requires changing a subsystem boundary, public contract, persistence rule, concurrency invariant, security posture, or release scope, stop and escalate.

### Claude / Codex — Technical leads and review board

Claude and Codex are scarce, high-leverage lanes. Use them where reasoning cost is higher than implementation cost.

Typical uses:

- architecture and ambiguous requirement analysis;
- cross-subsystem design;
- persistence, concurrency, lifecycle, security, recovery, and migration work;
- adversarial review;
- exact-HEAD / full-diff merge review;
- deciding whether a failing implementation indicates a local bug or a flawed design;
- release-blocker analysis.

For high-risk work, the final reviewer should be independent from the implementation lane whenever practical.

### Technical-lead mechanical closure exception

Claude / Codex technical leads are reviewers and decision-makers by default, not
a substitute implementation lane.

After a bounded OpenCode implementation, a technical lead may directly close a
worker-identified leftover only when **all** of the following are true:

1. the resulting patch is at most 3 changed lines;
2. the exact change was already identified and justified by the worker's own
   deterministic tooling or diagnostics;
3. the change has zero behavioral or semantic effect and is limited to
   mechanical cleanup such as removing a proven-unused import, dead code,
   whitespace, or equivalent non-functional residue;
4. the lead independently re-runs the relevant deterministic verification before
   committing the change;
5. the lead explicitly discloses the direct edit in the completion report.

This exception must not be used for changes to logic, defaults, comparisons,
control flow, error behavior, public APIs, persistence, concurrency, security,
release scope, or other contract-bearing behavior, even when the patch is only
one line.

If any condition above is not satisfied, the lead must not implement the fix
directly. Return the work to a bounded implementation lane or escalate to the
human operator.

The purpose of this exception is to avoid spending a full worker invocation or
human decision cycle on already-diagnosed non-semantic residue without allowing
the technical lead to quietly become the implementation lane.

### CI — Deterministic quality gate

Anything that can be checked mechanically should move to CI instead of consuming human or model judgment repeatedly.

Examples:

- unit/integration/E2E tests;
- coverage floors;
- lint and type checks;
- build/smoke tests;
- security/dependency checks;
- architectural AST/static guards;
- bootstrap zero-load assertions;
- release artifact validation.

A worker saying that a change is correct is never a substitute for an available deterministic check.

## Risk-based routing

### Low risk

Examples: docs fixes, mechanical cleanup, lint/type fixes, narrow tests, bounded release chores.

Route:

`OpenCode -> CI -> merge`

Escalate only if the task stops being mechanical.

### Medium risk

Examples: isolated bug fix, local behavior change, bounded refactor with an established contract.

Route:

`OpenCode -> CI -> one independent Claude or Codex review -> merge`

Use the reviewer with available quota and relevant context. Do not require both by default.

### High risk

Examples: cross-subsystem behavior, persistence contract, runtime lifecycle, cancellation/recovery, migrations, security-sensitive changes.

Route:

`Claude or Codex design -> OpenCode implementation -> CI -> independent Claude or Codex adversarial review`

The implementation lane must not broaden the approved design.

### Foundation / release blocker

Examples: shared concurrency primitives, data-loss risk, startup/install failure, release-critical security issue.

Route:

`technical-lead design -> bounded implementation -> deterministic proof -> independent adversarial review -> human merge decision`

Use both Claude and Codex only when the extra independence materially reduces risk. Do not convene both for routine work.

## OpenCode stop / escalation conditions

OpenCode must stop and report instead of widening scope when any of the following occurs:

- acceptance criteria conflict with current architecture;
- a public or persisted contract must change;
- the proposed fix crosses an unapproved subsystem boundary;
- concurrency/locking/lifecycle invariants need redesign;
- security or destructive-data risk appears;
- the same bounded implementation fails twice for the same reason;
- tests reveal a systemic rather than local defect;
- required verification cannot be run or trusted;
- remote HEAD moved when exact-head / fast-forward-only work was requested.

Stopping is a successful outcome when the task has reached its decision boundary.

## One worktree, one writer

All agent write work follows the repository's existing harness rules:

- one writer per worktree;
- isolated worktree for delegated writes;
- no concurrent provider edits in the same checkout;
- deterministic verification before integration;
- no recursive Claude <-> Codex delegation loops.

See `docs/agent-harness.md`, `docs/cross-agent-harness.md`, and `.agents/protocol/v1/worker-policy.md`.

## Required completion report

Every implementation task must end with a short operator-facing report containing exactly these decision categories:

1. **What changed** — files and behavior, not a narrative transcript.
2. **Why** — the acceptance criterion or defect being addressed.
3. **What could break** — realistic regression surface and remaining uncertainty.
4. **Evidence** — tests/checks actually observed, including failures if any.
5. **Human decision needed** — merge/review/scope question; write `none` when no human judgment is required beyond the normal merge gate.

If a technical lead used the mechanical closure exception, the report must name
the exact direct edit, why the exception applied, and the independent verification
performed afterward.

For higher-risk work also include:

- exact start and end HEAD;
- changed-file scope;
- whether runtime/public/persistence contracts changed;
- known residual risks;
- explicit reviewer verdict when a review was requested.

Do not report unobserved checks as passing.

## Human review budget

Human attention is reserved for decisions, not transcript replay.

The operator should primarily inspect:

- the five-part completion report;
- release/user impact;
- residual risks;
- reviewer disagreement;
- targeted code identified by an agent as requiring human judgment.

If a human must repeatedly reconstruct the entire change from raw agent output, the reporting contract has failed and should be improved.

## Release mode

When the project enters release mode:

- freeze the Stable scope;
- stop net-new feature work unless explicitly approved;
- classify unfinished features as Preview or Experimental instead of silently lowering Stable quality;
- only release blockers may reopen frozen foundations;
- send non-blocking improvements to the next-version backlog;
- prefer the smallest safe fix over architectural cleanup;
- require clean-environment E2E evidence for supported release paths.

A foundation marked `FROZEN` is not "perfect"; it means further changes require a concrete blocker or an explicitly scheduled post-release design task.

## Default product contract

The project should distinguish:

- **Stable** — behavior the release promises and treats regressions as blockers;
- **Preview** — usable behavior whose UX/platform/compatibility contract may still change;
- **Experimental** — opt-in or development-facing work with no stability promise.

New capabilities do not become Stable merely because implementation exists.

## Measuring the routing policy

Do not assume the provider/model routing above is permanently optimal. Track enough evidence to revise it.

Useful observations include:

- accepted without rework;
- number of review rounds;
- elapsed time;
- deterministic verification result;
- quota/cost when available;
- failure classification;
- escaped regression count.

Prefer measured routing changes over model reputation or intuition.
