# Model System

Creative AI Studio におけるモデル管理システム定義。

## Goal

- モデル定義をコードから分離する
- generator からモデル読み込み詳細を隠蔽する
- image / video / audio へ拡張可能にする
- ローカル環境で安全に切り替え可能にする

## Components

- `ModelManifest`
- `ModelRegistry`
- `ModelResolver`
- `LoaderRegistry`
- `ModelRuntimeCache`
- `ModelService`

## Directory Layout

```text
core/
├─ models/
│  ├─ manifest.py
│  ├─ registry.py
│  ├─ resolver.py
│  ├─ loader.py
│  ├─ readiness.py
│  ├─ cache.py
│  └─ service.py
models/
├─ manifests/
│  ├─ image/
│  │  └─ sdxl-local.json
│  ├─ video/
│  └─ audio/
```

## Flow

1. Generator requests runtime via `ModelService`
2. `ModelResolver` selects a manifest
3. `ModelRuntimeCache` is checked
4. `LoaderRegistry` returns the runtime-specific loader
5. The loader creates the runtime if needed
6. The runtime is cached and returned
7. Generator uses manifest metadata and treats the runtime itself as an opaque handle

## Initial Scope

- ローカル manifest JSON を読み込める
- public `model_id` / alias / internal manifest id、あるいは default から manifest を解決できる
- 最初の image model を `diffusers` runtime loader でロードできる
- 1 件の runtime をキャッシュできる
- 明示的な unload ができる

## Manifest Fields

- `id`: internal manifest id
- `public_id`: `GET /models` が返す public model id。未指定なら `id` を使う
- `display_name`: UI 表示名
- `media_type`: `image | video | audio`
- `task_type`: `text-to-image` などの用途
- `provider`: `local`, `huggingface`, `mlx` などの供給元
- `runtime`: `diffusers`, `transformers`, `mlx` などの実行系
- `family`: `sdxl`, `flux` など loader が pipeline class を選ぶためのモデル系統
- `variant`: `fp16` など明示的に読み込む weight variant。不要なモデルでは省略
- `local_path`: ローカル保存先
- `remote_ref`: 将来の取得元参照
- `loader`: 呼び出す loader 名
- `default_params`: 既定推論パラメータ
- `aliases`: legacy id や互換 id を internal manifest id に解決する追加マッピング
- `is_default`: デフォルト候補かどうか
- `enabled`: 利用可否

## Public IDs And Aliases

- API や generator が送る `model_id` は public model id を基本とする
- resolver は `aliases` / `public_id` を先に解決し、その後に internal manifest id lookup を行う
- alias 解決は model-system layer に閉じ込め、API route / generator / loader には持ち込まない
- 1 つの manifest に対して複数 alias を定義できる
- 同じ public id / alias を複数 manifest へ割り当てることはできない
- `GET /models` は public `id` と internal `internal_id` の両方を返すが、通常の API request では public `id` を使う

例:

```json
{
  "id": "sdxl-local",
  "public_id": "sdxl",
  "aliases": ["sdxl-local"]
}
```

挙動:

```text
resolve("sdxl") -> manifest "sdxl-local"
resolve("sdxl-local") -> manifest "sdxl-local"
```

## Design Rules

1. manifest は宣言のみを持つ
2. generator は `ModelService` だけに依存する
3. runtime 依存は loader に閉じ込める
4. registry と runtime cache を分離する
5. 初期実装では download 管理と高度なメモリ制御は含めない
6. generator は loader の戻り値内部構造に依存しない
7. alias lookup は manifest lookup より前に行い、その順序は resolver が一元管理する
8. API / docs / generator は `GET /models` が返す public `id` をそのまま送る
9. internal manifest id は model-system layer の内部識別子として扱う
10. 「利用可能」の判定は `core/model_readiness.py` だけが持ち、API・loader・script は同じ関数を呼ぶ

## Learned Video Runtime Contract

- `models/video/learned-runtime/runtime.py` は `load_runtime(manifest)` を公開します
- 戻り値は `runtime_adapter`, `pipeline`, `renderer`, `device`, `dtype` を持ちます
- 現行pilotはローカル`THUDM/CogVideoX-2b`とMP4出力だけを対象にします
- `/models`はheavy pipelineをloadせず、adapterと`pipeline_path`のcomponent設定・weight一式の存在だけを確認します
- 確認ルールは `core/model_readiness.py` の共通実装で、adapter load時の事前checkと同一です
- load/generation失敗はjob errorとして明示し、procedural storyboardへ自動fallbackしません

> ⚠️ セキュリティ注意: `LearnedVideoLoader` はモデルディレクトリ内の `runtime.py` / `adapter.py`
> を import して実行します（任意コード実行）。`MODELS_ROOT` 配下には信頼できる出所の
> モデルパックのみを配置し、第三者製・未検証の bundle は読み込まないでください。

## Text Runtime Contract

text 生成の backend は複数ありえます（llama.cpp / ローカル endpoint / weight 無しの雛形）。
generator 側が backend ごとに分岐しないよう、runtime は 1 つの呼び出し規約に正規化します。

```python
runtime["generate"](
    prompt,
    *, system=None, max_tokens=1024, temperature=0.8, top_p=0.95,
    seed=None, json_schema=None,
) -> str
runtime["context_window"]: int
runtime["supports_json_schema"]: bool
```

| loader | 対象 | 依存 | 既定 |
| --- | --- | --- | --- |
| `template_text_loader` | weight 不要の決定的スキャフォルダ | なし | ✅ |
| `llama_cpp_text_loader` | ローカル GGUF（Metal / CUDA offload） | `llama-cpp-python` | optional |
| `openai_compatible_text_loader` | Ollama / LM Studio / vLLM | `httpx` | optional |

### なぜ template runtime が既定なのか

`procedural_video_loader` と同じ考え方です。モデルを 1 つも配置していない状態でも
`logline → beat_sheet → scene_list → assembly` の全経路が動き、テストできます。
実 LLM の導入は「品質の向上」であって「機能の解禁」ではありません。

配置手順:

1. GGUF を `models/text/<model>/` に置く（1 ディレクトリ 1 ファイル、
   複数置く場合は manifest の `default_params.model_file` で選ぶ）
2. `models/manifests/text/qwen-writer-local.json` の `enabled` を `true` にする
3. `pip install llama-cpp-python`（Apple silicon は
   `CMAKE_ARGS="-DGGML_METAL=on" pip install --no-cache-dir llama-cpp-python`）

### endpoint loader の egress ガード

`openai_compatible_text_loader` は既定で loopback（`127.0.0.1` / `localhost` / `::1`）のみ
許可します。それ以外の host は `ALLOW_REMOTE_TEXT_ENDPOINTS=true` を明示しない限り
拒否されます。API key は manifest ではなく `default_params.api_key_env` が指す環境変数から
解決します。解決後の base URL は job metadata に残るため、送信先は常に追跡できます。

## Structured Output Contract

story task は出力 schema を固定しています。`supports_json_schema` が真の runtime には
JSON schema（llama.cpp では grammar）を渡し、そうでない runtime には JSON を要求します。
いずれの場合も generator 側で:

1. コードフェンスや前置き文を許容して JSON を抽出する
2. pydantic で検証する
3. 失敗したら検証エラーを添えて 1 回だけリペアを要求する
4. それでも失敗したら raw 応答をファイルに保存し、task 名と検証エラーを含む例外にする

「LLM は JSON を壊す」ことを前提にした設計であり、壊れた出力が後続の
画像・音声生成へ流れないための境界です。
