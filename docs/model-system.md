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

## Runtime Cache Residency（Issue #182）

`ModelRuntimeCache` は既定で 1 つの共有バジェットしか持ちません
（`max_entries`、既定値は `.env` の `MAX_CACHED_MODELS=1`）。media type ごとに
独立したバジェットを与えたい場合は `media_limits` を渡します。

```python
cache = ModelRuntimeCache(
    max_entries=1,
    media_limits={"text": 1, "image": 1},
    on_evict=release_runtime,
)
```

- `media_limits` に載っている media type は、その media type だけの独立した
  bucket として扱われる。bucket 内の追い出しは
  least-recently-used（最も長くアクセスされていない entry から）で決定的
- `media_limits` に載っていない media type（あるいは `put()` に
  `media_type` を渡さない呼び出し）は共有の default bucket に入り、
  `max_entries` を上限とする。これは #182 以前と完全に同じ挙動であり、
  「per-media 設定が無ければ既存挙動のまま」という後方互換の要件を満たす
- `ModelService.resolve_runtime()` は解決済みの `media_type` を
  `runtime_cache.put(manifest.id, runtime_obj, media_type=media_type)` へ
  そのまま渡す。generator 側の呼び出しは変更不要
- `core.models.cache.resolve_media_cache_limits(env)` が
  `MAX_CACHED_MODELS_{MEDIA}`（`MAX_CACHED_MODELS_TEXT` /
  `MAX_CACHED_MODELS_IMAGE` / `MAX_CACHED_MODELS_AUDIO` /
  `MAX_CACHED_MODELS_VIDEO`）を読み、`media_limits` に渡す dict を組み立てる。
  値が未設定・非整数・1 未満の media type は結果から除外され、その media
  type は default bucket に留まる

設定手順・推奨メモリバジェットは `docs/configuration.md` の
「per-media runtime cache（#182）」節を参照してください。

## Runtime Safety Core（Issue #414, PR4a）

`ModelRuntimeCache` は canonical id（`manifest.id`）ごとに `RuntimeEntry` を
1 つ持ちます。`runtime` オブジェクトそのものと、状態・pin 数・実行排他 lock
を分離したことで、「同じ runtime を 2 箇所が同時に使っている」「pin 中の
runtime が evict される」といった race を型として塞いでいます。

### RuntimeEntry の state

| state | 意味 |
| --- | --- |
| `LOADING` | slot は予約済みだが `runtime` は未公開。取得不可 |
| `READY` | 通常の cached runtime。pin 済みでのみ handle として公開される |
| `INVALID` | 新規 caller へは公開しない。既存 lease の unwind 待ち |
| `RETIRING` | cleanup owner が確定済み。新規 acquire/load/replace 不可 |

### 2 系統の API

- 既存の `get()` / `put()` / `unload()` / `unload_all()` / `resolve_runtime()`
  / `get_runtime()` は外部からの挙動を変えていません（PR4a はキャッシュの
  内部表現を `RuntimeEntry` に統一しただけ）。ただし **lease を取らない**
  ため concurrency-safe ではなく、legacy / transitional API です。PR4b で
  generator 側をすべて `acquire_runtime()` へ移行するまでの互換維持用と
  位置づけます。新規呼び出しをここへ追加しないでください
- 新しい `ModelService.acquire_runtime(model_id, media_type, task_type=None,
  *, timeout=None) -> RuntimeHandle` が安全な取得経路です

```python
with model_service.acquire_runtime(model_id, media_type, task_type) as handle:
    manifest = handle.manifest
    runtime = handle.runtime
    ...  # generate
```

`RuntimeHandle` は「取得している間、この canonical entry を排他的に使える」
ことを保証します。同じ entry を 2 つの caller が同時に取得しても、実行は
必ず直列化されます（fake runtime を使った決定的 concurrency test は
`tests/test_runtime_safety_core.py` を参照）。

### lock / admission の順序

- `G`：processごとに1つだけのadmission semaphore（既定capacity=1）。
  「重いruntimeのload」と「実行」はPR4aでは同じ1枠を共有します
- `L`：canonical idごとのload lock（`ModelRuntimeCache.lock_for()`。
  PR4a以前から存在するlockをそのまま再利用）
- `E`：`RuntimeEntry.execution_lock`。1entryにつき1つ、排他実行を保証
- `M`：`ModelRuntimeCache._metadata_lock`。entryのstate・lease数・
  LRU順序だけを守る、短命なlock

