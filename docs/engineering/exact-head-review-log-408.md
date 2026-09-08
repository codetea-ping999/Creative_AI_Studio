PR #408: Codex×Claude 8ラウンド Exact-HEAD レビューの記録

1. 概要

PR #408（PR3 — Batch stability, completion convergence, startup recovery）は、
2026-09-06 13:33 の初回実装コミットから、2026-09-08 08:36 の第8ラウンド修正まで、
約43時間・9コミット・8回のCodex exact-HEADレビューを経て、合計40件のfinding
（P1・P2）をすべて解消した状態でCodexの最終レビュー待ちとなっている。

このドキュメントを書いている時点で、PR #408はまだMergeされていない。
Mergeされていない理由は、失敗しているからではなく、[[contract-regression-lessons-376]]
が提案した「改善7: Exact-HEAD Final Review」を文字通り実践しているからである。
レビューされたcommitとMergeされるcommitを一致させ続けるために、8回目のCodex
再レビューが返ってくるまでClaude側からは意図的にMergeしていない。

本ドキュメントは、この8ラウンドの共同作業を記録することを目的とする。
関係者はCodex（adversarial reviewer）、Claude（implementer）、そして各ラウンドの
作業範囲を指示し最終的なMerge判断を持つ人間である。以下、人間A（本セッションの
運用者）、人間B（リポジトリ側のオーナー・受け入れ判断者）という2つの機能的な
役割として記述する。実際にこの2役が同一人物か別人物かは本ドキュメントの主題では
ない。この積み重ねてきた作業を、PRがいずれMergeされて個々のスレッドが埋もれて
しまう前に、共有の記録として残す。

⸻

2. 背景: PR3が解決しようとしている問題

Creative AI Studioは、ゼロモデル重みでも動くジョブキュー型の生成AIスタジオである。
ジョブ（Job, SQLite）、アセット（Asset）、プロジェクト（Project, JSON）、
バイブル（Bible, JSON）、バッチ（Batch, JSON）、ストーリー文書（StoryDocument, JSON）
という異なる永続化層をまたいだドメインモデルを持つ。

PR3が扱う中心的な問題は、次の一文に集約される。

"succeeded Job != completion fully applied"

ジョブが succeeded になったことと、そのジョブの結果が Asset への同期・Story
シーンへの反映・Batch の集計への反映まで完全に終わっていることは、別の事実である。
再起動、部分的な失敗、イベントロス、Batch途中停止のいずれが起きても、

generation result -> Asset -> Story -> Batch

が最終的に収束し、しかもその過程でジェネレーターを二度と再実行しない、という
契約を成立させることがPR3のゴールである。

この収束処理は、`core/jobs/completion.py` の `CompletionConverger.converge_job()`
という単一の経路に集約され、ライブのEventBus購読・起動時リカバリ・実行時retry
ループの3箇所すべてが同じロジックに収束するよう設計されている。

⸻

3. 体制: 誰が何を担当したか

Codex（Adversarial Reviewer）
    exact-HEAD（実際にpushされた正確なcommit SHA）に対してのみレビューを行う。
    レビュー後に新しいcommitが積まれた時点で、そのレビューは無効になるという
    厳格な運用（[[contract-regression-lessons-376]] 改善7の実践）。

Claude（Implementer）
    各ラウンドで指定されたfindingだけを修正する。PR3全体の再設計やPR4/PR5への
    スコープ拡張は毎回明示的に禁止されている。修正のたびに、
      1. reviewされたHEADに対してRED（再現）を確認
      2. 修正してGREENを確認
      3. 決定的なmonkeypatch・DB破損注入でsleepベースのテストを使わない
      4. 修正内容とテスト名とRED→GREEN証拠を該当スレッドへ返信
      5. 直したスレッドだけをresolve
      6. PR本文にラウンドのセクションを追記
      7. 新しいHEADで `@codex review` を再依頼
    という手順を守る。Claude自身は一度もMergeしていない。

