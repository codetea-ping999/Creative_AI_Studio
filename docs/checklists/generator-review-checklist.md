# Generator Review Checklist

対象:
- BaseGenerator
- ImageGenerator
- 将来の Video/Audio generator 余地

## Interface
- [ ] validate_request がある
- [ ] prepare がある
- [ ] generate がある
- [ ] cleanup がある
- [ ] 戻り値が統一されている

## Image Stub
- [ ] image request のみを受ける
- [ ] ダミー画像生成が成功する
- [ ] 出力先が明確
- [ ] metadata の返し方が整理されている

## Separation
- [ ] generator が API を知らない
- [ ] generator が DB を直接触らない
- [ ] core と generator の責務が分離されている

## Future
- [ ] video/audio を実装しやすい interface である
