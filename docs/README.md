# Documentation Guide

Creative AI Studio のドキュメント案内です。

今のリポジトリは実装メモ、完了レポート、現行仕様書が混在しています。  
そのため、最初に読む順番を固定しておくと理解しやすくなります。

## まず読む順番

1. [README.md](../README.md)
   プロジェクト全体像、現在できること、起動方法の入口です。

2. [setup-guide.md](./setup-guide.md)
   ローカル起動に必要な手順を確認します。

3. [codebase-guide.md](./codebase-guide.md)
   どのディレクトリに何があり、どこから読めばよいかを把握します。

4. [model-system.md](./model-system.md)
   manifest、resolver、runtime cache の構成を確認します。

5. [api-contract.md](./api-contract.md)
   API の全体像と各エンドポイントの役割を確認します。

6. [next-tasks.md](./next-tasks.md)
   現在の到達点と、次に進めるべき優先タスクを確認します。

7. [repository-issues-improved.md](./repository-issues-improved.md)
   実コードと検証結果から抽出した改善課題と解決状況を確認します。

8. [model-download-guide.md](./model-download-guide.md)
   実モデルの配置や manifest 運用を確認します。

## 目的別の読み方

### とにかく動かしたい

- [setup-guide.md](./setup-guide.md)
- [model-download-guide.md](./model-download-guide.md)

### コードの入口を知りたい

- [codebase-guide.md](./codebase-guide.md)
- [model-system.md](./model-system.md)
- [api-contract.md](./api-contract.md)

### API を叩きたい

- [api-contract.md](./api-contract.md)
- [setup-guide.md](./setup-guide.md)

### モデル周りを理解したい

- [model-system.md](./model-system.md)
- [model-download-guide.md](./model-download-guide.md)

## 現在の理解ポイント

このリポジトリで最初につまずきやすい点は次の 4 つです。

- `jobs` はジョブ状態の永続化、`assets` は生成物の再利用単位です。役割が違います。
- `generate/*` は便利な入口で、内部では最終的に共通の `JobService` に流れます。
- `projects` と `feedback` は SQLite ではなく `data/` 配下の JSON で持っています。
- Web UI は `apps/web/src/App.tsx` に集約されているため、フロント改修の入口が分かりやすい反面、責務がやや集中しています。

## 履歴資料

次のファイルは「当時の実装・修復メモ」としては有用ですが、現行仕様の一次資料として読む順番は後ろで構いません。

- [README_v0.2.md](../README_v0.2.md)
- [IMPLEMENTATION_SUMMARY.md](../IMPLEMENTATION_SUMMARY.md)
- [REPAIR_COMPLETE.md](../REPAIR_COMPLETE.md)
- [COMPLETION_CHECKLIST.md](../COMPLETION_CHECKLIST.md)
- [api-updates-v0.2.md](./api-updates-v0.2.md)
- [setup-guide-v0.2.md](./setup-guide-v0.2.md)

これらは「何が追加されたか」を追う資料であり、「今どう読むべきか」の導線としては弱いため、このガイドでは現行ドキュメントを先に読む構成にしています。
