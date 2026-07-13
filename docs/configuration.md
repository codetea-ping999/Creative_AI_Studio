# Configuration

Creative AI Studio の設定方法を、実装の読み込み順とあわせて整理したドキュメントです。

このプロジェクトでは、設定ファイルの場所だけでなく、
「誰がその設定を読むのか」を理解すると迷いにくくなります。

## 最初に押さえること

設定は大きく 2 系統あります。

| 系統 | ファイル | 主な利用者 |
| --- | --- | --- |
| API / backend | `.env` | `scripts/run_api_dev.sh`, `bootstrap/factories.py`, 各 route / service |
| Web UI | `apps/web/.env` | Vite, `apps/web/src/App.tsx` |

重要な点:

- ルートの `.env` は API 用です
- `apps/web/.env` はフロント用です
- Web はルート `.env` を読みません
- Python 本体が dotenv を自動で読む構成ではありません

例外として `./scripts/start_studio.sh` はルート `.env` を読み、`API_PORT` と `WEB_PORT` から
Vite の API URL を設定します。別設定で起動済みの API / Vite プロセスは変更できないため、
設定を変えた後はそれらを停止してから起動し直してください。

## 実際の読み込み経路

## API 側

標準起動では次の順です。

1. `scripts/run_api_dev.sh` がルート `.env` を `source` する
2. `uvicorn apps.api.main:app` を起動する
3. `bootstrap/factories.py` が `os.getenv()` で値を読む

つまり、API 側で `.env` が効くのは `run_api_dev.sh` を通した時です。
`uvicorn apps.api.main:app` を直接叩く場合は、自分で環境変数を export する必要があります。

## Web 側

Web は Vite の標準ルールで `apps/web/.env` を読みます。

1. `npm run dev`
2. `npm run build`

のどちらでも `VITE_` プレフィックス付き変数が使われます。

## 設定ファイルの準備

### API 側

```bash
cp .env.example .env
```

### Web 側

```bash
cp apps/web/.env.example apps/web/.env
```

## API 側設定

## ネットワーク

| 変数名 | 既定値 | 読み手 | 用途 |
| --- | --- | --- | --- |
| `API_HOST` | `127.0.0.1` | `scripts/run_api_dev.sh`, `verify_local_stack.py` | bind address |
| `API_PORT` | `8000` | `scripts/run_api_dev.sh`, `verify_local_stack.py` | bind port |
| `WEB_PORT` | `5173` | `scripts/start_studio.sh`, API CORS | ローカル Vite の port |

### 例

```dotenv
API_HOST=127.0.0.1
API_PORT=8001
WEB_PORT=5174
```

## 永続化と出力先

| 変数名 | 既定値 | 用途 |
| --- | --- | --- |
| `DB_PATH` | `./data/jobs.db` | job SQLite の保存先 |
| `OUTPUT_DIR` | `./outputs` | 出力ルート |
| `OUTPUT_IMAGE_DIR` | `./outputs/images` | 画像出力先 |
| `OUTPUT_AUDIO_DIR` | `./outputs/audio` | 音声出力先 |

### 実装上の補足

- video 出力先は明示変数ではなく、image 出力先の sibling として `videos` が導出されます
- asset export の既定先は `outputs/exports/...` 系です
- `/outputs/*` の static mount は API 起動時に行われます

### 例

```dotenv
DB_PATH=./data/jobs.db
OUTPUT_DIR=./outputs
OUTPUT_IMAGE_DIR=./outputs/images
OUTPUT_AUDIO_DIR=./outputs/audio
```

## モデル関連

| 変数名 | 既定値 | 用途 |
| --- | --- | --- |
| `MODELS_ROOT` | `./models` | モデル関連ルート |
| `MODELS_MANIFEST_ROOT` | `./models/manifests` | manifest 探索先 |
| `LORA_ROOT` | `./models/loras` | LoRA catalog 探索先 |
| `MAX_CACHED_MODELS` | `1` | runtime cache の最大保持数 |

### 実装上の優先順

manifest root の解決:

1. 関数引数
2. `MODELS_MANIFEST_ROOT`
3. `MODELS_ROOT/manifests`
4. コード既定値

image output root の解決:

1. 関数引数
2. `OUTPUT_IMAGE_DIR`
3. `OUTPUT_DIR/images`
4. `outputs/images`

audio output root の解決:

1. 関数引数
2. `OUTPUT_AUDIO_DIR`
3. image output root の sibling として `audio`

## 品質評価関連

| 変数名 | 既定値 | 用途 |
| --- | --- | --- |
| `QUALITY_ENABLE_SEMANTIC_JUDGE` | `false` | semantic judge の有効化 |
| `QUALITY_SEMANTIC_LOCAL_ONLY` | `true` | local file のみ許可 |
| `QUALITY_SEMANTIC_CACHE_DIR` | `data/semantic-cache` | semantic score cache の保存先 |
| `QUALITY_SEMANTIC_IMAGE_MODEL` | `openai/clip-vit-base-patch32` | image judge model |
| `QUALITY_SEMANTIC_AUDIO_MODEL` | `laion/clap-htsat-unfused` | audio judge model |
| `QUALITY_SEMANTIC_VIDEO_MODEL` | `openai/clip-vit-base-patch32` | video frame judge model |
| `QUALITY_SEMANTIC_IMAGE_MODEL_PATH` | unset | local image judge path override |
| `QUALITY_SEMANTIC_AUDIO_MODEL_PATH` | unset | local audio judge path override |
| `QUALITY_SEMANTIC_VIDEO_MODEL_PATH` | unset | local video judge path override |
| `QUALITY_SEMANTIC_ENABLE_IMAGE` | `true` | image semantic scoring の有効化 |
| `QUALITY_SEMANTIC_ENABLE_AUDIO` | `true` | audio semantic scoring の有効化 |
| `QUALITY_SEMANTIC_ENABLE_VIDEO` | `true` | video semantic scoring の有効化 |
| `QUALITY_SEMANTIC_VIDEO_BACKEND` | `image_frames` | video semantic backend |
| `QUALITY_SEMANTIC_VIDEO_SAMPLE_FRAMES` | `3` | video から採点する frame 数 |

