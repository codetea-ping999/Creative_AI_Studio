# Setup Guide

Creative AI Studio のローカル開発環境セットアップ手順です。

現在はローカル SDXL モデルを使った画像生成と、ローカル MusicGen モデルを使った音楽生成に対応しています。どちらもモデル未配置時は生成に失敗します。

## 前提

- Python 3.9 以上
- Node.js 18 以上
- `npm`
- Git で clone したリポジトリ直下を作業ルートとして使うこと

## Git とローカル生成物の扱い

- 以後のコマンドは `README.md` があるリポジトリ直下で実行します
- `.env`、`apps/web/.env`、`venv/`、`data/*.db`、`outputs/`、`apps/web/node_modules/` はローカル専用です
- `models/` 配下のうち、重い checkpoint や weight は Git 管理対象外です
- リポジトリには manifest と軽量な補助ファイルのみを保持し、実モデル本体は各環境でダウンロードしてください

## 最短セットアップ

```bash
git clone https://github.com/codetea-ping999/Creative_AI_Studio.git
cd Creative_AI_Studio
./setup.sh
```

このスクリプトは以下を行います。

- `venv` の作成
- Python 依存の導入
- `data/` と `outputs/images/`, `outputs/audio/` の作成
- SQLite 初期化
- `apps/web` の依存導入
- `.env` と `apps/web/.env` の初期作成

## 手動セットアップ

### 1. Python 環境

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

### 2. Web 環境

```bash
cd apps/web
npm install
cd ../..
```

### 3. 設定ファイル

```bash
cp .env.example .env
cp apps/web/.env.example apps/web/.env
```

ランタイム設定と manifest の考え方は [model-system.md](/Users/toyoharukohyama/Documents/Creative_AI_Studio/docs/model-system.md) を参照してください。

## 起動

### Studio をまとめて起動（推奨）

```bash
./scripts/start_studio.sh
```

このスクリプトは API と Web UI を必要な場合だけ起動し、ブラウザで Studio を開きます。
ルートの `.env` に設定した `API_PORT` と `WEB_PORT` を両方のプロセスへ反映するため、
通常は `apps/web/.env` の API URL を手動で合わせる必要はありません。別設定で API または
Vite がすでに起動している場合は、先に停止してから実行してください。設定不一致は起動時に
検出され、再起動方法が表示されます。

### API

```bash
source venv/bin/activate
./scripts/run_api_dev.sh
```

`run_api_dev.sh` はリポジトリ直下の `.env` を自動で読み込みます。

### Web UI

```bash
cd apps/web
npm run dev
```

Web UI は `apps/web/.env` を Vite の標準ルールで読み込みます。

## 動作確認

### 一括検証

```bash
./venv/bin/python scripts/verify_local_stack.py --start-api
```

このコマンドは setup check、web build、pytest、`/health` と `/models` の API smoke check をまとめて行います。
実モデルの存在まで確認したい場合は `--check-runtime-files` を追加してください。

### セットアップ検証

```bash
python3 scripts/check_local_setup.py
```

### API ヘルスチェック

```bash
curl http://127.0.0.1:8000/health
```

### モデル一覧

```bash
curl http://127.0.0.1:8000/models
```

```bash
curl "http://127.0.0.1:8000/models?media_type=audio"
```

### 生成ジョブ投入

```bash
curl -X POST http://127.0.0.1:8000/generate/image \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "editorial portrait, dramatic rim light",
    "model_id": "sdxl",
    "negative_prompt": "blurry, low quality",
    "seed": 42,
    "output_format": "png",
    "params": {
      "width": 1024,
      "height": 1024,
      "steps": 30
    }
  }'
```

### 音楽生成ジョブ投入

```bash
curl -X POST http://127.0.0.1:8000/generate/audio \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "dreamy ambient synth loop, soft pulse, midnight drive",
    "seed": 42,
    "output_format": "wav",
    "params": {
      "duration_seconds": 8,
      "bpm": 96,
      "mood": "dreamy"
    }
  }'
```

## よくある注意点

- `.env` は API 起動スクリプト経由で有効になります。`uvicorn apps.api.main:app` を直接叩く場合は、自分で環境変数を export してください。
- `GET /models` は manifest を読むだけで、モデル本体のロードや推論は行いません。
- 初回生成時は diffusers runtime のロードで待ち時間が出ます。
- 初回の音楽生成時も MusicGen runtime のロードで待ち時間が出ます。
- API smoke check だけ既存サーバーに向けたい場合は `./venv/bin/python scripts/verify_local_stack.py --skip-setup-check --skip-web-build --skip-tests --api-base-url http://127.0.0.1:8000` を使えます。

## 関連ドキュメント

- [model-system.md](/Users/toyoharukohyama/Documents/Creative_AI_Studio/docs/model-system.md)
- [model-download-guide.md](/Users/toyoharukohyama/Documents/Creative_AI_Studio/docs/model-download-guide.md)
- [api-contract.md](/Users/toyoharukohyama/Documents/Creative_AI_Studio/docs/api-contract.md)
