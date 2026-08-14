# Issue Execution Plan

Open issue 53 件（2026-08-14 時点）を実コードと照合して整理した実行計画です。
個別 issue の要件は GitHub 側が正、本書は**順番・依存・粒度**の判断根拠を持ちます。

## 1. 全体像

open issue はほぼ 4 つの塊に分かれます。

| トラック | issue | 件数 | 性格 |
| --- | --- | --- | --- |
| A. ISO ガバナンス | #78, #79, #80, #82〜#96 | 18 | 2026-08-01 追加。最新かつ P0 が 10 件 |
| B. v0.3 マルチメディア | #31〜#36, #39, #40, #45, #46, #49, #50, #54〜#57, #60, #62, #65, #66 | 20 | 実装済みが多く、残りは「接続漏れ」中心 |
| C. v0.1 / v0.2 積み残し | #5〜#8, #10, #11, #21, #25, #29 | 9 | 実weight を使う検証と release gate |
| D. 保守・品質 | #2, #3, #4, #18, #19, #20 | 6 | 単発の改善 |

番号範囲に含まれるが対象外のもの: **#9 と #81 は open issue ではありません**
（#9 はクローズ済み、#81 は issue ではなく PR）。合計は 18 + 20 + 9 + 6 = 53 で
`gh issue list --state open` の件数と一致します。

**meta issue の扱い**: #78（ISO）、#31〜#36（v0.3）、#5（v0.1 Image）、#25（v0.2 Audio）は
傘であり、固有の実装作業を持たないため Phase 表には現れません。**配下の子 issue が
すべてクローズした時点で、チェックリストを最終確認してクローズします**
（#78 なら #79〜#96、#96 の判定公開をもって閉じる）。件数には含めていますが、
着手対象としては数えないでください。meta は計 9 件（#5, #25, #31〜#36, #78）で、
実際に着手対象となる issue は 53 − 9 = **44 件**です。

**この計画の骨子**: トラック A は「ドキュメント 18 本を書く」ではなく
「まず CI で強制できるものをコードにし、残りを最小限の証跡パックに畳む」。
トラック B は「新機能」ではなく「実装済み部品の未接続を塞ぐ」。両者は並行できます。

### 先に述べておく懸念

ISO トラックは *accountable management*、*internal audit*、*certification body* を
前提にした文面（#86 / #96）を含みます。実体が単一開発者のローカル志向リポジトリである以上、
18 issue すべてを額面どおり満たすと、成果物の大半が運用実体のない文書になります。
本計画は **#86（ISMS）と #96（内部監査）を「組織証跡が実在する場合のみ着手」** と位置づけ、
それ以外を先に片付ける順序にしています。範囲を戻す判断は issue 側の更新で行ってください。

## 2. 実コードとの差分（計画の前提になった事実）

