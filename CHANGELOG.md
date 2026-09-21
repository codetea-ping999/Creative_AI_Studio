# Changelog

このファイルは利用者から見える変更を記録します。

## [1.0.0] - 未リリース

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

### 検証済みプラットフォーム

- Ubuntu: CI 検証済み
- macOS Apple Silicon: 手動検証

いずれも検証状況の表明であり、最終的なエンドユーザー向けサポート宣言ではありません。
