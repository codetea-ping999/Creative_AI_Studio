# Creative 3D Pipeline Roadmap

Tracking Epic: [#380](https://github.com/codetea-ping999/Creative_AI_Studio/issues/380)

## Vision

Creative AI Studio を Image / Video / Audio に加えて、3D Asset と Interactive/Game向け出力まで扱える **Creative Production Platform** へ拡張する。

製品体験は Studio に統合するが、Blender / Unity は本体へ密結合せず、optional integration / executor として分離する。

```text
Creative AI Studio
        │
        ├─ Image
        ├─ Video
        ├─ Audio
        └─ 3D
            │
            ▼
      Job / Asset Core
            │
       Blender Executor
            │
            ▼
     Creative Package
            │
      ┌─────┴─────┐
      ▼           ▼
    Unity       Web / Other
```

## Design principles

1. **Studio統合、実行系分離**
   - UI / API / Job / Gallery / Project は Creative AI Studio の既存基盤を使う。
   - Blender / Unity は subprocess / external executor として扱う。

2. **Blenderは制作エンジン**
   - headless CLI + Python (`bpy`) で validation / cleanup / optimization / preview / export を行う。

3. **Unityはdestination/runtime**
   - Unity CLI (`-batchmode`, `-executeMethod`) と Editor C# を使い、import / Prefab / Scene / Build を自動化する。

4. **GLBを第一標準outputにする**
   - 最初からUnity専用FBX/Presetへ閉じず、他のdestinationへ流せる中間形式を優先する。

5. **Creative Packageを中間成果物にする**
   - prompt、reference、raw mesh、optimized mesh、textures、preview、generation metadata、provenanceをmanifestで束ねる。

## Phase 1 — 3D core contract

Goal: 既存のJob / Asset基盤が3Dを第一級mediaとして扱えるようにする。

- `3d` media type / request / result schema
- GLB output contract
- 3D asset metadata
  - format
  - polygon / vertex count
  - dimensions / bounds
  - material count
  - texture metadata
  - provenance
- Gallery / Projectとの統合方針
- validation result / warning contract

Exit criteria:

- ダミーruntimeでも `3d` jobが作成・完了し、Gallery/Projectから成果物metadataを取得できる。

## Phase 2 — Blender executor MVP

Goal: Blenderを再現可能なheadless post-process engineとして使えるようにする。

- Blender executable discovery / version readiness check
- `blender --background --python ...` executor
- input GLB import
- mesh / scene validation
- transform normalization
- basic cleanup
- preview render
- GLB export
- timeout / cancellation
- subprocess isolation
- stdout / stderr artifact保存

Exit criteria:

```text
input.glb
   ↓
Creative AI Studio Job
   ↓
Blender headless
   ↓
validated + normalized
   ↓
preview.png
optimized.glb
metadata.json
```

がStudioの1 Jobとして完了する。

## Phase 3 — Creative Package

Goal: 3D生成物を単なる単一ファイルではなく、再利用可能な制作成果物として管理する。

Logical layout:

```text
asset.creative/
├─ manifest.json
├─ source/
│  ├─ prompt.json
│  └─ reference.png
├─ models/
│  ├─ raw.glb
│  └─ optimized.glb
├─ textures/
├─ renders/
│  └─ preview.png
└─ metadata/
   └─ generation.json
```

Manifestには最低限以下を含める。

- asset type
- generator/runtime
- seed / generation parameters
- source/reference provenance
- processing pipeline
- available outputs
- validation summary
- destination exports

Exit criteria:

- Creative Packageを保存・再読込し、別destinationへ再exportできる。

## Phase 4 — AI 3D generation adapter

Goal: raw mesh生成からBlender後処理までを1 Job pipelineにする。

- 3D generator interface
- local adapter候補（Hunyuan3D等）
- image-to-3D
- text-to-3Dはruntime能力に応じてoptional
- reference image / prompt metadata保持
- raw output → Blender post-process → Creative Package

Exit criteria:

- StudioのCreate surfaceから3D生成を開始し、preview付きCreative Packageを取得できる。

## Phase 5 — Unity destination

Goal: Studioで作った3D assetをUnity projectへ機械的に投入できるようにする。

- Unity Editor package skeleton
- Unity executable/project discovery
- `-batchmode -executeMethod` adapter
- Creative Package / GLB import
- material setup
- collider generation hook
- Prefab generation
- optional Scene placement
- batch build / smoke validation

Exit criteria:

```text
Creative Package
      ↓
Unity batchmode
      ↓
Import
      ↓
Prefab
      ↓
Test Scene
      ↓
Build / validation result
```

を人手操作なしで実行できる。

## Phase 6 — Asset QA / Agent harness

Goal: Issue駆動で3D制作をAI Agentへ委譲できる品質ループを作る。

- mesh corruption / empty geometry / NaN checks
- polygon / texture budget
- missing texture detection
- preview-based QA hook
- Blender / Unity logsをJob artifactとして保持
- deterministic retry policy
- Issue → Generate → Validate → Export → Test pipeline

将来的には以下へ拡張する。

- Character
- Environment
- Game Scene
- animation / rig pipeline
- Unreal Engine destination
- Godot destination
- Three.js / WebGL destination

## Priority

このトラックは **v0.4候補** とする。

現在のv0.3マルチメディア基盤、Job lane、reference conditioning、release/quality gateを先に安定させる。3D core contractはそれらの共通基盤を再利用するため、既存基盤を迂回して別システムとして実装しない。

推奨順序:

1. 既存v0.3 / reliability作業
2. 3D core contract
3. Blender executor MVP
4. Creative Package
5. AI 3D generator adapter
6. Unity destination
7. Agent-driven QA / Game Scene automation
