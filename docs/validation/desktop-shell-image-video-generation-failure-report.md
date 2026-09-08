# Desktop Shell での画像・動画生成失敗 調査レポート

- 調査日: 2026-09-07
- 対象 HEAD: `7c8c6ff` (detached) / `apps/desktop` = Tauri 2 Desktop Shell v0.1
- 調査方法: コード読解 + 起動中バックエンド (127.0.0.1:8000) への CORS 実測。編集は行っていない。

## 結論 (TL;DR)

パッケージ済み macOS デスクトップアプリ（Tauri WebView）で画像・動画（および音声）の生成ができない原因は、
**FastAPI の CORS 許可リストに Tauri の WebView オリジンが含まれていない**ためです。

バックエンドは正しく起動していても、デスクトップアプリ内からは API レスポンスを読み取れない状態になります。
これにより `GET /health` が失敗 → UI が「接続できません」表示になり、モデル一覧が読み込まれず、
生成ボタンが無効化されるため、画像も動画も作れません。

これは `docs/desktop/architecture-decision.md` の Desktop Shell v0.1 受け入れ条件（¶3, ¶7）をまだ満たしていないことによる実装ギャップです。

## 症状

- デスクトップアプリ（.app）を起動し、Studio 画面上で画像・動画を生成しようとすると失敗する
- 上部の readiness バナーが「接続できません」となりモデル読み込みが進まない
- 生成ボタン（「生成する」）が無効化されている / 押下しても早期リターンする

## フォールトチェーン

1. `App.tsx` は起動時に `loadReadiness()` → `GET /health` を実行する (`apps/web/src/App.tsx:249-259`)
2. この fetch は Tauri WebView (production origin = `tauri://localhost` on macOS) から、
   ビルド時に焼き込まれた `http://127.0.0.1:8000` へのクロスオリジンリクエストになる (`apps/web/src/studioClient.ts:1-3`)
3. `apps/api/main.py:88-94` の CORS 許可リストは Vite dev オリジン
   (`http://127.0.0.1:{WEB_PORT}` / `http://localhost:{WEB_PORT}`) のみ
4. `tauri://localhost` は許可されないため、ブラウザ/WebView はレスポンスを読み取れず fetch が例外になる
5. `apiReachable=false` → `loadModels()` が動かない (`App.tsx:261-278`) →
   `readinessState` が `offline` → `handleSubmit` が `readinessState !== "ready"` で早期リターン (`App.tsx:512-523`)
6. 結果: 画像・動画・音声すべてで生成が開始できない

## CORS 実測エビデンス（起動中 API に対して実施）

管理対象外の円滑な確認のため、実際に LISTEN している `127.0.0.1:8000` へ curl で preflight を送った。

| ケース | Origin | 結果 |
| --- | --- | --- |
| ブラウザ(dev) の POST preflight | `http://localhost:5173` | HTTP 200 + `access-control-allow-origin: http://localhost:5173` |
| Tauri(macOS) の POST preflight | `tauri://localhost` | HTTP 400 Bad Request、`access-control-allow-origin` なし |
| Tauri(macOS) の GET /health | `tauri://localhost` | HTTP 200 だが CORS ヘッダなし → fetch は例外 |

`tauri://localhost` からの preflight が 400 で拒否され、GET にも `access-control-allow-origin` が付かないため、
WebView の JS はどちらも扱えない。

## 根本原因（優先度順）

### 1. [必須] CORS 許可リストに Tauri WebView オリジンが無い

- 該当: `apps/api/main.py:26-39, 88-94`（`_local_web_origins()` → `allow_origins`）
- Tauri 2 の production アセットプロトコルは、macOS/Linux = `tauri://localhost`、Windows = `http://tauri.localhost`（`use_https_scheme: true` なら `https://tauri.localhost`）
- ADR (`docs/desktop/architecture-decision.md`) ¶3 には
  「packaged Tauri WebView のオリジンを FastAPI CORS へ明示的に許可し、CORS はワイルドカードではなく exact allowlist に保つ」と定められているが未実装
- 上記オリジン追加なしには、パッケージ版 WebView からの `POST /generate/*`（JSON → preflight 必須）は常にブロックされる

### 2. [設計上の制約] デスクトップはバックエンドを起動しない（v0.1 は「起動済みバックエンド前提」）

