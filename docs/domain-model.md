# Domain Model

Creative AI Studio のドメインモデル定義。

目的:
- コア概念を固定する
- API / UI / Generator の共通理解を作る
- Codex による実装のブレを防ぐ

---

# Core Entities

## GenerationRequest

生成処理の入力。

```python
GenerationRequest
```

Fields

| Field | Type | Description |
| --- | --- | --- |
| media_type | image \| video \| audio | 生成メディア |
| prompt | string | プロンプト |
| negative_prompt | string | ネガティブプロンプト |
| model_id | string | `GET /models` の `models[].id` に含まれる public model id。alias も resolver が受け付ける |
| seed | int | 乱数 seed |
| output_format | string | 出力形式 |
| params | dict | メディア固有パラメータ |

Example

```json
{
  "media_type": "image",
  "prompt": "cyberpunk city at night",
  "model_id": "sdxl",
  "seed": 42,
  "params": {
    "width": 1024,
    "height": 1024
  }
}
```

---

## GenerationResult

生成処理の結果。

```python
GenerationResult
```

Fields

| Field | Description |
| --- | --- |
| job_id | Job ID |
| status | job status |
| outputs | 出力ファイル |
| previews | preview |
| metadata | generation metadata |
| error_message | error |

---

# Job

生成処理の単位。

すべての生成は Job として扱う。

---

## JobStatus

```text
queued
preparing
running
postprocessing
succeeded
failed
cancelled
```

---

## JobRecord

```python
JobRecord
```

Fields

| Field | Description |
| --- | --- |
| id | job id |
| status | job status |
| request | GenerationRequest |
| result | GenerationResult |
| progress | 0.0 - 1.0 |
| created_at | timestamp |
| updated_at | timestamp |

---

# Model

生成モデル定義。

---

## ModelManifest

```python
ModelManifest
```

Fields

| Field | Description |
| --- | --- |
| id | internal manifest id |
| public_id | public model id returned by `GET /models` |
| aliases | additional compatible ids resolved by the model system |
| media_type | image/video/audio |
| task_type | text-to-image etc |
| provider | local/huggingface |
| local_path | model path |
| loader | python loader |
| dtype | fp16 etc |
| default_params | default inference params |

---

# Project

Creative AI Studio は Project 単位で管理する。

---

## Project

Fields

| Field | Description |
| --- | --- |
| id | project id |
| name | project name |
| description | description |
| created_at | timestamp |
| settings_json | project settings |

---

# Asset

生成素材。

---

## Asset

Fields

| Field | Description |
| --- | --- |
| id | asset id |
| project_id | project id |
| media_type | image/video/audio |
| path | file path |
| kind | input/output/preview |
| metadata_json | metadata |
| created_at | timestamp |

---

# Relationships

```text
Project
 ├ Jobs
 ├ Assets
 └ Outputs

Job
 ├ Request
 └ Result

Result
 └ Assets
```

---

# Design Principles

1. Core はメディア固有処理を持たない
2. Generator は差し替え可能
3. Job はすべての生成の単位
4. Asset は生成素材の統一表現
