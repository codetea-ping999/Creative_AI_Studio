# API Contract

Creative AI Studio の現行 API 定義。

Base URL

```text
http://127.0.0.1:8000
```

## GET /health

疎通確認用のヘルスチェック。

Response

```json
{
  "status": "ok"
}
```

## GET /models

有効なモデル manifest から UI 向けのモデル情報を返します。

Query

- `media_type=image|audio|video` は任意

Behavior

- loader や runtime は初期化しません
- 有効な manifest のみ返します
- `is_available` は local path の存在確認を含みます
- Web UI は `media_type=image|audio|video` を使って surface ごとの selector を構成します

Response

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

## GET /catalog/loras

ローカル配置済み LoRA ファイルの一覧を返します。

Behavior

- `LORA_ROOT` があればそれを使います
- なければ `./models/loras` を走査します
- `.safetensors`, `.pt`, `.bin`, `.ckpt` を返します

Response

```json
{
  "root": "/abs/path/to/models/loras",
  "items": [
    {
      "id": "mai_style.safetensors",
      "display_name": "Mai Style",
      "path": "/abs/path/to/models/loras/mai_style.safetensors",
      "relative_path": "models/loras/mai_style.safetensors"
    }
  ]
}
```

## GET /metrics/summary

Studio の成功率、保存成功率、平均品質、media 別のサマリを返します。

Query

- `window_size` は任意。既定値は `20`

Response

```json
{
  "total_jobs": 12,
  "succeeded_jobs": 10,
  "failed_jobs": 2,
  "running_jobs": 0,
  "success_rate": 83.3,
  "save_success_rate": 100.0,
  "average_quality_score": 74.8,
  "average_business_readiness_score": 72.4,
  "latest_quality_level": "strong",
  "recent_window_size": 12,
  "recent_success_rate": 83.3,
  "recent_average_quality_score": 74.8,
  "by_media": {
    "image": {
      "total_jobs": 8,
      "succeeded_jobs": 7,
      "failed_jobs": 1,
      "running_jobs": 0,
      "success_rate": 87.5,
      "save_success_rate": 100.0,
      "average_quality_score": 77.1,
      "average_business_readiness_score": 75.9,
      "latest_quality_level": "strong"
    }
  }
}
```

## POST /jobs

共通の job 作成 endpoint。通常は `/generate/*` の convenience endpoint を利用します。

Request

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

Response

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

## POST /jobs/{job_id}/rerun

既存 job の request を複製して新しい job を作成します。  
prompt / model / params / project を部分的に上書きできます。

Request

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

Response

```json
{
  "job_id": "job_124",
  "status": "queued"
}
```

## GET /jobs/{job_id}

単一 job の状態と結果を返します。  
Web UI は submit 後にこの endpoint を poll して stage を更新します。

Response

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

## GET /jobs

直近 job の一覧を返します。  
Web UI は session history として利用します。

Response

```json
[
  {
    "id": "job_1",
    "project_id": null,
    "media_type": "audio",
    "status": "succeeded",
    "request": {
      "media_type": "audio",
      "prompt": "dreamy ambient synth loop",
      "negative_prompt": null,
      "model_id": "",
      "seed": 42,
      "output_format": "wav",
      "params": {
        "duration_seconds": 8,
        "bpm": 96,
        "mood": "dreamy"
      }
    },
    "result": {
      "job_id": "job_1",
      "status": "succeeded",
      "outputs": ["outputs/audio/aud_abc.wav"],
      "previews": [],
      "metadata": {
        "stub": false,
        "generator": "AudioGenerator",
        "media_type": "audio",
        "task_type": "text-to-music",
        "quality_report": {
          "method": "heuristic_local_v1",
          "quality_score": 71.2,
          "quality_level": "strong",
          "semantic_report": {
            "status": "disabled",
            "mode": "off"
          }
        }
      },
      "error_message": null
    },
    "progress": 1.0,
    "error_message": null,
    "created_at": "2026-03-15T00:00:00+00:00",
    "updated_at": "2026-03-15T00:00:02+00:00"
  }
]
```

## POST /generate/image

画像生成ジョブを作成します。

Request

```json
{
  "prompt": "futuristic robot",
  "negative_prompt": "low quality, blurry",
  "model_id": "sdxl",
  "project_id": "project_123",
  "output_format": "png",
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 30,
    "guidance_scale": 7.5,
    "lora_path": "./models/loras/mai.safetensors",
    "lora_scale": 0.8
  }
}
```

Response

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

## POST /generate/audio

音楽ループ生成ジョブを作成します。

Request

```json
{
  "prompt": "dreamy ambient synth loop, soft arps, nighttime city",
  "seed": 42,
  "project_id": "project_123",
  "output_format": "wav",
  "params": {
    "duration_seconds": 8,
    "bpm": 96,
    "mood": "dreamy"
  }
}
```

Behavior

- 現在は local MusicGen runtime を使った text-to-music 実装です
- job queue / polling / output routing は image と同じ基盤を使います
- 出力は `outputs/audio/*.wav` に保存されます
- 生成後に `quality_report` が metadata に付与されます
- semantic judge は設定時のみ local transformer model を使って追加採点します

Response

```json
{
  "job_id": "job_456",
  "status": "queued"
}
```

## POST /generate/video

storyboard gif 生成ジョブを作成します。

Request

```json
{
  "prompt": "cinematic aerial shot of tokyo at dusk",
  "negative_prompt": "flat motion, messy composition",
  "model_id": "storyboard-video",
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

Behavior

- 現在は local procedural runtime を使った storyboard 実装です
- 出力は `outputs/videos/*.gif` に保存されます
- 生成後に `quality_report` が metadata に付与されます

Response

```json
{
  "job_id": "job_789",
  "status": "queued"
}
```

## Static Output Routing

FastAPI は `outputs/` を `/outputs` として mount しています。

Examples

```text
/outputs/images/img_abc.png
/outputs/audio/aud_abc.wav
/outputs/videos/vid_abc.gif
```

## Error Format

```json
{
  "detail": "message"
}
```

## Notes

- API は job を作成して queue に入れるだけで、生成処理は request thread で同期実行しません
- image / audio / video は同一の job lifecycle を共有します
- quality score は technical proxy であり semantic fidelity は評価しません
- 現在の `POST /generate/video` は procedural storyboard gif runtime を利用します
