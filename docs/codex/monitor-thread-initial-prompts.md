# Monitor Thread Initial Prompts

固定監視スレッドに最初に投入するための初期プロンプト集。

使い方:
- 各カテゴリごとに固定スレッドを1本作る
- 対応するプロンプトを最初のメッセージとして投入する
- 以後はそのスレッドを継続利用する
- 横断確認は `docs/codex/integration-review-task.md` を別スレッドで使う

---

## 1. Core Monitor

推奨スレッド名:
- Core監視

初期プロンプト:

```md
# Task
Run ongoing core review for Creative AI Studio bootstrap tasks.

## Role
This thread is dedicated to monitoring and reviewing the Core layer only.

## Scope
- core schemas
- job model
- storage
- model manifest
- events

## Inputs
- docs/checklists/core-review-checklist.md
- docs/architecture.md
- docs/domain-model.md
- docs/api-contract.md

## Review Focus
- Keep shared schema definitions unified
- Detect naming drift across request/result/job/storage
- Check storage consistency and future extensibility
- Prevent image-specific logic from leaking into core abstractions

## Rules
- Prefer review over implementation
- If an issue is found, describe both the mismatch and the correct fix location
- Keep recommendations minimal and architecture-aligned
- Do not redesign the system unless the issue is critical

## Output Format

### Summary
- healthy / warning / critical

### Findings
- category:
- issue:
- impact:
- affected files:
- recommendation:

### Follow-up
- fix task candidate 1
- fix task candidate 2
```

---

## 2. API Monitor

推奨スレッド名:
- API監視

初期プロンプト:

```md
# Task
Run ongoing API review for Creative AI Studio bootstrap tasks.

## Role
This thread is dedicated to monitoring and reviewing the API layer only.

## Scope
- FastAPI bootstrap
- route structure
- request/response contracts
- error format

## Inputs
- docs/checklists/api-review-checklist.md
- docs/architecture.md
- docs/domain-model.md
- docs/api-contract.md

## Review Focus
- Verify API payloads match shared schemas
- Verify response shapes remain stable
- Detect route naming drift and error-format inconsistency
- Keep API responsibilities separated from repository and generator logic

## Rules
- Prefer review over implementation
- If an issue is found, specify the contract mismatch and the exact correction point
- Keep recommendations small and incremental
- Do not introduce unrelated endpoints or redesign routing

## Output Format

### Summary
- healthy / warning / critical

### Findings
- category:
- issue:
- impact:
- affected files:
- recommendation:

### Follow-up
- fix task candidate 1
- fix task candidate 2
```

---

## 3. Generator Monitor

推奨スレッド名:
- Generator監視

初期プロンプト:

```md
# Task
Run ongoing generator review for Creative AI Studio bootstrap tasks.

## Role
This thread is dedicated to monitoring and reviewing the Generator layer only.

## Scope
- BaseGenerator
- ImageGenerator stub
- output handling
- future video/audio extensibility

## Inputs
- docs/checklists/generator-review-checklist.md
- docs/architecture.md
- docs/domain-model.md

## Review Focus
- Verify generator interface consistency
- Verify return values align with GenerationResult
- Detect coupling to API or DB layers
- Confirm stub output behavior is traceable and future-safe

## Rules
- Prefer review over implementation
- If an issue is found, separate interface problems from implementation problems
- Keep changes scoped to generator consistency
- Do not add full runtime integrations unless required for a critical fix

## Output Format

### Summary
- healthy / warning / critical

### Findings
- category:
- issue:
- impact:
- affected files:
- recommendation:

### Follow-up
- fix task candidate 1
- fix task candidate 2
```

---

## 4. UI Monitor

推奨スレッド名:
- UI監視

初期プロンプト:

```md
# Task
Run ongoing UI review for Creative AI Studio bootstrap tasks.

## Role
This thread is dedicated to monitoring and reviewing the UI layer only.

## Scope
- prompt form
- app shell
- request payload construction
- future history/gallery expansion

## Inputs
- docs/checklists/ui-review-checklist.md
- docs/architecture.md
- docs/domain-model.md
- docs/api-contract.md

## Review Focus
- Verify field names match API payload names
- Verify state shape and submit payload stay coherent
- Detect component responsibility bloat
- Preserve extensibility for video/audio tabs and history/gallery

## Rules
- Prefer review over implementation
- If an issue is found, state the UI mismatch and the API or state contract it violates
- Keep recommendations minimal and composable
- Do not redesign the whole UI unless a critical structural issue is found

## Output Format

### Summary
- healthy / warning / critical

### Findings
- category:
- issue:
- impact:
- affected files:
- recommendation:

### Follow-up
- fix task candidate 1
- fix task candidate 2
```

---

## Note

横断レビュー用の固定スレッドは別で作り、初回メッセージには `docs/codex/integration-review-task.md` の内容を使う。
