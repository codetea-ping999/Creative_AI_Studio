# Agent Cognitive Infrastructure Architecture Decision

Status: Proposed  
Date: 2026-09-13

## Context

Creative AI Studio already uses multiple AI providers and execution surfaces for planning, implementation, review, and verification. The repository has a cross-agent harness, deterministic broker contracts under `.agents/protocol/v1`, provider-specific configuration in `.claude/` and `.codex/`, repository-wide instructions in `AGENTS.md`, and durable engineering documentation under `docs/`.

The current system can hand work between agents, but the knowledge created by expensive review and debugging cycles is still mostly trapped in pull-request discussions, task reports, ad hoc prompts, and provider-specific context. A future agent can therefore repeat a failure that a previous agent already paid to discover.

The immediate motivating case is PR #415 (PR4a runtime safety). It produced reusable evidence about runtime ownership, lock ordering, deterministic race testing, invalidation ordering, and the distinction between ordinary scheduling races and asynchronous `BaseException` instruction-boundary risks. Those lessons should become reusable repository knowledge instead of remaining only as PR history.

The goal of v0.1 is deliberately small:

> Preserve one verified engineering lesson well enough that a later agent can use it to avoid repeating the same class of failure.

This ADR does not attempt to build autonomous self-improvement, a general RAG system, or a knowledge graph.

## Decision

### 1. `.agents/` is the provider-neutral source of truth

The existing `.agents/protocol/v1` remains the canonical cross-agent execution protocol. Cognitive infrastructure extends that namespace rather than creating a second agent subsystem.

Target structure:

```text
.agents/
├── protocol/                 # existing deterministic execution contracts
│   └── v1/
├── constitution/             # long-lived repository-level agent principles
├── memory/                   # validated reusable engineering knowledge
│   └── failure-patterns/
├── reflections/              # candidate lessons and their validation lifecycle
│   ├── inbox/
│   ├── validated/
│   ├── rejected/
│   └── archive/
└── skills/                   # reusable procedures derived from validated knowledge
```

`CLAUDE.md`, `AGENTS.md`, `.claude/*`, `.codex/*`, future Antigravity configuration, and local-LLM prompts are adapters or indexes. They are not the canonical copy of shared knowledge.

Provider-native memory may improve a local session, but it is never the repository source of truth.

### 2. Keep memory, reflection, and skill as different artifacts

The system distinguishes three concepts:

#### Memory: what is known

A memory records a reusable, evidence-backed engineering fact, invariant, failure pattern, or decision that should affect future work.

Examples:

- a leased runtime is not evictable;
- a load lock does not provide execution exclusion;
- ordinary thread-scheduling races and asynchronous signal interruption are different risk classes;
- unsafe runtime state may need to become visible before execution ownership is released.

A memory is not a chronological work log.

#### Reflection: what may have been learned

A reflection is a candidate lesson extracted from a concrete task, PR, incident, review, or experiment. It captures observations before they are allowed to become canonical knowledge.

Reflection is the quarantine boundary between agent-generated interpretation and repository truth.

#### Skill: how work should be performed

A skill is a reusable procedure or checklist that changes agent behavior.

Examples:

- construct concurrency RED tests with `Event`/`Barrier` or real lock contention;
- audit lock ordering before changing runtime ownership;
- perform bounded implementation from an already-approved invariant set;
- run an exact-HEAD merge gate.

A validated memory may cause a skill to be created or amended, but the two remain separate artifacts.

### 3. Do not promote agent reflection directly into canonical memory

An agent may propose a reflection, but v0.1 never allows a model to promote its own interpretation directly into validated memory.

A reflection is eligible for promotion only when all of the following are true:

1. **Concrete evidence exists.** At least one durable source supports the lesson, such as a merged PR, deterministic regression test, code reference, accepted ADR, incident record, or independent review finding.
2. **The claim is generalized carefully.** The lesson states the reusable invariant without pretending that one example proves a broader claim than the evidence supports.
3. **An independent validation step exists.** A human, a different provider/model, or a deterministic check validates the proposed lesson. The same agent's self-review alone is insufficient for promotion.
4. **Contradictions are checked.** Existing canonical memory and current code are checked for conflicts or superseding facts.
5. **Scope and freshness are explicit.** The artifact states where the lesson applies and when it should be reviewed again if it depends on mutable implementation facts.

Promotion states are:

```text
inbox -> validated -> promoted
      -> rejected
      -> archive
```

`validated` means the reflection is supported. `promoted` means its reusable content has been incorporated into memory and/or a skill. The reflection remains as provenance rather than becoming the canonical rule itself.

### 4. Canonical knowledge must carry provenance and lifecycle metadata

Every canonical memory introduced by v0.1 must include machine-readable frontmatter with at least:

