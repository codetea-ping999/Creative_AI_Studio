#376から学んだHarness Engineeringの改善

1. 概要

Creative AI Studio の PR #376 では、Reference Image Conditioning の実装と改善を進める中で、多数のレビュー、回帰テスト、CI検証を経て最終的にMergeまで到達した。

しかしMerge後の追加確認によって、strength=0 の意味論に関連するP1レベルの不整合が複数見つかった。

CIはgreenであり、主要なレビュー指摘も解消済みだったにもかかわらず、仕様変更の影響が複数レイヤーへ波及し、一部に旧来の意味論が残っていた。

この経験から、従来のHarnessには、

「変更したコードが正しいことを確認する力」はあったが、
「変更した意味がシステム全体で一貫していることを確認する力」が不足していた

と整理できる。

本ドキュメントでは、この事例から得られた教訓と、今後のHarness Engineering改善方針を記録する。

⸻

2. 発端

PR #376では、Reference Imageのstrengthについて次の契約を確定した。

strength = 0
→ conditioning effectなし
→ applied referenceとして扱わない
→ reference slotを消費しない
→ primary conditioning selection対象外
→ requested/audit metadataには残す

この仕様をOption Aとして採用し、JobServiceとImageGeneratorへ実装した。

さらに回帰テストも追加され、以下を確認した。

* zero-strength referenceはconditioningされない
* nonzero referenceと併存できる
* requested metadataにはzero-strength referenceを保持する
* applied metadataには実際に使ったreferenceのみ記録する
* pytest / ruff / mypy / CIが成功する

一見すると、仕様変更は完了していた。

⸻

3. Merge後に見つかった問題

Merge後の追加確認で、少なくとも以下のP1問題が判明した。

3.1 zero-strength referenceがper-role validationに残る

実装上は最終的に、

effective_references = strength > 0

としてconditioning対象を選んでいた。

しかし一部のvalidationでは、そのfilterより前にraw referencesを使っていた。

そのため、

same role:
reference A strength=0
reference B strength=0.8

のようなケースで、本来は有効なreferenceが1件しかないにもかかわらず、

2 references exist

としてrejectされる可能性があった。

つまり、

Runtime semantics:
zero-strength = no-op

なのに、

Validation semantics:
zero-strength = existing reference

という矛盾が残っていた。

⸻

3.2 all-zero referenceでもcapabilityを要求する

さらに、

reference A strength=0
reference B strength=0

のように、全referenceがno-opの場合にもreference capability validationが走るケースがあった。

Option Aの契約では、

effective references = 0

なので、これは通常のtext-to-image generationとして扱われるべきである。

しかし実装の一部では、

requested references > 0
→ reference-capable model required

という古い前提が残っていた。

その結果、

all references strength=0
+
reference非対応model

という本来成功すべきケースが422になる可能性があった。

⸻

4. なぜ予測できなかったか

今回の最も重要な問いは、

なぜ大量のテスト・レビュー・CIがあったにもかかわらず、これを予測できなかったのか

である。

原因は単純なテスト不足だけではない。

⸻

4.1 変更箇所を見ていて、意味の波及を見ていなかった

strength=0の修正対象として最初に意識されたのは、

reference count
primary selection
metadata

だった。

しかし実際には、この変更はより大きな意味論変更だった。

Option A以前は、

reference exists
≈
conditioning reference exists

と近似できた。

Option A以後は、

requested reference exists
≠
effective reference exists

となった。

つまり、単なる条件分岐修正ではなく、Referenceという1つの概念が、

Requested References
Effective References
Applied References

という3つの状態へ分裂した。

このdomain model changeを、局所的なimplementation changeとして扱ってしまったことが最初の原因だった。

⸻

5. テスト数ではなく「テスト空間」が不足していた

PR #376では1000件を超えるテストが成功していた。

しかし、

all-zero
+
unsupported capability

や、

zero + nonzero
+
same role
+
different source

といった組み合わせが存在しなければ、その領域は検証されていない。

つまり、

tests passed = 1265

という数字自体は品質を保証しない。

重要なのは、

仕様の状態空間をどこまでカバーしているか

である。

⸻

6. 従来Harnessの限界

従来のHarnessは主に以下の流れだった。

Issue
 ↓
Implement
 ↓
Unit / Integration Test
 ↓
Adversarial Review
 ↓
CI
 ↓
Merge

この構造は非常に強い。

しかし基本的には、

実装した差分が安全か

を確認する仕組みだった。

今回必要だったのは、

変更した契約がシステム全体で同じ意味を持っているか

を確認する仕組みだった。

ここが次の改善点となる。

⸻

7. 改善1: Contract / Invariantを先に定義する

今後、意味論を変更する場合は、実装より先にInvariantを明文化する。

今回なら、

Invariant: strength == 0
1. conditioning effectなし
2. capability requirementなし
3. applied slotを消費しない
4. primary selection対象外
5. requested/audit provenanceには残る

とする。

Agentには、

strength=0を実装せよ

ではなく、

この5つのInvariantを全レイヤーで成立させよ

と要求する。

Implementationは変わってもInvariantは維持される。

⸻

8. 改善2: Blast Radius Analysis

ContractやDomain Modelが変わった場合は、自動的にBlast Radius Reviewへ昇格する。

例えばReference semanticsの変更なら、

Reference contract change
        ↓
Blast Radius Analysis
        ↓
Schema
API
Request Validation
Capability Validation
JobService
Generator Runtime
Batch
Story
Persistence
Metadata
Web Client
Documentation
Tests

を洗い出す。

ここでは、

どのファイルを変更したか

ではなく、

変更した意味をどこが利用しているか

