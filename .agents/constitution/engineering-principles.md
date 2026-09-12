# Agent Engineering Principles

These principles govern the cognitive-infrastructure layer. They do not override product or domain specifications.

1. **Evidence beats recollection.** Prefer merged PRs, exact commit SHAs, deterministic tests, accepted ADRs, and current code over narrative summaries.
2. **Reflection is not truth.** A model may propose a lesson; canonical memory requires evidence, independent validation, contradiction checks, and explicit scope/freshness.
3. **Primary sources outrank cognitive memory.** If memory conflicts with accepted specifications, tests, or current code, the memory is stale or contradicted until revalidated.
4. **Memory must change behavior.** Keep knowledge that changes a future decision, implementation, review, or verification step. Archive trivia and chronology.
5. **Repeated known failure is an infrastructure signal.** If a validated and retrievable failure class repeats, inspect memory, retrieval, skill, adapter, and guidance failures before blaming a model.
6. **Procedures remain bounded.** Skills must define escalation conditions and must not silently invent new invariants.
7. **Game state is derived.** XP, bosses, quests, and skill levels may visualize durable engineering evidence but never create truth, priority, severity, or merge authority.