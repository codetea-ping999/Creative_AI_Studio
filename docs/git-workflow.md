# Git 運用ルール

Creative AI Studio の変更履歴を安全に保ち、複数人・複数エージェントの作業を
衝突なく統合するための共通ルールです。通常の開発、Codex、Claude Code を問わず
適用します。`issue-fleet` を使う並列実装では、追加で
[agent-harness.md](./agent-harness.md) が優先します。

## 守るべき原則

1. `main` は常に動作・検証可能な状態に保ち、直接変更・直接 push しない。
2. 1 ブランチは 1 目的、1 commit は 1 論理変更にする。無関係な整形、依存更新、
   リファクタリングを機能変更へ混ぜない。
3. 作業開始時と commit 前に `git status --short` を確認する。他者の未コミット変更を
   ステージ、上書き、削除しない。
4. 秘密情報、生成物、DB、モデル weight などローカル成果物を Git に入れない。
5. テストに通った状態だけを commit し、PR に変更理由・検証結果・影響範囲を残す。

## 通常の作業フロー

作業は最新の `origin/main` から短命なブランチを切って始めます。ブランチは原則として
1〜3 日で PR または統合に回します。未完了の機能を長期ブランチに置く代わりに、可能なら
feature flag を使います。

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c codex/feature/short-description
```

- ブランチ名は `codex/feature/...`、`codex/fix/...`、`codex/chore/...`、
  `codex/refactor/...` を使います。人が作成するブランチでも、作業種別と内容が
  分かる小文字の kebab-case を使います。
- 取り込み前の更新は、作業ツリーが clean であることを確認してから `git fetch origin`
  を行います。履歴を書き換える rebase や force push は、共有済みブランチでは行いません。
- 他の作業と並行する、または実験を隔離したい場合は worktree を使います。同一 worktree
  には同時に 1 人（または 1 エージェント）だけが書き込みます。

```bash
git worktree add ../Creative_AI_Studio-feature -b codex/feature/short-description origin/main
```

## 変更を commit する前

ステージ対象を明示的に選び、内容を必ず確認します。`git add -A` は、意図しない
ローカル成果物まで含めるおそれがあるため、通常作業では使いません。

```bash
git status --short
git add path/to/changed-file
git diff --cached --check
git diff --cached
```

次に、変更範囲に応じた検証を実行します。最低限の目安は次のとおりです。

| 変更範囲 | 必須の確認 |
| --- | --- |
| Python / API / core | `venv/bin/python -m pytest -q` |
| `apps/web/` | `npm --prefix apps/web test` と `npm --prefix apps/web run build` |
| 設定・依存関係 | 関連する setup / lint / typecheck と差分確認 |
| ドキュメントのみ | リンク、コマンド、Markdown の表示を確認 |

実行できなかった確認は、理由とともに PR・完了報告へ明記します。実行していない検証を
「通過」とは扱いません。

## commit と PR

commit message は命令形の具体的な英語にし、必要なら issue 番号を末尾に付けます。

```text
Add request validation for image jobs (#123)
Fix duplicate asset export on retry (#124)
Docs: clarify local model setup
```

- commit はレビュー可能で、単独で revert できる粒度にします。
- PR には「目的と変更理由」「主な変更ファイル」「検証コマンドと結果」
  「未検証事項」「関連 issue」を書きます。
- CI が失敗している、競合が未解決、または base branch が大きく古い PR は、まず
  修正・更新してからレビュー依頼します。
- マージ後はブランチと不要になった worktree を削除します。worktree は clean な状態で
  `git worktree remove <path>` を使います。

## Git に入れてはいけないもの

`.gitignore` は安全網であり、確認の代わりにはなりません。特に次を stage しません。

- `.env`、API token、password、cookie、個人情報、ローカル設定
- `venv/`、`node_modules/`、coverage、ビルド成果物、ログ
- `data/` 配下のローカル DB・生成履歴・キャッシュ、`outputs/` の生成物
- checkpoint、weight、`*.safetensors`、`*.gguf` などの大容量モデル本体

モデルは manifest・設定・取得手順・README・ライセンス・tokenizer などの軽量補助ファイルだけを
管理し、本体は外部ストレージまたは各ローカル環境に配置します。誤って secrets や大容量ファイルを
stage した場合は、commit / push の前なら `git restore --staged <path>` で外し、必要に応じて対象ファイルを
削除せず安全な場所へ退避します。すでに push 済みの secret は無効化・ローテーションを最優先にし、
履歴修正は管理者と相談して実施します。

## 並列・エージェント作業の追加ルール

`issue-fleet` または provider 間で作業を委任する場合は、
[agent-harness.md](./agent-harness.md) と
[cross-agent-harness.md](./cross-agent-harness.md) の契約がこの文書より優先します。
最低限、次を守ります。

- `main` checkout を作業場所にせず、Implement と Verify の双方を専用 worktree に分離する。
- **`git stash` を使わない。** stash は worktree 間で共有され、別作業の差分を失う事故につながる。
- サブエージェントは commit / push せず、検証済みの patch file を統合役へ渡す。
- 同じファイルを複数の writer が並行編集しない。共有ファイルは統合役だけが編集する。
- patch の検証目的の適用・revert は Verify 用の隔離 worktree だけで行い、base commit、SHA-256、
  変更ファイル、`git apply --check` を独立に確認する。統合役は受理した patch を統合作業用の
  worktree へ適用する。

## 禁止・要相談の操作

次の操作は通常の開発手順には含めません。必要になったときは、対象・理由・復旧方法を確認してから
リポジトリ管理者が実行します。

- `git push --force`、共有ブランチの rebase、履歴 rewrite
- `git reset --hard`、広範囲の `git clean`、他者の変更を捨てる操作
- push 済みの秘密情報・大容量バイナリを「.gitignore に追加するだけ」で済ませること

困ったときは、まず `git status --short`、`git diff`、`git log --oneline --decorate -n 10` で
状態を確認し、未コミット変更を破棄せずに統合役または管理者へ共有してください。
