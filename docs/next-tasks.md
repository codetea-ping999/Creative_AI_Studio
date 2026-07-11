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

## 次に進めるべきタスク

| 優先度 | 状態 | タスク | 目的 |
| --- | --- | --- | --- |
| P1 | Todo | CogVideoX-2B local smoke | weight配置後に `make cogvideox-smoke` で実MP4を生成しGallery再生を確認する |
| P2 | Todo | calibration sample収集 | 全体20件、media/model segment各10件以上のhuman feedbackを蓄積する |
| P2 | Todo | calibration review | 相関・MAE・biasを人手レビューし、補正重み変更を別変更として承認する |
| P2 | Todo | running job cooperative cancellation | CogVideoX推論step callbackから安全にcancelできる実行contextを設計する |

## 直近の推奨着手順

1. CogVideoX-2B weightを `models/video/cogvideox-2b` に配置して実MP4 smokeを通す
2. 制作時のhuman feedbackを蓄積し、`make calibration-report` で相関を確認する
3. 十分なsample数を満たした後、補正重み変更を独立レビューする