- `apps/desktop/src-tauri/src/lib.rs` は UI のみのシェルであり、backend spawn は明示的に禁止（`check_no_backend_spawn.py` で静的検証）
- `.app` 単体だけを起動してバックエンド (run_api_dev.sh) が動いていない場合は、そもそも API に到達できず同上の症状になる
- ブラウザ版は `scripts/start_studio.sh` が API と Vite を起動して CORS を preflight 確認までする（`start_studio.sh:107-116`）

### 3. [未実装] DesktopRuntime / StudioRuntime 抽象化が無い

- ADR ¶3・¶7 で「ビルド時 `VITE_API_BASE_URL` に依存しない」「packaged WebView から実行時エンドポイント解決」が v0.1 要件と定められている
- しかし `apps/web/src` に `DesktopRuntime`/`StudioRuntime`/tauri 連携コードは存在しない（grep で 0 件）
- `apps/web/src/studioClient.ts:1-3` は `VITE_API_BASE_URL`（未設定なら fallback）のみ
- 実際にビルド済み `apps/web/dist/assets/*.js` には `127.0.0.1:8000` が焼き込まれており、デスクトップと同じ dist を Tauri が内蔵するため、
  バックエンドが非既定ポートで動いていてもリビルドなしでは接続不能

## 正常系（ブラウザ）との比較

- ブラウザ経由（`start_studio.sh`）: Origin = `http://localhost:5173` が CORS 許可され、`VITE_API_BASE_URL` で API URL が注入される → 生成が機能する
- デスクトップ経由: Origin = `tauri://localhost` が許可されていない + API URL が固定 → 生成が機能しない

## 影響範囲

- `POST /generate/image` ... 画像（「画像を作る」）
- `POST /generate/video` ... 動画（video workflow / Story シーン生成）
- `POST /generate/audio` ... 音声
- `/gallery`・`/jobs`・`/metrics`・`/models` などの読み取りも全て失敗するため、ギャラリー一覧・ステータス表示も機能しない
- `<img>` での `/outputs/**` 表示自体は CORS 不要で表示できる場合があるが、一覧取得が失敗するため実用にならない

## 修正しない場合の確認手順（再現）

1. `./scripts/start_studio.sh` でバックエンドのみ起動しておく（ブラウザ版は正常動くことを確認）
2. `apps/desktop` を `npm run tauri build`（または `cargo tauri build`）でビルドし `.app` を起動
3. readiness バナーが「接続できません」になり、画像・動画生成が開始できないことを確認
4. コンソールで CORS エラー（`tauri://localhost` が許可されていない旨）を確認

## 推奨対応（未実施。今回は探索のみ）

1. `apps/api/main.py` の CORS `allow_origins` へ、パッケージ版 WebView オリジンを追加
   - macOS/Linux: `tauri://localhost`
   - Windows: `http://tauri.localhost` / `https://tauri.localhost`
   - 環境変数（例 `TAURI_ALLOWED_ORIGINS`）からのオプション注入を検討（exact allowlist のまま）
2. `apps/api` の CORS 単体テストを追加（dev origin / tauri origin 双方の preflight を検証）
3. `apps/web` に `StudioRuntime`（BrowserRuntime / DesktopRuntime）境界を導入し、desktop では実行時エンドポイント解決とポート変更を可能にする（ADR ¶7 の受け入れ条件）
4. デスクトップ起動時に「バックエンドが未起動/接続不可」を明確に伝える UX（health 表示の改善）

## 参考コード箇所

- `apps/api/main.py:26-39` / `:88-94` ... CORS 許可リスト
- `apps/web/src/studioClient.ts:1-3` ... API base URL 解決（固定 fallback のみ）
- `apps/web/src/App.tsx:249-259` ... `/health` ポーリングと `apiReachable`
- `apps/web/src/App.tsx:261-278` ... `apiReachable=false` でモデル読み込み停止
- `apps/web/src/App.tsx:512-523` ... `readinessState !== "ready"` で生成を早期リターン
- `apps/desktop/src-tauri/src/lib.rs` ... UI のみのシェル（backend 非 spawn）
- `scripts/start_studio.sh` ... ブラウザ版の起動フロー（CORS preflight 検証込み）
- `docs/desktop/architecture-decision.md` ¶3 / ¶7 ... v0.1 受け入れ条件（未達）