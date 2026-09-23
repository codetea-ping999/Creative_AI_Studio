# リリース成果物からインストールする

開発用チェックアウトではなく、配布された tarball から動かす手順です。
成果物には**ビルド済みの Web UI が含まれる**ため、実行時に Node も Vite も必要ありません。
モデルの重みは含まれないので、必要に応じて
[docs/model-download-guide.md](../model-download-guide.md) を参照して配置してください。

## 前提

- Python 3.10 以上
- Node.js は不要

## 手順

```bash
# 1. ダウンロードした tarball の SHA256 を照合する
sha256sum -c SHA256SUMS      # macOS では shasum -a 256 -c SHA256SUMS

# 2. 展開する（<VERSION> は VERSION ファイルの値。例: 1.0.0）
tar -xzf creative-ai-studio-v<VERSION>.tar.gz
cd creative-ai-studio-v<VERSION>

# 3. Python 環境を用意する
python3 -m venv venv
./venv/bin/pip install --upgrade pip   # venv 同梱の pip は古いことがあるため
./venv/bin/pip install -r requirements.txt

# 4. 起動する（ビルド済み UI を API と同じ origin で配信）
./scripts/run_studio.sh
```

既定では `http://127.0.0.1:8000` で起動します。port を変える場合はルートの `.env` に
`API_PORT` を設定してください。

## 起動後の確認

```bash
curl -s http://127.0.0.1:8000/health    # {"status":"ok"}
curl -s http://127.0.0.1:8000/version   # {"version":"...","release_tag":"v..."}
```

`/version` が返す `release_tag` は、その成果物を切り出したタグと一致します。
ダウンロードしたものが意図したリリースかどうかは、ここで確認できます。

## セキュリティ上の注意

API は未認証です。`scripts/run_studio.sh` はループバック以外への bind を既定で拒否し、
`ALLOW_UNSAFE_API_BIND=1` を明示した場合にのみ許可します。ネットワークに露出させる
場合は、その前段で認証を用意してください。

外部通信のガード（`ALLOW_REMOTE_TEXT_ENDPOINTS`、`ALLOW_REMOTE_AUDIO_ENDPOINTS`、
`ALLOW_CLOUD_PROVIDERS`）はすべて既定で閉じています。

`MODELS_ROOT` に置いたモデルディレクトリ内の `runtime.py` / `adapter.py` は、
`LearnedVideoLoader` によって読み込まれ実行されます（設計上の任意コード実行）。
信頼できるモデルバンドルのみを配置してください。
