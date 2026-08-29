# Memory Lifecycle Experiment Matrix

- Issue: [#351](https://github.com/codetea-ping999/Creative_AI_Studio/issues/351)
- Parent: [#350](https://github.com/codetea-ping999/Creative_AI_Studio/issues/350)
- 参照する契約: [#295](https://github.com/codetea-ping999/Creative_AI_Studio/issues/295)
  performance metric contract / [#297](https://github.com/codetea-ping999/Creative_AI_Studio/issues/297)
  reference hardware benchmark

Hybrid Runtime (#350) の Gate 0 は「主要なメモリ消費源と回収不能パターンを数値で
説明できること」です。本書はその数値を取るための **実験手順の固定** だけを行います。
実装・最適化・言語選定は行いません。

本書の identifier（`S1`〜`S6` の scenario id、`B0`〜`B7` の boundary id、metric 名）は
後続 issue が結果を報告するときの共通キーです。**名前を変えると過去の測定値と接続
できなくなる**ため、追加はしても改名はしないでください。

## 1. Scope

### 決めること

- 測定シナリオ（`S1`〜`S6`）
- 測定境界（`B0`〜`B7`）と、その前後比較のペア
- 指標・単位・取得コマンド
- run ごとに固定するメタデータ
- 反復回数と結果の記録形式

### 決めないこと

| 対象 | 決める場所 |
| --- | --- |
| メモリ閾値・退避ポリシー | #353 Low Memory Profile |
| TTL eviction / telemetry の実装 | #354 / #355 |
| 長時間 endurance の合否基準 | #356 |
| process isolation の設計 | #357 以降 |
| Rust benchmark | #362〜#364 |
| 一般性能指標（latency percentile 等）の定義 | #295 |

## 2. Reuse boundary (#295 / #297)

本書は #295 の performance metric contract を **参照し、再定義しません**。
Hybrid Runtime 固有の lifecycle boundary だけを足します。

| 項目 | 一次情報 | 本書での扱い |
| --- | --- | --- |
| API latency percentile / queue wait / throughput | #295 | 参照のみ。再定義しない |
| output growth / UI responsiveness | #295 | 参照のみ。本書の対象外 |
| benchmark metadata の基本項目 | #295 | 参照し、§4 で lifecycle 固有項目だけ追加 |
| reference hardware での real-model baseline | #297 | 参照のみ。同じ hardware 記述を使う |
| generation duration / cold・warm load time | #297 | 既存値があれば再利用し、再測定しない |
| runtime load / unload / process exit の前後比較 | **本書** | 新規（§5） |
| control plane と runtime のメモリ分離 | **本書** | 新規（§8） |
| idle / repeated switch の residency drift | **本書** | 新規（`S5` / `S6`） |

本書の執筆時点で #295 / #297 はいずれも open です。文書が出来た時点で、上表の
「一次情報」列の参照先をその文書へ差し替えてください。**重複定義を本書へ写経しない
でください**（写経すると #295 側の更新が本書へ伝播せず、2 つの契約が食い違います）。

## 3. Reference environment

primary reference environment は **Apple M1 Max 64GB** です。ここで再現しない結果は
Gate 0 の根拠になりません。

| 項目 | 取得コマンド | primary の実測値 |
| --- | --- | --- |
| hardware model | `sysctl -n hw.model` | `MacBookPro18,2` |
| physical memory | `sysctl -n hw.memsize` | `68719476736`（64 GiB） |
| page size | `sysctl -n hw.pagesize` | `16384` |
| OS version | `sysctl -n kern.osproductversion` | `26.6.2` |
| Python | `venv/bin/python --version` | `3.14.4` |
| PyTorch | `python -c "import torch; print(torch.__version__)"` | `2.10.0` |
| accelerator | `python -c "import torch; print(torch.backends.mps.is_available())"` | MPS available |

secondary environment（#297 が挙げている RTX 3090 など CUDA 環境）は #297 と同じ
hardware 記述を使い、**primary の結果が出てから追試**します。CUDA 環境では accelerator
指標を次のように読み替えます。単位も意味も 1 対 1 ではないので、環境を跨いだ引き算は
しないでください。

| MPS 指標 | CUDA での対応 |
| --- | --- |
| `torch.mps.current_allocated_memory()` | `torch.cuda.memory_allocated()` |
| `torch.mps.driver_allocated_memory()` | `torch.cuda.memory_reserved()` |
| `torch.mps.recommended_max_memory()` | `torch.cuda.get_device_properties(0).total_memory` |
| system memory pressure（`memory_pressure -Q`） | `nvidia-smi --query-gpu=memory.used,memory.total` |

Apple Silicon は unified memory なので、MPS の allocated と process RSS は **同じ物理
メモリを別の見方で数えた値**です。合算すると二重計上になります。

## 4. Fixed metadata per run

1 回の測定（run）ごとに次を固定・記録します。#295 の metadata 要件に加えて、lifecycle
固有の 3 項目（`cold_process` / `warm_runtime` / `file_cache`）を足しています。

| field | 決め方 | 例 |
| --- | --- | --- |
| `run_id` | `<date>-<hardware>-<scenario>-<repetition>` | `20260829-m1max-S2-03` |
| `date` | 測定日（ISO 8601） | `2026-08-29` |
| `git_commit` | `git rev-parse HEAD` | `70d9177…` |
| `scenario_id` | §7 の id | `S2` |
| `repetition` | 1 始まりの通し番号 | `3` |
| `hardware_id` | §3 の hardware model | `MacBookPro18,2` |
| `os_version` | `sysctl -n kern.osproductversion` | `26.6.2` |
| `python_version` | `python --version` | `3.14.4` |
| `torch_version` | `torch.__version__` | `2.10.0` |
| `device` | 実際に使われた device | `mps` / `cpu` / `cuda:0` |
| `model_public_id` | manifest の `public_id` | `sdxl` |
| `model_manifest_id` | manifest の `id` | `sdxl-local` |
| `model_revision` | weight ディレクトリの識別子（コミット hash か、無ければ総バイト数） | `total=6.94GiB` |
| `seed` | 生成 seed（固定） | `12345` |
| `workload_id` | §7 の workload 名 | `image_512_20steps` |
| `env_knobs` | `MAX_CACHED_MODELS`、`MAX_CACHED_MODELS_<MEDIA>`、`JOB_LANES` | `MAX_CACHED_MODELS=1` |
| `cold_process` | 計測用に新規起動した process か | `true` |
| `warm_runtime` | 同一 process 内で当該 runtime が既に cache 上にあるか | `false` |
| `file_cache` | `cold_boot`（OS 再起動直後の初回のみ）または `unknown` | `unknown` |

cold / warm を 3 つに割ったのは、macOS では OS の file cache を任意に空にできないから
です。「cold」を 1 語で書くと、process が新しいだけなのか weight の読み込みまで冷えて
いるのかが後から判別できません。`file_cache=cold_boot` を名乗れるのは **再起動直後の
1 回目だけ** です。

## 5. Measurement boundaries

lifecycle を 8 つの境界に切ります。`observer` 列は誰がサンプリングするかです。

- `in-process`: 対象 process 内の Python から取得（MPS 指標と `resource` はここでしか取れない）
- `external`: 対象 process を起動した親シェル／別 process から取得（`ps` / `vm_stat` / `memory_pressure`）

| id | 名前 | 位置 | observer | 目的 |
| --- | --- | --- | --- | --- |
| `B0` | `process_start` | interpreter 起動直後、repo の import 前 | both | interpreter 単体の下駄 |
| `B1` | `imports_ready` | `bootstrap` の import 完了直後（runtime 未 load） | both | import（torch 含む）の下駄 |
| `B2` | `api_ready` | `/health` が 200 を返した直後 | both | control plane の定常値 |
| `B3` | `pre_load` | 生成要求の直前 | both | runtime load の基準線 |
| `B4` | `post_load` | runtime load 完了直後、生成開始前 | both | model/runtime の常駐量 |
| `B5` | `post_generate` | 生成完了・成果物永続化後 | both | 生成後に残る量 |
| `B6` | `post_unload` | `unload` + `gc.collect()` 完了後 | both | in-process の回収結果 |
| `B7` | `post_exit` | 対象 process の終了を確認した後 | external のみ | OS への返却結果 |

### 前後比較のペア

| 比較 | 区間 | 何が分かるか |
| --- | --- | --- |
| unload 前後 | `B5` → `B6` | runtime を捨てた時点で process が返した量 |
| unload 残渣 | `B3` → `B6` | unload しても process に残る量（= 回収不能の疑い） |
| process exit 前後 | `B6` → `B7` | process 終了でしか OS に戻らなかった量 |

`B6` → `B7` が大きいほど、Python process を生かしたままではメモリが返らない、つまり
#350 Phase 2 の process isolation に効果がある、という判断材料になります。`B7` は対象
process が既に存在しないため **system-wide 指標でしか観測できません**。external
observer が `B6` と `B7` を同じ手順で取ることが前提です。

### サンプリング手順（全境界で共通）

順序を固定しないと差分が再現しません。各境界で必ずこの順に 1 セット取ります。

1. 対象 process が境界に到達したことを external observer へ通知し、**採取完了の応答を
   待って停止する**（`B0` / `B1` のような一瞬の境界を外から捉えるには、この握手が必要
   です。標準出力に `BOUNDARY <id>` を書いて 1 行読み返す程度で足ります）
2. `gc.collect()` を 1 回呼ぶ
3. 100 ms 待つ（accelerator の遅延解放と `ps` の更新を待つため）
4. in-process 指標を取る（`resource`、`torch.mps.*`）
5. external 指標を取る（`ps` → `vm_stat` → `memory_pressure` → `sysctl vm.swapusage`）
6. 取得時刻（`time.perf_counter()` と wall clock）を一緒に記録する
7. 対象 process を再開させる

`B7` だけはこの握手が使えません（対象 process が既に無い）。external observer が単独で
5 と 6 を行います。

**`B0` では手順 4 の `torch.mps.*` を取りません。** `B0` は「repo の import 前」と定義
されており、MPS 指標を読むには `import torch` が必要です。torch は本 stack で単独最大の
import なので、`B0` で読むと §8 の `import_overhead = rss(B1) − rss(B0)` が測ろうと
している当のコストを `interpreter_baseline` 側へ畳み込んでしまい、`import_overhead` が
ほぼ 0 に、`interpreter_baseline` が過大に出ます。`B0` の in-process 指標は `resource`
のみとし、MPS 指標は `B1` 以降で取り始めてください（`B0` の observer が `both` なのは
`resource` を取るためです）。

## 6. Metrics

| metric | 単位 | 取得方法 | observer | 備考 |
| --- | --- | --- | --- | --- |
| `rss_kib` | KiB | `ps -o rss= -p <pid>` | external | process の resident |
| `peak_rss_bytes` | bytes | `resource.getrusage(RUSAGE_SELF).ru_maxrss` | in-process | macOS は bytes、Linux は KiB。**現在値ではなく peak** |
| `mps_current_allocated_bytes` | bytes | `torch.mps.current_allocated_memory()` | in-process | tensor が実際に使用中の量 |
| `mps_driver_allocated_bytes` | bytes | `torch.mps.driver_allocated_memory()` | in-process | driver 側の確保量（キャッシュ込み） |
| `mps_recommended_max_bytes` | bytes | `torch.mps.recommended_max_memory()` | in-process | 環境定数。§3 の記録用 |
| `sys_free_percent` | % | `memory_pressure -Q` の `System-wide memory free percentage` | external | sudo 不要 |
| `sys_pages_free` | pages | `vm_stat` の `Pages free` | external | bytes 換算は `hw.pagesize` を掛ける |
| `sys_pages_active` | pages | `vm_stat` の `Pages active` | external | `sys_used` の構成要素 |
| `sys_pages_wired` | pages | `vm_stat` の `Pages wired down` | external | `sys_used` の構成要素 |
| `sys_pages_compressor` | pages | `vm_stat` の `Pages occupied by compressor` | external | 圧縮に逃げた分。RSS だけ見ると見落とす |
| `sys_swapins` / `sys_swapouts` | count | `vm_stat` の `Swapins` / `Swapouts` | external | 単調増加カウンタ。差分で見る |
| `swap_used_mib` | MiB | `sysctl -n vm.swapusage` の `used` | external | |
| `load_duration_ms` | ms | `ModelService.resolve_runtime()` 前後の `time.perf_counter()` | in-process | `B3` → `B4` |
| `unload_duration_ms` | ms | `ModelService.unload_model()` + `gc.collect()` 前後 | in-process | `B5` → `B6` |
| `generate_duration_ms` | ms | 生成呼び出し前後 | in-process | `B4` → `B5`。#295 の generation duration と同義 |
| `exit_duration_ms` | ms | 終了要求から process 消滅確認まで | external | `B6` → `B7` |

補助的に `footprint -p <pid>` を cross-check に使えます（sudo 不要）。ただし `-p` は
部分一致の **プロセス名** も受け付けるため、数値 PID を渡したつもりでも別プロセスに
一致することがあります。採用する場合は出力の PID が対象と一致することを毎回確認して
ください。必須指標には含めません。

`psutil` は `requirements.txt` に宣言されていません（現在の venv には推移的依存として
入っているだけです）。**測定手順は `ps` / `vm_stat` / `memory_pressure` / `resource` /
`torch.mps` だけで完結させ、psutil に依存させないでください。**

MPS 指標は process 単位、`vm_stat` / `memory_pressure` は system 単位です。**異なる
スコープの値を引き算しないでください。**

## 7. Scenario matrix

| id | 名前 | 目的 | 使う boundary | 主な導出量 | 反復 |
| --- | --- | --- | --- | --- | --- |
| `S1` | `api_only` | control plane だけの常駐量 | `B0` `B1` `B2` | control plane overhead | 5 |
| `S2` | `runtime_load` | model/runtime の常駐量 | `B3` `B4` | runtime load overhead | 5 |
| `S3` | `generation` | 生成中の一時的な増加 | `B4` `B5` + 1 s 間隔 | generation transient | 5 |
| `S4` | `unload` | unload と exit の返却量 | `B5` `B6` `B7` | unload 残渣、exit 返却 | 5 |
| `S5` | `idle` | 無操作時の drift | `B4` + 5 分間隔 × 30 分 | idle drift | 3 |
| `S6` | `repeated_switch` | 反復切り替えの累積 | 各 cycle の `B4` `B6` | cycle ごとの増加 | 3 |

`S1`〜`S4` は 1 本のセッションとして連続実行できます（`S1` → `S2` → `S3` → `S4` の
順に境界が並んでいます）。`S5` と `S6` は独立したセッションで走らせます。

### workload の固定

| workload_id | media | model | 主なパラメータ |
| --- | --- | --- | --- |
| `image_512_20steps` | image | `sdxl` | 512×512、20 steps、`seed=12345` |
| `audio_musicgen_8s` | audio | `musicgen-small` | 8 秒、`seed=12345` |
| `text_template_short` | text | `template-writer` | 短文 1 本、`seed=12345` |

`text_template_short` は weight 不要かつ決定的なので、**harness 自身の overhead を測る
control 群**として使います。`S1` の値がこの workload で説明できない量に膨らんでいる
場合、測っているのは studio ではなく harness です。

### S1 `api_only`

1. 新しい process で API を **reloader なしで** 起動する:
   `venv/bin/uvicorn apps.api.main:app --host 127.0.0.1 --port 8000`
2. `B0` / `B1` / `B2` を取る
3. job を 1 件も投げずに 60 秒待ち、もう一度 `B2` を取る（`B2_60s` として記録）
4. `B2_60s` − `B2` が有意に増えている場合、control plane 自体に drift がある

**`scripts/run_api_dev.sh` は使わないでください。** このスクリプトは `--reload` を無条件
で渡すため、uvicorn が reloader supervisor と worker の 2 process に分かれます。起動時に
得られる PID は supervisor 側で、`bootstrap` を import して `/health` を返すのは fork
された子です。supervisor の `rss_kib` を測ると `control_plane_overhead` と
`import_overhead` が 0 付近か負になり、§7 で `S1`〜`S4` を 1 セッションとして続ける以上
`runtime_load_overhead` と `unload_residual` まで同じ誤った基準線を引き継ぎます。

なお `.env.example` と `setup.sh` にある `API_RELOAD` は **どのコードからも読まれていない
死んだ変数**です（`grep -rn API_RELOAD` の一致は宣言側だけ）。`API_RELOAD=false` を付けて
も reloader は止まりません。reloader を外す唯一の方法は、上のように `--reload` を渡さず
uvicorn を直接起動することです。

**model runtime を一切 load しない**ことがこのシナリオの条件です。ここが崩れると §8 の
分離が成立しません。

### S2 `runtime_load`

1. `S1` の状態から `B3` を取る
2. 対象 workload を 1 件だけ投入し、runtime load 完了時点で `B4` を取る
3. `load_duration_ms` を記録する
4. `warm_runtime=true` の追試として、同じ model を 2 回目に load させた場合も記録する
   （cache hit なので load が起きないことの確認を含む）

### S3 `generation`

1. `B4` の直後から生成を開始する
2. 生成中は 1 秒間隔で `rss_kib` と `mps_driver_allocated_bytes` をサンプリングする
3. 生成完了・成果物永続化後に `B5` を取る
4. サンプル列の最大値を `peak_during_generation` として記録する

生成中の peak は `B4` にも `B5` にも現れません。**間引かずに時系列で取る**のはそのため
です。

### S4 `unload`

1. `B5` の後に `ModelService.unload_model(<manifest_id>)` を呼ぶ
2. `gc.collect()` の後に `B6` を取る
3. process へ **SIGTERM**（graceful shutdown）を送り、消滅を確認してから external
   observer が `B7` を取る。`SIGKILL` は使わない。使わざるを得なかった場合は
   `run_id` に併記する（強制終了は cleanup 経路を飛ばすため、`B6` → `B7` の意味が
   変わります）
4. `B5` → `B6`、`B3` → `B6`、`B6` → `B7` を §5 の定義どおり算出する

`unload_model` に対応する HTTP endpoint は現時点で存在しません（§10）。このシナリオは
in-process harness から直接呼びます。

### S5 `idle`

1. `S2` の `B4` 到達後、job を投げずに 30 分放置する
2. 5 分ごとに `B4` と同じ指標セットを取る（`B4_05m` … `B4_30m`）
3. 単調増加、圧縮ページの増加、swapouts の発生の有無を見る

これは #356 の endurance smoke の短縮版です。判定基準は本書では決めません。

### S6 `repeated_switch`

1. `MAX_CACHED_MODELS=1` を明示し、`MAX_CACHED_MODELS_<MEDIA>` は **設定せずに** 起動する
   （per-media budget を設定すると media ごとに別バケットになり、A/B が同時に常駐して
   evict が起きません。`core/models/cache.py` の bucket 規則を参照）
2. model A（`image_512_20steps`）と model B（`audio_musicgen_8s`）を交互に 10 cycle 実行する
3. 各 cycle の `B4` と `B6` を取る
4. cycle 番号に対する `B6` の回帰直線の傾きを `residual_growth_per_cycle` として記録する

weight が揃わない環境では、A/B の両方を text/audio の組み合わせに置き換えて構いません。
ただし **どの model を使ったかを `model_public_id` に必ず残してください**。

## 8. Overhead separation

`S1` が model runtime を一切 load しないことによって、control plane と runtime を分離
します。すべて同一 observer の同一指標同士で引きます。

| 導出量 | 式 | 意味 |
| --- | --- | --- |
| `interpreter_baseline` | `rss(B0)` | Python interpreter 単体の下駄 |
| `import_overhead` | `rss(B1) − rss(B0)` | import（torch 含む）が増やした量 |
| `control_plane_overhead` | `rss(B2) − rss(B0)` | Python + FastAPI + repository 層の常駐量 |
| `runtime_load_overhead` | `rss(B4) − rss(B3)` | model/runtime が増やした量 |
| `generation_transient` | `peak_during_generation − rss(B4)` | 生成中だけの一時的増加 |
| `unload_returned` | `rss(B5) − rss(B6)` | unload で process 内に戻った量 |
| `unload_residual` | `rss(B6) − rss(B3)` | unload しても残る量（回収不能の疑い） |
| `exit_returned` | `sys_used(B6) − sys_used(B7)` | process 終了でしか OS に返らなかった量 |
| `accelerator_residual` | `mps_driver_allocated_bytes(B6)` | `release_runtime()` 後に driver が抱えたままの量 |

`sys_used` は `(sys_pages_active + sys_pages_wired + sys_pages_compressor) × hw.pagesize`
で求めます。`hw.memsize − free` にしないのは、macOS では file cache（inactive /
speculative）が free に入らないため、process とは無関係な読み書きまで「使用中」に
数えてしまうからです。system 全体の値なので、測定中に他のアプリを起動・終了させない
でください。

`accelerator_residual` は `core/models/cleanup.py` の `release_runtime()` が
`torch.mps.empty_cache()` まで到達しているかの実測です（#23 で入った経路）。ここが 0 に
ならない場合、in-process では返らないという Phase 2 の根拠になります。

## 9. Repetition and reporting

- 各シナリオを **5 回**（`S5` / `S6` は 3 回）、いずれも `cold_process=true` で実行する
- 外れ値を捨てない。**中央値と min / max の 3 値**を報告する
- 1 行 = 1 つの `(run_id, scenario_id, boundary_id, metric, value)` の long format で記録する
- 比較は **同一 hardware かつ同一 `git_commit` の間だけ**で行う
- 結果は `docs/performance/results/<date>-<hardware>-<scenario>.md` に表として置き、
  生ログ・weight・出力メディアは repo に含めない（`docs/validation/musicgen-v0.2.md`
  と同じ扱い）

再現性の最低条件は「同じ `git_commit`・同じ `model_revision`・同じ `seed`・同じ
`env_knobs` で繰り返したとき、min / max の幅が中央値に対して十分小さいこと」です。幅が
中央値と同オーダーまで開いた run は、baseline ではなく環境ノイズです。**許容幅の具体値
は本書では決めません**（最初の実測 #352 で観測された散らばりから決めます）。

## 10. Known constraints（2026-08-29 時点）

実測前に把握しておくべき、この repository 側の制約です。

- **unload の HTTP endpoint が無い。** `ModelService.unload_model()` / `unload_all()` は
  `core/models/service.py` にありますが、`apps/api/routes/models.py` には対応する route
  がありません。`S4` / `S6` は API 越しではなく in-process harness から呼びます。API
  越しの unload が必要になった場合は #354 の TTL eviction を待つか、別 issue で admin
  route を足してください（本書では追加しません）。
- **`psutil` は宣言依存ではない。** §6 のとおり、標準コマンドと `resource` だけで測ります。
- **image weight が無い環境では `S2`〜`S6` の image ケースを実行できない。** SDXL の
  weight が `models/` 未配置なら、workload を `audio_musicgen_8s` か
  `text_template_short` に差し替え、`model_public_id` に記録します。**weight が無いまま
  「測定した」と書かないでください。**
- **TTS は既定で合成できない。** `kokoro` は無効、`voicevox` は外部エンジンが必要です。
  speech は本書の workload に含めません。
- **MPS 指標は device が `mps` のときだけ意味を持つ。** CPU 実行では 0 になるので、
  `device` を必ず一緒に記録します（`docs/validation/musicgen-v0.2.md` の MusicGen 計測は
  CPU 実行でした）。
- **`MAX_CACHED_MODELS` の既定は 1。** 既定のままだと media を跨いだ切り替えで毎回
  evict が起きます。`S6` はこれを利用しますが、`S5` では意図しない evict が入らない
  よう、使用する model を 1 つに限定してください。

## 11. Follow-ups

| issue | 本書の使い方 |
| --- | --- |
| #352 | `S1`〜`S4` を M1 Max で実測し、boundary id をそのまま使って報告する |
| #353 | `S2` / `S5` / `S6` の結果から low-memory profile の既定値を決める |
| #354 | `S5` の idle drift を TTL 設計の入力にする |
| #355 | §6 の指標のうち常時取得できるものを telemetry として実装する |
| #356 | `S5` を長時間版に拡張する |
| #361 | `B6` → `B7` の差を process-isolated path と比較する |
