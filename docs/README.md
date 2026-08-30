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

6. [domain-model.md](./domain-model.md)
   Job / Asset / Project に加え、BibleEntry / Batch / StoryDocument / Timeline が
   それぞれ何の主語なのかを確認します。

7. [multimedia-content-generation-plan.md](./multimedia-content-generation-plan.md)
   構想から完成動画までを 1 フローにする v0.3 の設計と段階計画です。

8. [issue-execution-plan.md](./issue-execution-plan.md)
   open issue 全体の着手順です。**次に何をするかはここが正**です。

9. [next-tasks.md](./next-tasks.md)
   現在の到達点（実装済み一覧）と、v0.3 トラック内の内訳を確認します。
   全体の着手順は上の実行計画を参照してください。

10. [repository-issues-improved.md](./repository-issues-improved.md)
    実コードと検証結果から抽出した改善課題と解決状況を確認します。

11. [model-download-guide.md](./model-download-guide.md)
    実モデルの配置や manifest 運用を確認します。

## 目的別の読み方

### とにかく動かしたい

- [setup-guide.md](./setup-guide.md)
- [model-download-guide.md](./model-download-guide.md)

### コードの入口を知りたい

- [codebase-guide.md](./codebase-guide.md)
- [model-system.md](./model-system.md)
- [api-contract.md](./api-contract.md)

### 複数エージェントで並列に実装したい

- [agent-harness.md](./agent-harness.md) — 検証ゲート、ファイル所有権、禁止事項

### Claude CodeとCodexを連携させたい

- [cross-agent-harness.md](./cross-agent-harness.md) — 双方向委任、quota/認証/停止時のみの自動フォールバック、worker-policy

### API を叩きたい

- [api-contract.md](./api-contract.md)
- [setup-guide.md](./setup-guide.md)

### モデル周りを理解したい

- [model-system.md](./model-system.md)
- [model-download-guide.md](./model-download-guide.md)
- [MusicGen v0.2 real-model validation](./validation/musicgen-v0.2.md)
- [MusicGen long-form validation](./validation/musicgen-long-form-v0.2.md)

### メモリと性能を測りたい

- [performance/memory-lifecycle-experiment-matrix.md](./performance/memory-lifecycle-experiment-matrix.md)
  — Hybrid Runtime (#350) の memory lifecycle 実験手順。measurement boundary、指標、
  シナリオ、記録形式を固定しています。実測は別 issue です。

## 現在の理解ポイント

このリポジトリで最初につまずきやすい点は次の 4 つです。

- `jobs` はジョブ状態の永続化、`assets` は生成物の再利用単位です。役割が違います。
- `generate/*` は便利な入口で、内部では最終的に共通の `JobService` に流れます。
- `projects` と `feedback` は SQLite ではなく `data/` 配下の JSON で持っています。
- Web UI は `apps/web/src/App.tsx` に集約されているため、フロント改修の入口が分かりやすい反面、責務がやや集中しています。

## 履歴資料

次のファイルは「当時の実装・修復メモ」としては有用ですが、現行仕様の一次資料として読む順番は後ろで構いません。

- [README_v0.2.md](./history/README_v0.2.md)
- [IMPLEMENTATION_SUMMARY.md](./history/IMPLEMENTATION_SUMMARY.md)
- [REPAIR_COMPLETE.md](./history/REPAIR_COMPLETE.md)
- [COMPLETION_CHECKLIST.md](./history/COMPLETION_CHECKLIST.md)
- [LFS_FIX_REPORT.md](./history/LFS_FIX_REPORT.md)
- [api-updates-v0.2.md](./api-updates-v0.2.md)
- [setup-guide-v0.2.md](./setup-guide-v0.2.md)

これらは「何が追加されたか」を追う資料であり、「今どう読むべきか」の導線としては弱いため、このガイドでは現行ドキュメントを先に読む構成にしています。
