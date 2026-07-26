# Multimedia Content Generation Plan (v0.3)

ストーリー構想からショート動画納品までを 1 つの制作フローとして扱うための設計です。
現行 v0.2（image / audio / video の単発生成）を壊さずに拡張することを前提にします。

## 1. この計画で解きたい課題

| # | 課題 | 現状 | v0.3 での解 |
| --- | --- | --- | --- |
| 1 | ストーリー構想・執筆をローカル LLM で行いたい | text メディアが存在しない | `text` media type + Story Engine |
| 2 | キャラクター / 背景の統一感を保ちたい | prompt を毎回手書き、統一手段なし | Creative Bible + 決定的 prompt 合成 |
| 3 | 30 パターン級の多重生成で比較検証したい | 1 リクエスト 1 ジョブのみ | Variation Matrix（軸展開 + 2 段階選抜） |
| 4 | ナレーションと BGM を付けたい | text-to-music のみ | Voice & Score（TTS + music + 後処理） |
| 5 | 短時間で完成動画にしたい | 単一クリップ出力のみ | Assembly（timeline mux → MP4） |

## 2. 全体像

```text
Story Engine (text)            Creative Bible              Variation Matrix
  logline                        character / style            axes = {structure, tone, ...}
  -> beat sheet                  -> prompt fragment           -> N 子ジョブ
  -> scene list                  -> lora / ref / seed         -> 比較 / 選抜 / promote
  -> prose | script                    |                            |
        |                              +--------------+-------------+
        |                                             |
        +--> scene.image_prompt --------------> Image Generator
        +--> scene.narration ----------------> Speech Generator (TTS)
        +--> scene.bgm_mood -----------------> Music Generator
                                                      |
                                                      v
                                              Assembly Generator
                                              (frames + audio mix -> MP4)
```

既存の `GenerationRequest -> Job -> GenerationResult -> Asset -> Project` の流れは変えません。
新概念は次の 4 つだけを追加します。

- `Batch`: 1 つの意図から派生した複数 job の束（多重生成の主語）
- `BibleEntry`: 再利用される作品設定（統一感の主語）
- `StoryDocument`: 構想テキストの構造化ドキュメント（物語の主語）
- `Timeline`: 完成動画の組み立て指示（納品物の主語）

## 3. 共有コントラクトの変更（最小限）

### 3.1 `MediaType` に `text` を追加

```python
MediaType = Literal["image", "video", "audio", "text"]
```

影響範囲: quality evaluator / `/metrics/summary` の `by_media` / gallery filter / UI ラベル / asset preview。

text の出力規約:

- 主出力は `outputs/text/<id>.md`（人が読む形）
- 構造化データは同名 `.json` を sidecar として書き、`metadata.structured_path` で参照
- asset は 1 job 1 件（gallery を汚さない）

### 3.2 `GenerationRequest.task_type` を追加

同じ media type に複数の生成種別が入るため、ルーティング用のキーを request に持たせます。

```python
class GenerationRequest(BaseModel):
    media_type: MediaType
    task_type: str | None = None   # 追加。None は generator の既定 task
    ...
```

`GeneratorRegistry` を `(media_type, task_type)` 解決に拡張し、`task_type=None` は
media type の既定 generator に落とします（後方互換）。

| media_type | task_type | generator |
| --- | --- | --- |
| image | `text-to-image`（既定） | ImageGenerator |
| audio | `text-to-music`（既定） | AudioGenerator |
| audio | `text-to-speech` | SpeechGenerator |
| video | `text-to-video`（既定） | VideoGenerator |
| video | `assembly` | AssemblyGenerator |
| text | `story`（既定） | TextGenerator |

### 3.3 `EventBus` に subscriber を追加

Batch の集計は job の終了イベントで駆動します。現在の `EventBus` は収集のみで
購読手段がなく、かつ無制限にイベントを保持します。

- `subscribe(callback)` / `unsubscribe(callback)` を追加
- 保持数を上限付き ring buffer にする（既存のメモリ増加も同時に解消）
- callback は runner スレッドから呼ばれる。例外は runner を殺さない（log して継続）

