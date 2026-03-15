# Integration Review Report Template

統合レビュー結果を記録するときはこのテンプレートを使う。

目的:
- blocker と follow-up を分離する
- アーキテクチャ境界の確認結果を残す
- 未検証パスと依存不足を明示する
- fix task へつながる形で記録する

---

# Review

レビュー名を書く。

例:

`review-003 Job execution pipeline integration review`

---

# Target

対象タスクまたは実装範囲を書く。

例:

`task-030`

---

# Summary

- overall status: `healthy | warning | critical`
- blocker count: `0`
- follow-up count: `0`

---

# Findings

重要度順に書く。問題がなければ `No blocker findings.` あるいは `No follow-up findings.` と明記する。

## Finding 1

- severity: `blocker | follow-up`
- area: `API | JobService | Queue | Runner | Repository | Generator | Bootstrap | Docs | Tests`
- file: `path/to/file.py:line`
- issue:
  何が問題かを簡潔に書く。
- impact:
  バグ、回帰リスク、設計漏れ、境界崩れのどれかを明確に書く。
- recommendation:
  どう直すべきかを書く。

## Finding 2

- severity:
- area:
- file:
- issue:
- impact:
- recommendation:

---

# What Passed

確認できた clean boundary や non-issue を列挙する。

- API does not execute generators directly
- JobService is the orchestration entry point for job creation and status updates
- JobQueue remains FIFO-only and does not know repository or generator internals
- JobRunner resolves generators through GeneratorRegistry and does not load models directly
- JobRepository remains persistence-only
- GeneratorRegistry remains lookup-only
- ImageGenerator keeps generation logic only
- bootstrap creates shared instances once per app startup
- `/generate/image` remains a convenience wrapper over the generic job path
- docs are aligned with the implemented request/response contract

---

# Confirmed Non-Issues

レビュー前に懸念していたが、問題なしと確認できた項目を書く。

- shared object lifetime is sane
- runtime cache is not recreated accidentally
- no duplicate generator selection logic exists across API and runner
- no job orchestration logic leaked into ImageGenerator

---

# Reviewed Areas With No Changes Needed

修正不要と判断した対象を残す。

- core/jobs/statuses.py
- core/jobs/events.py
- generators/registry.py

---

# Residual Risk

未検証パス、依存不足、仮定、将来課題を書く。

- runtime validation not executed because dependencies were missing
- API E2E path still needs validation with FastAPI installed
- in-memory queue is single-process only
- running job hard cancel is not implemented
- retry policy is not implemented

---

# Doc Mismatches

コードが動いていても docs とズレているなら必ず書く。なければ `None.` と書く。

---

# Follow-Up Tasks

必要なら fix task 候補を切る。

- `fix-030-001: ...`
- `fix-030-002: ...`

---

# Final Judgment

- verdict: `pass | pass with follow-ups | fail`
- ready for next step: `yes | no`
- next recommended action:
  例: `Run fix tasks for blocker findings before real text-to-image integration.`
