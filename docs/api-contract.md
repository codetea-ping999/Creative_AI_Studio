# API Contract

Creative AI Studio の現行 API 定義です。
このドキュメントは `apps/api/routes/` の実装に合わせて整理しています。

## Base URL

```text
http://127.0.0.1:8000
```

## 共通ルール

- 生成の実行単位はすべて `Job` です
- `POST /generate/*` は media type ごとの便利 endpoint です
- `POST /jobs` は media 共通の低レベル入口です
- 成功した job は `Asset` として `gallery` から参照・再利用できます
- 出力ファイル本体は `/outputs/*` で静的配信されます

## エンドポイント一覧

| 分類 | メソッド | パス | 用途 |
| --- | --- | --- | --- |
| System | `GET` | `/health` | API 生存確認 |
| Models | `GET` | `/models` | 利用可能モデル一覧 |
| Catalog | `GET` | `/catalog/loras` | LoRA 一覧 |
| Metrics | `GET` | `/metrics/summary` | 成功率、品質、feedback 集計 |
| Jobs | `POST` | `/jobs` | 共通 job 作成 |
| Jobs | `GET` | `/jobs` | job 一覧 |
| Jobs | `GET` | `/jobs/{job_id}` | job 詳細 |
| Jobs | `POST` | `/jobs/{job_id}/rerun` | 既存 job の再実行 |
| Generate | `POST` | `/generate/image` | 画像生成 job 作成 |
| Generate | `POST` | `/generate/audio` | 音声生成 job 作成 |
| Generate | `POST` | `/generate/video` | 動画生成 job 作成 |
| Gallery | `GET` | `/gallery` | asset 一覧 |
| Gallery | `GET` | `/gallery/stats` | gallery 集計 |
| Gallery | `GET` | `/gallery/job/{job_id}` | job から asset 詳細取得 |
| Gallery | `GET` | `/gallery/{asset_id}` | asset 詳細取得 |
| Gallery | `POST` | `/gallery/{asset_id}/reuse` | asset を元に新しい job を作成 |
| Gallery | `POST` | `/gallery/{asset_id}/export` | asset を export |
| Gallery | `PATCH` | `/gallery/{asset_id}/project` | asset と source job を project に bind |
| Projects | `POST` | `/projects` | project 作成 |
| Projects | `GET` | `/projects` | project 一覧 |
| Projects | `GET` | `/projects/{project_id}` | project 詳細 |
| Projects | `GET` | `/projects/{project_id}/assets` | project 配下 asset 一覧 |
| Projects | `GET` | `/projects/{project_id}/jobs` | project 配下 job と asset のサマリ |
| Projects | `PATCH` | `/projects/{project_id}` | project 更新 |
| Projects | `POST` | `/projects/{project_id}/jobs/{job_id}` | job を project に追加 |
| Projects | `DELETE` | `/projects/{project_id}/jobs/{job_id}` | job を project から外す |
| Projects | `POST` | `/projects/{project_id}/assets/{asset_id}` | asset 起点で project に追加 |
| Projects | `POST` | `/projects/{project_id}/export` | project bundle を export |
| Projects | `DELETE` | `/projects/{project_id}` | project 削除 |
| Feedback | `POST` | `/feedback` | feedback 作成 |
| Feedback | `GET` | `/feedback` | feedback 一覧 |
| Feedback | `GET` | `/feedback/job/{job_id}` | job 単位 feedback |
| Feedback | `GET` | `/feedback/asset/{asset_id}` | asset 単位 feedback |
| Feedback | `GET` | `/feedback/summary` | feedback 集計 |
| Feedback | `DELETE` | `/feedback/{feedback_id}` | feedback 削除 |

## 共通スキーマ

### GenerationRequest

`POST /jobs` の基準になる共通入力です。

```json
{
  "media_type": "image",
  "prompt": "editorial portrait, rim light",
  "negative_prompt": "blurry, low quality",
  "model_id": "sdxl",
  "seed": 42,
  "output_format": "png",
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 30
  }
}
```

主なフィールド:

- `media_type`: `image | audio | video`
- `prompt`: 必須
- `negative_prompt`: image / video で主に利用
- `model_id`: `GET /models` の public id
- `seed`: 任意
- `output_format`: 任意
- `params`: media 固有パラメータ

