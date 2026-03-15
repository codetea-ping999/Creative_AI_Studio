# API Updates - v0.2

Creative AI Studio v0.2で追加・変更されたAPI エンドポイント。

## 📌 新規エンドポイント追加

### Gallery API

生成済みの出力を一覧表示し、再利用や体験確認を容易にします。

#### `GET /gallery`

**説明**: ギャラリーアイテム（成功した生成物）の一覧を取得

**Query Parameters**
- `media_type` (string, optional): `image` / `audio` / `video` でフィルタ
- `project_id` (string, optional): project 単位でフィルタ
- `q` (string, optional): prompt / model / path を部分一致検索
- `limit` (integer, optional, default=50, max=200): 返すアイテム数

**Response (200 OK)**
```json
[
  {
    "job_id": "uuid",
    "project_id": "project_uuid",
    "media_type": "image",
    "prompt": "a beautiful landscape",
    "model_id": "sdxl",
    "output_path": "images/uuid.png",
    "preview_path": "images/uuid.png",
    "created_at": "2025-03-15T10:30:00Z",
    "quality_score": 7.8,
    "quality_level": "strong",
    "success": true
  }
]
```

#### `GET /gallery/stats`

**説明**: ギャラリーの統計情報を取得

**Response (200 OK)**
```json
{
  "total_items": 42,
  "total_by_media_type": {
    "image": 28,
    "audio": 10,
    "video": 4
  },
  "total_by_project": {
    "unassigned": 18,
    "project_uuid": 24
  },
  "average_quality_score": 7.65
}
```

### Project API

生成結果をプロジェクト単位でグループ化・管理します。

#### `POST /projects`

**説明**: 新しいプロジェクトを作成

**Request Body**
```json
{
  "name": "My Creative Project",
  "description": "A collection of designs for summer campaign"
}
```

**Response (201 Created)**
```json
{
  "id": "uuid",
  "name": "My Creative Project",
  "description": "A collection of designs for summer campaign",
  "created_at": "2025-03-15T10:30:00Z",
  "updated_at": "2025-03-15T10:30:00Z",
  "job_ids": []
}
```

#### `GET /projects`

**説明**: すべてのプロジェクト一覧を取得

**Response (200 OK)**
```json
[
  {
    "id": "uuid",
    "name": "My Creative Project",
    "description": "...",
    "created_at": "...",
    "updated_at": "...",
    "job_ids": ["job1", "job2", ...]
  }
]
```

#### `GET /projects/{project_id}`

**説明**: 特定のプロジェクトを取得

**Response (200 OK)**: 単一プロジェクト

**Response (404)**: プロジェクトが見つからない

#### `GET /projects/{project_id}/jobs`

**説明**: プロジェクト配下の job 一覧と media breakdown を取得

#### `PATCH /projects/{project_id}`

**説明**: project の name / description を更新

#### `POST /projects/{project_id}/jobs/{job_id}`

**説明**: ジョブをプロジェクトに追加

**Response (200 OK)**: 更新後のプロジェクト

#### `DELETE /projects/{project_id}/jobs/{job_id}`

**説明**: ジョブをプロジェクトから削除

**Response (200 OK)**: 更新後のプロジェクト

#### `DELETE /projects/{project_id}`

**説明**: プロジェクトを削除（ジョブ自体は削除されない）

**Response (204 No Content)**

### Feedback API

ユーザーの評価を保存し、品質スコアの改善に反映させます。

#### `POST /feedback`

**説明**: ジョブに対するフィードバック（評価・コメント）を送信

**Request Body**
```json
{
  "job_id": "uuid",
  "quality_rating": 4,
  "semantic_rating": 5,
  "comments": "Great result, very close to the prompt!"
}
```

**Response (201 Created)**
```json
{
  "id": "feedback_uuid",
  "job_id": "uuid",
  "quality_rating": 4,
  "semantic_rating": 5,
  "comments": "Great result, very close to the prompt!",
  "created_at": "2025-03-15T10:30:00Z"
}
```

- `quality_rating`: 1~5 の整数 (必須)
- `semantic_rating`: 1~5 の整数 (プロンプト忠実度, オプション)
- `comments`: 最大1000文字のテキスト (オプション)

#### `GET /feedback/job/{job_id}`

**説明**: 特定ジョブのすべてのフィードバックを取得

**Response (200 OK)**
```json
[
  {
    "id": "feedback_uuid",
    "job_id": "uuid",
    "quality_rating": 4,
    "semantic_rating": 5,
    "comments": "...",
    "created_at": "..."
  }
]
```

#### `GET /feedback`

**説明**: すべてのフィードバック（分析用）を取得

**Response (200 OK)**: フィードバック配列

#### `GET /feedback/summary`

**説明**: feedback の平均 rating / 件数を取得

**Query Parameters**
- `job_id` (string, optional): 特定 job に限定

#### `DELETE /feedback/{feedback_id}`

**説明**: フィードバックを削除

**Response (204 No Content)**

## 📦 環境変数とディレクトリ

### 新規ディレクトリ

```
data/
  ├─ projects/       # Project JSON files
  └─ feedback/       # Feedback JSON files
models/audio/        # Audio models directory (MusicGen)
outputs/audio/       # Audio output directory
```

### .env の推奨設定

```bash
# Model paths
MODELS_MANIFEST_ROOT=./models/manifests
MODELS_ROOT=./models

# LoRA catalog
LORA_ROOT=./models/loras

# Project/Feedback storage
PROJECTS_DIR=./data/projects
FEEDBACK_DIR=./data/feedback

# Quality evaluation
QUALITY_ENABLE_SEMANTIC_JUDGE=false  # Optional: CLIP/CLAP integration
```

## 🔄 統合の例

### ワークフロー: Create → Gallery → Project → Feedback → Re-run

```
1. POST /generate/image
   → returns job_id

2. GET /gallery?media_type=image&project_id=...&limit=10
   → 最新の成功済み画像を表示

3. POST /projects
   → 新プロジェクト作成

4. POST /generate/video or /generate/audio with project_id
   → 生成時点で project に自動紐付け

5. POST /feedback
   → ユーザーが品質評価を送信

6. GET /feedback/summary?job_id={job_id}
   → ジョブの評価集計を確認

7. POST /jobs/{job_id}/rerun
   → prompt / params を上書きして再実行
```

## 🚧 将来予定

- **Semantic Judge Integration**: `QUALITY_ENABLE_SEMANTIC_JUDGE=true` で CLIP/CLAP による prompt alignment 採点を有効化
- **Gallery Export**: ギャラリーアイテムを一括ダウンロード
- **Learned Video Runtime**: procedural runtime を checkpoint ベース video に置き換える
- **Feedback-driven Refinement**: フィードバックに基づく自動パラメータチューニング