### 3.4 `JobQueue` を排他化

lane を複数にすると `if not self._items: return None` → `popleft()` に競合が入ります。
`threading.Lock` で `dequeue` を保護します（single lane のままでも無害）。

## 4. Story Engine（ローカル LLM）

### 4.1 ランタイム層

`ModelManifest.loader` に text 用ローダーを追加します。ランタイムは backend 差を隠す
1 つの呼び出し規約に正規化します。

```python
runtime = {
    "generate": Callable[..., str],   # (prompt, *, system, max_tokens, temperature,
                                      #  top_p, seed, json_schema) -> str
    "context_window": int,
    "device": str,
    "supports_json_schema": bool,
}
```

| loader | 用途 | 依存 | 既定 |
| --- | --- | --- | --- |
| `template_text_loader` | weight 不要の決定的スキャフォルダ | なし | ✅ 既定 |
| `llama_cpp_text_loader` | ローカル GGUF（Metal / CUDA） | `llama-cpp-python` | optional |
| `openai_compatible_text_loader` | Ollama / LM Studio / vLLM | `httpx` | optional |

`template_text_loader` を既定にする理由:

- モデル未配置でも Story → Assembly の全経路が動き、テスト可能である
- `procedural_video_loader` と同じ「weight 無しでも動く基準実装」の思想を踏襲する
- 実 LLM 導入は「品質の向上」であって「機能の解禁」にしない

`openai_compatible_text_loader` は既定で loopback のみ許可し、
`ALLOW_REMOTE_TEXT_ENDPOINTS=true` を明示しない限り外部 host を拒否します
（運用ルール "Local-Only Safe" の維持）。

### 4.2 story task

`params.task` で執筆段階を選びます。構造化出力は JSON schema 制約（llama.cpp grammar が
使える場合）＋ pydantic 検証 ＋ 1 回リトライ、失敗時は raw 出力を保存して actionable に失敗させます。

| task | 入力 | 出力 |
| --- | --- | --- |
| `logline` | premise, genre, audience | logline 候補（多重生成の起点） |
| `beat_sheet` | logline, structure template | beats（3 幕 / 起承転結 / save-the-cat） |
| `scene_list` | beats | scenes（image_prompt, narration, bgm_mood, duration） |
| `prose` | scene, style, POV, tense | 章本文（小説） |
| `script` | scene | ナレーション台本 / 絵コンテ台本 |
| `prompt_pack` | scene or character | 画像 prompt バリエーション（Matrix へ） |
| `character_sheet` | character brief | Bible 登録用フィールド一式 |

### 4.3 `StoryDocument`

`data/stories/<id>.json` に `write_json_atomic` で保存（projects / assets / feedback と同一方式）。

```text
StoryDocument
  id, project_id, title, logline, genre, tone, audience, language, format
  characters: [bible_id | inline]
  beats:    [Beat(id, act, purpose, summary)]
  scenes:   [Scene(id, beat_id, heading, summary, narration, dialogue,
                   image_prompt, image_negative, bgm_mood, duration_seconds,
                   camera, bible_refs, asset_ids)]
  chapters: [Chapter(id, title, prose_markdown, word_count)]
  source_job_ids, metadata, created_at, updated_at
```

`scenes[].asset_ids` が「どの scene がどの素材で埋まったか」を保持し、Assembly の入力になります。

## 5. Creative Bible（統一感の担保）

### 5.1 レコード

```text
BibleEntry
  id, project_id, kind: character | style | brand | location | prop
  name, summary
  prompt_fragment, negative_fragment
  tokens: [str]                    # LoRA trigger など
  attributes: {slot: value}        # hair / eyes / outfit / material ...
  lora: {path, scale} | null
  reference_asset_ids: [str]       # 参照画像（img2img / IP-Adapter）
  seed_policy: {mode: locked|free, seed: int|null}
  palette: [hex]
  tone_and_manner: {...}
  locked_fields: [str]             # 軸展開で上書き禁止のフィールド
```

