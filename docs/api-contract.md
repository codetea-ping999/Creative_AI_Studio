# API Contract

Creative AI Studio の現行フロントエンド連携契約です。  
この文書は `apps/api/routes/` の実装と `apps/web/src/studioClient.ts` の利用経路に合わせて整理しています。

## Base URL

```text
http://127.0.0.1:8000
```

## Frontend-Facing Endpoints

| 分類 | メソッド | パス | Web UI の主用途 |
| --- | --- | --- | --- |
| System | `GET` | `/health` | dev stack 疎通確認 |
| System | `GET` | `/openapi.json` | 開発時の契約確認 |
| Models | `GET` | `/models` | メディア別モデル一覧 |
| Catalog | `GET` | `/catalog/loras` | LoRA 候補一覧 |
| Projects | `GET` | `/projects` | プロジェクト一覧 / filter |
| Projects | `POST` | `/projects` | プロジェクト作成 |
| Projects | `GET` | `/projects/{project_id}` | プロジェクト詳細 |
| Projects | `PATCH` | `/projects/{project_id}` | プロジェクト更新 |
| Projects | `GET` | `/projects/{project_id}/jobs` | Studio 中央の project context |
| Gallery | `GET` | `/gallery` | 素材一覧 |
| Gallery | `GET` | `/gallery/job/{job_id}` | 最新 job から素材 detail を引く |
| Gallery | `GET` | `/gallery/{asset_id}` | 右インスペクタ用素材 detail |
| Gallery | `POST` | `/gallery/{asset_id}/reuse` | 派生生成 |
| Gallery | `POST` | `/gallery/{asset_id}/export` | 素材 export |
| Gallery | `PATCH` | `/gallery/{asset_id}/project` | 素材と source job の project 再割当て |
| Jobs | `GET` | `/jobs/{job_id}` | polling / 最新状態取得 |
| Jobs | `POST` | `/jobs/{job_id}/cancel` | queued / running job の cancel 要求 |
| Generate | `POST` | `/generate/image` | 画像生成開始 |
| Generate | `POST` | `/generate/audio` | 音声生成開始 |
| Generate | `POST` | `/generate/video` | 動画生成開始 |
| Metrics | `GET` | `/metrics/summary` | Studio 全体の運用サマリ |
| Metrics | `GET` | `/metrics/calibration` | 自動scoreとhuman feedbackの相関レポート |
| Feedback | `POST` | `/feedback` | 人手評価保存 |
| Feedback | `GET` | `/feedback` | 一覧取得 |
| Feedback | `GET` | `/feedback/summary` | 評価集計取得 |

## Common Behavior

- 生成単位は `Job`。
- `/generate/*` は UI 用の media-specific endpoint。
- 成功 job は `Asset` として `gallery` から参照される。
- 出力ファイル本体は `/outputs/*` で静的配信される。
- datetime は FastAPI / Pydantic の ISO 8601 JSON 形式。

## Error Responses

### 404

存在しない `job_id` / `project_id` / `asset_id` などを指定した場合、現行実装は FastAPI の標準 `detail` 形式を返します。

```json
{
  "detail": "Project not found"
}
```

補足:

- `detail` は route ごとに `"Job not found"`、`"Gallery asset not found"` など文字列が異なります。
- dev stack 検証では、この 404 が出る場合に「別プロセスを見ている」のか「契約が壊れた」のかをログで判別できるようにしています。

### 422

入力不足や型不一致は FastAPI / Pydantic の標準 validation error を返します。

