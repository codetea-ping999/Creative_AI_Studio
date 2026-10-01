# リリース手順（runbook）

Creative AI Studio のリリースを切る手順です。管理点は issue
[#425](https://github.com/codetea-ping999/Creative_AI_Studio/issues/425)、
リリースの中身は [CHANGELOG.md](../../CHANGELOG.md) です。
成果物からインストールする側の手順は
[install-from-artifact.md](install-from-artifact.md) にあります。

## バージョンの単一情報源

リポジトリルートの `VERSION`（例: `1.0.0`）が唯一の版数です。ここから派生するもの:

派生先は 2 種類に分かれます。

**リリースの同一性**を表すもの。pre-release 接尾辞（`-rc.1`）をそのまま含みます。
「どれを切ったか」を名指しする必要があるためです。

| 参照元 | 経路 |
|---|---|
| タグ | `v` + `VERSION` |
| 成果物のファイル名 | `scripts/build_release_artifact.sh` |
| スモークテストの対象 | `scripts/smoke_release_artifact.py` |
| 実行中のインスタンス | `core/version.py` → `GET /version` |

**リリースの中身**を表すもの。接尾辞を落とした base version（`1.0.0-rc.1` → `1.0.0`）
を使います。候補版とその本番版で中身の記述は同じだからです。

| 参照元 | 経路 |
|---|---|
| CHANGELOG の節見出し | `## [1.0.0]` |
| Web UI の package metadata | `apps/web/package.json` と lockfile |
| デスクトップ bundle | `apps/desktop/src-tauri/tauri.conf.json`（存在する場合のみ） |

`tests/test_version_consistency.py` がこの対応を検証します。

**RC を切るときに書き換えるのは `VERSION` だけです。** `1.0.0-rc.1` にすると、
タグと成果物は `v1.0.0-rc.1` になり、CHANGELOG と package metadata は `1.0.0` の
ままで通ります。rc.2、rc.3 と重ねても同じで、CHANGELOG の見出しを書き換える必要は
ありません。

本番版に上げるときは `VERSION` から `-rc.N` を外します。メジャー / マイナー /
パッチを動かすときだけ、`VERSION` と `apps/web/package.json`（＋ lockfile）を
同時に更新してください。

## 手順

### 1. 事前確認

```bash
make verify          # CI と同じ検証一式
```

`main` の CI が green であること、`CHANGELOG.md` に base version の節が
あることを確認します（整合テストが落ちていなければ後者は満たされています）。

### 2. 成果物をローカルで作って検証する

```bash
make release-artifact   # artifacts/creative-ai-studio-v<VERSION>.tar.gz + SHA256SUMS
make release-smoke      # 展開 → venv → 起動 → 通し生成 → SIGTERM 停止まで
```

`build_release_artifact.sh` は **working tree が完全にクリーンであること**を要求します
（成果物は HEAD から `git archive` で切り出すため、未コミットの変更は入りません）。

`smoke_release_artifact.py` は開発用チェックアウトではなく展開した成果物に対して
走ります。これが Gate 3 の「リリース相当の成果物からインストールする」要件です。

RC（`-rc.N`）を切ったあとは、公開や本番版への昇格の前に
[validation-window.md](validation-window.md) の検証期間を実手元環境で実施します。
Cloud / CI の green だけでは、このゲートを満たしません。

### 3. third-party notices を再生成する

```bash
make third-party-notices     # requirements.txt 由来の venv と node_modules が必要
```

`THIRD_PARTY_NOTICES.md` は手書きのリストではなく、**実際にインストールされた
パッケージのメタデータから生成**します。依存を変えたときと、リリースを切る前に
再生成してコミットしてください。直接依存が環境に入っていない場合、スクリプトは
不完全なファイルを書かずに失敗します。

解決される依存はプラットフォームによって変わる（例: Linux では NVIDIA 系の
パッケージが入る）ため、生成は**リリースを切るのと同じ環境**で行ってください。
この理由から、生成物の一致を CI の検査には入れていません。

### 4. タグを打つ

タグは `VERSION` と一致していなければなりません。一致しない場合、リリース
ワークフローは成果物を作る前に失敗します。

```bash
git tag -a "v$(cat VERSION)" -m "Creative AI Studio v$(cat VERSION)"
git push origin "v$(cat VERSION)"
```

### 5. ワークフローの結果を確認して公開する

タグ push で `.github/workflows/release.yml` が動き、成果物を作って
**ドラフトの** GitHub Release に添付します。自動では公開されません。

公開前に確認すること:

- ドラフトに `creative-ai-studio-v<VERSION>.tar.gz` と `SHA256SUMS` が付いている
- 両方をダウンロードして同じディレクトリに置き、`sha256sum -c SHA256SUMS` が通る
- rc など接尾辞付きのタグでは、ドラフトが pre-release として印されている
- タグが意図した SHA を指している
- `CHANGELOG.md` の既知の問題が最新である

確認できたらドラフトを publish します。

### 6. 公開後

- `CHANGELOG.md` に次の `## [Unreleased]` 節を用意する
- issue #425 の Operator dashboard を更新する
- 持ち越した項目を v1.1 バックログへ移す

## 含まれないもの

- **デスクトップアプリ** — 成果物から `apps/desktop` は除外されます。
- **コード署名 / 公証** — 署名済みインストーラーは配布していません。
- **モデルの重み** — 成果物に重みは含まれません（ビルドスクリプトが混入を検査して拒否します）。
