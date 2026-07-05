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

## 次に進めるべきタスク

| 優先度 | 状態 | タスク | 目的 |
| --- | --- | --- | --- |
| P2 | Todo | Project 単位の整理強化 | project metadata、asset export、検索を追加する |
| P2 | Todo | learned text-to-video runtime | procedural runtime を実モデルベース video へ置き換える |
| P2 | Todo | human feedback loop | 採点結果に対する user feedback を保存して quality judge を補正する |
| P2 | Todo | semantic judge calibration dataset | local judge score と human feedback の相関を蓄積して閾値を調整する |

## 直近の推奨着手順

1. Project / Asset 単位の保存構造を強化して studio らしい管理単位を作る
2. procedural video を learned runtime に差し替えられる loader 構成を整える
3. semantic judge と human feedback の相関を見て閾値を調整する
