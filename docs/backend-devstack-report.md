# Backend Dev-Stack 検証レポート

日付: 2026-04-19

## 概要
ローカル開発スタック起動時に `GET /openapi.json` と `GET /projects` が 404 を返したという初回観測がありましたが、現行コードと標準検証では同事象は再現しませんでした。本レポートは、初回観測をそのまま確定事実として扱わず、現時点で確認できた事実と今後の確認手順を整理したものです。

## 現時点で確認できた事実
- `apps/api/main.py` の `create_app()` では FastAPI 標準の `/openapi.json` が有効で、`/projects` ルーターも登録されています。
- `StaticFiles` は `/outputs` にのみ mount されており、ルート `/` や `/openapi.json` を横取りする構成にはなっていません。
- `./venv/bin/python scripts/verify_local_stack.py --start-api` は成功し、API smoke check の中で `/projects` を含む主要エンドポイントが通過しました。
- CI は未設定ではなく、`.github/workflows/ci.yml` ですでに `python scripts/verify_local_stack.py --start-api` を実行しています。
- 初回観測の 404 ボディ `{"status":"failed","error":{"code":"not_found","message":"Static file not found."}}` は、現行 FastAPI アプリの標準 404 形式 `{"detail": ...}` と一致しません。

## 初回観測との差分
初回観測では次が記録されていました。

- `GET /health` → 200
- `GET /openapi.json` → 404
- `GET /projects` → 404

このうち `/openapi.json` と `/projects` の 404 は、現行コード・TestClient 検証・標準 smoke check のいずれでも再現しませんでした。したがって、これらは「現行アプリの確定不具合」ではなく、「当時接続していた `localhost:8000` の実体が期待していたプロセスではなかった可能性が高い観測結果」として扱います。

## 最有力の原因仮説
第一仮説は、`localhost:8000` で Creative AI Studio API とは別のプロセス、または古い起動済みサービスを見ていたことです。根拠は次のとおりです。

- `/health` だけが一致しても、別サービスが同名エンドポイントを持っていれば 200 は成立しうる。
- `/openapi.json` と `/projects` の 404 ボディが現行 FastAPI アプリの応答形式と一致しない。
- 現行の `create_app()` は `/openapi.json` と `/projects` を実際に公開している。

静的ファイルハンドラが `/openapi.json` を奪っていた、という仮説は現行コードからは支持されません。

## 標準の確認ルート
以後のローカル確認は次の順を標準とします。

```bash
make verify
```

または同等コマンド:

```bash
./venv/bin/python scripts/verify_local_stack.py --start-api
```

API smoke のみ確認したい場合は次を使います。

```bash
make api-smoke
```

手作業の `curl` は補助手段として扱い、単独では「正しいプロセスを見ている」根拠にしません。`./scripts/run_dev_stack.sh` は起動後に `/health` と `/openapi.json` を確認し、Vite の実際の待受 URL を表示する前提に更新します。

## 再現時の記録テンプレート
同様の事象を再度観測した場合は、少なくとも次を保存します。

```bash
# 1. 8000 番ポートの実体を確認
lsof -nP -iTCP:8000 -sTCP:LISTEN

# 2. dev stack の起動コマンド
./scripts/run_dev_stack.sh

# 3. API 応答確認
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/openapi.json
curl -i http://127.0.0.1:8000/projects

# 4. 標準 smoke
make api-smoke
```

保存するもの:

- `lsof` の出力
- `run_dev_stack.sh` の起動ログ
- `/health`、`/openapi.json`、`/projects` のレスポンス本文
- `make api-smoke` の結果

## 結論
- 現時点では API 実装不良は再現していません。
- 修正対象は API 本体ではなく、レポートの事実是正と、誤った接続先を見たときに早く気付ける開発フローの強化です。
- 今後の検証結果は、`make verify` / `make api-smoke` を基準に共有します。
