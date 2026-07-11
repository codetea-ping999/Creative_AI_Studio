# UI Review Checklist

対象:
- prompt form
- app shell
- history/panel の将来拡張

## Form
- [x] prompt を入力できる
- [x] negative_prompt を入力できる
- [x] width/height/steps/seed を入力できる
- [x] controlled components になっている
- [x] submit handler を差し込みやすい
- [x] quick / advanced の段階的開示が機能している
- [x] loading / error / disabled 状態が判別できる

## Naming
- [x] form field 名が API payload と一致している
- [x] image 固有パラメータが整理されている

## Structure
- [x] component が分割しやすい
- [x] state が肥大化しすぎていない
- [x] history/gallery を追加しやすい
- [x] `docs/design-system.md` のトークンだけを使用している
- [x] 不要なカード、影、グラデーション、hover transform がない

## Visual consistency
- [x] 余白が4pxグリッドに沿っている
- [x] 同じ役割のコントロールは同じ高さである
- [x] 角丸、境界線、タイポグラフィの規則が統一されている
- [x] 情報階層と主要操作が明確である

## Responsive
- [x] 390px で横スクロールや操作の重なりがない
- [x] 768px でサイドバーと本文が自然に再配置される
- [x] 1280px で主要作業が過密にならない
- [x] 1440px で行長と空白が過剰にならない

## Accessibility
- [x] `focus-visible` が明確である
- [x] 色だけで状態を表現していない
- [x] 入力に可視ラベルがある
- [x] 状態変更が適切な `role` / `aria-*` で伝わる
- [x] `prefers-reduced-motion` を尊重している

## Future
- [x] video/audio タブ拡張を妨げない構造になっている

## Review evidence (2026-07-11)

- Playwright実画面でlight/dark、390/768/1280/1440pxを確認し、全幅で横overflowなし。
- 31 projects、長文prompt、8件gallery、empty/error/loading/disabled/succeeded状態を確認。
- `:focus-visible`は2px solid focus ring、model readinessはdisabled optionとテキスト理由を併用。
- Frontend tests、production build、`make verify-lite`、`make verify`を通過。
