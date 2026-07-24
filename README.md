# Creative AI Studio

ローカルで動作する Creative AI Studio。  
現在は Image Generation、Storyboard Video Generation、Music Loop Generation / Playback を同じ Studio UI とジョブ基盤で扱います。

## Goals

- ローカルで動作する生成AI Studio を作る
- Image / Video / Audio を後から追加できる構造にする
- モデル差し替え可能な設計にする
- Codex で並行開発しやすい責務分離を行う

## Repository Basics

- Git 管理前提の作業ルートは、この `README.md` があるディレクトリです
- 新規作業は `git clone https://github.com/codetea-ping999/Creative_AI_Studio.git` で取得したリポジトリ直下で行ってください
- `.env`、`venv/`、`data/*.db`、`outputs/`、`apps/web/node_modules/` はローカル専用で Git には含めません
- 実モデル本体は Git 管理対象外です。`models/manifests/` と軽量な補助ファイルだけをリポジトリに含め、重い checkpoint や weight は各開発環境で配置します

## すぐに起動

依存関係と `.env` を準備した後は、次の 1 コマンドで API と Web UI を起動します。

```bash
./scripts/start_studio.sh
```

起動スクリプトはルートの `.env` にある `API_PORT` と `WEB_PORT` をそろえて
利用し、ブラウザを開きます。失敗時は表示された API / Web ログを確認してください。
すでに API または Vite を別の port / API URL で起動している場合は、先にそのプロセスを
停止してから実行してください。起動スクリプトは設定不一致を検出して再起動方法を案内します。

## Current Status

### ✅ 実装済み機能

- **Core Infrastructure**: ジョブキューシステム、イベントバス、モデルレジストリ、ストレージレイヤー
- **API (FastAPI)**: `/health`, `/models`, `/jobs`, `/generate/image`, `/generate/audio`, `/generate/video`, `/gallery`, `/projects`, `/feedback`, `/metrics/summary`, `/metrics/calibration`, `/catalog/loras`
- **Web UI (React + TypeScript)**: Composer / Stage / Session History を持つ Studio UI、image / video / song surface 切り替え、モデル選択ガード、LoRA カタログ選択、品質スコア表示、音楽再生
- **Image Generator**: ローカル SDXL と optional LoRA を使った画像生成
- **Audio Generator**: ローカル MusicGen runtime を使った text-to-music フロー
- **Video Generator**: procedural storyboard GIF と optional なローカル CogVideoX-2B MP4生成
- **Project / Feedback / Gallery**: project grouping、feedback 集計、asset detail、reuse、export、project bind を含む asset workflow
- **Quality Evaluation**: image / audio / video 出力に対するローカル heuristic quality report と運用メトリクス集計
- **Semantic Judge Scaffold**: optional な local CLIP / CLAP による prompt alignment 採点
- **Operational Quality**: `pytest`、`scripts/check_local_setup.py`、GitHub Actions CI による基本検証
- **Studio Asset Actions**: Web UI から asset detail の確認、composer への再投入、reuse rerun、export、project bind が可能
- **Calibration Dataset**: feedbackと自動quality scoreを結合するJSONL/相関レポートをローカル生成可能

### 🚧 進行中 / 計画中機能

- semantic judge を含む品質評価の高度化
- anime checkpoint の実配置とプリセット拡充
- CogVideoX-2B weightの実配置とM1 MaxでのMP4 smoke
- human feedback sample蓄積後のcalibration review

## 企画メモ

- ローカルAIを「プロンプト＋調整」で運用するための実行プラン: [docs/local-ai-creative-studio-plan.md](docs/local-ai-creative-studio-plan.md)

## アーキテクチャ詳細

全体の読み順は [docs/README.md](docs/README.md)、コードの入口は [docs/codebase-guide.md](docs/codebase-guide.md)、モデル解決の仕組みは [docs/model-system.md](docs/model-system.md) を参照してください。

### レイヤー構成

- **Apps**: FastAPI（API）、React（Studio Web UI）
- **Core**: ジョブシステム、モデル管理、スキーマ定義、ストレージ
- **Generators**: メディア固有の生成処理（Image、Video、Audio）
- **Models**: モデル・マニフェスト格納

### データフロー

1. UI/API が生成リクエストを受け取る
2. API が JobService にジョブ作成を依頼
3. JobService がジョブをエンキュー
4. JobRunner が Queue からジョブを取得
5. Generator が実際の生成処理を実行
6. 結果を保存してイベントを発行
7. quality evaluator が技術品質 proxy を採点
8. UI が polling と job history 表示で状態を表示

