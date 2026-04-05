# Architecture

Creative AI Studio の実装アーキテクチャを、現行コードに沿って整理したドキュメントです。

この文書は「レイヤーの名前」だけではなく、
どのファイルが何を担っていて、リクエストがどう流れるかを理解するための説明を目的にしています。

## システムの要約

Creative AI Studio は、ローカルで動く単一ユーザー向けの生成スタジオです。

現在は同じ Studio UI と job 基盤の上で、次の 3 系統を扱います。

- image generation
- audio generation
- storyboard video generation

アーキテクチャ上の中心は「すべてを Job として扱うこと」です。
API も UI も generator を直接実行せず、共通の job orchestration を通します。

## 設計上の基本方針

### 1. API は orchestration の入口に徹する

- route は request を受ける
- `GenerationRequest` に正規化する
- `JobService` に job 作成を依頼する

API route が generator 実装を直接知らないようにしています。

### 2. generator は media type ごとの差分だけ持つ

- image
- audio
- video

の違いは generator に閉じ込め、job、storage、metrics などの基盤部分は共有します。

### 3. モデル解決は model system に閉じ込める

- route で manifest を直接読まない
- generator で loader の詳細を持ち込まない
- `ModelService` に依存して runtime を取る

### 4. 成功結果は asset として再利用可能にする

job の完了だけで終わらず、成功した出力は `AssetRepository` に同期されます。
これにより gallery、reuse、export、project bind が共通資産として扱えます。

## レイヤー構成

## 1. Apps Layer

ユーザーとの接点です。

### API

主な入口:

- `apps/api/main.py`
- `apps/api/routes/*`

役割:

- request validation
- service への bridge
- response shaping
- `/outputs` の static mount

### Web UI

主な入口:

- `apps/web/src/App.tsx`
- `apps/web/src/components/PromptForm.tsx`

役割:

- composer 入力
- job polling
- gallery / metrics / project / feedback の表示
- asset reuse や export などの Studio 操作

## 2. Bootstrap Layer

アプリ全体の依存関係を組み立てる層です。

主な入口:

- `bootstrap/factories.py`

ここで次を 1 つの service graph として束ねます。

- `ModelService`
- `GeneratorRegistry`
- `JobRepository`
- `JobQueue`
- `EventBus`
- `JobService`
- `JobRunner`
- `AssetRepository`
- `ProjectRepository`
- `FeedbackRepository`

この層を挟むことで、API 側は「何を使うか」を意識せずに `ApplicationServices` を受け取れます。

## 3. Core Layer

メディア共通の基盤です。

主な責務:

- 共通スキーマ
- job persistence
- queue / runner
- model registry / resolver / cache
- asset persistence
- project / feedback persistence
- quality evaluation と metrics

### Jobs

主なファイル:

- `core/jobs/service.py`
- `core/jobs/runner.py`
- `core/storage/repositories/job_repository.py`

役割:

- job 作成
- status 更新
- queue 消費
- result 永続化

### Models

主なファイル:

- `core/models/registry.py`
- `core/models/resolver.py`
- `core/models/service.py`
- `core/models/cache.py`

役割:

- manifest 読み込み
- public id / alias 解決
- runtime loader 呼び出し
- runtime cache

### Assets / Projects / Feedback

主なファイル:

- `core/assets/__init__.py`
- `core/projects/__init__.py`
- `core/feedback/__init__.py`

役割:

- 生成物の永続化
- project grouping
- feedback 保存と集計

## 4. Generator Layer

メディア固有の処理です。

主なファイル:

- `generators/base.py`
- `generators/image/generator.py`
- `generators/audio/generator.py`
- `generators/video/generator.py`
- `generators/registry.py`

共通ライフサイクル:

1. `validate_request`
2. `prepare`
3. `generate`
4. `cleanup`

`JobRunner` は `media_type` を見て `GeneratorRegistry` から適切な generator を取り出します。

## 5. Runtime / Model Files

ローカルに置く実モデルと manifest 群です。

主な配置先:

- `models/manifests/**`
- `models/image/**`
- `models/audio/**`
- `models/video/**`

manifest は Git 管理対象、実 weight はローカル配置前提です。

## 起動時の流れ

API プロセス起動時の大まかな流れです。