取得順は常に`G -> L -> E`。`M`は単独の短いprobeとしてのみ使い、
G/L/Eを待っている間は絶対に保持しません（loader.load()やcleanup
callbackをM保持中に呼ぶことも禁止）。解放順は`E -> M`（lease減算・
state更新）`-> G`です。この順序が崩れない限り、この4資源の組み合わせで
自己deadlockは起きません。

### capacityはloadの前に確保する

cache miss時、`loader.load()`を呼ぶ前に:

1. 既存READY entryがあればpinして終了（hit）
2. なければbudgetを確認し、unleasedなvictimを1つ選ぶ
3. victimが無くcapacityが必要なら即座に`RuntimeBusyError`
   （`loader.load()`は0回、victim cleanupも0回、無期限waitはしない）
4. victimを`RETIRING`にしてMを解放し、cleanupをMの外で実行
5. LOADINGの予約を作ってから`loader.load()`を呼ぶ
6. 成功したらREADY化と最初のpinを同じM区間で行う。失敗したら
   予約を破棄し、G/L/leaseを一切残さない

新旧runtimeが同時にメモリ上へ存在する時間を作らないための設計です。

### unloadのcontract

- `ModelService.unload_model(model_id)`はpublic id・alias・manifest id
  のどれを渡してもresolverを経由し、同じcanonical entryを対象にします
  （PR4a以前はaliasを渡すとcache keyが一致せず無言でno-opしていた
  bugの修正でもあります）
- 対象がleased・loading・retiringなら`RuntimeBusyError`。runtimeは
  一切変更されません。未loadなら安全なno-op
- `unload_all()`はatomic preflight：1つでもbusyなentryがあれば
  全体を`RuntimeBusyError`とし、状態変更もcleanup呼び出しも0件。
  全entryがidleと確認できてはじめて、Mの同じ区間内で一括
  `RETIRING`化し、M解放後にcleanupします（cleanup自体はbest-effort
  のままで、rollback保証ではありません）

### 未解決のscope（PR4b）

productionの5generator移行、semantic classifier、legacy raw-runtime
APIの利用制限、generator側のcancellation/error統合、PR3との
end-to-end回帰確認はPR4bのscopeです。PR4a完了時点でも
production WorkerPoolと`JOB_LANES`のproduction活用は無効のままです。

## Image Provider Credentials（Issue #257）

`generators/image/providers.py` はクラウド image provider の credential を、
text runtime の `api_key_env` 規約（`core/models/text_runtimes.py`）と同じ
方法で扱います。manifest には credential の値そのものではなく、値を保持する
**環境変数名**だけを書きます。

```json
{
  "provider": "cloud",
  "default_params": {
    "api_key_env": "ACME_IMAGE_API_KEY"
  }
}
```

- `core.models.manifest.reject_literal_credential_fields` が
  `ModelManifest` の parse 時点（`model_validator`）で `default_params` を
  再帰的に検査し、`api_key` / `secret` / `token` / `password` /
  `credential` などの名前に literal な値が入っていたら
  manifest 自体を拒否します（`*_env` で終わるキーは環境変数名の参照なので
  除外されます）。これにより、リポジトリへ commit された manifest に
  secret が紛れ込むことは構造的にありません
- `generators.image.providers.resolve_image_provider_credential` が
  実際の値を解決します。ローカル provider（`is_local_image_provider`）は
  常に `None` を返す（credential 不要）。リモート provider で
  `api_key_env` が manifest に無ければ同じく `None`。`api_key_env` は
  あるが対応する環境変数が未設定/空なら
  `ImageProviderCredentialUnavailableError` を投げる。メッセージは
  「どの環境変数を設定すべきか」だけを含み、値は決して含まない
- `redact_secrets` / `redact_provider_error` / `redact_provider_metadata`
  が、解決済み credential の値をエラーメッセージ・diagnostics dict から
  除去する。実際の transport 呼び出し（sibling issue、未実装）は、
  provider から返ってきたエラーやメタデータをログ・job/asset metadata へ
  渡す前に必ずこれらを通すこと
- `ImageProviderCredential.__repr__` は `value` を含まない。`.value` を
  request の auth header/param 以外（`ImageGenerationSpec.extra_params` や
  `ImageProviderResult.metadata` など、job/asset/request へ永続化されうる
  フィールド）へ絶対に置かないこと

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
