# リポジトリ課題の改善版

作成日: 2026-07-06

## 解決状況

2026-07-06 時点で、本ファイルの P1/P2/P3 課題に対して次の対応を入れました。

- P1-1: Web の出力 URL 変換を `outputs/...`、`/outputs/...`、外部出力 root 配下の `images/audio/videos/exports` に対応。
- P1-2: `/models` を `ApplicationServices.model_service` に接続し、設定済み manifest root を使うよう修正。
- P1-3: `learned-video` scaffold を `is_available=false` として表示し、実 adapter 接続まで selectable な installed model と誤認しないよう修正。
- P1-4: `POST /jobs/{job_id}/cancel` と Web UI の Cancel 操作を追加。queued job は runner が実行しないことをテスト済み。
- P2-5: procedural video は gif のまま、learned runtime は mp4/webm/mov を受けられるよう出力契約を分離。
- P2-6: frontend helper tests を追加し、出力 URL、payload builder、video asset 判定、FastAPI validation error 表示を検証。
- P2-7: Web UI から project status/tags/pinned asset を更新可能にし、gallery を project/search context で絞り込めるよう修正。project export manifest に quality/feedback summary を追加。
- P2-8: `/metrics/summary` に calibrated score 平均を追加し、feedback による補正をテスト。
- P3-9: docs 入口に本ファイルを追加し、v0.2 系の履歴資料へ「現行契約ではない」注意書きを追加。

## 検証結果

現時点の軽量検証は通っています。

- `./venv/bin/python -m pytest -q`: 69 passed
- `npm --prefix apps/web test -- --run`: 5 passed
- `npm --prefix apps/web run build`: succeeded
- `./venv/bin/python scripts/check_local_setup.py --skip-runtime-files`: OK

したがって、今回の課題は「即時にテストが落ちている不具合」ではなく、実運用で詰まりやすい契約不整合、未接続機能、テスト不足を優先して切るのが妥当です。

## P1: 先に直すべき課題

### 1. Web UI が生成物プレビュー URL を作れないケースがある

根拠:

- `apps/web/src/App.tsx` の `createOutputUrl()` は `"/outputs/"` を含むパスだけを URL 化する。
- 生成器の既定出力は `outputs/images/...` のような相対パスになり得る。
- `docs/configuration.md` では `OUTPUT_DIR=/Users/yourname/CreativeOutputs` のような外部出力先も推奨例として載っている。

影響:

- 生成自体が成功しても Stage / Gallery の画像・動画プレビューが `None` になる可能性がある。
- 外部出力ディレクトリを使うと UI 表示が壊れやすい。

改善案:

- API 側で `output_url` / `preview_url` を返し、Web はファイルシステムパスを推測しない。
- 互換対応として、Web 側は `outputs/...` と `/outputs/...` の両方を受ける。
- 外部出力先を使う場合の static mount 方針を `docs/configuration.md` と合わせる。

受け入れ条件:

- 相対パス `outputs/images/a.png` が `http://127.0.0.1:8000/outputs/images/a.png` として表示できる。
- 絶対パス `/.../outputs/images/a.png` も表示できる。
- `createOutputUrl()` か API レスポンス正規化に frontend test を追加する。

### 2. `/models` がアプリの設定済み manifest root を使っていない

根拠:

- `apps/api/routes/models.py` の `list_models()` は `ModelRegistry()` を直接生成している。
- 一方で `bootstrap/factories.py` は `MODELS_MANIFEST_ROOT` / `MODELS_ROOT` を解決して `ModelService` を組み立てる。
- `docs/configuration.md` は manifest root の優先順を明記している。

影響:

- 外部ディスクや検証用 manifest root を設定しても、生成系と `/models` の表示がズレる。
- UI には存在しないモデルが出る、または利用可能なモデルが出ない可能性がある。

改善案:

- `list_models()` で `ApplicationServices` を `Depends(get_services)` から受け取り、既存の `ModelService` / registry を使う。
- `is_available` 判定も同じ manifest root を基準にする。

受け入れ条件:

- temp manifest root を注入した API テストで `/models` がその manifest だけを返す。
- `MODELS_MANIFEST_ROOT` 設定時に `scripts/check_local_setup.py` と `/models` の結果が一致する。

### 3. learned video が selectable だが実体は procedural fallback

根拠:

- `models/video/learned-runtime/runtime.py` は「実 loader に置き換える」前提の scaffold。
- `load_runtime()` は `load_error` を返し、fallback は `procedural_storyboard`。
- `/models` の availability 判定では `runtime.py` が存在するだけで learned model が available になり得る。

影響:

- ユーザーが `learned-video` を選んでも、期待する text-to-video モデルではなく storyboard gif が生成される。
- UI 表示上の available と実生成能力が一致しない。

改善案:

- `learned-video` を明示的に experimental / fallback として UI に表示する。
- もしくは実 runtime が接続されるまで manifest を disabled にする。
- 本当に learned runtime を進めるなら、adapter contract と smoke test を追加する。

受け入れ条件:

- `learned-video` 選択時に fallback 発生が UI と job metadata で明確に分かる。
- 実 learned adapter がある場合だけ `is_available=true` になる。
- learned runtime の adapter contract test を追加する。