### 5.2 決定的 prompt 合成

`core/prompting/composer.py` が `PromptSpec -> ComposedPrompt` を担います。

```text
PromptSpec(base_prompt, negative_prompt, bible_refs, axis_values, template, extra_fragments)
  -> ComposedPrompt(prompt, negative_prompt, seed, lora, reference_paths,
                    applied[], conflicts[])
```

要件:

- **決定的**: 同じ入力から常に同じ文字列を生成する（順序を固定: subject → character → location → style → tone → quality tail）
- **監査可能**: 何がどこから入ったかを `metadata.prompt_composition` に残す
- **衝突検知**: `locked_fields` と axis 値がぶつかったら `conflicts` に記録し UI で警告する
- **保存は宣言的**: job の request には bible_refs と axis を保存し、解決結果は metadata に残す
  （bible 更新後の再生成は意図的に再解決する。固定したい場合は `params.freeze_composed_prompt=true`）

合成は generator 側の共通ヘルパ（`generators/common/prompting.py`）に置き、
image / video / text から同一経路で呼びます。

### 5.3 同一性ロックの段階

| 段階 | 手段 | 精度 | コスト |
| --- | --- | --- | --- |
| L1 | prompt fragment + attributes 固定 | 低〜中 | ゼロ |
| L2 | + seed lock + 同一モデル / step / cfg 固定 | 中 | ゼロ |
| L3 | + character LoRA | 高 | 学習が必要 |
| L4 | + 参照画像条件付け（img2img / IP-Adapter） | 高 | 実装 + weight |

v0.3 は L1 / L2 を必須、L3 は既存 LoRA 機構の再利用、L4 を独立課題にします。

## 6. Variation Matrix（多重生成）

### 6.1 レコード

```text
BatchSpec
  name, project_id, media_type, task_type, model_id
  base_request {prompt, negative_prompt, params, seed}
  axes: [Axis(name, values: [AxisValue(label, patch)])]   # patch は request への deep merge
  strategy: grid | sample(max_items, seed)
  stages: [Stage(name, param_overrides, keep_top_n)]      # probe -> refine
  bible_refs, seed_policy: shared | per_item | sweep
  limit (既定 64 / env で上限管理)

BatchRecord
  id, spec, status, stage_index
  items: [BatchItem(id, index, label, axis_values, job_id, status, score, promoted)]
  aggregate {succeeded, failed, avg_score, best_item_id}
```

### 6.2 2 段階選抜（現実的な所要時間にするため）

M1 Max で SDXL 1024px / 30 steps は 1 枚あたり数十秒です。30 枚を全力生成すると
20〜30 分かかり「思考の速度」を失います。したがって既定を 2 段階にします。

| stage | 解像度 / steps | 件数 | 目的 |
| --- | --- | --- | --- |
| probe | 640px / 14 steps | 30 | 構造とトーンの当たり判定 |
| refine | 1024px / 34 steps | top 6 | 納品候補の作り込み |

同一 batch 内の子ジョブは `model_id` でグルーピングして enqueue し、
runtime cache の載せ替え（thrash）を避けます。

### 6.3 状態遷移と集計

- 子ジョブ生成時に `BatchItem.job_id` を確定
- `EventBus` の `job_succeeded` / `job_failed` / `job_cancelled` を購読して item を更新
- 読み取り時は job repository から再導出（プロセス再起動後も整合する）
- stage の全 item が terminal になったら次 stage を materialize（`POST /batches/{id}/advance` でも手動可）
- 一部失敗は `partial` として扱い、成功分の比較は継続できる

### 6.4 Brand Lab（ロゴ / サムネの 30 パターン検証）

`core/prompting/patterns/` にデータとして持つカタログ:

- `logo_structures`: 30 件（wordmark centered / stacked lockup / monogram in shield /
  circular badge / negative-space mark / geometric abstract / mascot bust / ...）
  各件が `prompt_fragment` と `negative_fragment`、推奨アスペクトを持つ