```yaml
schema_version: 1
id: FP-XXX
type: failure-pattern
status: validated
scope: []
created_at: YYYY-MM-DD
last_verified_at: YYYY-MM-DD
confidence: high | medium | low
evidence: []
supersedes: []
superseded_by: []
review_after: YYYY-MM-DD | null
```

The body should contain, when applicable:

- trigger / context;
- failed assumption;
- required invariant;
- proof or verification method;
- known occurrence(s);
- limits of the lesson.

Mutable implementation observations must have a review date. Long-lived architectural invariants may use `review_after: null` but still require evidence.

Superseded memories are retained for provenance and marked as superseded rather than silently rewritten as if the old rule never existed.

### 5. Do not store private chain-of-thought or secrets

Cognitive infrastructure stores conclusions, evidence, constraints, and reproducible reasoning artifacts. It does not store hidden chain-of-thought, credentials, tokens, private user data, or raw conversational transcripts merely because an agent saw them.

Useful reflection is expressed as externally inspectable engineering facts such as:

```text
observation -> evidence -> generalized lesson -> validation -> promotion
```

not as a transcript of internal reasoning.

Issue bodies, PR comments, stderr, previous-agent prose, generated reports, and imported external text remain untrusted inputs until independently validated.

### 6. Retrieval is deterministic in v0.1

v0.1 does not use embeddings, a vector database, autonomous semantic retrieval, or a RAG service.

Knowledge is selected through explicit metadata such as:

- repository path;
- subsystem;
- tags;
- task class;
- known failure class;
- explicit links from a skill or adapter.

Example:

```text
touches core/models/cache.py or core/models/service.py
    -> runtime ownership memories
    -> concurrency failure patterns
    -> concurrency-safety skill
```

This intentionally creates observable retrieval behavior that can later become evaluation data. Semantic retrieval is reconsidered only after the repository has enough high-quality canonical memories to measure retrieval quality against known expected results.

### 7. Skills are shared assets; provider adapters are derived views

Canonical reusable procedures live under `.agents/skills/`.

Provider-specific representations may exist under locations such as:

```text
.claude/skills/
.codex/...
future provider-specific directories
```

but they must either:

- reference the canonical source directly when supported; or
- be generated/synchronized from the canonical source by a deterministic repository tool.

Manual copy-and-edit divergence between providers is not an accepted steady state.

Provider-specific metadata such as model choice, effort level, tool declarations, or invocation syntax belongs in the adapter, not in the provider-neutral procedure unless it is genuinely part of the shared engineering contract.

`CLAUDE.md` and `AGENTS.md` should stay concise and act primarily as indexes to durable contracts rather than accumulating every procedural lesson inline.

### 8. Model routing uses escalation rules, not prestige

Cognitive infrastructure should make a less-capable or lower-cost model safer to use on bounded work by reducing ambiguity and surfacing prior evidence. It does not pretend all models are interchangeable.

Initial routing principle:

```text
ambiguous architecture / new invariants / systemic concurrency design
    -> strongest available planning/reasoning route

bounded implementation with approved invariants
    -> implementation route

independent adversarial review
    -> different provider/model when practical
```

A bounded implementation route must escalate rather than improvise when any of the following occurs:

- a new invariant is discovered during implementation;
- ownership boundaries across multiple subsystems must change;
- the same completion condition fails twice;
- deterministic RED evidence cannot be constructed for a claimed bug;
- a proposed fix weakens an approved contract;
- concurrency, persistence, migration, auth, or destructive behavior requires a new architectural decision.

These rules complement the existing routing policy in `docs/cross-agent-harness.md`; they do not replace it.

### 9. Gamification is a presentation layer over engineering evidence

Game mechanics may be used to make project progress and organizational learning easier to understand and more motivating.

Possible presentation concepts include:

- quests for bounded engineering goals;
- bosses for recurring failure classes;
- loot for reusable tests, memories, or skills;
- skill trees for demonstrated engineering capability;
- repository XP for validated reusable knowledge;
- locked quests for work whose prerequisites are not yet satisfied.

However:

> Gamification never creates engineering truth. Engineering evidence creates gamification state.

No merge gate, severity, priority, memory confidence, or architectural decision may depend on manually assigned XP, stars, streaks, issue counts, or leaderboard position.

Game state must be derivable from durable evidence such as merged PRs, tests, accepted reviews, validated memories, skills, and explicit project gates.

Failure itself is not a negative score. Repeating a previously validated failure class without retrieving/applying the relevant knowledge is a signal of a memory, retrieval, skill, or harness failure.

### 10. PR #415 is the Golden Memory Case

PR #415 (PR4a runtime safety core) is the single v0.1 pilot case.

The first reflection must be derived from the merged PR and its durable evidence, not from a free-form recollection alone.

The initial extraction should remain small. It should promote no more than approximately five high-value reusable lessons, with likely candidates including:

