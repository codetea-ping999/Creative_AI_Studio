# Desktop Shell v0.1 — 検証・ビルド・次ステップへの接続（Runbook）

Desktop Shell（Tauri 2）を変更したときに「何をどの順で確かめるか」を固定するための手順書です。
`docs/desktop/architecture-decision.md`（ADR）と対で読みます。このファイルの目的は、実装を
進めた結果として「次に何をすれば検証が再現できるか」を、セッションやエージェントを越えて
再現可能に保つことです。

## 対象ブランチと状態

- 機能ブランチ: `codex/desktop-shell-cors-runtime`
- PR: [#409](https://github.com/codetea-ping999/Creative_AI_Studio/pull/409)（Desktop Shell v0.1 + CORS + runtime endpoint 解決）
- ベースライン HEAD（レビュー指摘対応後）: `d97ada3`
- ハード不変条件: この Rust プロセスは Python/FastAPI を spawn せず、CUDA・モデルランタイムを
  初期化しない（`docs/desktop/architecture-decision.md` / `apps/desktop/scripts/check_no_backend_spawn.py`）。

## ビルド

前提: Rust toolchain（`rust-version = 1.88` 以上。ロック済み依存（darling 0.23、time 0.3.55 など）の MSRV に合わせています）と Tauri 2 CLI。CLI はバージョンを固定して入れる:

```bash
cargo install tauri-cli --version "^2" --locked   # `cargo tauri --version` が 2.x を返すこと
cd apps/desktop/src-tauri && cargo tauri build
```

Linux でビルド・`cargo check` する場合は WebKitGTK 等の system package も必要
（Debian/Ubuntu: `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev`）。

- `beforeBuildCommand` が `web/dist` を再ビルドする（`tauri.conf.json` の `frontendDist = ../../web/dist`）
- `bundle.targets = all` → `.app` と `.dmg`（`target/release/bundle/` 以下、両方 gitignore）
- 単体テストのみなら `cargo test --all-targets`（現在 23 passed）

## 検証ゲート（変更を「完了」と呼ぶ前に必ず回す）

| 対象 | コマンド | 備考 |
| --- | --- | --- |
| Python 全 suite | `venv/bin/python -m pytest -q` | 基準は 1269 passed / 206 subtests（2026-09-13, `d97ada3`） |
| Web test | `npm --prefix apps/web test` | 基準 80 passed |
| Web lint | `npm --prefix apps/web run lint` | 0 errors（react-hooks の警告群は変更前から存在） |
| Web build | `npm --prefix apps/web run build` | tauri build 内でも実行される |
| Rust check | `cd apps/desktop/src-tauri && cargo check --all-targets` | warning 0 |
| Rust test | `cd apps/desktop/src-tauri && cargo test --all-targets` | 23 passed |
| 境界ガード | `python3 apps/desktop/scripts/check_no_backend_spawn.py` | Rust に backend/spawn 系の実経路がないこと |
| diff 衛生 | `git diff --check` | 空白エラー禁止 |

## パッケージ版 Smoke（自動）

```bash
./scripts/desktop_smoke.sh            # ビルド済み .app が必要
./scripts/desktop_smoke.sh --force    # smoke 用 port の listener と、同じ bundle バイナリの起動中インスタンスを先に止める
```

安全性: 終了時に止めるのはスクリプト自身が起動した PID（backend とその子、app）だけで、パターン一致の
`pkill` はしない。既存の root `.env` は backup より前に一切触らず、実行中は退避（phase の `API_PORT` を
上書きさせない）、終了時に復元する。途中で失敗したコマンドの終了コードはそのまま返る。

検証内容（すべて断言付き）:

1. default port（`127.0.0.1:8000`）へ、env 上書きなしで packaged app が接続（`/catalog/loras` 等の読み出しが backend ログに現れる）
2. `Origin: tauri://localhost` からの CORS read / preflight / JSON write（201 → 作成プロジェクト削除）
3. non-default port（`API_PORT=8123 ./scripts/run_api_dev.sh` + root `.env`）へ、export なしの通常起動で接続し、
   **8000 へは 1 件もリクエストしない**（DesktopRuntime precedence の実証）
4. 二重起動は既存インスタンスをフォーカス（desktop プロセスが 1 つ）

自動 smoke の詳細・手動手順・注意点は次節以降を参照。

## 手動 smoke の要点（自動スクリプトがやっていること）

### default port（8000）

```bash
nohup ./scripts/run_api_dev.sh > /tmp/api8000.log 2>&1 &
env -u API_PORT -u STUDIO_BACKEND_URL \
  "apps/desktop/src-tauri/target/release/bundle/macos/Creative AI Studio.app/Contents/MacOS/creative-ai-studio-desktop" \
  > /tmp/app8000.log 2>&1 &
# 起動完了後、/tmp/api8000.log に GET /catalog/loras 等が現れれば接続成功
```

### 非既定 port（例: 8123）— 既存の `API_PORT` フローと整合

1. 既存の `.env` を退避し、`printf 'API_PORT=8123\n' > .env` を作る（末尾で削除・復元すること）
2. `API_PORT=8123 ./scripts/run_api_dev.sh` で backend を起動
3. `env -u API_PORT -u STUDIO_BACKEND_URL <binary> &` で「通常起動」を再現
4. 8123 の backend ログに UI の読み出しが現れ、8000 のログ行数が増えないことを確認

エンドポイント解決の precedence（ADR §7a）: `STUDIO_BACKEND_URL` > root `.env` の `API_PORT`
（最後の行が有効）> `API_PORT`（環境）> `http://127.0.0.1:8000`。`run_api_dev.sh` が `.env` を
環境より後に `source` するのと同じ順序。exact loopback（probing なし）。backend は起動しない（接続のみ）。

`.env` の場所は実行時に決まる（ビルドマシンのパスは埋め込まない）: `CREATIVE_AI_STUDIO_ROOT`
> 実行ファイルの祖先にある Studio checkout > アプリ設定ディレクトリ
（macOS: `~/Library/Application Support/com.creativeaistudio.desktop/.env`）> debug ビルドのみ build 時の
checkout。`/Applications` に置いた配布版で非既定 port を使うときは、アプリ設定ディレクトリの `.env` に
`API_PORT=8123` を書くか、`CREATIVE_AI_STUDIO_ROOT` で checkout を指す。

### global shortcut 失敗系（非 fatal の実証方法）

登録失敗はプラグイン setup 後、`setup` 内の `on_shortcut` で握り、`eprintln!` に記録して続行する。
OS 側で失敗を起こすのは難しいため、**不正な accelerator を一時的に**入れて build すると
決定論的に失敗経路を通る:

1. `apps/desktop/src-tauri/src/shortcuts.rs` の `DEFAULT_SHORTCUT` を `""` に変更（ローカル限定）
2. `cargo tauri build` → 起動 → stderr に
   `desktop: global shortcut registration failed (non-fatal): ...` が出る
3. その状態でも shell・window・backend 接続（UI の読み出し）が動くことを確認
4. 元の `"CommandOrControl+Shift+Space"` に戻して **再 build してから** 完了扱いにする

### autostart（tray トグル）

- default OFF。DEFAULT 起動直後は `~/Library/LaunchAgents/` に Creative AI Studio の plist が無いこと
- tray に `Start on login` のチェックメニューがあり、実 OS 状態を反映し、明示クリックで toggle
- クリック操作は accessibility 権限が無いと自動化できない。有効化→`~/Library/LaunchAgents/` の
  plist 生成→無効化→plist 除去を、実機の UI で 1 回確認するタスクを `docs/next-tasks.md` に積んでいる

## 環境で判明している事実（再発防止メモ）

| 項目 | 内容 |
| --- | --- |
| `/Applications/Creative AI Studio.app` との取り違え | 旧 DMG 版が同じ bundle ID `com.creativeaistudio.desktop` のため、`open` は LaunchServices で /Applications へ逃げる。**smoke は必ずビルドツリーのバイナリを直接起動する** |
| `models/image/sdxl` | tracked の stub（`LICENSE.md` / `README.md` / `model_index.json`）+ ローカル `~/models/image/sdxl` への component symlink（未追跡）。動作確認用に維持し、**削除・`git clean` しない** |
| `anime-sdxl` / `ssd-1b` weights | `git clean -dfx` で消失済み。復元は `scripts/setup_anime_sdxl.py` 再実行が必要（ローカル実機タスク） |
| `apps/desktop/src-tauri/target` / `gen` | gitignore 済み（`.gitignore` 修正済み、`b6204db`）。誤って追跡しない |
| 画面キャプチャ | screen recording 権限が無いと取得不可。ウィンドウ一覧は `swift` + CGWindowList で取得可能 |
| 並列エージェント | `codex/feature/desktop-shell-v0.1` は別ワークツリー（`Creative_AI_Studio-desktop-385`）に checkout。同一ワークツリーへの複数プロバイダ書き込み禁止（`docs/agent-harness.md`） |

## 変更時に確認すべき前提

- Web 側は触らない（`runtime.ts` / `studioClient.ts` はそのままで desktop へ接続が流れる）。
  `BrowserRuntime` の fetch 前 await 追加は禁止（既存タイミングを変えない）
- 新しい Rust コードは `check_no_backend_spawn.py` の probe 語（python/fastapi/uvicorn/cuda/torch/spawn）
  をコメント・文字列外に置かない
- import は `use tauri::{Manager, ...}` 等、未使用 import を残さない（`cargo check` warning 0 を保つ）

## 次に積むタスクの行き先

- マージ / weight 復元 / tray UI 実操作 / notarization / BackendSupervisor 等は
  `docs/next-tasks.md` の「Desktop Shell v0.1 — 次ステップ」セクションに記載済み
- 関連 ADR: `docs/desktop/architecture-decision.md` §1–§7a / §11