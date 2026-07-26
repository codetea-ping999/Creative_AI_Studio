# Codebase Guide

Creative AI Studio を勉強しながら読むためのコードベース案内です。

このドキュメントの目的は、実装の正しさをすべて説明することではなく、  
「どこから読むと理解しやすいか」と「何を直す時にどこを見るべきか」を明確にすることです。

## 最初に押さえる全体像

このプロジェクトは、ローカル実行の Creative AI Studio です。

- API は FastAPI です
- Web UI は React + Vite です
- 実行単位は `Job` です
- 実際の生成ロジックは media type ごとの `Generator` に分かれています
- 生成結果は `Asset` として再利用可能な形で保存されます

つまり、理解の軸は次の 5 つです。

1. リクエストを受ける場所
2. ジョブを作る場所
3. ジョブを実行する場所
4. 結果を保存する場所
5. UI が結果を読み出す場所

## ディレクトリの見方

| ディレクトリ | 主な役割 | 最初に読むファイル |
| --- | --- | --- |
| `apps/api/` | HTTP API の入口 | `apps/api/main.py` |
| `apps/web/` | Studio UI | `apps/web/src/App.tsx` |
| `bootstrap/` | サービスの組み立て | `bootstrap/factories.py` |
| `core/jobs/` | job 作成、状態更新、runner | `core/jobs/service.py`, `core/jobs/runner.py` |
| `core/schemas/` | 共通スキーマ | `core/schemas/generation.py` |
| `core/models/` | manifest、resolver、runtime cache | `core/models/service.py`, `core/models/registry.py`, `core/models/resolver.py` |
| `core/model_readiness.py` | dependency-freeなweight・tokenizer・processor readiness判定 | `core/model_readiness.py` |
| `core/assets/` | 生成物の保存と再利用 | `core/assets/__init__.py` |
| `core/projects/` | project 単位の grouping | `core/projects/__init__.py` |
| `core/feedback/` | human feedback 保存 | `core/feedback/__init__.py` |
| `core/bible/` | 再利用する作品設定（統一感の単位） | `core/bible/__init__.py` |
| `core/prompting/` | 決定的 prompt 合成と軸カタログ | `core/prompting/composer.py`, `core/prompting/patterns.py` |
| `core/batches/` | 多重生成の軸展開と段階実行 | `core/batches/expansion.py`, `core/batches/service.py` |
| `core/story/` | 物語ドキュメントと timeline 導出 | `core/story/schemas.py`, `core/story/timeline.py` |
| `generators/` | image / audio / video / text の生成処理 | `generators/base.py` と各 generator |
| `generators/common/` | generator 間で共有する解決処理 | `generators/common/prompting.py` |
| `models/manifests/` | 利用可能モデル定義 | 各 JSON manifest |
| `scripts/` | 開発補助と検証 | `scripts/run_api_dev.sh`, `scripts/verify_local_stack.py` |
| `tests/` | API、job pipeline、model system の確認 | `tests/test_job_pipeline.py` など |

## 読む順番

### 1. API の入口を読む

`apps/api/main.py` を見ると、次が分かります。

- FastAPI アプリをどこで作っているか
- job runner がどのタイミングで起動するか
- どの router が登録されているか
- `/outputs` が static mount されていること

ここで「このアプリに何の機能があるか」を先に把握します。

### 2. サービスの組み立てを読む

`bootstrap/factories.py` は依存関係の中心です。

ここで分かること:

- `ModelService`
- `GeneratorRegistry`
- `JobRepository`
- `JobService`
- `JobRunner`
- `AssetRepository`
- `ProjectRepository`
- `FeedbackRepository`

このファイルを読むと、アプリがどう配線されているかが一度に見えます。

### 3. ジョブの流れを読む

`core/jobs/service.py` と `core/jobs/runner.py` が基幹です。

- `JobService.create_job()` が job を作成して queue に入れます
- `JobRunner.run_forever()` が queue を消費します
- `JobRunner.process_job()` が generator を選び、実行し、結果を保存します
- 成功時は `AssetRepository.sync_job()` が呼ばれ、生成物が asset 化されます

この 2 ファイルが分かると、API と generator の責務分離が理解できます。

### 4. 共通スキーマを読む

`core/schemas/generation.py` は共通言語です。