### JobRecord

`GET /jobs` と `GET /jobs/{job_id}` で返る基本単位です。

主なフィールド:

- `id`
- `project_id`
- `media_type`
- `status`
- `request`
- `result`
- `progress`
- `error_message`
- `created_at`
- `updated_at`

状態は次を取ります。

```text
queued
preparing
running
postprocessing
succeeded
failed
cancelled
```

## System / Models / Catalog / Metrics

### GET /health

疎通確認用です。

```json
{
  "status": "ok"
}
```

### GET /models

有効な model manifest を UI 向けに整形して返します。

Query:

- `media_type=image|audio|video` は任意

Behavior:

- runtime はロードしません
- `enabled=true` の manifest のみ返します
- `is_available` は local path の存在と runtime ごとの最低限ファイルを見ます

```json
{
  "models": [
    {
      "id": "sdxl",
      "internal_id": "sdxl-local",
      "display_name": "SDXL Local",
      "media_type": "image",
      "task_type": "text-to-image",
      "provider": "local",
      "default_params": {
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "guidance_scale": 7.5
      },
      "tags": ["image", "base"],
      "is_default": true,
      "is_available": true
    }
  ]
}
```

### GET /catalog/loras

ローカル配置済み LoRA ファイルを返します。

Behavior:

- `LORA_ROOT` があればそれを優先します
- なければ `./models/loras` を走査します
- 拡張子は `.safetensors`, `.pt`, `.bin`, `.ckpt` を対象にします

```json
{
  "root": "/abs/path/to/models/loras",
  "items": [
    {
      "id": "models/loras/mai_style.safetensors",
      "display_name": "Mai Style",
      "path": "/abs/path/to/models/loras/mai_style.safetensors",
      "relative_path": "models/loras/mai_style.safetensors"
    }
  ]
}
```

### GET /metrics/summary

job 成功率、保存成功率、品質スコア、semantic score、feedback 集計を返します。

Query:

- `window_size`: 任意、既定値 `20`

主なフィールド:

- `total_jobs`
- `succeeded_jobs`
- `failed_jobs`
- `running_jobs`
- `success_rate`
- `save_success_rate`
- `average_quality_score`
- `average_business_readiness_score`
- `average_semantic_alignment_score`
- `average_creative_alignment_score`
- `feedback_total`
- `feedback_coverage_rate`
- `by_media`

## Jobs

### POST /jobs

共通 job 作成 endpoint です。
通常の UI では `/generate/*` の方を使います。

Request:

```json
{
  "media_type": "image",
  "prompt": "futuristic city",
  "model_id": "sdxl",
  "params": {
    "width": 1024,
    "height": 1024,
    "guidance_scale": 6.5
  }
}
```

Response:

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

### GET /jobs

job 一覧です。
現在は `created_at DESC` で返ります。

### GET /jobs/{job_id}

単一 job の状態と結果を返します。  
Web UI は submit 後、この endpoint を poll します。

```json
{
  "id": "job_123",
  "project_id": null,
  "media_type": "image",
  "status": "running",
  "request": {
    "media_type": "image",
    "prompt": "futuristic city",
    "negative_prompt": null,
    "model_id": "sdxl",
    "seed": null,
    "output_format": "png",
    "params": {
      "width": 1024,
      "height": 1024
    }
  },
  "result": null,
  "progress": 0.42,
  "error_message": null,
  "created_at": "2026-03-15T00:00:00+00:00",
  "updated_at": "2026-03-15T00:00:01+00:00"
}
```

### POST /jobs/{job_id}/rerun

既存 job の request を複製し、新しい job を作成します。

上書き可能:

- `prompt`
- `negative_prompt`
- `model_id`
- `seed`
- `output_format`
- `project_id`
- `params`

```json
{
  "prompt": "foggy harbor storyboard, sunrise variation",
  "project_id": "project_456",
  "params": {
    "duration_seconds": 3,
    "visual_style": "animatic"
  }
}
```

## Generate

`/generate/*` は media type ごとの入力差分を吸収する便利 endpoint です。

### POST /generate/image

主なフィールド:

- `prompt`
- `negative_prompt`
- `model_id`
- `seed`
- `output_format`
- `project_id`
- `params.width`
- `params.height`
- `params.steps`
- `params.guidance_scale`