- load ownership is not execution ownership;
- leased runtime lifetime must be protected from destructive cleanup;
- metadata locks must not span blocking work;
- ordinary scheduling races are different from asynchronous instruction-boundary interruption risks;
- unsafe state must be published before an execution owner can expose a known-unsafe runtime to a waiter.

The pilot should also produce one `concurrency-safety` skill that converts validated lessons into concrete behavior.

The objective is quality of one complete learning cycle, not volume of memories.

### 11. PR4b is the first evaluation, not the first knowledge import

PR4b runtime integration is held until the Golden Memory Case and initial `concurrency-safety` skill exist.

PR4b then becomes the first real evaluation of whether the cognitive infrastructure changes agent behavior.

At minimum compare PR4a and PR4b on:

- same-class failure recurrence;
- review findings by severity/class;
- number of fix/review rounds;
- first-pass deterministic test quality;
- accepted change rework;
- architecture escalation count;
- whether the relevant memory/skill was retrieved before the affected change.

v0.1 is considered useful if at least one failure class learned from PR4a is demonstrably prevented or caught earlier in PR4b because the relevant memory/skill was applied.

The target is not to prove that one model is better than another. The target is to measure whether the repository's environment makes later work better than earlier work.

### 12. Memory quality is evaluated by changed behavior

A memory that does not change a future decision, implementation, review, or verification step is archive material, not high-value canonical memory.

Primary health signal:

> Did the repository repeat a failure class it had already validated and made retrievable?

If yes, classify why:

```text
memory_missing
memory_stale
retrieval_failed
skill_missing
skill_ignored
adapter_drift
contradictory_guidance
novel_failure
```

Do not default to blaming the model when the environment failed to supply the knowledge it claimed to have learned.

## Initial implementation sequence

After this ADR is accepted:

1. Create the minimal `.agents/constitution`, `.agents/memory/failure-patterns`, `.agents/reflections`, and `.agents/skills` structure without introducing a database or service.
2. Create one Golden Reflection for PR #415 with direct evidence links.
3. Promote a small set of validated failure-pattern memories from that reflection.
4. Create one provider-neutral `concurrency-safety` skill.
5. Add the smallest Claude and Codex adapter/index changes needed to make the canonical knowledge reachable.
6. Add deterministic validation that provider adapters do not silently drift from canonical shared skills if generated copies are required.
7. Run PR4b as the first pilot evaluation.
8. Reflect on the pilot and revise the infrastructure to v0.2 only from observed friction or failure.

Each step should be a small, reviewable change. The ADR is intentionally separated from implementation so the repository does not commit to a file/schema layout merely because code happened to be written first.

## Non-goals for v0.1

v0.1 explicitly does not include:

- vector databases;
- embeddings or semantic RAG infrastructure;
- automatic ingestion of all historical PRs/issues/chats;
- automatic promotion of reflections into canonical memory;
- autonomous rewriting of repository rules by agents;
- storing chain-of-thought or private conversation transcripts;
- provider-specific memory as the shared source of truth;
- a universal agent reputation or leaderboard system;
- XP or gamification state as a merge/priority authority;
- automatic multi-agent self-improvement loops without bounded stopping rules;
- PR4b implementation in the same change as this ADR.

## Consequences

### Positive

- Expensive debugging and review work can become durable repository capability.
- Claude, Codex, future providers, and local models can share the same validated engineering knowledge.
- Provider replacement does not erase repository learning.
- Reflection is separated from truth, reducing self-reinforcing agent mistakes.
- Deterministic retrieval makes early behavior observable and testable.
- Skills can improve bounded-model performance without pretending to erase model capability differences.
- Gamification can visualize genuine progress without becoming the source of engineering decisions.
- PR4b provides an immediate real-world evaluation target.

### Costs and risks

- Canonical knowledge adds maintenance work and can become stale if lifecycle metadata is ignored.
- Excessive memory volume can reduce adherence and make retrieval noisy.
- Over-generalizing one incident can create harmful rules; promotion therefore requires evidence and independent validation.
- Provider adapters can drift if synchronization is manual.
- Metrics can be gamed if treated as targets rather than diagnostic signals.
- The system itself can become an attractive infrastructure project that distracts from Creative AI Studio; v0.1 therefore limits itself to one Golden Memory Case and one pilot skill.

## Revisit conditions

Revisit this ADR if any of the following becomes true:

- deterministic metadata-based retrieval cannot select the needed knowledge reliably;
- canonical memories grow large enough that manual/path-based discovery becomes a measurable bottleneck;
- provider skill formats cannot share a useful common core without lossy duplication;
- adapter drift occurs despite deterministic checks;
- PR4b shows no measurable behavioral benefit from the Golden Memory Case;
- automatic promotion can be demonstrated to be safer and more reliable than the v0.1 validation gate;
- the repository needs organization-wide knowledge sharing beyond a single Git repository.
