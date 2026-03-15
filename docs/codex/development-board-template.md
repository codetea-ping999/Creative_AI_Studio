# Creative AI Studio Development Board Template

AI + human hybrid development for Creative AI Studio should use a shared board model.

This template is designed to work with:

- GitHub Projects
- Notion
- Linear
- Trello
- Markdown

The goal is to keep parallel AI execution stable, reviewable, and easy to recover when threads drift.

---

## Board Lanes

Use these 6 lanes:

```text
Backlog
Next
In Progress
Review
Fix
Done
```

### Backlog

Future work that is not ready for execution yet.

Allowed here:

- undefined priority
- incomplete dependency analysis
- larger ideas that need to be split later

Examples:

```text
video generator implementation
audio generator implementation
workflow graph engine
batch generation
plugin system
model download manager
GPU monitoring
```

### Next

Tasks ready to hand off to Codex or another implementation thread.

A task can enter `Next` only when:

- task definition is written
- dependencies are known
- scope is small
- acceptance criteria are explicit

Examples:

```text
task-010 integration review
task-011 generator registry
task-012 model manifest loader
task-013 project repository
task-014 asset repository
```

### In Progress

Tasks currently being executed.

Recommended WIP:

- ideal: 3 to 7 parallel threads
- upper bound: around 10

Too much parallelism increases merge cost and review noise.

### Review

Completed implementation moves here before it is considered integrated.

Recommended review owners:

- Core Monitor
- API Monitor
- Generator Monitor
- UI Monitor
- Integration Monitor

Review focus:

- architecture consistency
- schema consistency
- API contract alignment
- generator interface alignment
- naming rules

### Fix

Only small fixes discovered during review belong here.

Examples:

```text
fix-core-001 schema naming mismatch
fix-api-002 job_id response mismatch
fix-generator-003 generator return type mismatch
fix-ui-004 payload field mismatch
```

Keep this lane narrow. Do not mix new feature work with review fixes.

### Done

Move a task here only when all of the following are true:

- review passed
- fix work is complete
- integration check passed

---

## Board Snapshot Example

```text
Backlog
│
├ video generator
├ audio generator
└ workflow graph

Next
│
├ task-010 integration review
├ task-011 generator registry
└ task-012 project repository

In Progress
│
├ task-013 asset repository
└ task-014 API job endpoint

Review
│
├ task-008 fastapi bootstrap
└ task-009 prompt form UI

Fix
│
├ fix-api-001 job response mismatch
└ fix-ui-002 payload mismatch

Done
│
├ task-001 generation schema
├ task-002 job schema
└ task-003 model manifest
```

---

## Operating Rules

### Rule 1

`1 task = 1 responsibility`

Good:

```text
implement JobRepository
```

Bad:

```text
implement job system
```

### Rule 2

Every implementation task must pass through `Review`.

Skipping review is what usually breaks AI parallel development.

### Rule 3

Separate `Fix` from new feature development.

Do not bury review corrections inside unrelated implementation cards.

### Rule 4

Keep `Next` to 10 items or fewer.

If `Next` gets too large, AI threads lose focus and prioritization quality drops.

---

## ID Rules

Use stable IDs for every board item.

Task:

```text
task-001
task-002
task-003
```

Fix:

```text
fix-001
fix-002
```

Review:

```text
review-001
```

IDs should stay stable even if titles change.

---

## Common Card Schema

Use the same fields regardless of tool.

| Field | Purpose |
| --- | --- |
| `ID` | Stable task identifier such as `task-013` |
| `Title` | Short action-oriented title |
| `Type` | `task`, `review`, or `fix` |
| `Area` | `core`, `api`, `generator`, `ui`, `integration`, `ops` |
| `Status` | One of the 6 board lanes |
| `Goal` | What the task must achieve |
| `Inputs` | Docs, files, and dependencies to validate against |
| `Constraints` | Explicit non-goals and guardrails |
| `Acceptance` | Concrete completion criteria |
| `Owner` | Human, Codex thread, or monitor |
| `Links` | Relevant doc or PR references |

Recommended optional fields:

- `Priority`
- `Blocked By`
- `Review Owner`
- `Notes`

---

## Card Template

Copy this into any system as the body or description:

```text
ID: task-000
Title: Short task title
Type: task
Area: core
Status: Next
Owner: Codex

Goal:
- one clear outcome

Inputs:
- docs/architecture.md
- docs/domain-model.md

Constraints:
- keep scope minimal
- no unrelated modifications

Acceptance:
- implementation compiles or imports
- behavior matches contract
- no architectural drift

Links:
- docs/checklists/core-review-checklist.md
```

---

## Tool Mapping

### GitHub Projects

Recommended fields:

- `Title`
- `Status` as a single select with `Backlog`, `Next`, `In Progress`, `Review`, `Fix`, `Done`
- `Type`
- `Area`
- `Priority`
- `Review Owner`

Recommended item body:

- paste the common card template into the issue or draft item body

### Notion

Use a database with these properties:

- `Name` as title
- `Status` as status/select
- `ID` as rich text
- `Type` as select
- `Area` as select
- `Owner` as people or text
- `Priority` as select
- `Blocked By` as text or relation
- `Links` as URL or text

Put `Goal`, `Inputs`, `Constraints`, and `Acceptance` in the page body.

### Linear

Map the 6-lane model onto workflow states:

- `Backlog`
- `Next`
- `In Progress`
- `Review`
- `Fix`
- `Done`

Recommended labels:

- `core`
- `api`
- `generator`
- `ui`
- `integration`

Put the common card template in the issue description.

### Trello

Create 6 lists:

- `Backlog`
- `Next`
- `In Progress`
- `Review`
- `Fix`
- `Done`

Card title format:

```text
task-013 Project Repository
```

Use checklist items for:

- Inputs
- Constraints
- Acceptance

### Markdown

Use headings for lanes and paste cards as checklist items or short blocks.

Example:

```md
# Creative AI Studio Development Board

## Backlog
- [ ] task-020 video generator implementation
- [ ] task-021 audio generator implementation

## Next
- [ ] task-010 integration review
- [ ] task-011 generator registry
- [ ] task-012 model manifest loader

## In Progress
- [ ] task-013 project repository

## Review
- [ ] task-008 fastapi bootstrap

## Fix
- [ ] fix-001 job response mismatch

## Done
- [x] task-001 generation schema
- [x] task-002 job schema
- [x] task-003 model manifest
```

---

## Codex Task Prompt

Recommended prompt format:

```text
Execute task-013.

Follow the project architecture:
docs/architecture.md

Validate against:
docs/checklists/core-review-checklist.md

Constraints:
- keep scope minimal
- no unrelated modifications
```

This works best when the board card and the Codex prompt use the same task ID.

---

## Recommended Daily Flow

### Morning

```text
Backlog review
Next selection
```

### Midday

```text
AI implementation
```

### Evening

```text
Review
```

### Night

```text
Fix
Integration check
```

---

## Recommended Initial Board For This Project

### Next

```text
task-010 Integration Review
task-011 Generator Registry
task-012 Model Manifest Loader
task-013 Project Repository
task-014 Asset Repository
```

### Backlog

```text
model management system
video generator implementation
audio generator implementation
workflow graph engine
batch generation
plugin system
GPU monitoring
```

---

## Why This Board Works

This structure stabilizes AI parallel development because it separates:

- future ideas from executable work
- implementation from review
- review findings from new features
- completion from true integration

When AI threads are small, review is explicit, and fixes are isolated, the project scales without losing architecture discipline.
