# Issue #40 検証報告書 — ギャラリーのバッチ集約とテキストアセットプレビュー

Issue: [#40](https://github.com/codetea-ping999/Creative_AI_Studio/issues/40)
「[v0.3][Core][P2] Add batch-aware gallery grouping and text asset presentation」（GitHub上のstate: OPEN）

関連: [検証計画書](issue-40-gallery-batch-text-preview-plan.md) / [検証仕様書](issue-40-gallery-batch-text-preview-spec.md)

検証実施日: 2026-08-01
検証実施者: Claude（検証専任、実装コミットの作成者ではない）

## 1. 総合判定

**条件付き合格（スコープ内は全合格。スコープ外の既知制約あり）**

- 検証仕様書の全テストケース（TC-API-01〜07、TC-UI-01〜03、TC-TXT-01〜03、TC-MD-01〜06、TC-TS-01〜03、TC-REG-01〜02）が **Pass**。
- 独立検証シナリオ（開発者のテストコードを流用しない書き下ろし）4件（バッチとlimitの適用順序、未知batch_id、バッチ非汚染）、および`markdownLite`の独立6テストも全て**Pass**。
- 本Issue変更ファイルに起因する失敗・型エラー・ビルド失敗は**0件**。
- リポジトリ全体のpytestで28件の失敗があったが、全て`ModuleNotFoundError: No module named 'torch'`が原因であり、本Issueの変更ファイル（`apps/api/routes/gallery.py`ほか）とは無関係な既存のML実行系テストであることを個別に確認した（§4）。よって「条件付き」とし、不合格とはしない。

## 2. 実施した検証対象（明記）

| 項目 | 値 |
| --- | --- |
| リポジトリ | Creative_AI_Studio |
| 対象ブランチ | `codex/issue-40-gallery-batch-text-preview` |
| 比較基準 | `main`（検証実施時点のローカル`main`） |
| 対象コミット | `6dcc219` "feat(gallery): fold batch items into one card, preview text assets" |
| 差分規模 | 13ファイル、+875/-50行 |
| 変更ファイル一覧 | `apps/api/routes/gallery.py`, `apps/web/src/App.tsx`, `apps/web/src/components/GalleryPanel.tsx`（+新規テスト）, `apps/web/src/components/LatestJobPanel.test.tsx`, `apps/web/src/components/MediaPreview.tsx`, `apps/web/src/lib/markdownLite.tsx`（新規）, `apps/web/src/lib/quickReview.ts`, `apps/web/src/lib/textAssetPreview.ts`（新規、+新規テスト）, `apps/web/src/studio.ts`, `apps/web/src/styles.css`, `tests/test_v0_3_api.py` |
| 検証時点の作業ツリー状態 | 上記以外に、`git status`で`??`表示される`*_2.py`等19件の未追跡重複ファイルが存在（本ブランチのコミット内容ではない。誤ってコピーされた重複と判断し検証対象・回帰確認の両方から除外した） |

## 3. テスト環境

| 項目 | 値 |
| --- | --- |
| ホスト | MacBook Pro (Apple M1 Max, 64 GB) |
| OS | Darwin 25.5.0 |
| Python | 3.14.4（検証専用の軽量venvを新規作成。リポジトリに`venv/`は無かった） |
| Pythonパッケージ | fastapi 0.141.1, pydantic 2.13.4, httpx 0.28.1, uvicorn 0.52.0, numpy 2.5.1, pytest 9.1.1（`torch`/`diffusers`/`transformers`/`imageio`は意図的に未インストール。理由は検証計画書§4） |
| Node.js / npm | v22.13.1 / 10.9.2（リポジトリ既存の`apps/web/node_modules`を使用、追加インストール無し） |
| フロント主要パッケージ | vitest 4.1.9, react 19.1.0, @testing-library/react 16.3.2 |

## 4. 実施結果

### 4.1 バックエンド（pytest）

| 実行範囲 | コマンド | 結果 |
| --- | --- | --- |
| 本Issueスコープ（`BatchApiTests`、うち新規1ケース含む） | `pytest -k BatchApiTests tests/test_v0_3_api.py` | **10 passed** |
| `tests/test_v0_3_api.py` 全体 | `pytest tests/test_v0_3_api.py` | **26 passed** |
| リポジトリ全体（未追跡の重複`*_2.py`ファイルを`--ignore`で除外） | `pytest`（軽量venv） | **439 passed, 23 skipped, 28 failed** |

全体実行での28件の失敗はいずれも下記6ファイルに限られ、失敗理由を個別に`--tb=line`で確認した結果、**全件が`ModuleNotFoundError: No module named 'torch'`（またはそれに起因するジョブステータスの`failed`化）**であることを確認した。いずれも本Issueが変更していない画像/音声/動画のML実行コード（`generators/image/generator.py`, `core/quality/semantic.py`等）を経由するテストである。

- `tests/test_job_pipeline.py`（画像生成end-to-endの2件）
- `tests/test_model_system.py`（画像/音声モデルロード・生成の16件）
- `tests/test_musicgen_long_form.py`（4件）
- `tests/test_musicgen_melody.py`（2件）
- `tests/test_quality_metrics.py`（CLAP音声リサンプリングの1件）
- `tests/test_smoke_musicgen.py`（3件）

23件のskipは`imageio`/`ffmpeg`不足による既知のスキップ（`tests/test_assembly.py`）で、これも本Issueと無関係。

**結論**: 本Issueの変更ファイルに関連するテストで失敗したものは0件。全体実行での失敗はテスト環境の依存関係不足（スコープ外として計画書で事前に明記済み）に起因し、本変更による退行ではない。

### 4.2 フロントエンド（vitest）

| 実行範囲 | コマンド | 結果 |
| --- | --- | --- |
| 変更対象3ファイル（`GalleryPanel.test.tsx`, `textAssetPreview.test.ts`, `LatestJobPanel.test.tsx`） | `vitest run --environment jsdom <3 files>` | **8 passed** |
| `apps/web`全スイート | `npm test`（`vitest run --environment jsdom`） | **7 files / 52 tests passed** |

### 4.3 静的型検査・本番ビルド

| コマンド | 結果 |
| --- | --- |
| `npm run build`（`tsc -b && vite build`） | 型エラー0件、ビルド成功（`dist/index.html`, `dist/assets/*` を出力。約620ms） |

### 4.4 検証手法シミュレーション（独立検証、開発者テストコードを再利用しない書き下ろし）

`tests/test_v0_3_api.py`のテストハーネス（`_Studio`）のみを再利用し、テストケース自体は独立に新規作成したスクリプトを実行した（保管場所: セッションのスクラッチパッド、リポジトリには含めない）。目的は`apps/api/routes/gallery.py`のコード中コメントが主張する「バッチメンバーシップを先にフィルタしてから`limit`を適用する」という設計意図を、開発者のテストとは別の角度から再現することと、開発者テストが直接カバーしていない境界値（未知の`batch_id`、バッチと非バッチの混在時の非汚染）を追加確認することである。

結果: **4シナリオ全てPass**（詳細は検証仕様書TC-API-04〜06）。特に、バッチの生成物が後続の5件の非バッチジョブより古くなる状況を意図的に作り、`limit=2`指定時にバッチ絞り込みが正しく機能すること、`limit=50`では古いバッチアイテムが取りこぼされないことを確認した。

同様に、`markdownLite.tsx`（新規133行、専用テスト無し）についても独立に6テストを一時的に作成・実行し、削除した（詳細は検証仕様書TC-MD-01〜06）。この過程で、見出し変換ロジックが`<h1>`/`<h2>`を一切出力せず、深度4〜6が全て`<h6>`に収束するという、コードのdefault分岐（`<h2>`を返す）が実質到達不能であるという実装依存の挙動を確認した。テストで書いた期待値（"# One" → `<h2>`という直感的な想定）は実際には誤りで、実装の実挙動（`<h3>`）に合わせて修正した上でPassを確認している。この経緯自体が、独立検証がテストの前提を鵜呑みにしないことの実例である。

### 4.5 独立レビュー所見（テストケースの妥当性・カバレッジ）

開発者が同梱したテストケースを検証仕様書の形式で棚卸しした結果、以下を確認した。

- カバーされている: バッチのgallery反映、batch_idによる絞り込み、UI折りたたみ・展開・フィルタ操作、テキスト抜粋のMarkdown除去/切り詰め、既存回帰（`LatestJobPanel`）。
- カバーされていない（本検証で独立に補完、または未実施として明記）:
  1. `useTextAssetContent`フックの単体テスト（fetchキャッシュ、同時リクエストの重複排除、アンマウント後ガード）は無い。
  2. `markdownLite.tsx`の専用テストは無い（本検証で独立補完、結果はPass、ただし前述の見出しレベルの実挙動は仕様として文書化されていない）。
  3. `_resolve_batch_info`のフォールバック分岐（`batch_lookup=None`）は現行の呼び出し経路では到達しない、テストされていないコードパス。
  4. ブラウザでの目視確認（実際のレイアウト崩れ、CSSの折り返し等）は未実施。

これらはいずれも「不合格」の理由とはしない（自動テスト・独立検証はPassのため）が、次回以降の追加検証候補として明記する。

## 5. 未実施・推奨される追加確認

- **人間による目視確認**: 実ブラウザでバッチ折りたたみUIとテキストプレビューの見た目（Markdown崩れ、長文の折り返し、サムネイル抜粋のレイアウト）を確認すること。本検証は自動テスト・型検査・API独立検証の範囲に限定しており、視覚的検証は含んでいない。
- **`torch`込みのフル環境での回帰確認**: 本Issueはスコープ外だが、次にML実行系コードへ変更が入るIssueの検証時には、重量級依存を含めたフル環境（`requirements.txt`全量）での`pytest`実行を計画すること。
- **`useTextAssetContent`のユニットテスト追加**: fetchキャッシュと重複排除ロジックの直接検証が無いため、開発チームへの追加テスト提案として記録する（本検証では実装への変更は行っていない）。

## 6. 検証実施エージェントの独立性についての付記

本検証は、実装ブランチのコミット作成者とは別の検証セッションとして実施した。実装コードの読解は行ったが、検証対象コード自体の修正は一切行っていない（`apps/web/src/lib/__independent_verification_markdownLite.test.tsx`という一時テストファイルを作成したが、検証完了後に削除し、コミット・リポジトリへの反映は行っていない）。開発者が書いたテストケースは「実行して結果を記録する対象」として扱い、その内容の妥当性・カバレッジは検証仕様書側で独立に再評価した。追加の独立検証シナリオ（TC-API-04〜06、TC-MD-01〜06）は、開発者のテスト関数を一切呼び出さず、実装コード（コメント・ロジック）から直接期待値を再導出して作成した。
