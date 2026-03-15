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

## 次に進めるべきタスク

| 優先度 | 状態 | タスク | 目的 |
| --- | --- | --- | --- |
| P1 | Todo | gallery export / reuse | gallery から再生成・書き出し・素材転用をしやすくする |
| P1 | Todo | semantic quality judge の実運用化 | local model 配置、cache 戦略、judge UX を整備する |
| P2 | Todo | Project 単位の整理強化 | project metadata、asset export、検索を追加する |
| P2 | Todo | learned text-to-video runtime | procedural runtime を実モデルベース video へ置き換える |
| P2 | Todo | human feedback loop | 採点結果に対する user feedback を保存して quality judge を補正する |
| P2 | Todo | video/audio semantic judge | media ごとに judge model を追加し creative blend を改善する |
| P2 | Todo | API smoke の拡張 | `/health` と `/models` に加えて代表的な create/list フローも自動検証する |
| P3 | Todo | package task runner | `make verify` や npm script で root からより短い検証導線を提供する |

## 直近の推奨着手順

1. gallery から export / reuse / re-run を行える導線を追加する
2. VLM / audio encoder を使った semantic judge を quality report に追加する
3. Project / Asset 単位の保存構造を強化して studio らしい管理単位を作る
4. procedural video を learned runtime に差し替えられる loader 構成を整える
