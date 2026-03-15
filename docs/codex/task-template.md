# Codex Task Template

Codex に渡すタスクは必ずこのフォーマットを使用する。

目的:
- 曖昧な指示を防ぐ
- 修正コストを減らす
- 並行開発を可能にする

---

# Task

簡潔なタスク名を書く。

例

Implement JobRepository.

---

# Goal

このタスクの目的を書く。

例

Persist job records in SQLite.

---

# Files

Codex が触るファイルを明示する。

例

```text
core/storage/repositories/job_repository.py
core/schemas/job.py
```

---

# Requirements

必須要件を書く。

例

- support create job
- support get job
- support update job status
- store payload as JSON

---

# Constraints

やってはいけないことを書く。

例

- do not implement API
- do not modify unrelated modules

---

# Acceptance Criteria

完了条件を書く。

例

- repository imports successfully
- create/get/update works
- typing complete
- code follows project structure

---

# Example

```text
Task:
Implement GeneratorRegistry

Goal:
Allow registering generators by media_type.

Files:
core/generators/registry.py

Requirements:
register(generator)
get(media_type)

Constraints:
no runtime logic

Acceptance:
registry returns correct generator
```
