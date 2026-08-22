# Agent Harness

複数のサブエージェントで実装を並列に進めるための規約です。
`.claude/workflows/issue-fleet.js` がこの規約に沿って動きます。

エージェントとして作業を始める前に、このファイルを最後まで読んでください。

## この規約が存在する理由

過去に次の 3 つの事故が起きました。規約はそれぞれを構造で防ぐためにあります。

| 事故 | 原因 | 対策 |
| --- | --- | --- |
| 並列エージェントが同じファイルを壊し合った | 全員が同一ワークツリーで作業した | ワークツリー分離と所有権 |
| 未検証のまま実装がマージされた | 検証エージェントが落ちても実装が進んだ | 検証を独立フェーズにし、単独で再実行可能にする |
| 検証役が共有 checkout にパッチを適用した | Verify のワークツリー分離が明示されていなかった | Implement と Verify の両方に専用 worktree を要求する |
| 自動マージが構文エラーを生んだ | 共有ファイルを複数エージェントが同時に編集した | 共有ファイルは統合役だけが触る |

## 検証ゲート

**これを通らないものを「完了」と呼ばないでください。**

`issue-fleet` の Triage・Implement・Verify はそれぞれ専用 worktree で動きます。
受け取ったパッチを適用・revert してよいのは、Verify の隔離された worktree だけです。

```bash
/Users/toyoharukohyama/Documents/Creative_AI_Studio/venv/bin/python -m pytest -q
```

**フロントのゲートは `apps/web/` を変更した場合だけ**適用されます。変更した場合は、
先に依存関係を張ってから実行してください（理由は「実行環境の前提」）。

```bash
ln -s /Users/toyoharukohyama/Documents/Creative_AI_Studio/apps/web/node_modules apps/web/node_modules
npm --prefix apps/web test
npm --prefix apps/web run build
```

`apps/web/` を変更していない場合は「該当なし」と報告してください。
**走らせていないゲートを「通した」と書かないでください。**

### 基準値は自分で測る

固定値をここに書くと必ず陳腐化します（実際に 2 回ずれました）。
**作業を始める前に、変更前の状態で 1 回計測**し、その数字を自分の基準にしてください。

参考値: `ffc34cc` 時点で backend 503 passed / 97 subtests、frontend 52 passed。
自分の変更で基準より減った場合、原因を特定するまで完了報告をしないでください。

作業中は自分のテストファイルだけを回して構いませんが、**完了報告の前に必ず全体を 1 回**通します。

## ファイル所有権

### 共有ファイル（並列エージェントは編集禁止）

次のファイルは複数の作業が集中し、自動マージが壊れやすい箇所です。
**統合フェーズの担当だけが編集します。**

- `bootstrap/factories.py`
- `apps/api/main.py`
- `core/models/loader.py`
- `core/models/__init__.py`
- `generators/*/__init__.py`
- `core/quality/__init__.py`
- `docs/next-tasks.md`
- `.env.example`
- `README.md`

新しいサービスや generator の配線が必要な場合は、**自分で配線せず**、
「どこに何を登録してほしいか」を報告に書いてください。統合役がまとめて行います。

### 自分の担当範囲

割り当てられた issue に紐づくファイルだけを編集します。
範囲外に手を入れたくなった場合は、編集せずに報告へ書いてください。

## 禁止事項

- **`git commit` / `git push` をしない。** コミットはオーケストレータが行います
  （`git add -A` は成果物の受け渡しでのみ使います。後述の「成果物の受け渡し」を参照）
- **`pip install -r requirements.txt` / `npm install` を実行しない。** torch を含み
  非常に重く、共有の venv と node_modules を使えば足ります
- **モデル weight をダウンロードしない。** ネットワークと時間を浪費します
- **既存のテストを「通すために」書き換えない。** 落ちたテストは仕様の主張です。
  仕様の方が誤っていると判断した場合は、変更せずに報告へ根拠を書いてください
- **`main` に直接触らない**

## 実行環境の前提

エージェントのワークツリーは **`origin/main` のクリーンな checkout** です。
`.gitignore` されているものは**存在しません**。実測で確認した内容が次の表です。

| 項目 | 状態 |
| --- | --- |
| Python | ワークツリーに `venv/` は**無い**。本体リポジトリの `venv/bin/python` を絶対パスで使う。自分のワークツリーのコードに対して正しく動くことを実測済み |
| Node | ワークツリーに `node_modules` は**無い**。`apps/web/` を変更した場合のみ、本体から symlink する |
| ffmpeg | `imageio-ffmpeg` 同梱。**システム ffmpeg は無い**ので前提にしない |
| 画像モデル | SDXL weight 未配置。画像生成は実行できない |
| テキストモデル | `template-writer` のみ有効（weight 不要、決定的） |
| TTS | `kokoro` は無効、`voicevox` は外部エンジン必須。**既定では合成できない** |

