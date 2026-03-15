# Model System Codex Tasks

モデル管理システムを小さい単位で進めるための Codex タスク文。

## task-011

**Implement ModelManifest schema**

- Scope: `core/models/manifest.py`
- Acceptance:
- `ModelManifest` が `pydantic` で定義されている
- `local_path` または `remote_ref` が必須
- `default_params` と `tags` が安全な default を持つ

## task-012

**Implement ModelRegistry**

- Scope: `core/models/registry.py`
- Acceptance:
- `models/manifests/**/*.json` をロードできる
- `id` lookup と filter API がある
- duplicate `id` を検知できる

## task-013

**Implement ModelResolver**

- Scope: `core/models/resolver.py`
- Acceptance:
- `model_id` 指定時に正しい manifest を返す
- `model_id` 未指定時に default を返す
- media/task mismatch を拒否する

## task-014

**Implement LoaderRegistry and BaseModelLoader**

- Scope: `core/models/loader.py`
- Acceptance:
- loader 抽象がある
- loader 名で register / get できる
- 初期 loader registry を組み立てられる

## task-015

**Implement ModelRuntimeCache**

- Scope: `core/models/cache.py`
- Acceptance:
- runtime を再利用できる
- `unload` と `unload_all` がある
- 初期版は 1 モデル常駐を前提にする

## task-016

**Implement ModelService**

- Scope: `core/models/service.py`
- Acceptance:
- resolver, loader, cache を統合する
- `get_runtime()` で cache hit / miss を扱える
- generator から利用できる API になる

## task-017

**Add first image model manifest JSON**

- Scope: `models/manifests/image/sdxl-local.json`
- Acceptance:
- `sdxl-local` manifest が追加されている
- `text-to-image` 用の default params を持つ
- 初期 loader 名と整合している

## task-018

**Implement DiffusersImageLoader stub**

- Scope: `core/models/loader.py`
- Acceptance:
- manifest を受けて stub runtime を返す
- 実 pipeline 未導入でも service から呼べる
- 将来の diffusers 実装に置換しやすい構造になっている

## task-019

**Integrate ImageGenerator with ModelService**

- Scope: `generators/image/generator.py`, `core/models/service.py`
- Acceptance:
- `ImageGenerator` が `ModelService` を受け取れる
- image request で `media_type="image"` / `task_type="text-to-image"` が service に渡る
- stub loader のまま end-to-end で runtime 解決と生成が通る