人間A（本セッションの運用者）
    各ラウンドの開始時に、今回直してよいfinding番号と、してはいけない範囲拡張
    （PR4/PR5、runtime lease、WorkerPool本番化、GPU concurrency、Desktop/iOS、
    distributed fencing、frontend polling再設計など）、そして「今回はこの
    N件だけを直してください」という具体的なスコープ制約を毎回明示していた。

人間B（リポジトリ側のオーナー・受け入れ判断者）
    最終的なMerge判断、そして [[contract-regression-lessons-376]] のような
    Harness Engineeringの振り返りドキュメントをこのリポジトリの慣習として
    確立した側。

⸻

4. 1ラウンドの流れ（プロセス構造）

Codex: exact-HEAD review
        │
        ▼
   N件のfinding（P1/P2）
        │
        ▼
   人間A: スコープを指定
   このN件だけ。再設計禁止。PR4/PR5禁止。
        │
        ▼
   Claude: 各findingについて
     RED（reviewされたHEADで再現）
        │
        ▼
     Fix
        │
        ▼
     GREEN（regression test追加）
        │
        ▼
   Claude: 要求された9点（後述）の
   Adversarial Self-Review を自分の diff に対して実行
        │
        ▼
   同じ failure boundary 内で新しい
   real finding が見つかった場合のみ
        │
        ├─ 見つからなかった → そのまま
        └─ 見つかった → 追加でFix + Test
                このループの中で完結させる。
                新しいスコープには広げない。
        │
        ▼
   commit → push
        │
        ▼
   各findingのスレッドへ
   fix / test / RED→GREEN証拠を返信
        │
        ▼
   直したスレッドだけresolve
        │
        ▼
   PR本文に「ラウンドN」セクションを追記
        │
        ▼
   新HEADで @codex review を再依頼
        │
        ▼
   Claudeはmergeしない。次のCodexレビューを待つ
        │
        └──────────────► Codexが0件なら人間Bがmerge判断へ
                          Codexがまだ見つければ次のラウンドへ

このループが8回、43時間の間に回り続けた。

⸻

5. 8ラウンドの記録

| ラウンド | HEAD遷移 | finding数 | 内訳 | 中心テーマ |
|---|---|---|---|---|
| 初回実装 | -> `bfd838e` | - | - | PR3のベース実装 |
| 1 | `bfd838e` -> `94000f1` | 10 | P1×6, P2×4 | 起動時ステージ再開、キャンセル競合、安定Job ID、Story replay候補選定、Batch reconciliation確認、SQLite値検証 |
| 2 | `94000f1` -> `52756f6` | 4 | P1×3, P2×1 | キャンセル未確定な子ジョブをqueueへ晒さない、ステージenqueue失敗の伝播、旧形式batchの再利用禁止項目 |
| 3 | `52756f6` -> `cf2f529` | 7 | P1×6, P2×1 | `run_exclusive()`によるキャンセル確認とqueue露出のmutual exclusion境界の確立（中心的な構造変更） |
| 4 | `cf2f529` -> `2c7c461` | 4 | P1×2, P2×2 | 壊れたbatchスキャンの安全な扱い、quarantine済みJobの状態をBatchへ反映、起動時の個別batch単位の障害隔離、completion retryのインデックス化 |
| 5 | `2c7c461` -> `e8490da` | 4 | P1×4 | malformed != absent の全面適用、terminal itemの再作成禁止、起動scanから漏れたbatchの有限回リトライ、raw SQLite値の型検証 |
| 6 | `e8490da` -> `ba9ffbf` | 2 | P1×1, P2×1 | enqueue再読み込み失敗時のリトライ、Story replay候補探索の`SceneCandidateIndex`によるコスト最適化 |
| 7 | `ba9ffbf` -> `f19eea8` | 4 | P1×1, P2×3 | ステージ資材化の早期exit伝播、completion空振り時のスキャン省略、terminal item保全、poison quarantine書き込み失敗のretry候補化 |
| 8 | `f19eea8` -> `9f20894` | 5 | P1×4, P2×1 | ディレクトリscan失敗とstat失敗の「absent」との混同解消、poison quarantine成功後のBatch convergence、poison retryの再検証 |

