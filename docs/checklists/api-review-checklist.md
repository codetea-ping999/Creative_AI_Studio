# API Review Checklist

対象:
- FastAPI app
- routes
- request/response
- error format

## Bootstrap
- [ ] app が起動する
- [ ] router 構成が分かりやすい
- [ ] health endpoint がある

## Contracts
- [ ] request schema と API payload が一致している
- [ ] response schema と返却値が一致している
- [ ] job create response の形式が固定されている
- [ ] models/projects/jobs の境界が明確

## Error Handling
- [ ] 最低限の error format がある
- [ ] validation error が崩れていない
- [ ] internal error の露出が過剰でない

## Future Expansion
- [ ] `/generate/video` を追加しやすい
- [ ] `/generate/audio` を追加しやすい
- [ ] image 固有ロジックが API 全体を汚していない
