# セットアップガイド v0.2

Creative AI Studio v0.2 のセットアップおよびアップグレード手順。

## 🚀 新規インストール

### 前提条件

- Python 3.10+ (推奨 3.13+)
- Node.js 18+ (Web UI用)
- 30GB 以上のディスク空き (モデル用)
- 16GB 以上のRAM推奨

### 初期セットアップ

```bash
# 1. リポジトリをクローン
git clone <repository-url>
cd Creative_AI_Studio

# 2. Python 環境を準備
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. 依存パッケージをインストール
pip install -r requirements.txt

# 4. Web UI 依存をインストール
cd apps/web
npm install
cd ../..

# 5. モデルをダウンロード（オプション）
# SDXL はデフォルトで配置済み
# MusicGen をダウンロード:
python3 -m huggingface_hub.cli download facebook/musicgen-small --cache-dir models/audio/musicgen-small

# 6. セットアップ検証
python3 scripts/check_local_setup.py
```

## 📦 モデルの状態

### ✅ 配置済み

- **SDXL** (`models/image/sdxl/`)
  - テキスト→画像 (1024x1024 推奨)
  - デフォルトモデル

### 🔧 セットアップ済み

- **MusicGen-Small** (`models/audio/musicgen-small/`)
  - テキスト→音楽 (8秒)
  - 軽量・高速生成

### 📝 LoRA対応

- **Anime SDXL** (`models/image/anime-sdxl/`)
  - SDXL ベース + LoRA ウェイトで実現
  - `/catalog/loras` で利用可能な LoRA を確認

## 🔄 v0.1 → v0.2 アップグレード

既存のv0.1からアップグレードする場合:

### 1. 新規ディレクトリ作成

```bash
mkdir -p data/projects data/feedback models/audio outputs/audio
```

### 2. Python 依存を更新

```bash
pip install -r requirements.txt
```

メイン変更:
- `huggingface-hub` 追加（モデルダウンロード用）
- `scipy` 追加（オーディオ処理用）

### 3. 新しい API エンドポイントを有効化

`apps/api/main.py` がすでにアップデート済みです:
- `GET /gallery` - ギャラリー表示
- `GET /gallery/stats` - ギャラリー統計
- `POST/GET/DELETE /projects/*` - プロジェクト管理
- `POST/GET/DELETE /feedback/*` - フィードバック評価

### 4. Web UI をリビルド（オプション）

新しいギャラリービューを使用する場合:
```bash
cd apps/web
npm run build
```

## ⚙️ 設定

### 環境変数 (.env)

```bash
# API設定
API_HOST=127.0.0.1
API_PORT=8000

# モデル設定
MODELS_MANIFEST_ROOT=./models/manifests
MODELS_ROOT=./models
LORA_ROOT=./models/loras

# ストレージ設定
PROJECTS_DIR=./data/projects
FEEDBACK_DIR=./data/feedback

# 品質評価（オプション）
QUALITY_ENABLE_SEMANTIC_JUDGE=false

# 出力設定
OUTPUT_DIR=./outputs
```

## 🏃 実行

### API サーバー起動

```bash
source venv/bin/activate
uvicorn apps.api.main:app --reload --port 8000
```

### Web UI 起動

```bash
cd apps/web
npm run dev
# ブラウザで http://localhost:5173 を開く
```

### ジョブランナー

API サーバー起動時に自動的にジョブランナーが起動します。

## ✅ セットアップ検証

```bash
# 総合チェック
python3 scripts/check_local_setup.py

# テスト実行
pytest tests/ -v
```

## 📊 ディレクトリ構造（更新版）

```
creative-ai-studio/
├── apps/
│   ├── api/
│   │   ├── main.py              # ✨ gallery, projects, feedback router 追加
│   │   └── routes/
│   │       ├── gallery.py       # ✨ 新規
│   │       ├── projects.py      # ✨ 新規
│   │       ├── feedback.py      # ✨ 新規
│   │       └── ...
│   └── web/
├── core/
│   ├── projects/                # ✨ 新規 Project管理
│   ├── feedback/                # ✨ 新規 Feedback管理
│   └── ...
├── generators/
│   ├── image/
│   ├── audio/
│   └── video/                   # ✨ 新規 (stub)
├── models/
│   ├── image/
│   │   ├── sdxl/               # ✅ 配置済み
│   │   └── anime-sdxl/         # 🔧 LoRA対応準備
│   ├── audio/
│   │   └── musicgen-small/     # 🔧 セットアップ済み
│   └── manifests/
├── data/                        # ✨ 新規
│   ├── projects/               # プロジェクト JSON
│   └── feedback/               # フィードバック JSON
├── outputs/
│   ├── images/
│   └── audio/                  # ✨ 新規
└── docs/
    ├── api-updates-v0.2.md     # ✨ 新規
    └── ...
```

## 🐛 トラブルシューティング

### MusicGen ロード失敗

```
Error: transformers not found
```

→ `pip install transformers librosa audiocraft`

### モデル見つからない

```
Error: Local model not found at ./models/audio/musicgen-small
```

→ モデルをダウンロード:
```bash
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('facebook/musicgen-small', cache_dir='models/audio/musicgen-small')"
```

### ポート競合

```
Address already in use: ('127.0.0.1', 8000)
```

→ 別のポートで起動:
```bash
uvicorn apps.api.main:app --port 8001
```

## 📖 次のステップ

1. **Semantic Judge 有効化** (オプション)
   - CLIP/CLAP モデルをダウンロード
   - `QUALITY_ENABLE_SEMANTIC_JUDGE=true` に設定

2. **Web UI ギャラリー実装**
   - React コンポーネントで `/gallery` を表示
   - プロジェクト管理UI を追加

3. **フォーマットサポート拡充**
   - WebP, JPEG 出力対応
   - MP3/OGG オーディオ形式対応

4. **Video Generator 実装**
   - SVD (Stable Video Diffusion) / Zeroscope 統合准備

5. **高度なフィードバック分析**
   - ユーザー評価データを活用した品質改善
   - プロンプト最適化レコメンデーション