合計: **40件のfinding**。すべてスレッドとしてPR上に残り、すべてresolve済みである。

⸻

6. 繰り返し現れたパターン: このPRが実際に学んだこと

8ラウンドを通して見ると、個別のfindingは毎回違う場所で起きているが、根底にある
パターンはいくつかの型に収束する。

6.1 「不確実」を「確定」と混同しない、という一つの原則の8つの現れ方

このPRのほぼすべてのfindingは、次の一つの原則の言い換えである。

    確認できなかった（uncertain）
        ≠
    確認した結果、存在しない・終わっている（confirmed）

これが具体的に現れた形:

    ラウンド1: 壊れたStoryファイルを「削除済み」と誤認しない
    ラウンド3: 読み取れないbatchのキャンセル再確認を「キャンセルされていない」と誤認しない
    ラウンド4: 壊れたbatchスキャンを「安全にrequeueしてよい」と誤認しない
    ラウンド5: malformed（構文的に壊れている）なbatchファイルを「所有者不在」と誤認しない
    ラウンド8: ディレクトリのstat・scan失敗（EACCES/EIO/一時的OSError）を
              ENOENTによる「確定した不在」と誤認しない

`Path.exists()` は Python の落とし穴の典型例として、ラウンド8で2つの
メソッド（Batch側の`get_or_diagnose()`、Story側の`get_for_recovery()`）に
同じ形で紛れ込んでいたことが発見された。`Path.exists()` は
`FileNotFoundError` だけでなく、あらゆる `OSError` に対して `False` を返す
ため、「一時的に読めない」と「そもそも存在しない」を静かに同じ結果に潰して
しまう。ラウンド8ではこれを `stat()` と明示的な `except FileNotFoundError`
・`except OSError` の分離に置き換え、Batch側とStory側の両方で修正した。

興味深いのは、`get_or_diagnose()`自身がこれで2回目の「absent判定」修正を
受けているという点である。ラウンド2で導入されたこのメソッドは、ラウンド5の
Adversarial Self-Reviewで一度「malformedな内容をconfirmed-absentと混同する」
という別の欠陥を修正されており（6.1節冒頭のラウンド5の項目に対応する）、
ラウンド8で今度は`Path.exists()`のOSError型の混同という、また別の角度からの
同じ「uncertain != confirmed」原則違反を修正されている。同じメソッドが3ラウンド
の間隔を空けて2回、異なる経路から同じ原則に違反していた、という事実は、
この種の「absent判定」を担うメソッドがいかに壊れやすいかを物語っている。

6.2 Terminal（終端）状態のitemを再作成しない

ラウンド2・5で2度現れたパターン。旧形式のキャンセル済みitem、あるいは
Job行が後から失われたsucceeded/failed/cancelled itemを、「job_idがない・
読めない」という理由だけで新しいJobとして再作成・再実行してしまうと、
一度確定した結果を静かに上書きしてしまう。「終端状態のitemは、それがどんな
経緯であれ、二度と再作成しない」という規則を、`_assign_ids()`と
`_enqueue_stage()`の両方に、それぞれ独立に適用する必要があった。

6.3 一つのprimitiveを複数箇所から呼ぶなら、その呼び出し箇所すべてを洗い出す

これはラウンド8自身のAdversarial Self-Reviewが発見した、最も再利用性の高い
教訓である。

ラウンド8で追加した「poison quarantine成功後にBatchへconvergenceの機会を
与える」という修正は、最初 `run_startup_recovery()` の起動時quarantine
経路にだけ実装された。しかし `quarantine_poison_row_safely()` という同じ
primitiveは、`CompletionConverger._retry_poison_quarantine_candidates()`
という実行時retryループからも呼ばれていた。この2つ目の呼び出し箇所には
同じフォローアップ処理が入っていなかったため、poison行が起動時ではなく
実行時retryで解決した場合に限って、Batchが古いステージに永久に止まったまま
になるという抜け穴が残っていた。

