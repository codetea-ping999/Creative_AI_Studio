# UI Review Checklist

対象:
- prompt form
- app shell
- history/panel の将来拡張

## Form
- [ ] prompt を入力できる
- [ ] negative_prompt を入力できる
- [ ] width/height/steps/seed を入力できる
- [ ] controlled components になっている
- [ ] submit handler を差し込みやすい

## Naming
- [ ] form field 名が API payload と一致している
- [ ] image 固有パラメータが整理されている

## Structure
- [ ] component が分割しやすい
- [ ] state が肥大化しすぎていない
- [ ] history/gallery を追加しやすい

## Future
- [ ] video/audio タブ拡張を妨げない構造になっている