### 4. Job cancellation がサービス層にあるが API / UI から使えない

根拠:

- `core/jobs/service.py` には `cancel_job()` がある。
- `apps/api/routes/jobs.py` には create / get / list / rerun はあるが cancel endpoint がない。
- `JobRunner` は処理開始前の `cancelled` は見るが、running generator を中断する協調キャンセルはない。

影響:

- 長時間の image / audio / learned video 生成を UI から止められない。
- ローカル GPU / CPU を占有した場合、ユーザーはプロセス停止に頼ることになる。

改善案:

- `POST /jobs/{job_id}/cancel` を追加する。
- queued job は runner が実行しないことを保証する。
- running job はまず `cancel_requested` 相当の状態か metadata を導入し、generator 協調キャンセルを段階実装する。

受け入れ条件:

- queued job を cancel すると terminal status になり runner が実行しない。
- terminal job への cancel は no-op か明示エラーになる。
- UI に running / queued job の Cancel 操作が出る。

## P2: 次フェーズで直す課題

### 5. Video output contract が GIF 固定で learned runtime と合っていない

根拠:

- `generators/video/runtime.py` の `SUPPORTED_VIDEO_OUTPUT_FORMATS` は `{"gif"}`。
- `generators/video/generator.py` は gif 以外を拒否する。
- しかし `LearnedVideoRuntime` は `mp4` など保存済み動画パスも正規化できる実装になっている。

影響:

- learned text-to-video 接続時に、一般的な `mp4` / `webm` 出力を API 契約として受けられない。

改善案:

- procedural は gif、learned は mp4/webm も許可するように manifest/runtime ごとの output format を定義する。
- Web の `isVideoAsset()` と stage 表示も gif / mp4 / webm を一貫して扱う。

受け入れ条件:

- learned runtime が `output_format=mp4` を返すテストが通る。
- procedural runtime は既存の gif smoke test を維持する。

### 6. Frontend のテストが PromptForm smoke だけで契約変更を守れない

根拠:

- 現在の frontend test は 1 ファイル 1 テスト。
- API レスポンス正規化、出力 URL 変換、gallery action、project binding、error detail 表示のテストがない。

影響:

- API と UI の契約が壊れても `npm --prefix apps/web test` では検知しにくい。

改善案:

- まず pure function を分離して、`createOutputUrl` / payload builder / request snapshot draft のテストを追加する。
- 次に gallery detail / reuse / export の UI interaction test を足す。

受け入れ条件:

- frontend test が少なくとも output URL、generate payload、reuse payload、validation error 表示を検証する。

### 7. Project / Asset 管理は baseline だが、実制作向けの整理機能が薄い

根拠:

- `docs/next-tasks.md` でも Project metadata / asset export / search が次タスクになっている。
- 現状は project grouping、bind、export はあるが、制作単位での検索・絞り込み・タグ運用は最小限。

影響:

- 生成数が増えると、採用候補、修正待ち、納品済みなどの管理が難しくなる。

改善案:

- project status / tags / pinned assets を UI から編集できるようにする。
- gallery search を project context に統合する。
- export manifest に feedback / quality summary を含める。

受け入れ条件:

- UI から project tag / status / pinned asset を更新できる。
- project export に asset 一覧、quality summary、feedback summary が含まれる。

### 8. Semantic judge は scaffold 済みだが、品質指標としての calibration が不足

根拠:

- `docs/next-tasks.md` は semantic judge calibration dataset と human feedback loop を次タスクとしている。
- 現状の quality score は heuristic proxy で、README も semantic fidelity / 芸術性は自動判定外と説明している。

影響:

- スコアが「制作判断に使える品質」ではなく、技術的 proxy に留まる。
- user feedback と judge score のズレを蓄積して改善できない。

改善案:

- feedback と quality report を紐付けた calibration dataset を作る。
- media type ごとに閾値と重みを設定できるようにする。
- `/metrics/summary` に calibrated score の分布を出す。

受け入れ条件:

- feedback 入力後、同一 asset の calibrated score が再計算される。
- calibration の before / after をテストできる fixture がある。

## P3: 整理・運用品質

### 9. docs と実装の「現在の契約」を一本化する

根拠:

- `README_v0.2.md`、`IMPLEMENTATION_SUMMARY.md`、`REPAIR_COMPLETE.md`、`COMPLETION_CHECKLIST.md` は履歴資料として残っている。
- 現在の契約は `README.md`、`docs/api-contract.md`、`docs/configuration.md`、`docs/next-tasks.md` に分散している。

影響:

- 新しく作業する人が、履歴資料と現行仕様を混同しやすい。

改善案:

- 現行仕様は `docs/README.md` から辿れる資料に集約する。
- 履歴資料には冒頭で「現行仕様ではない」と明記する。

受け入れ条件:

- docs の入口から、setup / architecture / API contract / next tasks に迷わず到達できる。
- 古い資料を読んでも現行仕様と誤認しない。

## 推奨着手順

1. Web 出力 URL の修正と frontend test 追加。
2. `/models` を service graph に接続し、manifest root の契約を修正。
3. learned video の available 表示または manifest 状態を正す。
4. cancel endpoint と queued cancellation のテストを追加。
5. video output format と frontend contract を learned runtime 対応に広げる。