```json
{
  "detail": [
    {
      "loc": ["body", "prompt"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

Web 側は `detail` 配列から `body.prompt: Field required` のような構造化エラーメッセージを組み立てます。

## Shared Types

### GenerationRequest Snapshot

`Job.request` と `GalleryAssetDetailResponse.request_snapshot` の共通形です。

```json
{
  "media_type": "image",
  "prompt": "editorial portrait",
  "negative_prompt": "blurry",
  "model_id": "sdxl",
  "seed": 42,
  "output_format": "png",
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 30,
    "variation_count": 1
  }
}
```

Image の `variation_count` は `1..4` の整数で、既定値は `1` です。複数生成では
request の `seed`（省略時は生成時に確定して result へ保存）を base seed とし、
variation index ごとに `base_seed + index` を使います。

### JobResponse

`GET /jobs/{job_id}` の返却形です。

```json
{
  "id": "job_123",
  "media_type": "video",
  "project_id": "project_123",
  "status": "running",
  "progress": 0.5,
  "error_message": null,
  "request": {},
  "result": null,
  "created_at": "2026-04-19T09:00:00Z",
  "updated_at": "2026-04-19T09:00:03Z"
}
```

状態は次を取ります。

```text
queued | preparing | running | postprocessing | succeeded | failed | cancelled
```

### POST /jobs/{job_id}/cancel

queued / running job を `cancelled` に更新します。queued job は実行されません。
Diffusers image job は step callback で進捗を更新し、推論途中の cancel を協調的に
検知します。その他の generator は現在、生成処理の前後の境界で cancel を検知します。
すでに terminal status の job に対しては現行状態の `JobResponse` を返します。

Response は `JobResponse`。

## Models

### GET /models

メディア別の enabled manifest を UI 向けに返します。

Query:

- `media_type=image|audio|video` は任意

Behavior:

- runtime 自体は load しません
- `enabled=true` の manifest のみ返します
- `is_available` は loader が実際に開くファイル一式を見ます。判定は `core/model_readiness.py` が一元管理し、loader・`scripts/check_local_setup.py`・`make cogvideox-smoke` と同じルールです
- diffusers runtime は `model_index.json` に記載された component ごとに設定ファイル、tokenizer語彙、完全な weight set (`*.safetensors` / `*.bin`) が必要です。shardはindexの参照先または連番がすべて揃うまでreadyになりません
- transformers runtime は `config.json`、完全なweight set、processor設定、tokenizer設定と語彙が必要です
- learned runtime はadapter entrypoint、`pipeline_path` の component設定、weight一式が揃った場合だけ`is_available=true`です
- `runtime_status` は `ready | missing_files | scaffold` のいずれかです
- `availability_message` はUIへ表示可能なローカルfile readiness理由で、不足時は不足ファイルを列挙します
- FLUX family の生成結果は `metadata.negative_prompt_applied=false` を返し、
  入力された negative prompt が推論には使われなかったことを示します

完全レスポンス:

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
        "steps": 30
      },
      "tags": ["image", "base"],
      "is_default": true,
      "is_available": true,
      "runtime_status": "ready",
      "availability_message": "Diffusers model files are ready."
    }
  ]
}
```

UI の最小利用項目:

- `id`
- `display_name`
- `default_params`
- `tags`
- `is_available`
- `is_default`
- `runtime_status`
- `availability_message`

## Projects

### GET /projects

一覧取得です。

Query:

- `q`: 部分一致検索
- `status`: status filter
- `tag`: tag filter

完全レスポンス要素:

```json
{
  "id": "project_123",
  "name": "Spring Launch",
  "description": "campaign motion work",
  "status": "active",
  "tags": ["campaign", "spring"],
  "metadata": {
    "client": "internal"
  },
  "pinned_asset_ids": [],
  "created_at": "2026-04-19T09:00:00Z",
  "updated_at": "2026-04-19T09:00:00Z",
  "job_ids": [],
  "job_count": 0,
  "asset_count": 0,
  "cover_asset_path": null
}
```

### POST /projects

Request:

```json
{
  "name": "Spring Launch",
  "description": "campaign motion work",
  "status": "active",
  "tags": ["campaign", "spring"],
  "metadata": {
    "client": "internal"
  }
}
```

Response は `ProjectResponse`。

### PATCH /projects/{project_id}

差分更新です。  
`name` / `description` / `status` / `tags` / `metadata` / `pinned_asset_ids` を更新できます。

### GET /projects/{project_id}/jobs

Studio 中央面で使う project context です。

完全レスポンス:

```json
{
  "project": {},
  "jobs": [],
  "assets": [],
  "job_count": 0,
  "asset_count": 0,
  "media_breakdown": {
    "image": 1,
    "video": 2
  },
  "average_quality_score": 74.3,
  "average_creative_alignment_score": 69.1
}
```

UI の最小利用項目:

- `project`
- `jobs`
- `assets`
- `job_count`
- `asset_count`
- `media_breakdown`
- `average_quality_score`
- `average_creative_alignment_score`

## Gallery

### GET /gallery

一覧取得です。

Query:

- `media_type`
- `project_id`
- `q`
- `limit` 既定 `50`

一覧要素:

```json
{
  "asset_id": "asset_123",
  "job_id": "job_123",
  "project_id": "project_123",
  "project_name": "Spring Launch",
  "media_type": "image",
  "prompt": "editorial portrait",
  "model_id": "sdxl",
  "output_path": "/abs/path/to/outputs/images/sample.png",
  "preview_path": "/abs/path/to/outputs/images/sample.png",
  "created_at": "2026-04-19T09:00:00Z",
  "updated_at": "2026-04-19T09:00:00Z",
  "quality_score": 73.0,
  "quality_level": "good",
  "semantic_alignment_score": null,
  "creative_alignment_score": 61.0,
  "quality_score_calibrated": 79.0,
  "semantic_alignment_score_calibrated": null,
  "creative_alignment_score_calibrated": 64.0,
  "feedback_count": 1,
  "average_feedback_quality": 4.0,
  "reuse_count": 0,
  "export_count": 1,
  "success": true
}
```

