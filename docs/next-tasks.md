# Next Tasks

現在の状態を踏まえた次タスク一覧です。

## このフェーズで反映した項目

| 状態 | タスク | 内容 |
| --- | --- | --- |
| Done | Studio UI 再構成 | image / music を横断できる Creative AI Studio 画面に更新 |
| Done | Session history 表示 | `GET /jobs` を使って直近ジョブを Web UI に表示 |
| Done | Stage 表示統合 | 画像プレビューと音楽再生を同じ stage で扱う構成に整理 |
| Done | Docs の整合 | architecture / api-contract を現状に合わせて更新 |
| Done | quality metrics | image / audio quality report と `/metrics/summary` を追加 |
| Done | LoRA catalog UX | `/catalog/loras` と選択式 LoRA UI を追加 |
| Done | CI baseline | build / setup check / pytest を GitHub Actions に追加 |
| Done | Verification suite | `scripts/verify_local_stack.py` を追加し、CI とローカルの検証フローを共通化 |
| Done | onboarding docs hardening | Git 管理対象とローカル専用ファイル、モデル配置の前提を README / setup guide に明記 |
| Done | semantic judge scaffold | optional な local CLIP / CLAP judge の土台を追加 |
| Done | video storyboard flow | `/generate/video`、procedural runtime、Studio UI の基本導線を追加 |
| Done | gallery / project / feedback baseline | gallery filter、project job binding、feedback summary を追加 |
| Done | gallery asset workflow | asset detail、reuse、export、project bind を API / Studio UI に接続 |
| Done | repository JSON hardening | asset / project / feedback の破損 JSON を一覧取得から隔離 |
| Done | manifest validation hardening | duplicate manifest id / public id / alias を setup check で検出 |
| Done | expanded API smoke | `/health` / `/models` に加え project / video generate / gallery / project jobs を検証 |
| Done | frontend test path | `npm --prefix apps/web test` と PromptForm smoke test を追加 |
| Done | package task runner | root `Makefile` から verify / verify-lite / api-smoke を実行可能に整理 |
| Done | semantic judge operational docs | local judge model 配置、cache、video frame backend の設定を明文化 |
| Done | Project / Asset 整理強化 | project metadata、tags、pin、検索、asset/project export を API / UI に接続 |
| Done | atomic JSON persistence | asset / project / feedback / export manifest を replace-on-success 保存へ統一 |
| Done | learned video pilot adapter | CogVideoX-2B adapter、MP4契約、model readiness表示を追加 |
| Done | feedback calibration dataset | JSONL export、相関レポート、`/metrics/calibration` を追加 |

## v0.3 で反映した項目

| 状態 | タスク | 内容 |
| --- | --- | --- |
| Done | 共有契約の拡張 | `text` media type、`task_type` ルーティング、購読可能な EventBus、排他化した JobQueue（#37） |
| Done | Story Engine | text runtime 3 種、story task 7 種、text quality evaluator（#41 #42 #44） |
| Done | StoryDocument | beats / scenes / chapters の永続化、text 結果のマージ、timeline 導出（#43） |
| Done | Creative Bible | 作品設定レコードと決定的 prompt 合成、監査ログ、衝突検知（#47 #48） |
| Done | Variation Matrix | 軸展開、probe → refine、job repository からの再導出、自動 stage 遷移（#38） |
| Done | 軸カタログ | ロゴ 30 構造 / サムネ 30 構造 / トーン 10 種（#52） |
| Done | API 接続 | `/generate/text`、`/bible`、`/batches`、`/stories`（#51 の API 部分を含む） |

## 次に進めるべきタスク

| 優先度 | 状態 | タスク | 目的 |
| --- | --- | --- | --- |
| P0 | Todo | ナレーション（TTS）と音声後処理 | 台本から音声を作り、動画に載せられる音量に整える（#55 #56） |
| P0 | Todo | timeline assembly | 素材と timeline から音声付き MP4 を書き出す（#58） |
| P0 | Todo | Story surface / Matrix 比較グリッド | 構想と多重生成を UI から回せるようにする（#61 #53） |
| P1 | Todo | 実 LLM の Golden Path 検証 | GGUF を配置し、生成時間・JSON 失敗率・日本語品質を実測する（#45） |
| P1 | Todo | job lane と常駐上限 | text ↔ image の載せ替えと待ち行列の詰まりを解消する（#39） |
| P1 | Todo | 参照画像条件付け | prompt + seed で足りない同一性を補う（#50） |
| P1 | Todo | CogVideoX-2B local smoke | weight配置後に `make cogvideox-smoke` で実MP4を生成しGallery再生を確認する |
| P2 | Todo | 30 パターン probe の実測 | 「10 分以内」が成立するか測り既定値を決める（#54） |
| P2 | Todo | calibration sample収集 | 全体20件、media/model segment各10件以上のhuman feedbackを蓄積する |
| P2 | Todo | calibration review | 相関・MAE・biasを人手レビューし、補正重み変更を別変更として承認する |
| P2 | Todo | running job cooperative cancellation | CogVideoX推論step callbackから安全にcancelできる実行contextを設計する |

## 直近の推奨着手順

1. TTS と音声後処理（#55 #56）を入れ、台本から音声が出る状態にする
2. assembly（#58）を入れ、scene 3 本以上の MP4 を音声付きで書き出す
3. Story surface と Matrix グリッド（#61 #53）で UI から通しで回せるようにする
4. GGUF を配置して Story Engine の実測（#45）を行い、既定パラメータを決める
5. CogVideoX-2B weightを `models/video/cogvideox-2b` に配置して実MP4 smokeを通す
6. 制作時のhuman feedbackを蓄積し、`make calibration-report` で相関を確認する
