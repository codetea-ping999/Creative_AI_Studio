# Frontend Design Directions

## Product context

Creative AI Studio is a local, single-user production workspace for generating,
reviewing, organizing, reusing, and exporting image, audio, and video assets.
It is used repeatedly during an iterative creative workflow, so clarity and
speed matter more than decorative novelty.

## Reference synthesis

The references below are used for interaction and information-architecture
patterns only. Brand assets, proprietary layouts, copy, and visual identity are
not copied.

| Reference | Pattern to learn from | Applied here |
| --- | --- | --- |
| [Vercel dashboard navigation](https://vercel.com/changelog/dashboard-navigation-redesign-rollout) | A persistent, workflow-prioritized sidebar that can adapt to smaller screens | Stable media/project navigation and compact mobile reflow |
| [Vercel Geist](https://vercel.com/geist/stack) | High-contrast neutral tokens, precise typography, and restrained component hierarchy | Neutral surfaces, one accent, border-led depth, compact type scale |
| [Raycast UI](https://developers.raycast.com/api-reference/user-interface) | Fast list/detail/form primitives and keyboard-first interaction | Searchable asset list, visible focus, direct actions near selected content |
| [Runway generation workflow](https://help.runwayml.com/hc/en-us/articles/37425232841875-Getting-Started-with-Generative-Video) | A session-centered loop of prompt, generate, inspect, and reuse | Composer first, latest run beside history, reuse actions in asset detail |
| [Adobe Firefly workspace](https://helpx.adobe.com/firefly/web/get-started/access-the-app/firefly-workspace-overview.html) | Generate, organize, edit, and manage files within one workspace | Creation, projects, gallery, and inspection remain connected |
| [ElevenLabs Studio](https://help.elevenlabs.io/hc/en-us/articles/18537818120465-What-is-Projects) | Production editing with history and granular review | Feedback, lineage, and export metadata are treated as production controls |

## Direction A: Precision Console

- Dense developer-tool layout with compact rows and minimal media emphasis.
- Neutral gray palette with cobalt status/action color.
- Strong for operations, model management, and diagnostics.
- Weakness: generated assets feel secondary in a creative product.

```text
┌──────────┬────────────────────────────────────┐
│ Media    │ Composer controls                  │
│ Projects ├─────────────────┬──────────────────┤
│ Models   │ Job table       │ Inspector        │
│ Metrics  │                 │                  │
└──────────┴─────────────────┴──────────────────┘
```

## Direction B: Gallery Atelier

- Media-first layout with large previews, generous whitespace, and a visual grid.
- Warm off-white palette with a restrained editorial accent.
- Strong for browsing and presenting final work.
- Weakness: operational state, parameters, and project management become slow.

```text
┌───────────────────────────────────────────────┐
│ Studio / Project / Search                     │
├───────────────────────────────┬───────────────┤
│                               │ Prompt        │
│          Large canvas         │ Controls      │
│                               │ Generate      │
├───────────────────────────────┴───────────────┤
│ Visual asset grid                             │
└───────────────────────────────────────────────┘
```

## Direction C: Quiet Creative Workbench — selected

- Combines an operational rail with a media-aware composer, latest run, asset
  list, and detail workspace.
- Neutral light/dark themes, solid cobalt accent, border-led hierarchy, and a
  compact 4px spacing system.
- Strong for the complete loop: create, monitor, compare, review, reuse, export.
- Tradeoff: more information is visible than in a consumer creation tool, so
  progressive disclosure is required inside the composer.

```text
┌──────────┬────────────────────────────────────┐
│ Brand    │ Workspace context + compact stats  │
│ Media    ├────────────────────────────────────┤
│ Project  │ Composer (quick / advanced)        │
│ Activity ├───────────────────┬────────────────┤
│          │ Latest run        │ Asset history  │
│          ├───────────────────┴────────────────┤
│          │ Selected asset + review / lineage  │
└──────────┴────────────────────────────────────┘
```

### Selection rationale

Direction C matches the domain model instead of forcing the product into a
generic dashboard or image gallery. It keeps `Job`, `Asset`, `Project`, and
`Feedback` visible at the points where the user acts on them, while the visual
system stays quiet enough for generated media to remain the focus.
