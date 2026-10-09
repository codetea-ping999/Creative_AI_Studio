# Changelog

このファイルは利用者から見える変更を記録します。

## [Unreleased]

v1.1 に持ち越した項目は [#458](https://github.com/codetea-ping999/Creative_AI_Studio/issues/458) に集約しています。
v1.0.0 の既知の制限と検証の所見のうち、次のものを v1.1 で扱う予定です。

- Storyboard GIF の静止表示（`prefers-reduced-motion` 対応）
- 日本語 Storyboard overlay の文字が MP4 で表示されない問題
- Preview / Experimental 用の ML 依存の更新（`torch`、`diffusers`、`transformers`）

### Fixed

- procedural storyboard（`storyboard-video`）のフレームで日本語が □（豆腐）になる問題を修正。
  既定フォントで描けない文字を含む行は CJK 対応のシステムフォントで描画します。
  `STORYBOARD_FONT_PATH` で任意のフォントを指定できます（#449）。

## [1.0.0] - 2026-09-30

v1.0.0 は「約束する範囲を明示した最初のリリース」です。機能を Stable / Preview /
Experimental に分類し、Stable のみをサポート対象として扱います。分類の一覧は
[README](README.md#v10-で約束する範囲) を参照してください。

### Stable として提供する機能

- **Runtime Safety**: モデル runtime の所有権と load / unload の基盤。起動時に意図しない
  モデル / CUDA ロードを行いません。
- **Job Lifecycle（単一レーン）**: ジョブの作成、状態遷移、実行。v1.0 は単一の job runner
  スレッドで動作します。
- **キャンセル / 起動時リカバリ**: キュー済みジョブの協調的キャンセルと、再起動後の状態収束。
- **永続化**: ジョブ DB と JSON データの読み書き。
- **Template Text / Story**: weight 不要の決定的 template runtime による logline / beat sheet /
  scene list / 本文生成。
- **Procedural Visual / Storyboard**: weight 不要の手続き型シーンビジュアル生成。
- **決定的 Assembly**: timeline から MP4 を書き出す工程。
- **Gallery / Reuse**: 生成結果の一覧、詳細確認、再投入、export、project への紐付け。

主要ジャーニーは次のとおりで、モデル weight を必要としません。

```text
Project -> Template Story -> Procedural Visual / Storyboard -> Gallery / Reuse -> Assembly MP4
```

ナレーション（TTS）と BGM は任意の追加ステップであり、この主要ジャーニーの必須要素では
ありません。

### Preview

SDXL Image、Variation Matrix / Batch、Feedback / Calibration / metrics。
動作しますが、UX / API の安定性を v1.0 では約束しません。

### Experimental

MusicGen、CogVideoX / learned video、実 TTS、実 LLM、Semantic Judge、
WorkerPool / `JOB_LANES`、cloud / remote provider、Creative Bible、Agent handshake / broker。
opt-in の開発者向け機能であり、安定性の約束はありません。

Desktop Shell は v1.0 の成果物にも約束にも含みません。

### リリース運用

- リポジトリルートの `VERSION` が唯一の版数です。`core/version.py` がこれを読み、
  `GET /version`、`/openapi.json`、リリース成果物のファイル名、成果物スモークが
  すべて同じ値から導出されます。
- `GET /version` を追加しました。実行中のインスタンスがどのリリースから切り出されたかを
  確認できます。
- 配布物は tarball（ビルド済み Web UI を同梱、モデルの重みは非同梱）です。導入手順は
  `docs/release/install-from-artifact.md`、リリースを切る手順は `docs/release/runbook.md`
  にあります。
- 依存パッケージのライセンスを `THIRD_PARTY_NOTICES.md` にまとめました。

### 永続データの引き継ぎ

main の `55c4127` が生成した永続データ形式からの更新を回帰テストしています。v1.0 は汎用的な
マイグレーション基盤を導入しません。それより古い履歴上のスキーマは暗黙には保証しません。

### 既知の制限

- リカバリは再起動後の状態収束を指し、中断された実行中ジョブの再開ではありません。
- `JOB_LANES` / WorkerPool は production に配線されていません（#405）。`.env.example` では
  既定でコメントアウトしています。
- quality score は `heuristic_local_v1` による技術品質の proxy であり、意味的な正しさや
  芸術性の判定ではありません。
- Matrix パネルが batch の stage-advance エラーを表示しません（#390）。
- CogVideoX-2B と MusicGen の実 weight は未取得です（#421）。Stable ジャーニーには影響しません。
- SDXL の実 weight を使った通し検証とドッグフードは未完了です（#7、#10）。
- Preview / Experimental 用の ML 依存（`torch` 2.10、`diffusers` 0.37、`transformers` 4.57）に
  公開済みの脆弱性情報があります（2026-09-23 の pip-audit）。Stable ジャーニーは `diffusers` と
  `transformers` を読み込まず、`torch` はモデル runtime 解放時のアクセラレータキャッシュ
  解放にのみ使います。SDXL などの実モデルを使う場合は、信頼できる配布元のモデルファイル
  だけを置いてください。依存の更新は v1.1 で扱います。
- macOS の Safari は既定でダウンロードした `.tar.gz` を自動で展開するため、tarball を
  `SHA256SUMS` で照合できないことがあります。照合する場合は `curl -LO` で取得するか、
  Safari の「ダウンロード後、"安全な"ファイルを開く」を無効にしてからダウンロードしてください。
- Storyboard の GIF は Gallery で繰り返し再生され、OS の「視差効果を減らす」
  （`prefers-reduced-motion`）設定でも止まりません。静止表示への切り替えは v1.1 で扱います。

### 検証済みプラットフォーム

- Ubuntu: CI（`ubuntu-latest`、Python 3.10）で `make verify` とリリース成果物のスモークを検証
- macOS Apple Silicon: macOS 27.0 / arm64、Python 3.14.4、Chrome で、ダウンロードした
  リリース成果物のクリーンインストール、Stable ジャーニー、キャンセル、強制終了後の再起動、
  二重起動の拒否、port 衝突、SIGTERM を手動検証
- Windows: 未検証

いずれも検証状況の表明であり、最終的なエンドユーザー向けサポート宣言ではありません。