同じセルフレビューはもう一つ、`run_startup_recovery()`のstep 5（バックストップ
のreconcileパス）が、ラウンド1で修正したはずの
「`Path.glob()`が`os.scandir()`のOSErrorを静かに飲み込む」というバグを、
`BatchService.list_batches()`という別の呼び出し経路経由でまだ持っていることも
発見した。

    quarantine_poison_row_safely()
            │
    ┌───────┴────────┐
    ▼                ▼
起動時経路        実行時retry経路
（修正済み）      （最初は未修正のまま
                   放置されていた）

    Path.glob()による走査
            │
    ┌───────┴────────┐
    ▼                ▼
list_all_tolerant()   list_all()
（ラウンド1で修正）   （step 5経由で
                       同じ穴が残っていた）

いずれも、「バグを見た場所」ではなく「バグの原因となっている関数・パターンを
呼んでいる全箇所」を機械的に洗い出すことで発見できた。これは
[[contract-regression-lessons-376]] が「改善2: Blast Radius Analysis」として
提案した考え方、すなわち「どのファイルを変更したかではなく、変更した意味をどこが
利用しているかを見る」という視点が、実際のレビューループの中で機能した具体例である。

6.4 Mutual Exclusion境界を「前後のチェック」で近似しない

ラウンド3は、それまでの2ラウンドが「キャンセル確認 → ウィンドウ → queueへの
露出」という順序を、個別のif文の追加で塞ごうとしていたのに対して、根本的な
構造変更を行った。`BatchRepository.run_exclusive(fn)`という新しいprimitiveで、
「キャンセル状態の読み取り」と「queueへの露出」を同じロックの中でatomicに
実行するようにした。これ以降のラウンド（4から8まで）では、この境界に対する
新しいfindingは一度も出ていない。個別のrace conditionを一つずつ塞ぐのではなく、
race自体が構造的に起こり得ない境界を一度作ってしまう方が、長期的には
findingの発生率を下げる、という実例になっている。

6.5 正しさを保ったままコストを削る

ラウンド6は、他のラウンドとは性質が異なり、正しさそのものではなく
「Story replay候補選定が、収束させるJobの数だけ全件スキャンを繰り返している」
というコスト面のfindingだった。`SceneCandidateIndex`という1パスに1回だけの
共有キャッシュを導入したが、このとき自己レビューが「poison行の分類ロジックを
書き換えた際に、無関係なpoison行1件がプラットフォーム全体のStory replay
収束を止めてしまう」という重大な退行を、コミット前に発見して修正している。
最適化それ自体は正しかったが、最適化を実装する過程で新しい退行を生みかけた、
という点は率直に記録しておく価値がある。

⸻

7. Round 8のAdversarial Self-Reviewが実際に機能した様子

ラウンド8では、5件のfindingを修正したあと、次の9項目の自己レビューが
明示的に要求されていた。

    1. Path.exists() がrecovery truth判定に残っていないか
    2. directory enumeration failureがempty scan扱いされる別経路がないか
    3. Batch/Story双方でENOENTとother OSErrorが正しく分離されているか
    4. poison quarantine成功後のBatch convergenceがduplicate advanceを
       起こさないか
    5. delayed poison retryがfresh-valid rowを誤quarantineしないか
    6. repaired queued Jobがduplicate queue exposureされないか
    7. poison retry candidateのleakがないか
    8. recovery pathからgeneratorを直接呼んでいないか
    9. previous cancellation/queue exclusion lock orderingを壊していないか

この9項目を、3つの独立した視点（それぞれ別々のAgentインスタンス）から
並行して検証した。結果は7項目がPASS、2項目がFAILだった。

