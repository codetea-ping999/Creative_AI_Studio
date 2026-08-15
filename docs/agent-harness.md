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
| 自動マージが構文エラーを生んだ | 共有ファイルを複数エージェントが同時に編集した | 共有ファイルは統合役だけが触る |

## 検証ゲート

**これを通らないものを「完了」と呼ばないでください。**

```bash
./venv/bin/python -m pytest -q
npm --prefix apps/web test
npm --prefix apps/web run build
```

現在の基準値は **backend 498 passed / frontend 58 passed / build 成功** です。
自分の変更で減った場合、原因を特定するまで完了報告をしないでください。

作業中は自分のテストファイルだけを回して構いませんが、**完了報告の前に必ず全体を 1 回**通します。

```bash
./venv/bin/python -m pytest -q tests/test_<your_area>.py   # 作業中
```

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

- **`git commit` / `git add` / `git push` をしない。** コミットはオーケストレータが行います
- **`pip install -r requirements.txt` を実行しない。** torch を含み非常に重く、
  検証環境には既にインストール済みです
- **モデル weight をダウンロードしない。** ネットワークと時間を浪費します
- **既存のテストを「通すために」書き換えない。** 落ちたテストは仕様の主張です。
  仕様の方が誤っていると判断した場合は、変更せずに報告へ根拠を書いてください
- **`main` に直接触らない**

## 実行環境の前提

| 項目 | 状態 |
| --- | --- |
| Python | `./venv/bin/python`（依存関係はインストール済み） |
| Node | `npm --prefix apps/web`（`node_modules` はインストール済み） |
| ffmpeg | `imageio-ffmpeg` 同梱。**システム ffmpeg は無い**ので前提にしない |
| 画像モデル | SDXL weight 未配置。画像生成は実行できない |
| テキストモデル | `template-writer` のみ有効（weight 不要、決定的） |
| TTS | `kokoro` は無効、`voicevox` は外部エンジン必須。**既定では合成できない** |

weight が要る検証は、この環境では**できません**。必要な場合は報告に
「ローカル実機が必要」と明記してください。推測で「動作確認した」と書かないでください。

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