## Success Metrics

- **Image success rate**: `succeeded / total` を `/metrics/summary` で集計
- **Save success rate**: succeeded job のうち output file が存在する割合を集計
- **Image quality score**: 解像度、露出、コントラスト、ディテール、クリッピングから heuristic 採点
- **Audio quality score**: クリッピング、無音率、音量、ダイナミクス、長さから heuristic 採点
- **Video quality score**: 解像度、フレーム数、明るさ、輪郭量、フレーム差分から heuristic 採点
- **Business readiness score**: 技術品質 proxy を業務利用観点で再重み付けした補助指標
- **Semantic alignment score**: optional な local judge model による prompt alignment 採点

補足:

- semantic fidelity や芸術性は現状の自動判定には含めていません
- 現在の quality score は `heuristic_local_v1` で、技術品質の proxy です
- semantic judge は `QUALITY_ENABLE_SEMANTIC_JUDGE=true` のときだけ動作します

## プロジェクト構造

## Repository Structure

```text
creative-ai-studio/
├─ apps/
│  ├─ api/
│  └─ web/
├─ core/
│  ├─ schemas/
│  ├─ jobs/
│  ├─ models/
│  ├─ storage/
│  ├─ projects/
│  └─ events/
├─ generators/
│  ├─ base.py
│  ├─ image/
│  ├─ video/
│  └─ audio/
├─ models/
├─ outputs/
├─ docs/
│  ├─ api-contract.md
│  ├─ codebase-guide.md
│  ├─ initial_issues.md
│  ├─ local-ai-creative-studio-plan.md
│  ├─ model-download-guide.md
│  ├─ model-system.md
│  ├─ next-tasks.md
│  ├─ setup-guide.md
│  ├─ codex/
│  │  ├─ task-template.md
│  │  ├─ development-board-template.md
│  │  ├─ monitor-thread-template.md
│  │  ├─ integration-review-task.md
│  │  ├─ integration-fix-task.md
│  │  └─ e2e-validation-task.md
│  └─ checklists/
│     ├─ integration-checklist.md
│     ├─ core-review-checklist.md
│     ├─ api-review-checklist.md
│     ├─ generator-review-checklist.md
│     └─ ui-review-checklist.md
├─ scripts/
└─ tests/
```

## ドキュメント

- [Documentation Guide](docs/README.md) - まず読む順番とドキュメントの使い分け
- [Codebase Guide](docs/codebase-guide.md) - コードを勉強しながら読むための入口
- [API Contract](docs/api-contract.md) - APIスペック
- [Setup Guide](docs/setup-guide.md) - セットアップと起動確認
- [Model System](docs/model-system.md) - manifest、resolver、runtime cache の構成
- [Model Download Guide](docs/model-download-guide.md) - モデル配置と manifest 管理
- [Next Tasks](docs/next-tasks.md) - 次に進めるべきタスク
- [Initial Issues](docs/initial_issues.md) - 初期段階での課題

補足:

- `docs/history/` に履歴資料（`README_v0.2.md`、`IMPLEMENTATION_SUMMARY.md`、`REPAIR_COMPLETE.md`、`COMPLETION_CHECKLIST.md`、`LFS_FIX_REPORT.md`）をまとめています
- 現在の構造理解や学習には、まず `docs/README.md` と `docs/codebase-guide.md` から読むのを推奨します

## トラブルシューティング

### Python パッケージのインストール失敗

```bash
# キャッシュをクリアして再試行
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt --no-cache-dir
```

### ポート既に使用中エラー

```bash
# 異なるポートで起動
API_PORT=8001 ./scripts/run_api_dev.sh

# または使用中のプロセスを確認（macOS/Linux）
lsof -i :8000
kill -9 <PID>
```

### Node.js パッケージエラー

```bash
# キャッシュをクリア
cd apps/web
rm -rf node_modules package-lock.json
npm install
```

### モデルが見つからないエラー

```bash
# models/manifests/ ディレクトリにモデルマニフェストが存在することを確認
ls -la models/manifests/

# モデルレジストリのリセット
# (必要に応じて models/manifests/ 内のマニフェストを確認)
```

## セットアップと実行手順

### クイックセットアップ（推奨）

初めて環境を構築する場合は、自動セットアップスクリプトを使用してください：

```bash
# リポジトリのルートディレクトリで実行
chmod +x setup.sh
./setup.sh
```

