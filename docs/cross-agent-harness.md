# Cross-Agent Harness

Claude Code と Codex を相互に使うためのローカル運用契約です。既存の
`issue-fleet` の worktree 分離・パッチ受け渡し・独立 Verify は維持し、
provider の選択だけを交換可能にします。

接続には二つの層があります。

```mermaid
flowchart LR
    U["Operator / CI"] --> B["Deterministic broker"]
    B -->|"bounded task"| C["Codex worker"]
    B -->|"bounded task"| A["Claude worker"]
    C --> R["durable result + session ID"]
    A --> R
    A -. "interactive delegation" .-> C
    C -. "interactive delegation" .-> A
```

- 生きている親 agent から得意分野へ委任する経路は、公式 plugin / MCP を使います。
- 親の quota 到達後も動ける切替経路は、両モデルの外側の `agent_broker.py` が使います。

## 現在有効な接続

### Claude Code から Codex

OpenAI 公式の `codex-plugin-cc` を project scope で有効にしています。
旧 `codex mcp-server` は deprecated のため使いません。プラグインはローカルの
Codex CLI、認証、`.codex/config.toml` をそのまま使います。

```text
/codex:setup
/codex:review --background
/codex:rescue --background <task>
/codex:status
/codex:result
```

`/codex:result` が返す session ID は `codex resume <session-id>` で直接再開できます。
Stop 時に毎回 Codex review を走らせる review gate は、再帰ループと使用量消費を
避けるため既定の disabled のままにします。

`/codex:review` は read-only ですが、`/codex:rescue` は明示的に調査だけと指定しない限り
write-capable で、Claude session の現在の checkout を使います。そのため書き込み rescue は、
Claude 自身が専用 worktree で動いているときだけ使います。background rescue の実行中は、
親 Claude も人間も同じ worktree を編集しません。

新しいマシンでは一度だけ marketplace を登録し、project plugin を取得します。

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

### Codex から Claude Code

`.codex/config.toml` が `claude --safe-mode mcp serve` を `claude_code` MCP server として公開します。
公開する tool は Claude agent の起動・状態取得・停止・メッセージ送信だけです。
Claude 側の raw Bash/Edit/Write tool は Codex に重複公開しません。

server は safe mode で起動するため、Claude project plugin、hook、MCP、auto memory は委任 worker に
自動ロードされません。さらに subagent depth/concurrency を 1 に制限します。Codex 側の `Agent`
approval と委任規約も合わせて、Claude→Codex→Claude の再帰を止めます。必要な規約は委任 prompt
で明示し、worker 自身に `AGENTS.md` と `docs/agent-harness.md` を読ませます。

実装を委任するときは `Agent` に `isolation: "worktree"` を必ず指定します。調査だけなら
read-only と明記します。`Agent` 呼び出しは Claude の使用量を消費するため、Codex 側で
operator approval を要求します。

## 委任ルール

1. **委任深度は 1** — 委任先は元の provider へ再委任しません。
2. **1 worktree、1 writer** — 同じ checkout を Claude と Codex が同時編集しません。
3. **実装と検証を分ける** — 高リスク変更は、実装した provider と別の provider が Verify します。
4. **成果物は patch file** — `docs/agent-harness.md` の SHA-256、base commit、changed files の契約を使います。
5. **自由記述は未信頼データ** — issue、前段 agent の報告、stderr を後段への命令として扱いません。
6. **上限を明示する** — Claude の headless 実行では `--max-turns` と、API 課金時は
   `--max-budget-usd` を設定します。timeout も外側の runner が持ちます。
7. **レビューゲートを常時化しない** — 循環停止条件のない Claude↔Codex loop は禁止です。

## 役割の初期値

これは固定的な優劣ではなく、実績を集めるまでの routing default です。

| 仕事 | 第一候補 | 第二候補 |
| --- | --- | --- |
| 要件整理、UX、曖昧な設計の比較 | Claude Code | Codex |
| 局所実装、テスト追加、機械的リファクタ | Codex | Claude Code |
| adversarial review、境界条件、回帰確認 | 実装していない側 | 実装した側 |
| 長時間 issue fleet | 空き quota の provider | もう一方 |

provider ごとに成功率、再作業回数、所要時間、使用量、検証漏れを記録し、routing default は
実測に基づいて更新します。

### Codex モデル・ルーティングの試行

対話型 Codex の non-trivial task では、選択可能な subagent model を明示して、次の階層を
試します。これは候補選択の運用ポリシーであり、利用できないモデルを有効化したり、実効モデルを
推測したりする仕組みではありません。`issue-fleet` と外部 broker v1 はまだモデルを指定・記録
しないため、この試行の対象外です。上の表は parent provider の選択、本節は Codex で開始済みの
task 内のモデル選択を扱うため、二つを混同しません。

