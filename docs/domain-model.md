# Domain Model

Creative AI Studio の主要概念を、実装での役割に沿って整理したドキュメントです。

この文書の目的は、型定義を列挙することではなく、
「何が主語で、何が派生物で、何が集約単位なのか」をはっきりさせることです。

## 最初に押さえる関係

このプロジェクトで混同しやすい概念は次です。

- `Job`: 生成処理の単位
- `GenerationResult`: job の出力
- `Asset`: 成功した出力を再利用可能にした単位
- `Project`: job / asset を束ねる単位
- `Feedback`: job / asset / project に対する人手評価
- `BibleEntry`: 再利用される作品設定（統一感の単位）
- `Batch`: 1 つの意図から派生した複数 job の束（多重生成の単位）
- `StoryDocument`: 構想テキストと scene の構造（物語の単位）
- `Timeline`: 完成動画の組み立て指示（納品物の単位）

つまり、流れは概ねこうです。

```text
GenerationRequest
  -> Job
  -> GenerationResult
  -> Asset
  -> Project / Feedback へ紐づく
```

## 1. GenerationRequest

生成処理の入力です。
route 層から job 基盤、generator まで共通で使います。

主なフィールド:

| Field | Type | 意味 |
| --- | --- | --- |
| `media_type` | `image | audio | video` | 生成対象のメディア種別 |
| `prompt` | `str` | 主プロンプト |
| `negative_prompt` | `str \| null` | 否定プロンプト |
| `model_id` | `str` | public model id |
| `seed` | `int \| null` | 再現性用 seed |
| `output_format` | `str \| null` | `png`, `wav`, `gif` など |
| `params` | `dict[str, Any]` | media 固有パラメータ |

例:

```json
{
  "media_type": "image",
  "prompt": "cyberpunk city at night",
  "negative_prompt": "low quality, blurry",
  "model_id": "sdxl",
  "seed": 42,
  "params": {
    "width": 1024,
    "height": 1024
  }
}
```

## 2. Job

生成処理そのものを表す単位です。
このシステムでは、すべての生成はまず job として扱います。

### Job が持つ責務

- request を保持する
- status を持つ
- progress を持つ
- result をぶら下げる
- project への所属を持つ

### JobStatus

```text
queued
preparing
running
postprocessing
succeeded
failed
cancelled
```

### JobRecord

主なフィールド:

| Field | 意味 |
| --- | --- |
| `id` | job id |
| `project_id` | 所属 project。未所属なら `null` |
| `media_type` | image / audio / video |
| `status` | 現在状態 |
| `request` | `GenerationRequest` |
| `result` | `GenerationResult \| null` |
| `progress` | `0.0 .. 1.0` |
| `error_message` | 失敗時メッセージ |
| `created_at` | 作成時刻 |
| `updated_at` | 更新時刻 |

### Job と API の関係

- `POST /generate/*` は job を作るための便利入口
- `POST /jobs` は job 共通の低レベル入口
- `GET /jobs` と `GET /jobs/{id}` は job の状態確認

## 3. GenerationResult

generator が返す共通出力です。

主なフィールド:

| Field | 意味 |
| --- | --- |
| `job_id` | 対応する job |
| `status` | 実行結果の状態 |
| `outputs` | 出力ファイルパス一覧 |
| `previews` | プレビュー用パス一覧 |
| `metadata` | generator 追加情報 |
| `error_message` | 失敗時詳細 |

### metadata に入る代表例

- generator 名
- media type
- task type
- quality report
- semantic report

`GenerationResult` は generator の戻り値ですが、
永続化されると job の `result` として保持されます。

## 4. Asset

成功した出力を再利用可能にした単位です。

ここがこのリポジトリの重要なポイントです。
job が成功しても、それだけでは gallery や reuse には使いにくいため、
成功済み job から asset を同期して「扱いやすい資産」に変換しています。

### Asset の役割

- gallery 表示の土台
- reuse の元データ
- export 対象
- project bind 対象
- feedback 集計の対象

### 主なフィールド

| Field | 意味 |
| --- | --- |
| `id` | asset id |
| `job_id` | 生成元 job |
| `project_id` | 所属 project |
| `media_type` | image / audio / video |
| `kind` | 現状は主に `output` |
| `title` | prompt 由来の短い表示名 |
| `prompt` | 元 prompt |
| `model_id` | 利用 model |
| `path` | 出力ファイルパス |
| `preview_path` | プレビュー用パス |
| `parent_asset_id` | reuse 元 asset |
| `lineage` | asset 系譜 |
| `export_paths` | export 済みパス |
| `tags` | 任意 tag |
| `metadata` | quality report など追加情報 |

### Job と Asset の違い

