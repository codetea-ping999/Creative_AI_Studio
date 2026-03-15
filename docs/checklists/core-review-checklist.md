# Core Review Checklist

対象:
- schema
- jobs
- storage
- models
- events

## Schema
- [ ] 共通 schema が1箇所にまとまっている
- [ ] media_type が統一されている
- [ ] request/result の責務が明確
- [ ] future video/audio を壊さない命名になっている

## Jobs
- [ ] JobRecord の責務が明確
- [ ] status 遷移が破綻していない
- [ ] progress の型と意味が明確
- [ ] retry/cancel 追加余地がある

## Storage
- [ ] SQLite 接続方式が統一されている
- [ ] repository 責務が薄く保たれている
- [ ] JSON 保存方針が明確
- [ ] path 管理が一貫している

## Models
- [ ] ModelManifest が汎用的
- [ ] image 専用に寄りすぎていない
- [ ] loader 情報の責務が適切

## Events
- [ ] event 命名が一貫している
- [ ] UI 連携を見据えた最低限の粒度になっている