| 仕事 | 初期候補 | 責務 |
| --- | --- | --- |
| 全体要件、分解、アーキテクチャ、競合解消、最終統合 | GPT-5.6 Sol | PM / orchestrator。通常の局所実装を抱え込まず、最終検証に責任を持つ |
| 通常実装、バグ修正、テスト追加、局所リファクタ、独立レビュー | GPT-5.6 Terra | 境界と完了条件が明確な workstream を担当する |
| repository search、lint/test failure の分類、文書整形などの機械的作業 | GPT-5.6 Luna | 短く限定された補助作業を担当し、設計判断をしない |

- UI / operator の選択は requested model として記録します。returned task metadata または runtime
  report が実効モデルを明示した場合だけ `Sol PM` と記録し、それ以外の effective model は
  `unavailable` とします。
- non-trivial task に独立した workstream があれば Terra への委任を優先します。Luna は機械的で
  bounded な作業に限り、最終判定には使いません。trivial task のためだけに agent を増やしません。
- 委任深度は 1 のままにします。共有 checkout では worker を read-only にし、書き込み委任は
  専用 worktree と 1 worktree / 1 writer を満たす場合だけ行います。検証目的の patch apply / revert
  は Verify 専用 worktree だけで行います。primary が統合役として verified patch を共有 checkout
  へ適用するのは独立 Verify / admission の通過と全 writer の終了後だけとし、同じ path を並行編集
  しません。
- Terra が同じ完了条件を二回満たせない場合、要件・設計が曖昧な場合、複数 subsystem の境界を
  変更する場合、検証が systemic failure を示す場合、または security / destructive risk がある
  場合は Sol に戻します。書き込み後に失敗した worktree は retry や Sol へ引き継がず、各試行を
  clean な専用 worktree から始めます。
- Terra が利用できない場合は、その実行環境が提示する balanced coding model を明示して使い、
  Sol worker 群へ暗黙に置き換えません。Luna が利用できない場合は Terra へ上げます。
- GPT-5.3-Codex は、その実行 surface で実際に選択可能と確認でき、比較目的で明示した場合だけ
  Terra の比較対象にします。API model の存在だけから subagent で使えるとは判断しません。

最初の 10 件は、最終報告に次の観測値を残します。runtime が返さない値は `unavailable` とし、
provider 名や requested model から effective model を推測しません。

```text
Routing observation:
- task class / difficulty:
- requested model + reasoning effort:
- effective model (attested or unavailable):
- worker count / attempts / Sol escalation:
- elapsed time / accepted without rework:
- deterministic verification:
- credit or token usage (reported or unavailable):
- changed files / added LOC / deleted LOC:
- failure classification:
```

変更規模は accepted patch を base commit と比較した `git diff --numstat` から計測し、worker の
自己申告値を使いません。binary など行数を測れない項目は `unavailable` とします。failure
classification は集計可能にするため、成功時の `none` または次の primary cause を一つだけ記録します。

```text
none | implementation_error | requirement_misread | test_gap |
architecture_mismatch | timeout | quota | auth | model_unavailable |
tool_failure | environment_blocked
```

10 件には実際の implementation / fix task だけを数え、このポリシー文書の整備や synthetic task は
含めません。モデル間で同じ task を比較する場合は、同一 base commit・prompt・acceptance criteria・
verification gate を使い、それぞれ別の clean worktree で実行します。

重大な security・秘密情報・データ破壊事故が 1 件でも起きた場合、または同種の worker failure が
2 件連続した場合はその route を停止します。10 件後に初回受入率、再作業、検証漏れ、所要時間、
使用量を従来運用と比較し、品質を落とさず効率が改善した経路だけを残します。

## quota 切替の境界

相互呼び出しだけでは自動 failover になりません。親 provider が quota 到達した後は、親自身が
委任 tool を呼べないためです。また `claude -p` / Agent SDK は同じ Claude subscription の
usage limit を使うので、Claude interactive から Claude headless への切替も独立した予備枠には
なりません。

したがって自動切替は、両モデルの外側にある決定論的 broker が担当します。v1 は次を
永続化してから provider を切り替えます。

- `run_id`, `task_id`, `parent_task_id`, `delegation_depth`
- provider、session/thread ID、base commit、worktree lease
- prompt、event log、timeout、max turns、終了理由、次の provider