### GET /gallery/job/{job_id}

最新 job から asset detail を引く用途です。  
返却形は `GET /gallery/{asset_id}` と同じ `GalleryAssetDetailResponse`。

### GET /gallery/{asset_id}

右インスペクタで使う detail です。

追加項目:

```json
{
  "quality_report": {},
  "request_snapshot": {},
  "metadata": {},
  "feedback_summary": {},
  "export_paths": [],
  "parent_asset_id": null,
  "lineage": [],
  "tags": []
}
```

### POST /gallery/{asset_id}/reuse

派生生成を開始します。

`action` は `"rerun"`（既定）または `"variation"` を指定します。`variation` は選択した
asset 固有の seed と実効パラメータを引き継ぎます。複数生成の各 asset は
`variation_count=1` の request snapshot を持つため、選択した1枚だけを再利用できます。
`rerun` で `seed` を省略するか `null` にすると、新しいランダム seed で同じ request を
再実行します。レビュー画面からの派生理由など、UI 固有の補足情報は `params` に任意の
JSON 値として保存できます。

Request:

```json
{
  "action": "variation",
  "prompt": "refine this look",
  "negative_prompt": null,
  "model_id": "sdxl",
  "seed": null,
  "output_format": "png",
  "project_id": "project_123",
  "params": {
    "width": 1024,
    "height": 1024,
    "review_issue_tags": ["color_lighting"],
    "review_source": "quick-review"
  }
}
```

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

Response:

```json
{
  "asset_id": "asset_123",
  "export_path": "/abs/path/to/exports/image/sample.png",
  "metadata_path": "/abs/path/to/exports/image/sample.json"
}
```

### PATCH /gallery/{asset_id}/project

Request:

```json
{
  "project_id": "project_123"
}
```

Response は再バインド後の `GalleryAssetDetailResponse`。

## Generate

### POST /generate/image

```json
{
  "prompt": "editorial portrait",
  "negative_prompt": "blurry",
  "model_id": "sdxl",
  "seed": 42,
  "project_id": "project_123",
  "output_format": "png",
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 30,
    "guidance_scale": 7.5,
    "variation_count": 4
  }
}
```

`variation_count > 1` は逐次生成され、job progress は全 variation を通した `0..1` として
集約されます。途中で1件でも失敗または cancel された場合は、その job で先に保存した
output も削除し、部分成功の asset は作成しません。

### POST /generate/audio

```json
{
  "prompt": "dreamy synth loop",
  "model_id": "musicgen-small",
  "seed": 42,
  "project_id": "project_123",
  "output_format": "wav",
  "params": {
    "duration_seconds": 8,
    "guidance_scale": 3,
    "bpm": 96,
    "mood": "dreamy"
  }
}
```

### POST /generate/video

```json
{
  "prompt": "cinematic storyboard",
  "negative_prompt": "blurry motion",
  "model_id": "storyboard-video",
  "seed": 42,
  "project_id": "project_123",
  "output_format": "gif",
  "params": {
    "width": 576,
    "height": 320,
    "duration_seconds": 4,
    "camera_motion": "push-in",
    "visual_style": "storyboard"
  }
}
```

CogVideoX-2B learned runtimeは`model_id=learned-video`、`output_format=mp4`を使います。
既定値は720x480、49 frames、8 fps、20 inference stepsです。weight未配置時は
`GET /models`が`is_available=false`を返し、procedural runtimeへ自動fallbackしません。

共通 response:

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

## Metrics

### GET /metrics/summary

運用サマリです。  
`window_size` query の既定値は `20`。

完全レスポンス:

```json
{
  "total_jobs": 12,
  "succeeded_jobs": 10,
  "failed_jobs": 1,
  "running_jobs": 1,
  "success_rate": 83.3,
  "save_success_rate": 100.0,
  "average_quality_score": 74.2,
  "average_quality_score_calibrated": 76.8,
  "average_business_readiness_score": 71.1,
  "average_semantic_alignment_score": null,
  "average_semantic_alignment_score_calibrated": null,
  "average_creative_alignment_score": 66.5,
  "average_creative_alignment_score_calibrated": 69.2,
  "latest_quality_level": "good",
  "semantic_scored_jobs": 0,
  "semantic_unavailable_jobs": 0,
  "recent_window_size": 12,
  "recent_success_rate": 83.3,
  "recent_average_quality_score": 74.2,
  "feedback_total": 4,
  "feedback_coverage_rate": 33.3,
  "average_human_quality_rating": 4.0,
  "average_human_semantic_rating": null,
  "average_human_creative_rating": 3.5,
  "by_media": {
    "image": {
      "total_jobs": 6,
      "succeeded_jobs": 5,
      "failed_jobs": 1,
      "running_jobs": 0,
      "success_rate": 83.3,
      "save_success_rate": 100.0,
      "average_quality_score": 78.4,
      "average_quality_score_calibrated": 80.1,
      "average_business_readiness_score": 76.2,
      "average_semantic_alignment_score": null,
      "average_semantic_alignment_score_calibrated": null,
      "average_creative_alignment_score": 63.5,
      "average_creative_alignment_score_calibrated": 66.0,
      "latest_quality_level": "good",
      "semantic_scored_jobs": 0,
      "semantic_unavailable_jobs": 0,
      "feedback_total": 3,
      "feedback_coverage_rate": 50.0,
      "average_human_quality_rating": 4.3,
      "average_human_semantic_rating": null,
      "average_human_creative_rating": 3.7
    }
  }
}
```

UI の最小利用項目:

- `total_jobs`
- `succeeded_jobs`
- `failed_jobs`
- `running_jobs`
- `success_rate`
- `average_quality_score`
- `average_quality_score_calibrated`
- `average_semantic_alignment_score`
- `average_semantic_alignment_score_calibrated`
- `average_creative_alignment_score`
- `average_creative_alignment_score_calibrated`
- `feedback_total`
- `feedback_coverage_rate`
- `by_media`

### GET /metrics/calibration

自動quality scoreとhuman feedbackの一致度を返します。queryは任意の
`media_type=image|audio|video`と`model_id`です。レスポンスには全体と
media/model別の`sample_count`、`coverage_rate`、Pearson相関、MAE、mean biasを含みます。

- 全体20件未満、segment 10件未満では`recommendation_status=insufficient_data`
- 十分な件数では`review_recommended`
- `automatic_updates_applied`は常に`false`で、採点重みを自動変更しません

```json
{
  "sample_count": 12,
  "eligible_job_count": 20,
  "coverage_rate": 60.0,
  "minimum_sample_count": 20,
  "recommendation_status": "insufficient_data",
  "metrics": {
    "quality": {
      "paired_count": 12,
      "pearson_correlation": 0.72,
      "mae": 8.4,
      "mean_bias": -2.1
    }
  },
  "segment_minimum_sample_count": 10,
  "by_media": {},
  "by_model": {},
  "automatic_updates_applied": false
}
```

## Feedback

### POST /feedback

Request:

```json
{
  "job_id": "job_123",
  "asset_id": "asset_123",
  "project_id": "project_123",
  "quality_rating": 4,
  "semantic_rating": 5,
  "creative_rating": 3,
  "reuse_intent": true,
  "export_ready": false,
  "issue_tags": ["prompt_mismatch"],
  "comments": "usable but needs refinement",
  "metadata": {
    "semantic_status": "disabled",
    "semantic_backend": null
  }
}
```

Response:

```json
{
  "id": "feedback_123",
  "job_id": "job_123",
  "asset_id": "asset_123",
  "project_id": "project_123",
  "quality_rating": 4,
  "semantic_rating": 5,
  "creative_rating": 3,
  "reuse_intent": true,
  "export_ready": false,
  "issue_tags": ["prompt_mismatch"],
  "comments": "usable but needs refinement",
  "metadata": {},
  "created_at": "2026-04-19T09:00:00Z"
}
```

### GET /feedback

Query:

- `asset_id`
- `project_id`

### GET /feedback/summary

Query:

- `job_id`
- `asset_id`
- `project_id`

Response:

```json
{
  "total_feedback": 1,
  "average_quality_rating": 4.0,
  "average_semantic_rating": 5.0,
  "average_creative_rating": 3.0,
  "comment_count": 1,
  "export_ready_rate": 0.0,
  "reuse_intent_rate": 100.0,
  "issue_tag_counts": {
    "prompt_mismatch": 1
  },
  "human_quality_score": 80.0,
  "human_semantic_alignment_score": 100.0,
  "human_creative_alignment_score": 60.0,
  "latest_feedback_at": "2026-04-19T09:00:00Z"
}
```