- `GenerationRequest`
- `GenerationResult`
- `MediaType`
- `GenerationStatus`

ルート、ジョブ、ジェネレータがどのデータを受け渡しているかをここで確認します。

### 5. 各 generator を読む

`generators/base.py` で抽象ライフサイクルを確認してから、各 generator に進むのが読みやすいです。

- `generators/image/generator.py`
- `generators/audio/generator.py`
- `generators/video/generator.py`

共通の形は次です。

1. `validate_request`
2. `prepare`
3. `generate`
4. `cleanup`

## 代表的な処理フロー

### 画像生成リクエスト

`POST /generate/image` の流れは次です。

1. `apps/api/routes/generate.py`
   `GenerateImageRequest` を受けて `GenerationRequest` に変換します。

2. `core/jobs/service.py`
   `JobService.create_job()` で job を作成します。

3. `core/jobs/runner.py`
   `JobRunner` が queue から job を取り出します。

4. `generators/registry.py`
   `media_type="image"` から image generator を引きます。

5. `generators/image/generator.py`
   実際の画像生成を行います。

6. `core/assets/__init__.py`
   成功した job から asset を同期します。

7. `apps/web/src/App.tsx`
   `/jobs/{id}` や `/gallery` を読んで UI を更新します。

### Asset の再利用

asset workflow を理解したい場合はこの順番です。

1. `apps/api/routes/gallery.py`
2. `core/assets/__init__.py`
3. `apps/api/routes/projects.py`
4. `apps/api/routes/feedback.py`

ここでは次の概念が重要です。

- gallery は「成功した生成結果の見え方」です
- asset は「再利用・export の単位」です
- project は「job と asset の grouping」です
- feedback は「human 評価の保存と集計」です

## フロントエンドの見方

Web 側は今のところ `apps/web/src/App.tsx` に責務が集まっています。

最初に見るポイント:

- 初期 state
- API fetch 関数
- polling の流れ
- gallery / metrics / project の状態管理

フォーム自体は `apps/web/src/components/PromptForm.tsx` にあります。

この構成なので、

- UI 文言やレイアウト調整は `PromptForm.tsx` と `styles.css`
- API 応答の扱い変更は `App.tsx`

という見分け方ができます。

## データの保存先

| 種類 | 保存先 |
| --- | --- |
| Job | `data/jobs.db` |
| Project | `data/projects/*.json` |
| Feedback | `data/feedback/*.json` |
| Asset | `data/assets/*.json` |
| 出力ファイル | `outputs/images`, `outputs/audio`, `outputs/videos` |
| model manifest | `models/manifests/**/*.json` |

ここは理解しておくと、デバッグ時に「どこを見れば良いか」が明確になります。

## 変更したい内容ごとの入口

### API を追加したい

1. `apps/api/routes/` に route を追加
2. 必要なら `bootstrap/factories.py` の service を使う
3. `apps/api/main.py` に router を登録
4. `tests/` に API テストを追加
5. `docs/api-contract.md` を更新

### 新しい media type を増やしたい

1. `core/schemas/generation.py` の `MediaType`
2. `generators/` に generator 実装
3. `generators/registry.py` と `bootstrap/factories.py`
4. `models/manifests/`
5. `apps/web/src/App.tsx` と `PromptForm.tsx`

### モデル解決を変えたい

1. `core/models/registry.py`
2. `core/models/resolver.py`
3. `core/models/service.py`
4. manifest JSON

### 出力の保存や再利用を変えたい

1. `core/assets/__init__.py`
2. `apps/api/routes/gallery.py`
3. 必要なら `apps/api/routes/projects.py`

## 学習時の注意点

- `Job` と `Asset` を同じものとして読まないこと
- `GET /models` は manifest を読むだけで、推論 runtime を起動しないこと
- `generate/*` と `POST /jobs` は入口が違うだけで、最終的には job 基盤に載ること
- `ProjectRepository` と `FeedbackRepository` は JSON 永続化、`JobRepository` は SQLite 永続化であること

## 次に読むと理解が深まるファイル

- `tests/test_job_pipeline.py`
- `tests/test_api_extensions.py`
- `tests/test_model_system.py`
- `scripts/verify_local_stack.py`

実装だけ読むより、テストと検証スクリプトを合わせて読む方が「何を壊してはいけないか」が見えやすくなります。
