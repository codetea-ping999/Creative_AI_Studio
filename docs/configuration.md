# Configuration

Creative AI Studio では API と Web UI で設定ファイルの場所が異なります。

## 設定ファイルの役割

| ファイル | 用途 | 読み込みタイミング |
| --- | --- | --- |
| `.env` | API / bootstrap / ストレージ / manifest 解決 | `./scripts/run_api_dev.sh` 実行時 |
| `apps/web/.env` | Web UI から API を呼ぶ先 | `npm run dev`, `npm run build` 実行時 |

## API 側設定

`.env.example` を `./.env` にコピーして使います。

```bash
cp .env.example .env
```

### 主な設定値

| 変数名 | 既定値 | 説明 |
| --- | --- | --- |
| `API_HOST` | `127.0.0.1` | API bind address |
| `API_PORT` | `8000` | API port |
| `DB_PATH` | `./data/jobs.db` | SQLite DB の保存先 |
| `MODELS_ROOT` | `./models` | モデル関連ファイルのルート |
| `MODELS_MANIFEST_ROOT` | `./models/manifests` | manifest JSON の探索ルート |
| `OUTPUT_DIR` | `./outputs` | 出力ルート |
| `OUTPUT_IMAGE_DIR` | `./outputs/images` | 画像出力先 |
| `OUTPUT_AUDIO_DIR` | `./outputs/audio` | 音声出力先 |
| `LORA_ROOT` | `./models/loras` | LoRA catalog の探索ルート |
| `QUALITY_ENABLE_SEMANTIC_JUDGE` | `false` | semantic judge の有効化 |
| `QUALITY_SEMANTIC_LOCAL_ONLY` | `true` | local file のみで judge model を読む |
| `QUALITY_SEMANTIC_IMAGE_MODEL` | `openai/clip-vit-base-patch32` | image semantic judge model |
| `QUALITY_SEMANTIC_AUDIO_MODEL` | `laion/clap-htsat-unfused` | audio semantic judge model |
| `LOG_LEVEL` | `INFO` | ログレベル |
| `MAX_CACHED_MODELS` | `1` | runtime cache の最大件数 |

### 優先順位

API 側の解決順は以下です。

1. 関数引数で明示した値
2. 環境変数
3. コード既定値

補足:

- `MODELS_MANIFEST_ROOT` があればそれを優先します。
- `MODELS_MANIFEST_ROOT` がなければ `MODELS_ROOT/manifests` を使います。
- `OUTPUT_IMAGE_DIR` がなければ `OUTPUT_DIR/images` を使います。
- `OUTPUT_AUDIO_DIR` があればそれを優先します。
- `OUTPUT_AUDIO_DIR` がなければ `OUTPUT_IMAGE_DIR` の sibling として `audio` を使います。
- `LORA_ROOT` は `/catalog/loras` の探索先です。
- semantic judge は既定で無効です。
- `QUALITY_SEMANTIC_LOCAL_ONLY=true` の場合、judge model が local に無ければ `unavailable` 扱いになります。

## Web 側設定

`apps/web/.env.example` を `apps/web/.env` にコピーして使います。

```bash
cp apps/web/.env.example apps/web/.env
```

### 主な設定値

| 変数名 | 既定値 | 説明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | Web UI が呼び出す API ベース URL |

## 推奨パターン

### ローカル標準

```dotenv
API_HOST=127.0.0.1
API_PORT=8000
DB_PATH=./data/jobs.db
MODELS_ROOT=./models
MODELS_MANIFEST_ROOT=./models/manifests
OUTPUT_DIR=./outputs
OUTPUT_IMAGE_DIR=./outputs/images
OUTPUT_AUDIO_DIR=./outputs/audio
LORA_ROOT=./models/loras
QUALITY_ENABLE_SEMANTIC_JUDGE=false
QUALITY_SEMANTIC_LOCAL_ONLY=true
QUALITY_SEMANTIC_IMAGE_MODEL=openai/clip-vit-base-patch32
QUALITY_SEMANTIC_AUDIO_MODEL=laion/clap-htsat-unfused
MAX_CACHED_MODELS=1
```

### ポート変更

`.env`

```dotenv
API_PORT=8001
```

`apps/web/.env`

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8001
```

## 現状の制約

- `.env` は `run_api_dev.sh` 経由では自動読込されますが、Python 本体が dotenv を直接読んでいるわけではありません。
- Web UI はルートの `.env` を読みません。`apps/web/.env` を使ってください。
