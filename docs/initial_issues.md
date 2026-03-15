# 初期 Issue 一覧

## Milestone 1: Bootstrap

### Issue 1

**Initialize repository structure**

受け入れ条件:

* `apps/`, `core/`, `generators/`, `docs/`, `tests/` が作成されている
* README が存在する

### Issue 2

**Add architecture and domain docs**

受け入れ条件:

* `docs/architecture.md`
* `docs/domain-model.md`
* `docs/api-contract.md`

### Issue 3

**Implement generation schemas**

受け入れ条件:

* `GenerationRequest`
* `GenerationResult`
* 型定義完了

### Issue 4

**Implement job schemas**

受け入れ条件:

* `JobRecord`
* `JobStatus`

### Issue 5

**Implement model manifest schema**

受け入れ条件:

* `ModelManifest`
* 基本バリデーション

## Model System Track

### Issue MS-01

**Implement model registry**

受け入れ条件:

* `models/manifests/**/*.json` をロードできる
* `id` で manifest を取得できる
* `media_type` / `task_type` で絞り込める

### Issue MS-02

**Implement model resolver**

受け入れ条件:

* `model_id` から manifest を解決できる
* default model fallback がある
* alias の注入余地がある

### Issue MS-03

**Implement loader registry**

受け入れ条件:

* loader の register / get ができる
* loader 名と manifest の接続ができる

### Issue MS-04

**Implement diffusers image loader stub**

受け入れ条件:

* `diffusers_image_loader` が追加される
* 実 pipeline 未導入でも呼び出せる

### Issue MS-05

**Implement runtime cache**

受け入れ条件:

* 再ロードを防げる
* `unload` / `unload_all` がある
* 初期版は 1 モデル常駐を前提にする

### Issue MS-06

**Implement model service facade**

受け入れ条件:

* generator が service 経由で runtime を取得できる
* registry / resolver / loader / cache を直接知らなくてよい

### Issue MS-07

**Add first image manifest**

受け入れ条件:

* `sdxl-local` manifest が追加される
* `text-to-image` default params を持つ

### Issue MS-08

**Add unit tests for model system skeleton**

受け入れ条件:

* registry / resolver / cache / service の最低限テストがある

---

## Milestone 2: Core / Storage

### Issue 6

**Set up SQLite storage**

### Issue 7

**Implement job repository**

### Issue 8

**Implement project repository**

### Issue 9

**Implement asset repository**

### Issue 10

**Implement minimal event bus**

---

## Milestone 3: API

### Issue 11

**Bootstrap FastAPI app**

### Issue 12

**Add health endpoint**

### Issue 13

**Add job create endpoint**

### Issue 14

**Add job detail endpoint**

### Issue 15

**Add project CRUD endpoints**

### Issue 16

**Add model list endpoint**

---

## Milestone 4: Generator

### Issue 17

**Implement BaseGenerator interface**

### Issue 18

**Implement ImageGenerator stub**

### Issue 19

**Add dummy image generation flow**

### Issue 20

**Integrate text-to-image pipeline**

### Issue 21

**Implement image saving and thumbnail generation**

### Issue 22

**Implement image-to-image pipeline**

---

## Milestone 5: UI

### Issue 23

**Bootstrap web UI**

### Issue 24

**Implement app shell layout**

### Issue 25

**Implement prompt form**

### Issue 26

**Implement job status panel**

### Issue 27

**Implement output gallery**

### Issue 28

**Implement history panel**

### Issue 29

**Implement project sidebar**

---

## Milestone 6: Stabilization

### Issue 30

**Add schema tests**

### Issue 31

**Add repository tests**

### Issue 32

**Add API tests**

### Issue 33

**Add generator mock tests**

### Issue 34

**Implement retry / cancel handling**

### Issue 35

**Add metadata persistence**

---

## Cross-Cutting Integration

実装 issue とは別に、横断レビューと検証を管理する。
`Task 10` のような単一実装タスクとしては扱わない。

### Integration Issue A

**Run integration review for bootstrap tasks**

受け入れ条件:

* `docs/checklists/integration-checklist.md` に沿って全項目を確認する
* pass / partial / fail を含むレビュー結果が残る
* 修正が必要な差分と修正先が明記される
* 後続の fix task が切り出される

### Integration Issue B

**Fix inconsistencies found in integration review**

受け入れ条件:

* レビューで見つかった naming / schema / import / API / UI の不整合が修正される
* 変更は最小スコープに保たれる
* 未解決項目があれば明示される

### Integration Issue C

**Run end-to-end validation for image bootstrap flow**

受け入れ条件:

* UI request shape から API, Job, Generator, Output までの流れを確認する
* ダミー出力の作成可否が確認される
* `GenerationResult` 互換のレスポンス可否が確認される
* fail point があれば具体的に記録される