このスクリプトは以下を自動実行します：
- ✅ Python 仮想環境の作成
- ✅ 依存パッケージのインストール
- ✅ 必要なディレクトリ構造の作成
- ✅ SQLite データベースの初期化
- ✅ Web UI パッケージのインストール
- ✅ 環境設定ファイルの作成
- ✅ image / audio / video 出力ディレクトリの作成

セットアップ後の一括検証は次の 1 コマンドで実行できます。

```bash
./venv/bin/python scripts/verify_local_stack.py --start-api
```

この検証は以下をまとめて実行します。

- `scripts/check_local_setup.py --skip-runtime-files`
- `apps/web` の `npm run build`
- `pytest -q`
- 一時起動した API に対する `/health` と `/models` の smoke check

### 手動セットアップ

スクリプトが使用できない場合は、以下の手順を実行してください：

#### 前提条件

- Python 3.9 以上
- Node.js 18 以上
- npm または yarn

#### ステップ 1: Python 環境の構築

```bash
# リポジトリのルートディレクトリに移動
cd Creative_AI_Studio

# 仮想環境を作成（推奨）
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# または
# venv\Scripts\activate  # Windows

# 依存パッケージをインストール
pip install -r requirements.txt
```

#### ステップ 2: ディレクトリ構造を作成

```bash
# 出力ディレクトリの作成
mkdir -p outputs/images outputs/audio outputs/videos

# データディレクトリの作成
mkdir -p data data/projects data/feedback
```

#### ステップ 3: データベース初期化

```bash
# Python スクリプトでデータベースを初期化
python3 << 'EOF'
from pathlib import Path
from core.storage.repositories.job_repository import JobRepository

db_path = Path("data/jobs.db")
repo = JobRepository(str(db_path))
print(f"Database initialized at: {db_path}")
EOF
```

#### ステップ 4: 環境設定ファイルを作成

```bash
# .env.example から .env を作成
cp .env.example .env

# 必要に応じて .env を編集
# nano .env または vi .env
```

#### ステップ 5: Web UI パッケージをインストール

```bash
cd apps/web
npm install
cd ../..
```

#### ステップ 6: ローカルモデルを配置

モデル本体は Git に含まれていないため、生成機能を使う場合は別途配置が必要です。

- image: `./models/image/sdxl`
- audio: `./models/audio/musicgen-small`
- video: `./models/video/procedural`

詳細は [docs/model-download-guide.md](docs/model-download-guide.md) を参照してください。

## 起動手順

### サーバーの起動

#### 1. API サーバーを起動（ターミナル1）

```bash
# 仮想環境が有効であることを確認
source venv/bin/activate  # 既に有効な場合は不要

# API サーバーをポート 8000 で起動
./scripts/run_api_dev.sh
```

このスクリプトは以下を行います。

- ルートの `.env` を自動で読み込む
- `venv` や `apps/web/node_modules` を監視対象から外し、`watchfiles` の無限リロードを避ける

API が起動し、以下のエンドポイントにアクセスできます：
- **API Docs (Swagger UI)**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health
- **Models List**: http://localhost:8000/models
- **Jobs**: http://localhost:8000/jobs
- **Metrics**: http://localhost:8000/metrics/summary
- **LoRA Catalog**: http://localhost:8000/catalog/loras

#### 2. Web UI を起動（ターミナル2）

```bash
cd apps/web
npm run dev
```

Web UI が起動し、http://localhost:5173 でアクセスできます。
Web UI の接続先変更は [apps/web/.env.example](/Users/toyoharukohyama/Documents/Creative_AI_Studio/apps/web/.env.example) を参考に `apps/web/.env` で行います。

Job runner は API プロセス内で自動起動します。追加のターミナルは不要です。

### サーバーの状態確認

```bash
# API がアクティブか確認
curl http://localhost:8000/health

# 利用可能なモデル一覧を表示
curl http://localhost:8000/models
```

## API 使用例

### ヘルスチェック

```bash
curl http://localhost:8000/health
```

### 利用可能なモデルを取得

```bash
curl http://localhost:8000/models
```

### 画像生成をキューイング

```bash
curl -X POST http://localhost:8000/generate/image \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a cat sitting on a beach",
    "model_id": "sdxl",
    "negative_prompt": "ugly, blurry",
    "seed": 42,
    "output_format": "png",
    "params": {}
  }'
```

レスポンス例：
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

### ジョブの状態確認

```bash
curl http://localhost:8000/jobs/{job_id}
```

