# Monitor Thread Template

このスレッドは実装ではなく監視・レビュー専用とする。

## Role
- 対象カテゴリの整合性確認
- 命名規則の監視
- 将来拡張を壊す変更の検出
- 統合前の差分整理

## Scope
例:
- Core
- API
- Generator
- UI

## Inputs
- 関連タスク一覧
- 関連ファイル一覧
- 既存 docs
- architecture / domain-model / api-contract

## Review Rules
- 実装そのものは最小限に留める
- 差分を見つけたら必ず issue 化できる形で記述
- 影響範囲を明記する
- 「何がズレているか」と「どこを直すべきか」を分けて書く

## Output Format

### Summary
- healthy / warning / critical

### Findings
- category:
- issue:
- impact:
- affected files:
- recommendation:

### Follow-up
- fix task candidate 1
- fix task candidate 2