### 実装上の意味

- `QUALITY_ENABLE_SEMANTIC_JUDGE=false` の間は judge は動きません
- `QUALITY_SEMANTIC_LOCAL_ONLY=true` なら local に model が無い場合 `unavailable` 扱いです
- `QUALITY_SEMANTIC_*_MODEL_PATH` を設定すると Hugging Face ID より local path を優先します
- video は既定で gif の sample frame を image judge に通します
- learned videoのMP4も同じ`image_frames` backendでsample frameを採点します
- CogVideoX-2B pipeline pathと推論既定値は`models/manifests/video/learned-local.json`で管理します
- 採点結果は output path、file stat、prompt、model ref を含む key で cache されます

## ログ関連

| 変数名 | 既定値 | 用途 |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | API / runtime のログレベル |

## Web 側設定

| 変数名 | 既定値 | 用途 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | Web UI が呼ぶ API ベース URL |

### 例

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

## よくある設定パターン

## 1. ポートだけ変更する

`.env`（一括起動を使う場合）

```dotenv
API_PORT=8001
WEB_PORT=5174
```

```bash
./scripts/start_studio.sh
```

`start_studio.sh` は `VITE_API_BASE_URL` と API CORS を同じ port 設定へ自動で
そろえます。API と Web UI を別々に起動する場合だけ、`apps/web/.env` に次を設定します。

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8001
```

## 2. DB を別場所に分ける

```dotenv
DB_PATH=./data/local/dev-jobs.db
```

## 3. モデル配置先を外部ディスクにしたい

```dotenv
MODELS_ROOT=/Volumes/AI/models
MODELS_MANIFEST_ROOT=/Volumes/AI/models/manifests
LORA_ROOT=/Volumes/AI/models/loras
```

## 4. 出力先を別ディレクトリにしたい

```dotenv
OUTPUT_DIR=/Users/yourname/CreativeOutputs
OUTPUT_IMAGE_DIR=/Users/yourname/CreativeOutputs/images
OUTPUT_AUDIO_DIR=/Users/yourname/CreativeOutputs/audio
```

## 推奨ローカル設定

```dotenv
API_HOST=127.0.0.1
API_PORT=8000
WEB_PORT=5173
DB_PATH=./data/jobs.db
MODELS_ROOT=./models
MODELS_MANIFEST_ROOT=./models/manifests
OUTPUT_DIR=./outputs
OUTPUT_IMAGE_DIR=./outputs/images
OUTPUT_AUDIO_DIR=./outputs/audio
LORA_ROOT=./models/loras
QUALITY_ENABLE_SEMANTIC_JUDGE=false
QUALITY_SEMANTIC_LOCAL_ONLY=true
QUALITY_SEMANTIC_CACHE_DIR=./data/semantic-cache
QUALITY_SEMANTIC_IMAGE_MODEL=openai/clip-vit-base-patch32
QUALITY_SEMANTIC_AUDIO_MODEL=laion/clap-htsat-unfused
QUALITY_SEMANTIC_VIDEO_MODEL=openai/clip-vit-base-patch32
QUALITY_SEMANTIC_VIDEO_BACKEND=image_frames
QUALITY_SEMANTIC_VIDEO_SAMPLE_FRAMES=3
LOG_LEVEL=INFO
MAX_CACHED_MODELS=1
```

## トラブルシューティング

## `.env` を書いたのに API に効かない

原因:

- `uvicorn` を直接起動している

対処:

- `./scripts/run_api_dev.sh` で起動する
- または手動で `export` してから起動する

## Web UI が別ポートを向いてしまう

原因:

- API と Web UI を別々に起動し、ルート `.env` だけ変更して `apps/web/.env` を変えていない

対処:

- `./scripts/start_studio.sh` を使う
- または `VITE_API_BASE_URL` も合わせて更新する

## model は見えるが生成時に失敗する

原因:

- `GET /models` は manifest と最低限ファイルの存在を見るだけで、実推論の成功までは保証しない

対処:

- `models/` 配下の実体を確認する
- [model-download-guide.md](./model-download-guide.md) を参照する

## semantic judge が動かない

原因:

- `QUALITY_ENABLE_SEMANTIC_JUDGE=false`
- または local only で judge model が存在しない
- local path override が空、または存在しない

対処:

- `QUALITY_ENABLE_SEMANTIC_JUDGE=true` にする
- local model 配置を [model-download-guide.md](./model-download-guide.md) で確認する
- local path を固定したい場合は `QUALITY_SEMANTIC_IMAGE_MODEL_PATH` や `QUALITY_SEMANTIC_AUDIO_MODEL_PATH` を設定する

## 読むべきコード

設定がどこで使われるか追いたい場合は、次を読むと分かりやすいです。

1. `scripts/run_api_dev.sh`
2. `bootstrap/factories.py`
3. `apps/api/main.py`
4. `apps/web/src/App.tsx`
