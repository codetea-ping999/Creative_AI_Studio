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
| Done | Scene 素材紐付け | job 成功イベントから `SceneBinder`（`core/story/binding.py`）が scene に画像 / ナレーション / BGM を自動紐付け（#43 残分） |
| Done | ナレーション（TTS） | `SpeechGenerator`（`generators/audio/speech.py`）+ `kokoro_tts_loader` / `voicevox_http_loader`（`core/models/loader.py`）。`voicevox-endpoint` manifest が既定 enabled、`POST /generate/speech` まで接続（#55） |
| Done | timeline assembly（ワンショット） | `core/story/timeline.py`の`build_timeline`と`generators/video/assembly.py`の`AssemblyGenerator`が字幕焼き込み・narration/music ミックス（ducking）・ffmpeg mux で音声付き MP4 を出力。`POST /assemble/story/{id}` と Studio UI（`StoryPanel.tsx`）から起動可能（#58） |
| Done | Story surface / Matrix 比較グリッド | `apps/web/src/components/StoryPanel.tsx`で premise→logline→beat→scene→各素材生成→assemble を1画面で実行、`MatrixPanel.tsx`で probe→refine の比較・promote を実行（#61 #53） |

## 次に進めるべきタスク

コードで確認した実態をもとに優先度を見直しました。判定の根拠は本 PR の説明を参照してください。

| 優先度 | 状態 | タスク | 目的 |
| --- | --- | --- | --- |
| P0 | Todo | job lane と media 別常駐上限 | `bootstrap/factories.py`は単一 `JobQueue`/`JobRunner`、`MAX_CACHED_MODELS`もmedia別ではない単一値。text↔image↔audioを1フローで回すと載せ替えが頻発する（#39） |
| P0 | Done | 音声後処理を music 生成経路にも適用 | `generators/audio/generator.py`のmusic経路（短尺・長尺とも）に`process_audio(..., preset=MUSIC_PRESET)`を接続。`params.postprocess`（既定true）で無効化も可能にし、`speech.py`側にも同じ無効化オプションを追加。結果は`audio_postprocess`に`enabled`/`chain`付きで記録（#56 残分） |
| P1 | Todo | video 生成の cooperative cancellation 接続 | `core/jobs/context.py`の`GenerationContext`と`core/jobs/cancellation.py`は実装済みで image generator は`raise_if_cancelled()`を複数箇所で呼ぶが、`generators/video/generator.py`は`context`引数を受け取るのみで内部では未使用（#18） |
| P1 | Todo | バッチ勝者の Bible 反映 | `core/batches/service.py`の`promote()`は`item.promoted=True`を立てるだけで bible への書き込みがない。character sheet batch template（`core/batches/templates.py`）自体は存在する（#49） |
| P1 | Todo | 参照画像条件付け（identity lock L4） | `core/prompting/composer.py`は`reference_asset_ids`を収集するが、`generators/image/generator.py`はmetadataに記録するのみで img2img / IP-Adapter のpixel conditioning呼び出しがない（#50） |
| P2 | Todo | assembly timeline panel UI | scene順・duration・narration/BGM割り当てを手動編集できるUIが無い。現状は自動生成timelineでの一発assembleボタンのみ（`StoryPanel.tsx`）（#62） |
| P2 | Todo | batch-aware gallery grouping | `apps/api/routes/gallery.py`・gallery UIにbatch単位のグルーピングが無い（#40） |
| P2 | Todo | 字幕のプラットフォーム別セーフエリア | 字幕焼き込み自体（`_burn_subtitles`）は実装済みだが、9:16等のsafe-area presetが無い（#60 残分） |
| P2 | Todo | クラウド provider adapter（voice / image） | `ALLOW_CLOUD_PROVIDERS`相当の実装がコードベースに存在しない。voice（#57）・image（#66）とも未着手 |
| P2 | Todo | Visual Orchestrator | scene単位で複数カットを合成する仕組み（Orchestratorクラス）が存在しない（#65） |
| P2 | Todo | novel-length continuity memory | `generators/text/tasks.py`の`_prose_prompt`は単一sceneのみを入力にしており、前章の継続情報を渡す仕組みがない（#46） |
| P2 | Todo | calibration sample収集 | 全体20件、media/model segment各10件以上のhuman feedbackを蓄積する（#19） |
| P2 | Todo | calibration review | 相関・MAE・biasを人手レビューし、補正重み変更を別変更として承認する（#20） |