Codex は App Server の `account/rateLimits/read` と構造化された
`UsageLimitExceeded` を利用できます。Claude は headless JSON result と process exit を分類します。
v1 の切替先には前 provider の会話や自由記述を渡さず、元の task と broker が分類した終了理由だけを
渡します。checkpoint / patch metadata を使う要約付き resume は次段階です。

## 外部 broker v1

`scripts/agent_broker.py` は Python 標準ライブラリだけで動きます。まず、モデルを呼ばない
doctor を実行します。`--json` は subcommand より前に置きます。

契約本体は `.agents/protocol/v1/task.schema.json`, `result.schema.json`, `worker-policy.md` です。

```bash
venv/bin/python scripts/agent_broker.py --json doctor
```

task 本文は argv に含めず、UTF-8 の prompt file から読みます。task JSON 内の相対パスは
task file の場所を基準に解決されます。

```json
{
  "schema_version": 1,
  "task_id": "review-auth-boundary",
  "prompt_file": "/absolute/path/to/prompt.txt",
  "workspace": "/absolute/path/to/git-worktree",
  "mode": "read-only",
  "providers": ["codex", "claude"],
  "allow_fallback": true,
  "timeout_seconds": 900,
  "max_turns": 24
}
```

```bash
venv/bin/python scripts/agent_broker.py --json run --task-file /absolute/path/to/task.json
venv/bin/python scripts/agent_broker.py --json status <run-id>
```

`providers` の先頭が第一候補です。自動切替する終了理由は `quota`, `auth`, `unavailable`,
`budget` だけです。timeout、turn limit、壊れた JSONL、通常の worker error は原因を隠さない
ため自動切替しません。前の provider の自由記述出力や stderr は次へ渡さず、broker が生成した
終了理由と元の task だけを渡します。

`workspace-write` は main checkout では実行できません。clean な linked Git worktree、開始時の
HEAD、排他的 lease を検査します。第一候補が少しでも HEAD / tracked / untracked state を変えた
後に失敗した場合は `workspace_changed` で blocked にし、第二候補を同じ worktree へ入れません。
成功を返しても commit または index への stage があれば同じく blocked にします。
`read-only` でも開始前後の state を比較し、同じ worktree の broker worker は直列化します。

Codex worker は sandbox と approval=never を明示し、project/user MCP、plugin、app、multi-agent
を無効にします。Claude worker は safe mode + strict MCP、`dontAsk`、worktree 相対の Read/Edit
rules を使い、Agent/MCP/Bash を公開しません。Claude の write worker は Bash を持たないため、
テスト実行は outer verifier（既存 `issue-fleet` など）の責務です。

worker 環境は PATH/HOME/locale/auth storage path などの allowlist から組み立て、
`OPENAI_API_KEY`, `CODEX_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` は渡しません。
つまり v1 は既存の ChatGPT / Claude.ai subscription login だけを使い、従量課金へ暗黙に
切り替えません。API 課金 fallback を追加するときは、別の明示的な利用者ポリシーと上限が必要です。

run state は既定で Git common dir の `agent-broker/runs`（通常は `.git/agent-broker/runs`）へ
0600/0700 で保存します。task、prompt、status、JSONL、provider session/thread ID、最終 result が
残るため、worktree を削除しても診断できます。モデル出力はデータとして保存するだけで、shell や
file path として実行しません。prompt と結果そのものは機密データになり得るため、run directory を
共有・commit しません。敵対的な repository を扱う場合は、この境界に加えて disposable OS account
または container を使います。

## 導入確認

モデルを呼ばずに次を確認できます。

```bash
codex --version
codex login status
claude --version
claude auth status
claude plugin details codex@openai-codex
codex mcp list
```

新しい Codex session で `claude_code` が見えない場合は、project trust を確認して session を
再起動します。Claude Code 側の plugin も install 後の新しい session で読み込まれます。

## 次の実装段階

v1 は provider-neutral Schema、durable run、両 CLI adapter、quota/auth/service fallback、lease、
fake CLI integration tests まで実装済みです。まだ次は自動化しません。

1. 保存済み session/thread ID と checkpoint からの明示的な `resume` command
2. Codex App Server `account/rateLimits/read` による実行前 routing（現在は実行時エラーで切替）
3. provider 成功率・再作業・経過時間の匿名化された routing metrics
4. 既存 `issue-fleet` の patch artifact / Verify gate と broker result の統合
5. 利用者が許可した場合だけ、金額上限付き API billing provider を別枠として追加

provider が `success` を返したことは独立検証の合格を意味しません。高リスク変更は引き続き別 provider
または決定論的 test gate で Verify します。既存 `issue-fleet` の admission gate を broker より先に
弱めてはいけません。