FAILした2項目は、どちらも「今回の5findingsと直接同じfailure boundary」で
見つかった実在のP1級の抜け穴であり（6.3節で述べた2つの抜け穴）、指示どおり
その場で追加のFixとテストを作成し、RED→GREENを確認してから同じcommitに
含めた。PR4/PR5への拡張は一切行っていない。

これは、単に「レビューを求められたから形式的にやった」チェックリストでは
なく、実際に2件の本物のバグを本番commitに入る前に止めた、という点で、
このプロセスが実際に機能していることの一つの証拠である。

⸻

8. #376との接続: 「Exact-HEAD Final Review」は理論から実践になった

[[contract-regression-lessons-376]] は、PR #376がMerge後に発見したP1級の
意味論の不整合から、次の教訓を導いていた。

    reviewされたcommit と mergeされたcommit が一致しない状態を
    許してはいけない

    Final HEAD abc123
           ↓
    CI abc123 ✅
           ↓
    Adversarial Review abc123 ✅
           ↓
    new commitなし
           ↓
    Merge abc123

PR #408は、この規律を8回連続で実行し続けている生きた実例である。8回とも、
「新しいcommitが積まれた時点でそれ以前のレビューはINVALIDとして扱い、
必ず新しいHEADに対して再レビューを依頼する」という手順を一度も崩していない。
このドキュメントを書いている時点でも、Claudeは9f20894というHEADに対して
Codexの再レビューが返ってくるのを待っており、それが返るまでMergeしていない。

また、#376が「改善2: Blast Radius Analysis」として提案した、変更した意味を
どこが利用しているかを見るという考え方は、6.3節で示した通り、
ラウンド8のAdversarial Self-Reviewの中で、Claude自身が自分の直前の修正に
対して実行することで、実際に2件のfindingを発見する形で機能した。

一方で、#376が提案した「改善6: Contract Verifier Agent」のような専用の
役割分担や、「改善8: Post-Merge Canary」はPR #408のこのプロセスにはまだ
組み込まれていない。8ラウンドの中でCodexとClaudeの役割は一貫して
「Adversarial Reviewer」と「Implementer + Self-Reviewer」の2役のままであり、
Contract Verifierに相当する第三の狭い役割は、少なくとも今のところ、
Claude自身の自己レビューフェーズが兼務している。これは#376が提案した
理想形に対してまだ発展途上の部分であり、今後の改善候補として残しておく。

⸻

9. 数字で見る8ラウンド

- PR作成: 2026-09-06 13:33:58 UTC
- 最終更新（ラウンド8）: 2026-09-08 08:36:12 UTC、約43時間後
- コミット数: 9（初回実装 + 8ラウンド）
- 解消したfinding総数: 40。P1が過半数を占める。ラウンド1の6件、ラウンド3の6件、
  ラウンド5の4件、ラウンド8の4件など、毎ラウンドP1が中心だった
- レビュースレッド: 40件、すべてresolve済み、0件が未解決
- テスト数の推移: 1639件（ラウンド1後）→ 1644 → 1654 → 1668 → 1680 →
  1686 → 1699（ラウンド7時点）→ 1708件（ラウンド8時点）
- カバレッジ: 89.10%〜89.27%の間で常に floor 85%を上回って推移
- ラウンドを通じて ruff/mypy が失敗した回数: 0
- ラウンドを通じてsleepベースのテストが使われた回数: 0
  すべてthreading.Barrier・threading.Event・決定的monkeypatchで代替している
- ラウンドを通じてgeneratorが誤って再実行された回数: 0
  「succeeded jobを二度と再実行しない」という契約は8ラウンドを通じて一度も破られていない

⸻

10. まだ残っているリスク: 正直に書く

このドキュメントは成功だけを記録するものではない。ラウンド5・6で明示的に
「調査したが今回は直さない」と判断された残存リスクは、ラウンド8の時点でも
未解決のままである。

