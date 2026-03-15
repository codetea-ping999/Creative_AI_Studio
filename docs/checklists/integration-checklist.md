# Integration Checklist

対象:
- Task 01: generation schema
- Task 02: job schema
- Task 03: model manifest
- Task 04: sqlite bootstrap
- Task 05: job repository
- Task 06: base generator
- Task 07: image generator stub
- Task 08: fastapi bootstrap
- Task 09: prompt form UI

目的:
- 初期実装の整合性確認
- 命名・型・依存関係の統一
- E2E 接続前のズレ検出
- 修正タスク切り出しのための基準化

運用ルール:
- 実装完了 = 完了ではない
- このチェックリスト通過 = 統合完了
- 指摘は必ず「差分」と「修正先」を明記する

---

## 1. Schema Consistency

- [ ] `GenerationRequest` の定義が1箇所に統一されている
- [ ] `GenerationResult` の定義が1箇所に統一されている
- [ ] `JobRecord` が `GenerationRequest` / `GenerationResult` を参照している
- [ ] `media_type` の値が全体で一致している (`image`, `video`, `audio`)
- [ ] `params` の型が全体で一致している
- [ ] `job_id` と `id` の使い分けが明確である
- [ ] 命名ゆれ（`outputs` / `output_files` など）がない
- [ ] Pydantic の書き方が全体で揃っている
- [ ] Optional 項目の扱いが統一されている
- [ ] 日付・時刻型の表現が統一されている

---

## 2. Import / Module Consistency

- [ ] import path が全体で統一されている
- [ ] schema の import 元がズレていない
- [ ] 循環 import がない
- [ ] `__init__.py` や公開APIが必要なら整理されている
- [ ] 同一責務のコードが複数箇所に重複していない
- [ ] モジュール名と責務が一致している

---

## 3. Storage Consistency

- [ ] SQLite 初期化モジュールが repository から利用できる
- [ ] `job_repository` が schema の最終形に追従している
- [ ] request payload が保存可能
- [ ] result payload が保存可能
- [ ] status / progress 更新が可能
- [ ] DB 接続方法が1つに統一されている
- [ ] JSON 保存方針が明確である
- [ ] path 管理が OS 依存で壊れにくい
- [ ] 例外時の最低限の失敗ハンドリングがある

---

## 4. Generator Consistency

- [ ] `BaseGenerator` の interface が明確
- [ ] `ImageGenerator` が `BaseGenerator` を正しく継承している
- [ ] `generate()` の戻り値が `GenerationResult` に統一されている
- [ ] image request の最低限バリデーションがある
- [ ] stub output がローカルに保存される
- [ ] generator 側が core schema に依存しすぎていない
- [ ] media-specific な処理が core 側に漏れていない

---

## 5. API Consistency

- [ ] FastAPI app が起動する
- [ ] `/health` が動作する
- [ ] router 構造が今後拡張しやすい
- [ ] API request/response が schema と矛盾していない
- [ ] job create の response 形式が決まっている
- [ ] status code の扱いが破綻していない
- [ ] エラー形式が最低限統一されている
- [ ] API レイヤと repository レイヤの責務が分離されている

---

## 6. UI Consistency

- [ ] prompt form の項目名が API payload 名と一致している
- [ ] `prompt`, `negative_prompt`, `width`, `height`, `steps`, `seed` が扱える
- [ ] controlled component として実装されている
- [ ] submit handler を後から接続しやすい
- [ ] image 固有項目と共通項目の分離が意識されている
- [ ] 初期 state と送信 payload の設計が矛盾していない
- [ ] コンポーネント責務が過剰に肥大化していない

---

## 7. Naming Rules

- [ ] `JobRecord.id` を job の主キーとして扱う
- [ ] API create response は `job_id` を返す
- [ ] `GenerationResult.job_id` を使用する
- [ ] `outputs` / `previews` / `metadata` の命名が全体で統一されている
- [ ] `request_json` / `result_json` など storage 用命名が揃っている
- [ ] ファイル名規則が最低限揃っている

---

## 8. End-to-End Bootstrap Check

- [ ] UI から生成リクエスト相当のデータを作れる
- [ ] API がリクエストを受け取れる
- [ ] Job を生成できる
- [ ] `ImageGenerator` stub を呼べる
- [ ] ダミー画像を保存できる
- [ ] `GenerationResult` を返せる
- [ ] 失敗時に最低限のエラーが返る
- [ ] ログや出力で追跡可能である

---

## 9. Cleanup / Follow-up

- [ ] 重複 schema がない
- [ ] 不要ファイルがない
- [ ] TODO が整理されている
- [ ] 次タスクに必要な不足点が明文化されている
- [ ] 統合で見つかった問題が修正タスク化されている

---

## Review Output Format

統合レビュー結果は以下フォーマットで残す。

### Summary
- pass / partial / fail

### Findings
- issue:
- impact:
- affected files:
- suggested fix:

### Follow-up Tasks
- fix-001
- fix-002
- fix-003