- `thumbnail_structures`: 30 件（left-face right-text / big-number / before-after split /
  arrow-focus / question-overlay / ...）
- `tone_and_manner`: minimal / premium / playful / technical / retro / organic / editorial /
  neon / hand-drawn / corporate（palette + typography hint + negative fragment 付き）

これを軸に組むと「30 構造 × 1 トーン」「6 構造 × 5 トーン」等が 1 リクエストで表現できます。
比較 UI は既存の gallery / feedback を再利用し、quick rating と heuristic score を並べます。

## 7. Voice & Score

### 7.1 TTS

`task_type=text-to-speech`。ランタイム規約:

```python
runtime = {"synthesize": Callable[..., tuple[ndarray, int]], "voices": [str], "device": str}
```

| loader | 対象 | 備考 |
| --- | --- | --- |
| `kokoro_tts_loader` | ローカル軽量 TTS（JA / EN） | pip 導入のみ、サーバ不要 |
| `voicevox_http_loader` | ローカル VOICEVOX engine | loopback 限定、日本語品質重視 |

### 7.2 音声後処理（music / speech 共通）

`core/audio/postprocess.py`（numpy のみ、新規依存なし）:

- ピーク / RMS 正規化とターゲットラウドネス寄せ
- 無音トリム、フェード in/out
- ナレーション優先のダッキング（music を -12dB 程度へ）
- 30 秒超の music は窓連結 + クロスフェード（既存 issue #28 と連携）

### 7.3 クラウド provider

`provider: "cloud"` の manifest を許容する seam のみ用意し、既定は無効。
`ALLOW_CLOUD_PROVIDERS=true` と provider ごとの明示設定がない限りリクエストしません。
「どのデータが外に出るか」を manifest とレスポンス metadata に必ず残します。

## 8. Assembly（完成動画）

`task_type=assembly`、出力 MP4。新しいシステム依存は追加しません
（`imageio-ffmpeg` 同梱の ffmpeg を `get_ffmpeg_exe()` で使用）。

```json
{
  "task": "assembly",
  "timeline": {
    "resolution": [1920, 1080],
    "fps": 30,
    "tracks": {
      "visual": [{"asset_id": "...", "duration_seconds": 3.5,
                  "transition": "crossfade", "motion": "ken_burns_in"}],
      "narration": [{"asset_id": "...", "start_seconds": 0.0}],
      "music": [{"asset_id": "...", "gain_db": -14, "loop": true, "duck": true}],
      "subtitles": [{"text": "...", "start_seconds": 0.0, "end_seconds": 3.5}]
    }
  }
}
```

実装方針:

- 映像: PIL で crop / resize して Ken Burns を作り、imageio で書き出す
- 字幕: PIL でフレームに焼き込む（ffmpeg の libass 依存を持ち込まない）
- 音声: numpy でミックス（narration + ducked music）して wav を書く
- 多重化: 同梱 ffmpeg で映像 + 音声 → MP4（faststart）
- 品質: 既存 `evaluate_video_output` を通し、追加で音声トラック有無と尺一致を検証

`POST /assemble/story/{story_id}` で StoryDocument の scene から timeline を自動生成します
（scene 順 / duration / narration / bgm を割り当て、未生成 scene は明示エラー）。

## 9. 実行基盤（lane と常駐モデル）

- lane を導入: `heavy`（image / video / music）と `light`（text / tts / assembly）
  - `JOB_LANES=heavy:1,light:1` を既定
  - assembly は CPU 主体なので、次の画像生成と重ねられる実益がある
- `MAX_CACHED_MODELS=1` のままだと story pipeline で text ↔ image の載せ替えが頻発する
  - media 別の常駐上限（`MAX_CACHED_MODELS_TEXT` など）とメモリ予算の明文化を行う
- 複数 lane を有効化する前提として `JobQueue` の排他化が必須（3.4）

## 10. API 追加

