# リリース手順（runbook）

Creative AI Studio のリリースを切る手順です。管理点は issue
[#425](https://github.com/codetea-ping999/Creative_AI_Studio/issues/425)、
リリースの中身は [CHANGELOG.md](../../CHANGELOG.md) です。
成果物からインストールする側の手順は
[install-from-artifact.md](install-from-artifact.md) にあります。

## バージョンの単一情報源

リポジトリルートの `VERSION`（例: `1.0.0`）が唯一の版数です。ここから派生するもの:

| 参照元 | 経路 |
|---|---|
| Python / API | `core/version.py` → `GET /version` |
| 成果物のファイル名 | `scripts/build_release_artifact.sh` |
| スモークテストの対象 | `scripts/smoke_release_artifact.py` |
| Web UI の package metadata | `apps/web/package.json`（一致を検証） |
| デスクトップ bundle | `apps/desktop/src-tauri/tauri.conf.json`（存在する場合のみ検証） |

`tests/test_version_consistency.py` がこの一致を検証します。版数を上げるときは
`VERSION` と `apps/web/package.json`（＋ lockfile）を同時に更新してください。
テストが落ちたら、それは同期漏れです。

RC を切る場合は `VERSION` を `1.0.0-rc.1` にします。ビルドとスモークの両方が
pre-release 付きの版数を受け付け、成果物は `creative-ai-studio-v1.0.0-rc.1.tar.gz`
になります。

## 手順

### 1. 事前確認

```bash
make verify          # CI と同じ検証一式
```

`main` の CI が green であること、`CHANGELOG.md` の最新見出しが `VERSION` と
一致していることを確認します。

### 2. 成果物をローカルで作って検証する

```bash
make release-artifact   # artifacts/creative-ai-studio-v<VERSION>.tar.gz + SHA256SUMS
make release-smoke      # 展開 → venv → 起動 → 通し生成 → SIGTERM 停止まで
```

`build_release_artifact.sh` は **working tree が完全にクリーンであること**を要求します
（成果物は HEAD から `git archive` で切り出すため、未コミットの変更は入りません）。

`smoke_release_artifact.py` は開発用チェックアウトではなく展開した成果物に対して
走ります。これが Gate 3 の「リリース相当の成果物からインストールする」要件です。

### 3. タグを打つ

タグは `VERSION` と一致していなければなりません。一致しない場合、リリース
ワークフローは成果物を作る前に失敗します。

```bash
git tag -a "v$(cat VERSION)" -m "Creative AI Studio v$(cat VERSION)"
git push origin "v$(cat VERSION)"
```

### 4. ワークフローの結果を確認して公開する

タグ push で `.github/workflows/release.yml` が動き、成果物を作って
**ドラフトの** GitHub Release に添付します。自動では公開されません。

公開前に確認すること:

- ドラフトに `creative-ai-studio-v<VERSION>.tar.gz` と `SHA256SUMS` が付いている
- タグが意図した SHA を指している
- `CHANGELOG.md` の既知の問題が最新である

確認できたらドラフトを publish します。

### 5. 公開後

- `CHANGELOG.md` に次の `## [Unreleased]` 節を用意する
- issue #425 の Operator dashboard を更新する
- 持ち越した項目を v1.1 バックログへ移す

## 含まれないもの

- **デスクトップアプリ** — 成果物から `apps/desktop` は除外されます。
- **コード署名 / 公証** — 署名済みインストーラーは配布していません。
- **モデルの重み** — 成果物に重みは含まれません（ビルドスクリプトが混入を検査して拒否します）。