| 事実 | 確認箇所 | 影響する issue |
| --- | --- | --- |
| CI は `verify_local_stack.py --start-api` のみ。lint / type / coverage / format ゲートなし | [ci.yml](../.github/workflows/ci.yml) | #83 |
| CONTRIBUTING は flake8 を要求するが `requirements.txt` に無い。mypy / ruff / eslint も未導入 | [CONTRIBUTING.md#L63](../CONTRIBUTING.md#L63), [requirements.txt](../requirements.txt) | #83 |
| `npm audit` は High 3 件（postcss / nanoid / undici）。**#87 が挙げる postcss だけでは解消しない** | [package-lock.json](../apps/web/package-lock.json) | #87 |
| API は無認証、`allow_methods=["*"]` / `allow_headers=["*"]`、`/outputs` を StaticFiles で公開 | [main.py#L86](../apps/api/main.py#L86) | #85 |
| `run_api_dev.sh` は `API_HOST` を Uvicorn へそのまま渡す（既定 `127.0.0.1`、非 loopback を拒否しない） | [run_api_dev.sh#L22](../scripts/run_api_dev.sh#L22) | #85 |
| Python 依存はすべて範囲指定でハッシュ固定なし。SBOM 生成なし | [requirements.txt](../requirements.txt) | #84 |
| CI は Ubuntu / Python 3.10 / Node 20.19.0 の 1 構成のみ | [ci.yml](../.github/workflows/ci.yml) | #90 |
| #55（TTS）は実装済みだが issue が open のまま | [next-tasks.md](./next-tasks.md) | issue 整理 |
| #56 は narration 経路のみ適用済み、music 経路が未適用 | [postprocess.py](../core/audio/postprocess.py) / [generator.py](../generators/audio/generator.py) | #56 |
| テストは 26 モジュール / `test_*` 関数 469 件（`tests/__init__.py` はテストを含まない） | [tests/](../tests/) | #82 |

## 3. フェーズ計画

### Phase 0 — 今すぐやる（実コード変更、ISO 判断を待たない）

ISO のスコープ議論の結論に関係なく価値が確定している 3 件。ここから着手します。

| 順 | issue | 内容 | 規模 | 状態 |
| --- | --- | --- | --- | --- |
| 1 | #87 | **High 3 件を全て解消**してから `npm audit` を CI に追加（下記） | M | ✅ 実施済み（2026-08-14） |
| 2 | — | issue 整理: #55 をクローズ、#56 を「music 経路適用」に絞って再記述、**#87 の範囲を更新** | S | #56 は本文が既に対象範囲を正しく含んでいたため再記述は不要と判明。#55 クローズと #87 本文更新は GitHub 操作のため未実施（要承認） |
| 3 | #83 | ruff（lint+format）/ mypy / eslint / coverage を導入し `make verify` と CI を一致させる | L | ✅ 実施済み（2026-08-14）。ruff/mypy は `core/`・`generators/` の既存違反 146 件（ruff 36 + mypy 110）を修正した上でゲート化。coverage は Python 85%・Web 40% を現状値の床として設定 |

トラック B の **#18**（video generator のキャンセル対応）も本バッチで実施済み（✅ 2026-08-14）。

**#87 の範囲は issue の記述より広い**。issue #87 は GHSA-r28c-9q8g-f849（postcss ≤8.5.17）
のみを挙げていますが、2026-08-14 時点の `npm audit` は High 3 件を報告します。

| package | lock | 該当範囲 | 必要バージョン |
| --- | --- | --- | --- |
| postcss | 8.5.16 | ≤8.5.22（#87 の勧告は不完全修正で、後続勧告が範囲を拡大） | ≥8.5.23 |
| nanoid | 3.3.15 | ≤3.3.17 | ≥3.3.18 |
| undici | 7.28.0 | ≥7.0.0 <7.29.0 | ≥7.29.0 |

`npm audit` は脆弱性を 1 件でも検出すると既定で非ゼロ終了します。
**postcss だけ更新してゲートを入れると CI は赤のままです。** 手順は
(1) 3 件すべてを更新（いずれも `fixAvailable: true`）、(2) `npm audit` が
クリーンなことを確認、(3) そのうえでゲートを追加、の順にしてください。
閾値を `--audit-level=high` などに緩める場合は、除外した内容と理由を
#84 の例外記録として残します。**issue #87 の本文も、postcss 単体から
「監査ベースラインをクリーンにする」範囲へ更新が必要です**（Phase 0-2 に含む）。

**#83 の設計方針（ゲートの接続先に注意）**: 現在 `make verify` も CI も
`scripts/verify_local_stack.py --start-api` を直接呼んでおり、`verify-lite` を経由しません。
そのため lint / typecheck を `verify-lite` にだけ足すと、**`make verify` と CI の
どちらもゲートを実行しないまま通過します**。新しいゲートは
`verify_local_stack.py` 自身のステップとして追加する（CI とローカルの入口を 1 本に保つ）か、
`Makefile` と `ci.yml` の両方をゲートを含むターゲット呼び出しに変更するかの
どちらかを選んでください。本計画は前者（verifier にステップを足す）を推奨します。
mypy は `core/` と `generators/` から段階導入（`--strict` は後回し、まず未定義参照レベル）。

### Phase 1 — 枠を決める（後続すべての前提）

| 順 | issue | 内容 | 依存 | 規模 |
| --- | --- | --- | --- | --- |
| 4 | #79 | 適用範囲・除外・所有者・証跡マトリクス。**#6（Production/Labs 境界）を先に確定** | #6 | M |
| 5 | #80 | 25010 の 9 特性それぞれに適用判断と測定可能な要件 | #79 | L |
| 6 | #82 | 要件 ID とトレーサビリティ。**追跡単位をテストケースで定義**（下記） | #80 | L |

ここを飛ばして #88〜#95 に着手すると、閾値の根拠がないまま測定だけが増えます。
#79 の成果物は 1 ファイル（`docs/iso/scope-and-evidence.md` 想定）に集約し、
#80 / #82 はその表の列を埋める形にすると 18 本の文書化を避けられます。

**#82 の追跡単位**: テストは 26 モジュールに対し `test_*` 関数が 469 件あります。
ファイル名単位で紐づけると、1 モジュール内で独立に検証されている数十の振る舞いが
1 要件に潰れて可視性を失います。

さらに `tests/test_speech_audio.py` は `@pytest.mark.parametrize` を 6 箇所で使い、
1 関数が複数ケースへ展開されます（例: `test_speech_generator_rejects_invalid_controls_before_synthesis`
は `speed=0` / `speed=inf` / `pitch=nan` / `max_chunk_characters=0` を個別に収集）。
関数名までで切ると、これらが 1 エントリに潰れます。

**追跡単位は `[parameter]` サフィックスまで含む完全な pytest node ID**
（`tests/test_speech_audio.py::test_xxx[params3-...]` 形式）と定義してください。
実際の一覧は `pytest --collect-only -q` の出力を正とし、
要件に紐づかないテスト（orphan）の検出も同じ粒度で行います。

### Phase 2 — セキュリティとデータ（P0 集中帯）

| 順 | issue | 内容 | 規模 |
| --- | --- | --- | --- |
| 7 | #85 | 信頼境界の明文化。**非 loopback bind は fail-closed**（下記） | L |
| 8 | #84 | 依存ロック（ハッシュ固定）、CI スキャン、SBOM、モデル来歴台帳 | L |
| 9 | #95 | データ棚卸し・保持・エクスポート・安全な削除 | L |
| 10 | #94 | 生成物の来歴、同意（参照画像 / 声）、安全性 vs 品質スコアの区別 | L |

**#85 の bind 方針**: 既存の egress guard（`ALLOW_REMOTE_TEXT_ENDPOINTS` 等）と同じ
「既定で閉じる」方針を bind アドレスまで広げます。ここで**警告表示を
fail-closed の代替にしてはいけません**。API は無認証で、生成物を `/outputs` に
StaticFiles で公開しており、`scripts/run_api_dev.sh` は `API_HOST` を
Uvicorn へそのまま渡します。警告のみの実装では `API_HOST=0.0.0.0` を設定した時点で
generate / project / job / outputs の各エンドポイントが同一 LAN へ無防備に露出します。
**明示的な opt-in フラグか認証境界のどちらかが設定されていない限り、
非 loopback bind は起動を拒否**してください。
#94 は #48（prompt audit trail）が既に来歴の半分を持っているため、
不足は「参照画像・音声の同意記録」と「品質スコアと安全性判定の表示上の分離」に絞れます。

### Phase 3 — 測れるものを測る

| 順 | issue | 内容 | 関連 | 規模 |
| --- | --- | --- | --- | --- |
| 11 | #89 | 障害注入・復旧・RTO/RPO。JSON/SQLite の破損と部分書き込み | — | L |
| 12 | #88 | 性能ベースライン。#54 の probe 計測を土台にする | #54, #39 | L |
| 13 | #90 | サポート対象マトリクスと CI マトリクス化 | #7, #21, #45 | M |
| 14 | #91 | アクセシビリティ検証の自動化（390/768/1280/1440px の証跡化） | #62 | M |

**#89 は復旧手段の新規実装が主作業です**（当初「失敗注入テストが主作業」と書いていましたが、
コードを読み直して訂正しました）。既存の atomic save が防ぐのは
**書き込み途中の中断が新たな破損を生むこと**だけで、**既に壊れたデータの復旧はできません**。

- [`write_json_atomic()`](../core/storage/json_files.py#L32) は tempfile → `os.replace` で、
  直前の内容を退避しません。置換後に破損すれば以前の版は失われます。
- [`JobRepository`](../core/storage/repositories/job_repository.py#L261) は
  `journal_mode=WAL` によりクラッシュ時の torn write は防ぎますが、
  backup / restore / `PRAGMA integrity_check` のいずれも実装していません。

したがって #89 は、まず **バックアップと復元の経路（jobs.db のスナップショット、
JSON リポジトリの世代保持または再構築手順、整合性チェック）を実装**したうえで、
失敗注入テストでその復旧が機能することを検証する順になります。
復旧手段を持たないまま障害注入だけを行っても、障害の再現を確認できるだけで
RTO / RPO の達成は証明できません。

### Phase 4 — ライフサイクル（運用実体が要る）

| 順 | issue | 内容 | 規模 |
| --- | --- | --- | --- |
| 15 | #92 | 構成管理・変更管理・リリース再現性 | M |
| 16 | #93 | 運用 / 保守 / 非推奨 / 廃止 | M |
| 17 | #86 | ISMS 証跡パック（**組織実体がある場合のみ**） | XL |
| 18 | #96 | 内部監査と公開可能な判定（#11 の release gate と統合） | M |

#96 は #11（v0.1 release gate）と目的が重なります。**別々に走らせず、
#11 の判定レポートに ISO 章を追加する形に統合**してください。

## 4. トラック B（v0.3）— Phase 0〜2 と並行

実装済み部品の未接続を塞ぐのが中心で、ISO トラックとファイルの衝突がほぼありません。

| 優先 | issue | 実態 | 規模 |
| --- | --- | --- | --- |
| P0 | #39 | 単一 `JobQueue`/`JobRunner`、`MAX_CACHED_MODELS` が media 別でない | L |
| P0 | #56 残 | music 経路が `[-1,1]` clamp のみ、`MUSIC_PRESET` 未適用 | M |
| P1 | #18 | video generator が `context` を受け取るだけで `raise_if_cancelled()` 未呼び出し | S |
| P1 | #49 | `promote()` が `promoted=True` を立てるのみ、bible 書き込みなし | M |
| P1 | #50 | `reference_asset_ids` を metadata 記録のみ、img2img/IP-Adapter 未接続 | L |
| P1 | #62 | timeline 手動編集 UI なし | L |
| P1 | #65 | scene 単位の Visual Orchestrator | L |
| P2 | #40, #46, #54, #57, #60, #66 | 拡張系 | 各 M〜L |

**#18 と #89 に順序依存はありません**（当初この計画は依存ありと書いていましたが、
コードを読み直して撤回しました）。`JobRunner.run_once()` は `generator.run()` の復帰直後に
永続化された cancellation 状態を確認し、postprocessing と成功確定の前に抜けます
（[runner.py#L141](../core/jobs/runner.py#L141)、同 L130-132 のコメントが
「context を無視する generator は境界でのみ cancellation が効く」と明記）。
したがって video generator が `context` を無視していても終端状態は不定になりません。
#18 の実害は**中断が遅く、その間の計算が無駄になること**であり、
#89 の JSON/SQLite 障害注入・復旧テストとは独立に進められます。

## 5. トラック C / D

- **#6 は Phase 1 の入口**（#79 が製品境界を必要とするため）。単独で先に確定させます。
- **#7 / #21 / #45** は実 weight とハードウェアが要る検証。#90 の証跡としてそのまま使えるので、
  ISO 側の要求フォーマット（環境・モデル・seed・日付のメタデータ）を決めてから実施すると二度手間になりません。
- **#8（画像プリセット 3 種）** は #7 / #21 で既定パラメータが決まってから着手します。
  検証前に作ると、実測後にプリセットを作り直すことになります。
- **#10（実生成 50 件のドッグフーディング）** は #8 の後。#88 の性能ベースラインと
  #94 の安全性リスク登録簿に流し込む実データを兼ねるので、記録フォーマットは
  #80 で決めた測定項目に合わせてから走らせてください。
- **#11（v0.1 release gate）** は #10 の結果を受けて判定。**#96 と統合**します（§3 Phase 4 参照）。
- **#19 / #20 / #29** は較正データ待ち。#80 の「proxy metric であることを明示する」要件と直結します。
- **#3（App.tsx 分割）** は #91 と #62 の前にやると UI 作業が楽になります。
- **#2 / #4** は独立。手が空いたときの詰め物。

トラック C の実行順は **#6 → #7 / #21 → #8 → #10 → #11（#96 と統合）** です。
実 weight とハードウェアが前提になるため、Phase 0〜1 とは独立に、
機材が用意できたタイミングで走らせてください。

## 6. 直近の着手順（最初の 5 手）

§3 の Phase 0 と同じ順序です（1〜3 が Phase 0 そのもの）。4 以降は
トラックをまたいで次に来るものを並べています。

1. ✅ #87 — postcss/nanoid/undici 更新 + `npm audit` ゲートを `verify_local_stack.py` に追加（Phase 0-1、実施済み）
2. issue 整理 — #55 クローズ、#87 本文更新（Phase 0-2。GitHub 操作のため未実施、要承認）
3. ✅ #83 — ruff / mypy / eslint / coverage ゲート（Phase 0-3、実施済み）
4. **次はこれ** — #6 → #79 — 製品境界を確定し、ISO の適用範囲マトリクスを 1 本作る（Phase 1）
5. ✅ #18 — video generator に `raise_if_cancelled()` を接続（トラック B、実施済み）

**#83 を後ろに回さないでください。** ゲートが入る前にトラック B の実装を始めると、
lint / type エラーを含むコードが積み上がってから一括修正することになります。
（2026-08-14 実施分はこの順序どおり、#83 を #6/#79 より先に完了させています。）

## 7. 判断が要る分岐

| 論点 | 選択肢 | 影響 |
| --- | --- | --- |
| ISO の到達目標 | (a) 外部審査を目指す / (b) 内部整合のみ | (b) で縮退できるのは #86（ISMS）と #96（内部監査）の 2 件。**18 → 16**（うち #78 は meta なので着手対象は 17 → 15）。工数では #86 が唯一の XL なので削減幅は件数より大きい |
| トラック優先 | (a) ISO 先行 / (b) v0.3 完成先行 / (c) 並行 | 本計画は (c) 前提。単独作業なら (b)→(a) の逐次のほうが速い |
| #90 の CI マトリクス | (a) macOS を CI に追加 / (b) 手動証跡のまま | (a) は CI 時間とコスト増、(b) は #90 の受け入れ条件を満たしにくい |