| メソッド | パス | 用途 |
| --- | --- | --- |
| `POST` | `/generate/text` | story task の実行 |
| `POST` | `/generate/speech` | ナレーション合成 |
| `POST` | `/generate/assembly` | timeline から MP4 生成 |
| `POST` | `/batches` | 多重生成の作成 + 起動 |
| `GET` | `/batches` / `/batches/{id}` | 進捗 / item 一覧 / スコア |
| `POST` | `/batches/{id}/advance` | 次 stage を materialize |
| `POST` | `/batches/{id}/cancel` | 一括キャンセル |
| `POST` | `/batches/{id}/items/{item_id}/promote` | 勝者確定（refine / bible 反映） |
| `GET` | `/batches/templates` | preset（logo-30 / thumbnail-ab / character-sheet ...） |
| `GET`/`POST`/`PATCH`/`DELETE` | `/bible` | Creative Bible CRUD |
| `POST` | `/bible/preview` | prompt 合成のドライラン |
| `GET`/`POST`/`PATCH` | `/stories` | StoryDocument CRUD |
| `POST` | `/stories/{id}/expand` | 次段階の text job を起動 |
| `POST` | `/assemble/story/{id}` | story → timeline → MP4 |

## 11. UI 追加（既存デザイントークンの範囲で）

- **Story surface**: premise 入力 → logline 候補 → beat → scene 表を段階的に埋める。各行から画像 / 音声生成へ。
- **Matrix surface**: 軸エディタ（preset 選択 + 値のトグル）、進捗、比較グリッド（スコア / quick rating / promote）。
- **Assembly panel**: scene 順の timeline、尺、narration / BGM の割り当て、書き出し。

`AGENTS.md` の UI レビュー要件（390 / 768 / 1280 / 1440px、キーボード / ARIA、
loading / empty / error / long-text、frontend test + production build）を各 PR で満たします。

## 12. フェーズ計画

| Phase | 内容 | 完了条件 |
| --- | --- | --- |
| P0 | 共有コントラクト（text / task_type / event / queue） | 既存 98 テストが緑のまま新契約が入る |
| P1 | Story Engine（loader / generator / StoryDocument / API） | weight 無しで logline→scene まで通る |
| P2 | Creative Bible + prompt 合成 | 同一 bible で 3 枚生成し属性の一致を確認できる |
| P3 | Variation Matrix + Brand Lab | 30 パターンの probe→refine が 1 リクエストで回る |
| P4 | Voice & Score | ナレーション + BGM が正規化済み wav で出る |
| P5 | Assembly | scene 3 本以上の MP4 が音声付きで出る |
| P6 | Story→Video ワンショット | premise 入力から MP4 までを 1 導線で完了できる |

## 13. 成果判定（Definition of Done）

- premise 1 行から、ナレーションと BGM の付いた 60 秒 MP4 を **30 分以内** に出せる
- 同一 bible / seed で生成したキャラクターが 3 カット以上で同一と判断できる
- ロゴ / サムネの 30 パターン probe が **10 分以内** に完了し、比較 UI で選抜できる
- ローカル既定でネットワーク送信が発生しない（cloud は明示 opt-in のみ）
- すべての出力が「実行時パラメータ + bible + 軸値」から再現できる

## 14. リスクと対策

| リスク | 対策 |
| --- | --- |
| 30 パターン生成が遅すぎて使えない | probe / refine の 2 段階を既定にし、probe を低解像度・低 step に固定 |
| LLM の JSON 出力が壊れる | schema 制約 + pydantic 検証 + 1 リトライ + raw 保存で actionable に失敗 |
| text ↔ image の載せ替えでメモリが枯れる | lane 分離 + media 別常駐上限 + 明示的な unload |
| キャラの同一性が prompt だけでは足りない | L1〜L4 を段階化し、L4（参照画像条件付け）を独立課題として持つ |
| 生成物が増えて gallery が読めなくなる | text は 1 job 1 asset、batch は batch 単位で束ねて表示 |
| cloud 連携で意図せず外部送信 | 既定無効 + loopback 限定 + 送信内容の metadata 記録 |