weight が要る検証は、この環境では**できません**。必要な場合は報告に
「ローカル実機が必要」と明記してください。推測で「動作確認した」と書かないでください。

`flake8` は venv に入っていません。`CONTRIBUTING.md` の lint コマンドは実行できないので、
実行したと書かないでください。

## 成果物の受け渡し

**diff を戻り値の文字列として返さないでください。パッチはファイルで渡します。**

実測した失敗です（run `wf_08239cc6-8cb`）。構造化出力の文字列フィールドで diff を返した
ところ、片方は**ハンクの途中で切り詰められ**、両方が **HTML エスケープ**されました
（`<` が `&lt;` に化ける）。結果としてどちらも `git apply` に失敗しています。さらに、
ワークツリーはワークフロー終了時に削除されるため、**壊れたパッチしか残りません**。

```bash
PATCH_ROOT=/tmp/creative-ai-studio-harness-patches
RUN_ID=<workflow が渡した artifact run ID>
mkdir -p "$PATCH_ROOT"
artifact_dir="$(mktemp -d "$PATCH_ROOT/$RUN_ID-issue-fleet-<issues>.XXXXXX")"
patch_path="$artifact_dir/change.patch"
base_commit="$(git rev-parse HEAD)"
git add -A
git diff --cached --binary > "$patch_path"
changed_files="$(git diff --cached --name-only)"
patch_bytes="$(wc -c < "$patch_path" | tr -d '[:space:]')"
patch_sha256="$(shasum -a 256 "$patch_path" | awk '{print $1}')"
git reset
```

- `git add -A` を先に打つのは、**新規ファイルを含めるため**です
- `--binary` は情報を落とさないため
- 出力先は**ワークツリーの外**にします。中に置くと消えます
- `mktemp -d` を使うため、同じ issue の retry や並列 fleet でも成果物を上書きしません
- 構造化出力の `handoff` に `patch_path`、`patch_bytes`、`patch_sha256`、`base_commit`、
  `changed_files` を返します。`files_changed` と `handoff.changed_files` は同じ一覧です
- `patch_path` はその run と cluster 用の
  `$RUN_ID-issue-fleet-<issues>.<mktemp>/change.patch` でなければなりません。別の一時
  ファイルや過去 run の成果物は統合対象になりません

書いたら、**自分で受け渡しを検証してください。** 別の場所に `origin/main` のワークツリーを
作り、そこで `git apply --check` が通ることを確認してから成功と報告します。

Verify は、適用前に base revision・バイト数・SHA-256・変更ファイルを再計測し、
`git apply --check` を実行します。観測値を `handoff` として返し、実装側の値との不一致、
空の検証証跡、共有ファイルの編集、または `apply_check: false` のどれかがあれば、
`ship` と報告しても統合対象には入りません。Verify の issue 番号も、実装側の
`completed` と完全一致しなければなりません。さらに `ship` には
`tests_rerun_passed: true` と `red_proof_is_assertion_failure: true` が必須です。
未達の acceptance criteria または high-severity finding が一つでもあれば、`ship` は
統合対象になりません。

## コード規約

`CLAUDE.md` と `AGENTS.md` が一次情報です。要点のみ再掲します。

- `from __future__ import annotations` を付ける
- pydantic は v2、スキーマは `ConfigDict(extra="forbid")`
- ファイル末尾に `__all__`
- コメントは **why** を書く。what はコードが語ります
- エラーメッセージは、何が悪いかを名指しする（「invalid input」ではなく対象を書く）
- JSON 永続化は `core/storage/json_files.py` の `write_json_atomic` を使う
- 日本語ドキュメントは既存ファイルの書式に合わせる

### Web UI を変更する場合の追加要件

`AGENTS.md` の要求を満たすまで完了ではありません。

- 390 / 768 / 1280 / 1440px で横スクロールが出ないこと
- キーボード操作、フォーカス可視、ARIA、色以外の状態表現
- loading / empty / error / disabled / 長文 / 大量リスト の各状態
- UI ライブラリを追加しない。既存の design token と CSS を再利用する
- frontend テストと production build を通す

## コミットメッセージ

`CONTRIBUTING.md` に従い、動詞で始めて具体的に書きます。issue 番号を含めます。

```
Constrain scene camera to the supported motion set (#101)
```

## 報告の書き方

完了報告には次を必ず含めてください。

1. **変更したファイル**（フルパス）
2. **公開シグネチャ**（追加・変更した関数やクラス）
3. **検証結果** — 実行したコマンドと出力の末尾
4. **配線の依頼** — 共有ファイルへの登録が必要な場合、その内容
5. **できなかったこと** — 環境制約で検証できなかった項目を正直に書く

「動くはずです」は報告になりません。実行した証拠か、実行できなかった理由を書いてください。