### ローカル実機が必要（クラウド実行環境では検証不可）

| 状態 | タスク | 根拠 |
| --- | --- | --- |
| Todo | 実 LLM の Golden Path 検証（#45） | `LlamaCppTextLoader`（`core/models/loader.py`）は実装済みだが`models/manifests/text/qwen-writer-local.json`は`"enabled": false`。GGUF weight とGPUが無いと生成時間・JSON失敗率・日本語品質を測れず、`docs/`にも実測記録が無い |
| Todo | 30 パターン probe の実測（#54） | `core/batches/expansion.py`のprobe→refine実装自体はあるが、実GPUでのスループット実測値が`docs/`に記録されていない。「10分以内」の既定値はまだ未検証 |
| Todo | CogVideoX-2B local smoke | weight配置後に `make cogvideox-smoke` で実MP4を生成しGallery再生を確認する（GPU / weight必須） |

## 直近の推奨着手順

> **この節の順序は [issue-execution-plan.md](./issue-execution-plan.md) に置き換えられました。**
> 以下は v0.3 トラック内での順序であり、リポジトリ全体の着手順ではありません。
> 全体順序（セキュリティ勧告の解消 #87 と CI ゲート #83 を先行させる Phase 0）は
> 実行計画のほうを参照してください。この節は v0.3 の内訳として残しています。

1. job lane と media 別常駐上限（#39）を入れ、Story→画像→音声→動画の一気通貫フローでモデル載せ替えが詰まらないようにする
2. 音声後処理を music 経路にも適用し（#56 残分）、video 側の cancellation 接続（#18）を仕上げる — どちらも配線のみで規模が小さい
3. バッチ勝者の Bible 反映（#49）と参照画像条件付け（#50）でキャラクター同一性のループを閉じる
4. assembly timeline panel（#62）と batch-aware gallery（#40）でUIの手動編集・閲覧性を上げる
5. ローカル実機（GPU + weight）が用意でき次第、GGUF Golden Path 実測（#45）、30パターン probe 実測（#54）、CogVideoX-2B smoke を行い既定パラメータを決める
6. 制作時のhuman feedbackを蓄積し（#19）、`make calibration-report` で相関を確認する（#20）

## v0.4 候補 — Creative 3D / Game Pipeline

Tracking Epic: [#380](https://github.com/codetea-ping999/Creative_AI_Studio/issues/380)  
Detailed roadmap: [creative-3d-roadmap.md](./creative-3d-roadmap.md)

Creative AI StudioをImage / Video / Audioから **3D Asset / Interactive production** へ拡張する将来トラック。
製品UXはStudioへ統合するが、Blender / Unity本体はoptional executor / destinationとして分離する。

| 順 | 状態 | タスク | 目的 |
| --- | --- | --- | --- |
| 1 | Future | 3D core contract | `3d` media type、Job/result schema、GLB output、3D asset metadataを既存Coreへ追加 |
| 2 | Future | Blender executor MVP | headless Blenderでvalidation / cleanup / normalization / preview / GLB exportをJob化 |
| 3 | Future | Creative Package | prompt・reference・raw/optimized mesh・texture・preview・provenanceをmanifestで束ねる |
| 4 | Future | AI 3D generator adapter | Hunyuan3D等のlocal runtimeを接続し、生成→Blender後処理まで一気通貫化 |
| 5 | Future | Unity destination | Unity batchmode + Editor C#でGLB import、Material、Collider、Prefab、Scene/Buildを自動化 |
| 6 | Future | Agent-driven asset QA | mesh/texture budget、破損検証、preview QA、Issue→Generate→Validate→Export→Testループ |

**着手順の原則:** Unityから始めない。まず3D contractとBlender executorで再現可能なCreative Packageを安定生成し、その成果物をUnityや将来のUnreal / Godot / Three.jsへ渡す。

このトラックは現行v0.3 / reliability作業を置き換えない。既存のJob lane、reference conditioning、quality/release gateが安定した後にPhase 1へ入る。