## 設定フォルダ・ファイル

### ディレクトリ構造

```
Creative_AI_Studio/
├── venv/                 # Python 仮想環境（自動作成）
├── data/                 # データファイル（自動作成）
│   └── jobs.db          # ジョブデータベース
├── outputs/             # 生成結果（自動作成）
│   └── images/          # 生成画像の保存先
├── node_modules/        # Node 依存（npm install で作成）
├── .env                 # API / bootstrap 用環境変数
├── apps/web/.env        # Web UI 用環境変数
├── .env.example         # 環境変数テンプレート
└── setup.sh            # セットアップスクリプト
```

### 環境変数ファイル

API 用:

```bash
cp .env.example .env
```

Web UI 用:

```bash
cp apps/web/.env.example apps/web/.env
```

主要な設定項目:

- API / bootstrap の設定は `./.env`
- Web UI の設定は `apps/web/.env`
- モデルと runtime の詳細は [docs/model-system.md](/Users/toyoharukohyama/Documents/Creative_AI_Studio/docs/model-system.md) を参照

| 変数名 | デフォルト値 | 説明 |
|--------|-------------|------|
| `API_HOST` | `127.0.0.1` | API バインドアドレス |
| `API_PORT` | `8000` | API ポート番号 |
| `WEB_PORT` | `5173` | Studio Web UI のポート。API の CORS 設定にも使われます。 |
| `DB_PATH` | `./data/jobs.db` | SQLite データベースファイルパス |
| `MODELS_ROOT` | `./models` | モデル関連ファイルのルートディレクトリ |
| `MODELS_MANIFEST_ROOT` | `./models/manifests` | manifest 探索ディレクトリ |
| `OUTPUT_DIR` | `./outputs` | 出力ルートディレクトリ |
| `OUTPUT_IMAGE_DIR` | `./outputs/images` | 画像出力ディレクトリ |
| `LOG_LEVEL` | `INFO` | ログレベル（DEBUG, INFO, WARNING, ERROR） |
| `MAX_CACHED_MODELS` | `1` | runtime cache の最大件数 |

## セットアップ完了の確認

セットアップが完了したら、以下で動作確認をしてください：

### 1. API の稼働確認

```bash
# API の起動
./scripts/run_api_dev.sh

# 別のターミナルでヘルスチェック
curl http://localhost:8000/health
```

期待される応答：
```json
{"status":"ok"}
```

### 2. モデルの確認

```bash
curl http://localhost:8000/models
```

### 3. サンプル画像生成リクエスト

```bash
curl -X POST http://localhost:8000/generate/image \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a beautiful sunset over mountains",
    "model_id": "sdxl",
    "negative_prompt": "ugly, blurry",
    "seed": 42
  }'
```

レスポンス例：
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

### 4. Web UI の動作確認

```bash
# Web UI の起動
cd apps/web
npm run dev

# ブラウザで http://localhost:5173 を開く
```

## MVP チェックリスト

- [x] ジョブキューシステム
- [x] APIエンドポイント（基本）
- [x] Web UI フレームワーク
- [x] Image Generator 実ランタイム統合
- [x] checkpoint 選択 / LoRA 入力 UI
- [x] ジョブランナー実装（API プロセス内で自動起動）
- [x] 履歴・ギャラリー表示
- [ ] Web UI 完全実装

補足:

- 現時点の画像生成はローカル配置した SDXL 系 checkpoint を使います。Apple Silicon では安定性優先で MPS 実行時に `float32` を使います。

## 開発ガイド

### テストの実行

```bash
# 全テストを実行
python -m pytest tests/

# 特定のテストを実行
python -m pytest tests/test_job_pipeline.py -v

# カバレッジ付きで実行
python -m pytest --cov=core tests/
```

### キーとなるコードモジュール

- [core/jobs/service.py](core/jobs/service.py) - ジョブ管理の中核
- [core/models/service.py](core/models/service.py) - モデル管理システム
- [generators/base.py](generators/base.py) - ジェネレータの基盤
- [apps/api/main.py](apps/api/main.py) - API エントリーポイント
- [apps/web/src/App.tsx](apps/web/src/App.tsx) - Web UI メインコンポーネント

### 新しいエンドポイント追加

1. [apps/api/routes/](apps/api/routes/) に新しいルータを追加
2. [apps/api/main.py](apps/api/main.py) に `include_router()` で登録
3. FastAPI Docs (http://localhost:8000/docs) で自動的にドキュメントが生成
