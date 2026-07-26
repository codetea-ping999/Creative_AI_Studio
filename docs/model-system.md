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