| 観点 | Job | Asset |
| --- | --- | --- |
| 主語 | 実行 | 成果物 |
| 作成タイミング | request 受理時 | job 成功時 |
| 失敗状態 | ある | ない |
| gallery 表示 | 間接的 | 直接的 |
| reuse | ベースになりにくい | ベースになる |

## 5. Project

job と asset を束ねる作業単位です。

Project は「生成をどうまとめて扱うか」の単位であり、
推論の実行単位ではありません。

### 主な用途

- まとまりのある制作物を管理する
- job / asset を横断して束ねる
- export bundle の単位にする

### 主なフィールド

| Field | 意味 |
| --- | --- |
| `id` | project id |
| `name` | project 名 |
| `description` | 説明 |
| `status` | `active` などの状態 |
| `tags` | 分類 tag |
| `metadata` | 任意補足情報 |
| `pinned_asset_ids` | 代表 asset |
| `job_ids` | 所属 job 一覧 |
| `created_at` | 作成時刻 |
| `updated_at` | 更新時刻 |

### Project の実装上の特徴

- SQLite ではなく `data/projects/*.json` に保存される
- job を追加すると、関連 asset 側の `project_id` も同期される
- project を削除しても job や asset 自体は削除しない

## 6. Feedback

人手による評価情報です。

### 主な用途

- quality score の補助指標
- semantic / creative の主観評価
- export readiness や reuse intent の判断材料
- issue tag の蓄積

### 主なフィールド

| Field | 意味 |
| --- | --- |
| `id` | feedback id |
| `job_id` | 対象 job |
| `asset_id` | 対象 asset |
| `project_id` | 対象 project |
| `quality_rating` | 必須。1..5 |
| `semantic_rating` | 任意。1..5 |
| `creative_rating` | 任意。1..5 |
| `reuse_intent` | 再利用したいか |
| `export_ready` | そのまま出せるか |
| `issue_tags` | 問題分類 |
| `comments` | 自由記述 |
| `metadata` | 追加情報 |
| `created_at` | 作成時刻 |

### 派生指標

feedback 集計では rating を 100 点換算した次の値も使います。

- `human_quality_score`
- `human_semantic_alignment_score`
- `human_creative_alignment_score`

## 7. ModelManifest

利用可能モデルを宣言する定義です。

### 役割

- generator にモデル実体を直接埋め込まない
- UI に public model id を返す
- internal id と alias を切り分ける

### 主なフィールド

| Field | 意味 |
| --- | --- |
| `id` | internal manifest id |
| `public_id` | API / UI が使う public id |
| `aliases` | 互換 id |
| `media_type` | image / audio / video |
| `task_type` | text-to-image など |
| `provider` | local, huggingface など |
| `runtime` | diffusers, transformers など |
| `local_path` | ローカル配置先 |
| `loader` | 使用 loader |
| `default_params` | 既定推論値 |
| `is_default` | デフォルト候補か |
| `enabled` | 利用可能か |

## 8. GalleryItem

コード上は repository の永続モデルではありませんが、
UI が触る概念として重要なのでここで整理します。

gallery item は実体としては次の合成です。

```text
Asset
+ source Job
+ Project 情報
+ Feedback 集計
+ QualityReport
```

つまり gallery は独立エンティティではなく、asset を見やすくした view model です。

## 関係図

```text
Project
 ├─ Job
 │   ├─ GenerationRequest
 │   └─ GenerationResult
 │        └─ Asset
 │             └─ Feedback
 └─ Asset
```

もう少し実態に寄せると次です。

```text
GenerationRequest
  -> JobRecord
  -> GenerationResult
  -> Asset

Project
  -> many JobRecord
  -> many Asset

Feedback
  -> one JobRecord
  -> optional Asset
  -> optional Project
```

## 9. BibleEntry（作品設定）

統一感の主語です。キャラクター・スタイル・ブランド・場所・小道具を、
prompt に展開できる形で 1 レコードとして保持します。

実装: `core/bible/__init__.py`、永続化先は `data/bible/`。

| Field | 意味 |
| --- | --- |
| `kind` | `character` / `style` / `brand` / `location` / `prop` |
| `prompt_fragment` / `negative_fragment` | prompt へ展開される断片 |
| `attributes` | `hair`、`eyes` などの固定スロット |
| `tokens` | LoRA trigger word など |
| `lora` | `{path, scale}`。pipeline の adapter 枠は 1 つなので先着優先 |
| `reference_asset_ids` | 参照画像（img2img / IP-Adapter 用） |
| `seed_policy` | `{mode: locked \| free, seed}`。locked は request の seed を上書きする |
| `locked_fields` | 軸展開で上書きさせない attribute 名 |

`BibleEntry` は単体では何もしません。`core/prompting/composer.py` の
`PromptComposer` が `PromptSpec -> ComposedPrompt` として展開します。