1. `scripts/run_api_dev.sh` が `.env` を読み込む
2. `apps/api/main.py` が `create_app()` を呼ぶ
3. `bootstrap/factories.py` が `ApplicationServices` を組み立てる
4. FastAPI lifespan 内で `JobRunner` 用スレッドを起動する
5. route 群を登録し、`/outputs` を static mount する

ここで重要なのは、job runner が API プロセス内で自動起動する点です。
別プロセスの worker を前提にした構成ではありません。

## 代表的なデータフロー

## 1. 画像生成

1. Web UI または `POST /generate/image` が request を送る
2. route が `GenerationRequest` を組み立てる
3. `JobService.create_job()` が job を作成し queue に積む
4. `JobRunner` が queue から job を取得する
5. `GeneratorRegistry.get("image")` で image generator を解決する
6. generator が `ModelService` 経由で runtime を取得して生成する
7. 出力ファイルを `outputs/images/` に保存する
8. quality report を `GenerationResult.metadata` に格納する
9. job result を永続化する
10. `AssetRepository.sync_job()` が asset を作成または更新する
11. Web UI が `/jobs` や `/gallery` を読み直して表示を更新する

## 2. asset reuse

1. UI が `GET /gallery` で asset 一覧を取得する
2. ユーザーが reuse を選ぶ
3. `POST /gallery/{asset_id}/reuse` を呼ぶ
4. source job の request をベースに新しい `GenerationRequest` を作る
5. `params` に `source_asset_id` など reuse 文脈を注入する
6. 新しい job を作成して queue に積む
7. reuse 元 asset の reuse count を更新する

## 3. project bind

1. project を作る、または既存 project を選ぶ
2. job または asset を project に bind する
3. job の `project_id` と asset の `project_id` を同期して更新する

このため project は job と asset の両方にまたがる grouping 単位です。

## データの置き場所

| 種類 | 保存先 | 補足 |
| --- | --- | --- |
| Job | `data/jobs.db` | SQLite |
| Asset | `data/assets/*.json` | job 成功時に同期 |
| Project | `data/projects/*.json` | JSON 永続化 |
| Feedback | `data/feedback/*.json` | JSON 永続化 |
| 出力画像 | `outputs/images/*` | static mount 対象 |
| 出力音声 | `outputs/audio/*` | static mount 対象 |
| 出力動画 | `outputs/videos/*` | static mount 対象 |

ここで混乱しやすいのは、job と asset が別物だという点です。

- job は「生成処理の単位」
- asset は「成功した生成物の再利用単位」

です。

## ルーティングと責務の切り分け

### generate routes

役割:

- media type ごとの便利 endpoint
- `project_id` を解決
- `GenerationRequest` を組み立てる

### jobs routes

役割:

- 共通 job API
- rerun
- job status 取得

### gallery routes

役割:

- asset 一覧
- asset detail
- reuse
- export
- project bind

### projects routes

役割:

- project CRUD
- job / asset の grouping
- project bundle export

### feedback routes

役割:

- human 評価の保存
- 集計の取得

## モデルシステムの位置づけ

モデルシステムは generator の内部依存を減らすために存在します。

generator が知るべきことは最小限です。

- どの `model_id` を使うか
- runtime が取得できること

逆に、次は model system 側に閉じ込めます。

- manifest 読み込み
- alias 解決
- loader 選択
- runtime cache

この分離によって、新しいモデルや runtime を追加しても route や UI を大きく崩さずに済みます。

## 現在の実装上の特徴

- API と job runner は同一プロセス内です
- `App.tsx` にフロント責務が比較的集約されています
- project / feedback / asset は JSON 永続化です
- job は SQLite 永続化です
- semantic judge は optional で、既定では無効です

## 今読むと理解しやすいファイル順

1. `apps/api/main.py`
2. `bootstrap/factories.py`
3. `core/jobs/service.py`
4. `core/jobs/runner.py`
5. `core/assets/__init__.py`
6. `apps/api/routes/gallery.py`
7. `apps/web/src/App.tsx`

この順で追うと、「API が job を作り、job が asset を生み、その asset を UI が扱う」という幹が見えます。

## 制約

- Local-first
- 単一ユーザー前提
- 長期運用向けの分散ジョブ基盤ではない
- 実モデルファイルは Git 管理対象外
- Apple Silicon ローカル開発をかなり意識した構成
