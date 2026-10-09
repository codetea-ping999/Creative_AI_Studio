# Implementation Validation Window チェックリスト

RC（`v1.0.0-rc.N`）を切ってから RC 判定までの検証期間に、手元の実機で辿る手順です。
ゲートの定義は issue
[#425](https://github.com/codetea-ping999/Creative_AI_Studio/issues/425) の
「Gate 4 addition — mandatory Implementation Validation Window」コメントが正で、
この文書はそれを実行できる手順に落としたものです。

**Cloud / CI の green だけではこのゲートを満たしません。** 少なくとも 1 回は、
利用者が受け取るのと同じ成果物を、実際の手元環境（macOS Apple Silicon）で
動かしてください。開発用チェックアウトでの確認は数えません。

所見は末尾の記録表に書き、必ず **Release Blocker / Known Issue / v1.1** の
どれか 1 つに分類します。使っていて気になった点は、分類する前に機能追加へ
変えないでください。

## 0. 準備

- ドラフト Release から `creative-ai-studio-v<VERSION>.tar.gz` と `SHA256SUMS` を
  ダウンロードする。開発用チェックアウト（`~/Documents/Creative_AI_Studio`）とは
  **別の空ディレクトリ**で作業する。
- 検証用に、開発用チェックアウトの API（port 8000 など）は止めておく。

```bash
mkdir -p ~/rc-validation && cd ~/rc-validation
# ここに tarball と SHA256SUMS を置く
python3 --version          # 3.10 以上であること（下の注意を参照）
```

> **macOS の注意**: Xcode Command Line Tools の `/usr/bin/python3` は 3.9 系です。
> 3.10 未満しかない場合は Homebrew などで 3.10 以上を入れ、そのパスで venv を
> 作ってください。3.9 で進めて失敗した場合も、その事実を所見に記録します
> （インストール手順の前提が守れるかどうか自体が検証対象です）。

## Track 1. Clean Install

[install-from-artifact.md](install-from-artifact.md) の手順**だけ**で起動できるかを
確認します。手順にない操作（ファイルのコピー、`npm`、手修正）が必要になったら、
それ自体が所見です。

```bash
shasum -a 256 -c SHA256SUMS
tar -xzf creative-ai-studio-v<VERSION>.tar.gz
cd creative-ai-studio-v<VERSION>
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt   # 所要時間を記録する
./scripts/run_studio.sh
```

別ターミナルで:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/version    # release_tag がダウンロードしたタグと一致
```

- [ ] SHA256 が一致した（`v1.0.0-rc.1` の `SHA256SUMS` はビルド環境の絶対パスを含むため
      `-c` での照合が `No such file or directory` で失敗します。修正は #437。その場合は
      `shasum -a 256 <tarball>` の値と `SHA256SUMS` の値を見比べてください）
- [ ] 展開物に `._*`（AppleDouble）、`.git`、`venv`、`node_modules`、`data`、
      `outputs`、`.env` が含まれていない
      （`find . -name '._*' -o -name .git -o -name node_modules | head`）
- [ ] `pip install` が手順どおり完了した（所要時間: ____ 分）
- [ ] `run_studio.sh` が Node なしで起動し、ブラウザで `http://127.0.0.1:8000/` が開いた
- [ ] `/version` の `release_tag` がタグと一致した
- [ ] 手順にない手作業が不要だった

## Track 2. Real Product Use

実 UI で Stable ジャーニーを**繰り返し**通します（最低 2 回、できれば日を変えて）。
モデルの重みは不要です。ナレーションと BGM は任意で、この検証の必須項目ではありません。

```text
Project -> Template Story -> Procedural Visual / Storyboard -> Gallery / Reuse -> Assembly MP4
```

1. Project を作成する。
2. Story パネルで「ストーリーを作成」し、Logline → Beats → Scenes の順に生成する。
3. 各シーンで「手続き型ビジュアルを生成（Stable）」を実行し、全シーンにビジュアルを揃える。
4. Gallery で生成物を開き、詳細確認・再投入・Project への紐付けを試す。
5. 「動画を書き出す」で MP4 を書き出し、Gallery から再生して中身を目で確認する。

- [ ] 1 回目を通せた（日付: ____）
- [ ] 2 回目を通せた（日付: ____）
- [ ] 書き出した MP4 を QuickTime などで再生でき、シーン数と尺が想定どおりだった
- [ ] 長いタイトル・長い Logline、シーン数の多いストーリーで表示が崩れなかった
- [ ] エラー表示が出た場合、次に何をすればよいか画面から分かった

## Track 3. Failure / Recovery Use

「リカバリ」は再起動後の状態収束を指し、中断した実行中ジョブの再開ではありません。
プロセスが強制終了されたとき実行中だったジョブは再起動後に `failed`（`process_interrupted`）、
キャンセル済みのジョブは `cancelled` のまま、キューに残っていたジョブは再投入されて
実行されるのが契約です。

`Ctrl+C` / `kill`（SIGTERM）は正常終了なので、実行中のジョブを終えてから止まることが
あります。中断の検証には `kill -9` を使ってください。

- [ ] **キャンセル**: ジョブをキューに積み、Latest Job の「Cancel job」で取り消せた
- [ ] **実行中の強制終了と再起動**: 複数シーンのビジュアル生成を積み、1 件目の実行中に
      別ターミナルから `kill -9 <pid>` で止め、`run_studio.sh` で再起動した。
      実行中だったジョブが `failed` になり、残りのジョブが続けて完了し、UI が固まらなかった
- [ ] **既存データの保持**: 再起動後も Project、Story、Gallery、紐付けが残っていた
- [ ] **失敗ジョブの扱い**: 失敗したジョブが Gallery や Story を壊さず、再生成できた
- [ ] **二重起動の拒否**: 起動中にもう 1 つ `API_PORT=8001 ./scripts/run_studio.sh` を
      同じディレクトリで起動し、`DataDirectoryInUseError` で止まった
- [ ] **port の衝突**: 8000 を別プロセスが使っている状態で起動し、分かるエラーで止まった
- [ ] **SIGTERM**: `kill <pid>` で止まり、直後に同じ port で再起動できた

## Track 4. Environment Difference

- [ ] macOS Apple Silicon で Track 1〜3 を実施した（macOS バージョン: ____、
      Python: ____）
- [ ] Safari でダウンロードした場合に自動展開されなかったか確認した
      （自動展開された場合、`SHA256SUMS` との照合手順が成り立つかを記録する）
- [ ] 展開後のファイルに quarantine 属性が付いていても起動できた
      （`xattr -l scripts/run_studio.sh`）
- [ ] 書き出し（ffmpeg）が `imageio-ffmpeg` 同梱のバイナリで動いた
      （Homebrew の ffmpeg に依存していない）
- [ ] Ubuntu は CI とクラウドでの成果物スモークを証跡とする（別途の手動検証は、
      問題が見つかった場合のみ）

## 終了条件

#425 の Exit criteria と同じです。

- [ ] リリース相当成果物のクリーンインストールを観測した
- [ ] Stable UI ジャーニーを手動で繰り返し通した
- [ ] キャンセル / 再起動 / リカバリを実際に試した
- [ ] 実際の手元環境での実行を観測した
- [ ] 隠れた手作業の修復が不要だった
- [ ] すべての所見を Release Blocker / Known Issue / v1.1 に分類した
- [ ] Release Blocker は修正とレビュー済み、または RC 昇格を止めている
- [ ] 最終的な対応プラットフォームの表現を、観測した証跡に基づいて決めた

## 所見の記録

分類の基準:

- **Release Blocker**: Stable の約束、インストール / 起動、データ安全性、対応する
  移行 / リカバリ、セキュリティ、成果物の使用可否のいずれかを破るもの
- **Known Issue**: 実在する制限だが Stable の約束は破らず、v1.0 で文書化できるもの
  （`CHANGELOG.md` の「既知の制限」へ）
- **v1.1**: 改善、UX の磨き込み、性能、Preview / Experimental の問題、整理

| 日付 | Track | 環境 | 観察したこと | 再現手順 | 分類 | 対応 |
|---|---|---|---|---|---|---|
| | | | | | | |