重要な性質:

- **決定的**: 同じ入力から常に同じ prompt 文字列になる（kind 順 → 参照順で固定）
- **監査可能**: 何がどこから入ったかを `ComposedPrompt.applied` に残し、
  job metadata の `prompt_composition` として保存する
- **壊れても止まらない**: 未知の bible id、seed lock の衝突、LoRA の衝突、
  `locked_fields` の上書き試行は例外ではなく `conflicts` に記録される。
  30 件の batch が 1 件の古い参照で全滅しないための判断である

## 10. Batch（多重生成）

「1 つの意図から派生した複数 job」の主語です。

実装: `core/batches/`、永続化先は `data/batches/`。

```text
BatchSpec（宣言）
  axes: [Axis(name, values: [AxisValue(label, patch)])]
  stages: [Stage(name, param_overrides, keep_top_n)]
  -> expand_items()（純粋関数）
  -> BatchItem[]（それぞれ GenerationRequest を持つ）
  -> JobService.create_job() で子 job 化
  -> BatchRecord（items / aggregate / status）
```

| 概念 | 意味 |
| --- | --- |
| `Axis` | 探索軸。宣言順が最も遅く変化する（結果の読み順が安定する） |
| `AxisValue.patch` | request への deep merge。`prompt_suffix` は追記される |
| `Stage` | 1 パス。`probe` → `refine` の 2 段階が既定 |
| `keep_top_n` | この stage の上位何件を次 stage に送るか |
| `seed_policy` | `shared` / `per_item` / `sweep`（`per_item` は base + index） |

状態の扱い:

- item の状態は **job repository から再導出** される。プロセス再起動後も整合するのはこのため
- `job_succeeded` / `job_failed` / `job_cancelled` を購読して自動で stage を進める
- 一部失敗は `partial`。成功分の比較は続けられる
- 子 job は `model_id` でグルーピングして enqueue する（runtime cache の載せ替え回避）

## 11. StoryDocument（物語）

執筆結果と生成素材を結びつける主語です。

実装: `core/story/`、永続化先は `data/stories/`。

```text
StoryDocument
  logline / premise / genre / tone / audience / language / format / structure
  beats:    [Beat(act, purpose, summary)]
  scenes:   [Scene(heading, narration, image_prompt, bgm_mood,
                   duration_seconds, camera, asset_ids, job_ids)]
  chapters: [Chapter(title, prose_markdown, word_count)]
```

`Scene.asset_ids` が `role -> asset id`（`visual` / `narration` / `music`）を保持し、
「どの scene がどの素材で埋まったか」の唯一の記録になります。

`apply_text_result(story, task, structured)` が text job の出力をマージします。
純粋関数で、入力を壊しません。**scene list を作り直しても、同じ order の scene が
既に持っていた `asset_ids` / `job_ids` は保持されます** — 文章の微修正で
数分ぶんの画像・音声生成を捨てないための判断です。

## 12. Timeline（納品物の組み立て）

`build_timeline(story)` が scene から生成する、assembly 工程の入力です。

```text
{"resolution": [w, h], "fps": n, "total_duration_seconds": t,
 "tracks": {"visual": [...], "narration": [...], "music": [...], "subtitles": [...]}}
```

- narration の `start_seconds` は先行 scene の尺の累積
- 連続する scene が同じ BGM を使う場合は 1 エントリに連結される（cut ごとに曲を再開させない）
- 字幕は日本語を考慮した行分割（CJK は 20 文字、Latin は 42 文字が既定）
- visual が欠けている場合は `ValueError` で scene id を列挙する。
  壊れた動画を出すより、明示的に止める方が診断しやすい

## 設計原則

1. Core はメディア固有処理を持たない
2. Generator は request を受け、result を返す
3. Job は処理の単位、Asset は成果物の単位として分離する
4. Project は grouping、Feedback は評価であり、推論本体とは切り分ける
5. ModelManifest は宣言、runtime 実体化は model system が担う
6. Bible / Batch も宣言。job には「意図」を保存し、解決は生成時に行う
   （bible を直した後の再生成が、古い文字列の再演にならないようにするため）

## 読み進める順番

このドメインモデルをコードに対応づけるなら、次の順で読むと理解しやすいです。

1. `core/schemas/generation.py`
2. `core/jobs/schemas.py`
3. `core/assets/__init__.py`
4. `core/projects/__init__.py`
5. `core/feedback/__init__.py`
6. `apps/api/routes/gallery.py`
7. `core/bible/__init__.py` と `core/prompting/composer.py`
8. `core/batches/schemas.py` と `core/batches/expansion.py`
9. `core/story/schemas.py` と `core/story/timeline.py`
