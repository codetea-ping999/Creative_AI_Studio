#!/bin/bash

# Creative AI Studio - 初期セットアップスクリプト
# このスクリプトはプロジェクトの初期化に必要な全てのステップを実行します

set -e

echo "🚀 Creative AI Studio セットアップを開始します..."
echo ""

# カラー定義
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ステップカウンター
STEP=1

print_step() {
    echo -e "${BLUE}[ステップ $STEP]${NC} $1"
    ((STEP++))
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

# ステップ 1: 前提条件確認
print_step "前提条件を確認しています..."

if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 がインストールされていません"
    exit 1
fi
print_success "Python 3 は インストール済み ($(python3 --version))"

if ! command -v node &> /dev/null; then
    echo "⚠️  Node.js がインストールされていません（オプション機能）"
    SKIP_WEB=true
else
    print_success "Node.js はインストール済み ($(node --version))"
fi

echo ""

# ステップ 2: Python 環境構築
print_step "Python 仮想環境を構築しています..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
    print_success "仮想環境を作成しました"
else
    print_warning "仮想環境 (venv) は既に存在します"
fi

# 仮想環境をアクティベート
source venv/bin/activate
print_success "仮想環境をアクティベートしました"

echo ""

# ステップ 3: Python パッケージインストール
print_step "Python パッケージをインストールしています..."

pip install --upgrade pip setuptools wheel > /dev/null 2>&1
print_success "pip をアップグレードしました"

pip install -r requirements.txt > /dev/null 2>&1
print_success "requirements.txt からパッケージをインストールしました"

echo ""

# ステップ 4: ディレクトリ構造を作成
print_step "必要なディレクトリを作成しています..."

mkdir -p outputs/images
print_success "outputs/images を作成しました"

mkdir -p outputs/audio
print_success "outputs/audio を作成しました"

mkdir -p outputs/videos
print_success "outputs/videos を作成しました"

mkdir -p data
print_success "data ディレクトリを作成しました"

mkdir -p data/projects data/feedback
print_success "project / feedback データディレクトリを作成しました"

echo ""

# ステップ 5: データベース初期化
print_step "データベースを初期化しています..."

python3 << 'EOF'
from pathlib import Path
from core.storage.repositories.job_repository import JobRepository

db_path = Path("data/jobs.db")
repo = JobRepository(str(db_path))
print(f"✓ ジョブリポジトリを初期化しました: {db_path}")
EOF

print_success "データベースと初期テーブルをセットアップしました"

echo ""

# ステップ 6: Web UI セットアップ（オプション）
if [ "$SKIP_WEB" != "true" ]; then
    print_step "Web UI パッケージをインストールしています..."
    
    cd apps/web
    npm install > /dev/null 2>&1
    print_success "Web UI の依存パッケージをインストールしました"
    
    cd - > /dev/null
else
    print_warning "Node.js がインストールされていないため Web UI のセットアップをスキップしました"
fi

echo ""

# ステップ 7: 環境設定ファイル作成
print_step "環境設定ファイルを作成しています..."

if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# Creative AI Studio 環境設定

# API
API_HOST=127.0.0.1
API_PORT=8000
API_RELOAD=true

# Database
DB_PATH=./data/jobs.db

# Models
MODELS_ROOT=./models
MODELS_MANIFEST_ROOT=./models/manifests

# Output
OUTPUT_DIR=./outputs
OUTPUT_IMAGE_DIR=./outputs/images
OUTPUT_AUDIO_DIR=./outputs/audio
OUTPUT_VIDEO_DIR=./outputs/videos

# Local asset catalogs
LORA_ROOT=./models/loras

# Optional semantic judge
QUALITY_ENABLE_SEMANTIC_JUDGE=false
QUALITY_SEMANTIC_LOCAL_ONLY=true
QUALITY_SEMANTIC_IMAGE_MODEL=openai/clip-vit-base-patch32
QUALITY_SEMANTIC_AUDIO_MODEL=laion/clap-htsat-unfused

# Logging
LOG_LEVEL=INFO

# Runtime
MAX_CACHED_MODELS=1
EOF
    print_success ".env ファイルを作成しました"
else
    print_warning ".env ファイルは既に存在します"
fi

if [ "$SKIP_WEB" != "true" ]; then
    if [ ! -f "apps/web/.env" ]; then
        cp apps/web/.env.example apps/web/.env
        print_success "apps/web/.env ファイルを作成しました"
    else
        print_warning "apps/web/.env ファイルは既に存在します"
    fi
fi

echo ""

# ステップ 8: 初期化完了
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}✓ セットアップが完了しました！${NC}"
echo -e "${GREEN}================================================================${NC}"
echo ""

echo "📋 次のステップ:"
echo ""
echo "1️⃣  API サーバーを起動する:"
echo "   ./scripts/run_api_dev.sh"
echo ""
echo "2️⃣  Web UI を起動する:"
echo "   cd apps/web && npm run dev"
echo ""
echo "3️⃣  API Docs を確認:"
echo "   http://localhost:8000/docs"
echo ""
echo "4️⃣  Web UI を表示:"
echo "   http://localhost:5173"
echo ""
echo "📚 詳細は README.md を参照してください"
echo ""