- ラウンド5由来: 「terminal eventが失われる」かつ「その直後にJob行が
  独立して削除される」という2つの独立した低確率failureが特定の順序で
  重なった場合、Batch側は「終端状態のitemのJob行が後から消えた」ケースと
  「そもそも一度も開始されていない」ケースを区別できない。これを完全に
  塞ぐには`run_startup_recovery()`のステップ順序そのものを変える構造変更が
  必要で、「今回はこのN件だけ」というスコープ制約の外にあるとして、
  8ラウンドを通じて一貫して先送りされている。
- ラウンド6由来: 実行時retryループが1tickにつき1つの`SceneCandidateIndex`
  を共有することで、「そのtickの間に新しくsucceededになったJob」がその
  tickのindexには見えない、という既存の（このPRが導入したのではない）
  race conditionの窓が、1クエリ分から1tick分に広がっている。次のtickか
  ライブのEventBus経路のどちらかで必ず正しく収束するため、許容範囲内と
  判断され、修正はされていない。
- ラウンド8由来: poison retryの再検証（finding 5）には、再検証用の
  `get()`呼び出しと`quarantine_poison_row_safely()`内部の実際のCAS判定の
  間に、理論上のごく短いTOCTOUの窓が残っている。ただしこの窓で競合が
  起きてもCAS自体が安全に失敗し（`already_resolved`として扱われ、
  誤った上書きは起きない）、実害は「復旧が1tick遅れる」だけに留まる。

いずれも、原因は特定済み・影響範囲は限定的・修正には今回のスコープを
超える構造変更が必要、という共通点を持つ。「今は直さない」という判断を
下したこと自体も、この8ラウンドの意思決定プロセスの一部として記録しておく。

⸻

11. このプロセスから一般化できる教訓

1. 「今回はこのN件だけ直す」という強いスコープ制約は、レビューの往復回数を
   増やす代わりに、1回あたりの変更の検証可能性を極端に高くする。8ラウンド
   すべてでRED→GREENの証拠が個別のfinding単位で残っているのは、この
   スコープの狭さのおかげである。

2. Adversarial Self-Reviewを「自分の直前の変更に対してだけ」実行するという
   狭い範囲設定でも、6.3節・7節で示した通り、実在のP1級バグを検出できる。
   ただし、その自己レビュー自体が見つけたfindingにもさらにスコープの
   境界線（「今回の5findingsと直接同じfailure boundaryのみ」）を課すことで、
   自己レビューが際限なくPR4/PR5の領域まで踏み込むことを防いでいる。

3. 「不確実」と「確定」を型として明示的に分離するパターン、すなわち
   `(record, uncertain: bool)` や `(record, confirmed_absent: bool)` の
   ようなタプル戻り値は、このPRの中で最も繰り返し再利用された設計判断
   である。一つのメソッドがこの区別を持つと、そのメソッドを呼ぶ側は
   自然と「uncertainなら保守的に倒す」という判断を強制される。

4. 同じprimitiveへの複数の呼び出し箇所は、修正を1箇所に適用したあとで
   必ず横展開の確認が要る。これは6.3節で見た通り、レビューの見落としの
   中で最も再発しやすいパターンだった。

⸻

12. 結び

このPRの40件のfindingのほとんどは、単体では小さい。`Path.exists()`の
挙動、`Path.glob()`が飲み込む例外、1つのif分岐の順序。しかしその一つ一つが、
「再起動しても、部分的に失敗しても、succeeded jobを二度と再実行せずに
最終的に正しい状態へ収束する」という、このプロジェクトが掲げた契約を
壊し得るものだった。

8ラウンド・43時間・40件のfinding・一度もMergeを急がなかった判断。
これはCodexとClaudeという2つのAgentだけの成果ではなく、各ラウンドで
スコープを絞り、拡張の誘惑を断り、最終的な受け入れ可否を握り続けた
人間の判断があって初めて成立したプロセスである。このドキュメントは、
その積み重ねを、PRがいずれMergeされて個々のスレッドが埋もれてしまう前に、
共有の記録として残すために書かれている。