を見る。

⸻

9. 改善3: Cross-layer Contract Test

同じテストケースを複数レイヤーへ流す。

               same case
                  │
        ┌─────────┼─────────┐
        ↓         ↓         ↓
    Preflight   Runtime   Metadata

例えばReferenceなら、

Source	Role	Strength	Capability	Expected
direct	same	0 + 0.8	supported	success
Bible	same	0 + 0.8	supported	success
mixed	same	0 + 0.8	supported	success
direct	any	all 0	unsupported	text2img success
Bible	any	all 0	unsupported	text2img success
mixed	any	all 0	unsupported	text2img success
any	different	>0 + >0	single-image backend	reject

というmatrixを作る。

これによって、

JobService = OK
Generator = NG

のような意味論の分裂を検出できる。

⸻

10. 改善4: Requested / Effective / Appliedを共通概念化する

今回得た最も再利用性の高い考え方の一つが、

Requested
 ↓
Effective
 ↓
Applied

である。

Referenceだけでなく、

Requested Model
→ Effective Model
→ Applied Runtime

や、

Requested Config
→ Effective Config
→ Applied Config

にも使える。

この3段階を明確にすると、silent fallbackやvalidation/runtime divergenceを検知しやすくなる。

⸻

11. 改善5: Sentinel Boundary Test

今回の0のように特殊な意味を持つ値は、バグの発生源になりやすい。

代表例:

0       = disabled
None    = inherit
[]      = empty
""      = absent
-1      = unlimited
1.0     = maximum

今後はsentinel semanticsが存在する場合、

exact sentinel
just above sentinel
normal value
maximum value
sentinel + active value
sentinel + unsupported capability

を標準テスト項目とする。

⸻

12. 改善6: Contract Verifier Agent

Implement AgentとAdversarial Reviewerの間に、Contract Verifierを配置する。

Claude Implement
       ↓
Contract Verifier
       ↓
Codex Adversarial Review

Contract Verifierはコード品質をレビューしない。

確認対象はInvariantのみとする。

例えば、

assert zero.requires_capability == false
assert zero.consumes_slot == false
assert zero.is_applied == false
assert zero.exists_in_requested_metadata == true

のような確認に専念する。

Agentの役割を狭くすることで、検証の見落としを減らす。

⸻

13. 改善7: Exact-HEAD Final Review

今回の重要な教訓として、

reviewされたcommit

と、

mergeされたcommit

が一致しない状態を許してはいけない。

今後の高リスクPRでは、

Final HEAD abc123
       ↓
CI abc123 ✅
       ↓
Adversarial Review abc123 ✅
       ↓
new commitなし
       ↓
Merge abc123

を必須とする。

Final Review後にcommitが追加された場合、

Final Review = INVALID

として再レビューする。

⸻

14. 改善8: Post-Merge Canary

Mergeを最終地点にしない。

main
 ↓
Contract Canary

を追加する。

Canaryでは、重いfull testではなく、最重要Invariantを短時間で確認する。

例:

reference_contract_canary
runtime_cache_canary
job_state_canary
auth_boundary_canary

失敗した場合は、

Canary failure
 ↓
P1 regression
 ↓
dependent work停止
 ↓
Hotfix Queue

として扱う。

⸻

15. 新しいHarness構成

今後の理想的なHarnessは次の形となる。

                    User
                     │
                     ▼
                Orchestrator
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
     Planner      Contract     Blast Radius
      Agent        Agent          Agent
        │            │             │
        └────────────┼─────────────┘
                     ▼
                 Implement
                   Agent
                     │
                     ▼
              Contract Verifier
                     │
                     ▼
              Adversarial Review
                     │
                     ▼
                    CI
                     │
                     ▼
              Final HEAD Guard
                     │
                     ▼
                  Merge
                     │
                     ▼
                Main Canary
                     │
              ┌──────┴──────┐
              ▼             ▼
             OK         Regression
                             │
                             ▼
                       P1 Hotfix Queue

⸻

16. 今回の本質

今回の問題は、

テストが少なかった

だけではない。

また、

レビューが甘かった

だけでもない。

本質は、

「Reference」というdomain concept自体が変化したにもかかわらず、その変更を局所的な実装修正として扱った

ことにある。

今後は変更を、

Implementation Change
Behavior Change
Contract Change
Domain Model Change

に分類する。

Contract ChangeまたはDomain Model Changeであれば、自動的に、

Invariant Definition
Blast Radius Analysis
Cross-layer Testing
Exact-HEAD Review
Post-Merge Canary

を要求する。

⸻

17. 開発思想としての教訓

複雑なシステムでは、すべての影響を人間が事前に予測することはできない。

したがって目標は、

人間が全て予測する

ではない。

目指すべきは、

予測漏れが発生することを前提に、
Harnessが未知の影響範囲を探索する

ことである。

Harness Engineeringの目的は、AI Agentを大量に動かすことではない。

変更の意味を定義し、その意味がシステム全体で一貫していることを継続的に証明する仕組みを作ること

である。

⸻

18. 今後

この経験を一度きりの反省で終わらせない。

今回のP1 hotfixを、新しいQuality Gateの最初のpilotとする。

今後も、

失敗
 ↓
原因分析
 ↓
Invariant化
 ↓
Harness改善
 ↓
再発防止
 ↓
次の失敗からさらに改善

を繰り返す。

個々のバグをゼロにすることより、

バグや見落としから開発システムそのものが学習する速度を上げる

ことを重視する。

このサイクルを継続することで、Creative AI Studioだけでなく、今後のAgent Runtimeや他プロジェクトにも再利用できるHarnessへ成長させていく。