```json
{
  "prompt": "editorial portrait, dramatic rim light",
  "model_id": "sdxl",
  "negative_prompt": "blurry, low quality",
  "seed": 42,
  "output_format": "png",
  "project_id": null,
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 30,
    "guidance_scale": 7.5
  }
}
```

### POST /generate/audio

主なフィールド:

- `prompt`
- `model_id`
- `seed`
- `output_format`
- `project_id`
- `params.duration_seconds`
- `params.bpm`
- `params.mood`

```json
{
  "prompt": "dreamy ambient synth loop",
  "model_id": "musicgen-small",
  "seed": 42,
  "output_format": "wav",
  "project_id": null,
  "params": {
    "duration_seconds": 8,
    "bpm": 96,
    "mood": "dreamy"
  }
}
```

### POST /generate/video

主なフィールド:

- `prompt`
- `negative_prompt`
- `model_id`
- `seed`
- `output_format`
- `project_id`
- `params.duration_seconds`
- `params.camera_motion`
- `params.visual_style`

```json
{
  "prompt": "cinematic storyboard, neon city drive",
  "negative_prompt": "flat composition",
  "model_id": "storyboard-video",
  "seed": 7,
  "output_format": "gif",
  "project_id": null,
  "params": {
    "duration_seconds": 4,
    "camera_motion": "push-in",
    "visual_style": "storyboard"
  }
}
```

いずれもレスポンスは共通です。

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

## Gallery

gallery は成功済み job から同期された asset を扱います。

### GET /gallery

一覧取得です。

Query:

- `media_type`: 任意
- `project_id`: 任意
- `q`: タイトル、prompt、model id、path、metadata に対する簡易全文検索
- `limit`: 既定値 `50`, 最大 `200`

主なレスポンスフィールド:

- `asset_id`
- `job_id`
- `project_id`
- `project_name`
- `media_type`
- `prompt`
- `model_id`
- `output_path`
- `preview_path`
- `quality_score`
- `feedback_count`
- `reuse_count`
- `export_count`

### GET /gallery/stats

gallery 全体の集計を返します。

```json
{
  "total_items": 12,
  "total_by_media_type": {
    "image": 8,
    "audio": 2,
    "video": 2
  },
  "total_by_project": {
    "unassigned": 4,
    "project_abc": 8
  },
  "average_quality_score": 76.4,
  "total_reuse_count": 5,
  "total_export_count": 3
}
```

### GET /gallery/job/{job_id}

source job から primary asset を引いて詳細を返します。

### GET /gallery/{asset_id}

個別 asset の詳細を返します。

追加で含まれる主なフィールド:

- `quality_report`
- `request_snapshot`
- `metadata`
- `feedback_summary`
- `export_paths`
- `parent_asset_id`
- `lineage`
- `tags`

### POST /gallery/{asset_id}/reuse

asset を元に新しい job を作成します。

Request:

```json
{
  "action": "rerun",
  "prompt": "same composition, stronger backlight",
  "project_id": "project_123",
  "params": {
    "strength": 0.65
  }
}
```

Behavior:

- source job の request を土台にします
- `params` に `source_asset_id`, `source_job_id`, `reference_asset_path`, `reuse_action` を注入します
- reuse 元 asset の `reuse_count` を更新します

Response:

```json
{
  "asset_id": "asset_123",
  "job_id": "job_456",
  "status": "queued",
  "project_id": "project_123"
}
```

### POST /gallery/{asset_id}/export

asset ファイルを export します。

Request:

```json
{
  "destination_dir": "/tmp/creative-ai-exports",
  "destination_name": "hero-visual",
  "include_metadata": true
}
```

既定では `outputs/exports/{media_type}` 配下へ出力します。

### PATCH /gallery/{asset_id}/project

asset と、その source job に紐づく asset 群を project に bind します。

Request:

```json
{
  "project_id": "project_123"
}
```

`project_id: null` を送ると unbind できます。

## Projects

project は job と asset の grouping 単位です。

### POST /projects

Request:

```json
{
  "name": "Spring Campaign",
  "description": "KV, music loop, storyboard",
  "status": "active",
  "tags": ["campaign", "spring"],
  "metadata": {
    "client": "internal"
  }
}
```

### GET /projects

一覧取得です。

Query:

- `q`: 名前、説明、tag、metadata への簡易検索
- `status`: status 絞り込み
- `tag`: tag 絞り込み

### GET /projects/{project_id}

project 単体の基本情報です。

主なフィールド:

- `id`
- `name`
- `description`
- `status`
- `tags`
- `metadata`
- `pinned_asset_ids`
- `job_ids`
- `job_count`
- `asset_count`
- `cover_asset_path`

### GET /projects/{project_id}/assets

project に属する asset 一覧です。

### GET /projects/{project_id}/jobs

project 本体、job 一覧、asset 一覧、media breakdown、平均 quality をまとめて返します。

### PATCH /projects/{project_id}

更新可能:

- `name`
- `description`
- `status`
- `tags`
- `metadata`
- `pinned_asset_ids`

### POST /projects/{project_id}/jobs/{job_id}

job を project に追加します。

Behavior:

- job が別 project にいた場合は先に外します
- job の `project_id` を更新します
- job 配下の asset も同じ project に bind します

### DELETE /projects/{project_id}/jobs/{job_id}

project から job を外します。

Behavior:

- job の `project_id` を `null` に戻します
- job 配下の asset も unbind します

### POST /projects/{project_id}/assets/{asset_id}

asset 起点で project へ追加します。

Behavior:

- 実際には asset の source job を project に参加させます
- その job に紐づく asset も bind されます

### POST /projects/{project_id}/export

project bundle を export します。

Request:

```json
{
  "destination_dir": "/tmp/project-bundles"
}
```

既定では `outputs/exports/projects/{project_id}` 配下に出力します。

### DELETE /projects/{project_id}

project を削除します。

Behavior:

- job と asset の project binding は解除します
- job 自体や asset 自体は削除しません

## Feedback

feedback は human 評価の保存と集計に使います。

### POST /feedback

Request:

```json
{
  "job_id": "job_123",
  "asset_id": "asset_123",
  "project_id": "project_123",
  "quality_rating": 5,
  "semantic_rating": 4,
  "creative_rating": 4,
  "reuse_intent": true,
  "export_ready": true,
  "issue_tags": ["hands", "contrast"],
  "comments": "ほぼ使えるが、手だけ少し修正したい",
  "metadata": {
    "reviewer": "self"
  }
}
```

制約:

- `quality_rating` は必須、`1..5`
- `semantic_rating` と `creative_rating` は任意、`1..5`
- 指定した `job_id`, `asset_id`, `project_id` は存在確認を行います

### GET /feedback

一覧取得です。

Query:

- `asset_id`: 指定時は asset 単位一覧
- `project_id`: 指定時は project 単位一覧

### GET /feedback/job/{job_id}

job 単位の feedback 一覧です。

### GET /feedback/asset/{asset_id}

asset 単位の feedback 一覧です。

### GET /feedback/summary

feedback 集計を返します。

Query:

- `job_id`
- `asset_id`
- `project_id`

主なレスポンスフィールド:

- `total_feedback`
- `average_quality_rating`
- `average_semantic_rating`
- `average_creative_rating`
- `comment_count`
- `export_ready_rate`
- `reuse_intent_rate`
- `issue_tag_counts`
- `human_quality_score`
- `human_semantic_alignment_score`
- `human_creative_alignment_score`
- `latest_feedback_at`

### DELETE /feedback/{feedback_id}

feedback を削除します。

## Static Outputs

### GET /outputs/*

FastAPI で `outputs/` 直下を static mount しています。

例:

- `/outputs/images/...`
- `/outputs/audio/...`
- `/outputs/videos/...`

gallery や job result の `output_path`, `preview_path` はこの配信を前提に UI で扱います。

## 代表的な利用フロー

### 1. 画像生成

1. `POST /generate/image`
2. `GET /jobs/{job_id}` を polling
3. `GET /gallery/job/{job_id}` で asset 詳細を取得

### 2. asset を再利用して再生成

1. `GET /gallery`
2. `POST /gallery/{asset_id}/reuse`
3. `GET /jobs/{job_id}` を polling

### 3. project にまとめて export

1. `POST /projects`
2. `POST /projects/{project_id}/jobs/{job_id}` または `POST /projects/{project_id}/assets/{asset_id}`
3. `POST /projects/{project_id}/export`
