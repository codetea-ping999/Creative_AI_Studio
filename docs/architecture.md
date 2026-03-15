# Architecture

## Overview

Creative AI Studio は、ローカルで動作する単一ユーザー向けの Creative AI 実行基盤です。  
現在の v0 では、以下を同一の Studio UI とジョブ基盤で扱います。

- image generation
- storyboard video generation
- music loop generation and playback

## Design Rule

Core と Generator を分離し、UI はメディア種別ごとの差分を最小限に保つ。

- Core: 共通実行基盤
- Generator: メディア固有の生成処理
- Apps: Studio 体験の組み立て

## Layers

### 1. Apps Layer

ユーザーとの接点を提供する。

- FastAPI
- Web Studio UI

Web UI は現在、以下の 3 つの領域で構成される。

- Composer: image / video / music の入力フォーム
- Stage: 出力プレビューと再生
- Session History: `GET /jobs` を使った直近ジョブ履歴
- Quality HUD: quality score / save success / readiness の確認

### 2. Core Layer

すべての生成処理を統一的に扱う基盤。

責務:

- request / result schema
- job queue
- job runner
- storage
- event bus
- model registry
- output routing
- quality evaluation
- operational metrics
- local catalog discovery
- optional semantic judge

### 3. Generator Layer

各メディア固有のロジックを実装する。

- image generator: SDXL ローカル生成
- audio generator: MusicGen ローカル生成
- video generator: procedural storyboard gif 生成

### 4. Runtime Layer

ローカルモデルや各種ランタイム。

- diffusers
- transformers
- PyTorch MPS
- MLX (future)

## Data Flow

1. Web UI or API が generation request を受け取る
2. API は JobService に job 作成のみを依頼する
3. JobService が job を保存し queue に投入する
4. JobRunner が queue から job を取得する
5. JobRunner が `media_type` から Generator を選択する
6. Generator がモデルまたは runtime を使って生成する
7. 結果ファイルを `outputs/` 配下へ保存する
8. quality evaluator が output file を読み取り technical proxy を採点する
9. JobRunner が result を永続化し job 状態を更新する
10. Web UI が `/jobs/{id}` と `/jobs` で状態と履歴を表示する
11. metrics endpoint が成功率と品質平均を返す

## Core Concepts

### GenerationRequest

メディア種別に依存しない共通入力。

例:

- media_type
- prompt
- negative_prompt
- model_id
- seed
- output_format
- params

### GenerationResult

生成処理の共通出力。

例:

- job_id
- status
- outputs
- previews
- metadata
- error_message

### Job

すべての生成は Job として扱う。

状態:

- queued
- preparing
- running
- postprocessing
- succeeded
- failed
- cancelled

責務分離:

- API は generator を直接呼ばない
- JobService は job 作成、保存、状態更新を扱う
- JobRunner は queue 消費と生成実行の orchestration だけを扱う
- Generator は request を受けて result を返す
- Web UI は job poll と履歴表示だけを行う

### ModelManifest

利用可能なモデル情報を表す。

現状は image 用 manifest を中心に使い、Web UI は `/models?media_type=image` を参照する。

### QualityReport

生成後に付与される heuristic な品質評価。

- method
- quality_score
- quality_level
- business_readiness_score
- semantic_alignment_score
- creative_alignment_score
- checks
- metrics

### Asset Routing

出力ファイルは API の `/outputs` mount 経由で Web UI に公開される。

- image: `outputs/images/*`
- audio: `outputs/audio/*`
- video: `outputs/videos/*`

## Current v0 Scope

### Implemented

- image generation with local SDXL
- storyboard video generation with local procedural runtime
- optional LoRA catalog / manual path / scale input
- audio generation endpoint with local MusicGen runtime
- shared job queue / runner
- session history via `GET /jobs`
- output stage for image preview, storyboard reel, and audio playback
- heuristic quality report and `/metrics/summary`
- gallery / project / feedback API
- `/catalog/loras` による local asset selection
- optional semantic judge based on local CLIP / CLAP

### Planned Next

- asset export / reuse workflow
- semantic quality judge の追加
- learned text-to-video runtime integration

## Constraints

- Local-first
- MacBook Pro M1 Max / 64GB 想定
- SQLite を利用
- 単一ユーザー前提
