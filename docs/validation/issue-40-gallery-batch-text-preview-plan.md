# Issue #40 検証計画書 — ギャラリーのバッチ集約とテキストアセットプレビュー

Issue: [#40](https://github.com/codetea-ping999/Creative_AI_Studio/issues/40)
「[v0.3][Core][P2] Add batch-aware gallery grouping and text asset presentation」

対象ブランチ: `codex/issue-40-gallery-batch-text-preview`
対象コミット: `6dcc219` "feat(gallery): fold batch items into one card, preview text assets"（`main` からの差分 13 ファイル、+875/-50 行）

作成日: 2026-08-01
作成者: Claude（検証実施エージェント）— 本変更の実装者ではない。実装差分は git 履歴から読み取ったのみで、コード作成には関与していない。

## 1. 目的

Issue #40 の実装（ギャラリー画面でバッチ生成したアイテムを1カードに折りたたむ表示、およびテキストアセット＝Story生成物のインライン・プレビュー機能）が、要求仕様どおりに動作し、既存機能（画像/音声/動画ギャラリー、コンポーザーへの再利用フロー等）を退行させていないことを、実装差分・自動テスト・独立した追加検証によって確認する。

## 2. 検証対象（スコープ）

### 2.1 対象に含むもの

| 領域 | ファイル |
| --- | --- |
| API: バッチ所属情報の付与とバッチ絞り込み | `apps/api/routes/gallery.py` |
| フロント: ギャラリーのバッチ折りたたみ表示 | `apps/web/src/components/GalleryPanel.tsx` |
| フロント: テキスト/画像/音声/動画プレビューの分岐 | `apps/web/src/components/MediaPreview.tsx` |
| フロント: テキストアセットの取得・キャッシュ | `apps/web/src/lib/textAssetPreview.ts` |
| フロント: 簡易Markdownレンダラ | `apps/web/src/lib/markdownLite.tsx` |
| フロント: 型定義・ヘルパー拡張（`GalleryMediaType`, `isTextAsset` 等） | `apps/web/src/studio.ts` |
| フロント: 画面状態配線（バッチフィルタ、media_type の型ナローイング） | `apps/web/src/App.tsx` |
| フロント: クイックレビュー選択肢のテキスト対応 | `apps/web/src/lib/quickReview.ts` |
| 既存自動テスト（同ブランチで追加・更新されたもの） | `apps/web/src/components/GalleryPanel.test.tsx`（新規）、`apps/web/src/lib/textAssetPreview.test.ts`（新規）、`apps/web/src/components/LatestJobPanel.test.tsx`（fixture更新）、`tests/test_v0_3_api.py`（1テスト追加） |

### 2.2 対象外（スコープ外として明記）

- 画像・音声・動画生成そのもの（実モデル推論を伴うパイプライン、`core/models/loader.py`, `generators/*` の ML 実行部分）。本Issueの変更はギャラリー表示層のみであり、生成パイプラインには変更が入っていない。
- `torch` / `diffusers` / `transformers` / `imageio` に依存する既存テスト群（後述「4. テスト環境」で理由を明記）。
- 作業ツリーに存在する `*_2.py` 等の未追跡・重複ファイル（`git status` で `??` 表示、本ブランチのコミットに含まれない。誤ってコピーされた重複ファイルと判断し、収集対象から除外した）。

## 3. 検証体制と独立性の方針

- 本検証は、実装コミットの作成主体（コーディングエージェント／開発者）とは別セッションの検証エージェントが実施する。実装コードの再利用は「テスト対象コード」としての参照に限り、テストコードそのものの妥当性は開発者が書いた既存テスト（`GalleryPanel.test.tsx` 等）を**作成者側の一次証跡**、検証エージェントが別途起票する追加シナリオを**独立した二次証跡**として区別して扱う。
- 独立性を担保する具体策:
  1. 既存の自動テストは「実行して結果を記録する」対象とし、**その内容を鵜呑みにせず**、テストケース一覧を検証仕様書側で再構成し、カバレッジの過不足を独立にレビューする（§6）。
  2. 開発者のテスト関数を呼び出さない、別ファイルの独立検証スクリプトを新規に書き下ろし、コード中のコメントが主張する境界条件（例: バッチ絞り込みと `limit` の適用順序）を一次資料（実装コードとAPI仕様）から再導出して検証する（検証報告書 §4）。
  3. UIコンポーネントについては、開発者テストの合格だけでなく、`tsc -b && vite build` による型検査でテストがカバーしない型不整合の有無を独立に確認する。

## 4. テスト環境

| 項目 | 値 |
| --- | --- |
| ホスト | MacBook Pro (Apple M1 Max, 64 GB) — ユーザーの主環境 |
| OS | Darwin 25.5.0 (macOS) |
| Python | 3.14.4（リポジトリ同梱の `venv/` は無かったため、検証用に軽量venvを新規作成） |
| Python主要パッケージ | fastapi 0.141.1 / pydantic 2.13.4 / httpx 0.28.1 / uvicorn 0.52.0 / numpy 2.5.1 / pytest 9.1.1 |
| Node.js | v22.13.1 |
| npm | 10.9.2 |
| フロント主要パッケージ（既存 `node_modules` を使用） | vitest ^4.1.9 / react ^19.1.0 / @testing-library/react ^16.3.2 |

**環境構築上の判断（明記）**: `requirements.txt` には `torch`, `diffusers`, `transformers` 等の大容量MLライブラリが含まれるが、本Issueの変更はギャラリーAPI／表示層のみで、これらのML実行コードには一切触れていない（`apps/api/routes/gallery.py` および変更対象のフロントコードは重い依存を遅延importでも参照しない）。そのため検証環境ではAPI層に必要な軽量依存のみをインストールし、`torch` 等が無いために失敗するモデル実行系テスト（`test_model_system.py`, `test_musicgen_*`, `test_job_pipeline.py` の画像生成系, `test_quality_metrics.py` 等）は**本Issueのスコープ外の既知の未検証項目**として区別する。これらは本変更前から存在する既存テストであり、本変更による退行ではないことは、失敗理由が全件 `ModuleNotFoundError: No module named 'torch'` であることから確認済み（詳細は検証報告書）。

## 5. 検証アプローチ

1. **静的差分レビュー**: `git diff main...HEAD` で変更点を精読し、意図（コード中のコメント、テスト名）と実装の整合を確認する。
2. **既存自動テストの実行**: `tests/test_v0_3_api.py`（対象領域）、`apps/web` の `npm test`（全スイート）を実行し、結果を記録する。
3. **回帰確認**: リポジトリ全体の pytest をスコープ外理由付きで実行し、本Issue変更ファイルに関連する失敗が無いことを確認する。
4. **静的型検査**: `npm run build`（`tsc -b && vite build`）でTypeScriptの型エラー・ビルド失敗が無いことを確認する。
5. **検証手法シミュレーション（独立シナリオのドライラン）**: 開発者のテスト関数を呼ばない独立スクリプトで、コード中の実装コメントが主張する境界条件（バッチ絞り込みと `limit` の適用順序、未知の `batch_id`、バッチラベルの由来）を再現・確認する。
6. **テストケースの独立レビュー**: 開発者が追加したテストケース一覧を棚卸しし、意図・カバレッジ・欠落観点を検証仕様書側で評価する。
7. **総合判定**: 上記の結果を検証報告書にまとめ、合格/不合格基準（検証仕様書で定義）に基づき判定する。

## 6. 合否判定の基本方針

個々のテストケースの合否基準は検証仕様書（`issue-40-gallery-batch-text-preview-spec.md`）に定義する。プロジェクト全体としての合否は以下のとおり判定する。

- **合格**: スコープ内の全テストケースが合格し、スコープ内ファイルに起因する回帰・型エラーが無いこと。
- **条件付き合格**: スコープ内は全合格だが、スコープ外の既知の制約（本計画書§4）が残存する場合。是正ではなく明記による合格とする。
- **不合格**: スコープ内のテストケースに1件でも不合格（Fail）があるか、独立検証シナリオで実装コメントの主張と異なる挙動が確認された場合。

関連ドキュメント: [検証仕様書](issue-40-gallery-batch-text-preview-spec.md) / [検証報告書](issue-40-gallery-batch-text-preview-report.md)
