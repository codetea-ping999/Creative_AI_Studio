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

作業は canonical repository の最新 `main` から短命なブランチを切って始めます。ブランチは
原則として 1〜3 日で PR または統合に回します。未完了の機能を長期ブランチに置く代わりに、
可能なら feature flag を使います。直接 clone した共同開発者は `origin`、fork を clone した
開発者は canonical repository を `upstream` として参照します。

```bash
# fork を clone した場合は最初に一度だけ追加
git remote add upstream https://github.com/codetea-ping999/Creative_AI_Studio.git
git fetch upstream
git switch -c feature/short-description upstream/main
```

- 人のブランチは `feature/...`、`fix/...`、`chore/...`、`refactor/...` を使います。Codex が
  作成するブランチだけは `codex/feature/...`、`codex/fix/...`、`codex/chore/...`、
  `codex/refactor/...` とします。どちらも内容が分かる小文字の kebab-case を使います。
- 直接 clone していて `origin` が canonical repository の場合は、上の `upstream/main` を
  `origin/main` に読み替えます。
- 取り込み前の更新は、作業ツリーが clean であることを確認してから canonical remote を
  fetch します。履歴を書き換える rebase や force push は、共有済みブランチでは行いません。
- 他の作業と並行する、または実験を隔離したい場合は worktree を使います。同一 worktree
  には同時に 1 人（または 1 エージェント）だけが書き込みます。次のコマンドは
  `git switch -c` の代替であり、同じブランチを先に作成してから実行しません。

```bash
git worktree add ../Creative_AI_Studio-fix -b codex/fix/short-description upstream/main
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

コード変更を PR へ出す前の canonical verifier は次のコマンドです。setup、pytest、Python
coverage、Ruff、mypy、Web test/coverage/build、ESLint、npm audit、API smoke を一括で実行します。

```bash
venv/bin/python scripts/verify_local_stack.py --start-api
```

開発中の早いフィードバックには、変更範囲に応じて次の部分検証を使えます。部分検証だけを
実行した場合は「全ローカル検証済み」とせず、未実行項目を PR へ記載します。

| 変更範囲 | 開発中の部分検証 |
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
- マージ後はブランチと不要になった worktree を削除します。ただし `git status --short` が
  clean でも ignored 資産は表示されず、`git worktree remove <path>` で一緒に削除されます。
  削除前に `git -C <path> status --short --ignored` で削除対象自身の ignored file を確認し、
  モデル、DB、outputs、cache などの固有データがあれば別の安全な場所へ複製して checksum を
  確認します。残すデータがないことを確認できた worktree だけを削除します。

## Git に入れてはいけないもの

`.gitignore` は安全網であり、確認の代わりにはなりません。特に次を stage しません。

- `.env`、API token、password、cookie、個人情報、ローカル設定
- `venv/`、`node_modules/`、coverage、ビルド成果物、ログ
- `data/` 配下のローカル DB・生成履歴・キャッシュ、`outputs/` の生成物
- checkpoint、weight、`*.safetensors`、`*.gguf` などの大容量モデル本体

モデルは manifest・設定・取得手順・README・ライセンスなど、`.gitignore` が追跡を許可する
軽量補助ファイルだけを管理します。tokenizer を含む現在 ignore 対象の component を追跡したい
場合は、必要な軽量ファイルだけに限定した ignore 例外を別レビューで追加します。本体は外部
ストレージまたは各ローカル環境に配置します。誤って secrets や大容量ファイルを stage した
場合は、commit / push 前なら `git restore --staged <path>` で外し、対象ファイルを削除せず
安全な場所へ退避します。push 済みの secret は無効化・ローテーションを最優先にし、履歴修正は
管理者と相談して実施します。

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
  byte 数（`wc -c`）、変更ファイル、`git apply --check` を独立に確認する。統合役は受理した
  patch を統合作業用の worktree へ適用する。

## 禁止・要相談の操作

次の操作は通常の開発手順には含めません。必要になったときは、対象・理由・復旧方法を確認してから
リポジトリ管理者が実行します。

- `git push --force`、共有ブランチの rebase、履歴 rewrite
- `git reset --hard`、広範囲の `git clean`、他者の変更を捨てる操作
- push 済みの秘密情報・大容量バイナリを「.gitignore に追加するだけ」で済ませること

困ったときは、まず `git status --short`、`git diff`、`git log --oneline --decorate -n 10` で
状態を確認し、未コミット変更を破棄せずに統合役または管理者へ共有してください。
