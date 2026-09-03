# Desktop Upstream Sources

This file records OSS sources considered for the Creative AI Studio desktop implementation.
It is a provenance and engineering-decision ledger, not a declaration that every listed source has been copied.

## Decision legend

- **Adopt**: reuse with minimal changes.
- **Adapt**: reuse the implementation pattern or selected code after narrowing it to CreativeStudio requirements.
- **Reference**: study the implementation, but implement locally rather than copying it directly.
- **Reject**: do not use.

## Audited sources

| Component | Upstream | License | Decision | Candidate implementation units | Notes |
| --- | --- | --- | --- | --- | --- |
| Tauri shell foundations | `kitlib/tauri-app-template` | MIT | Adapt | `src-tauri/src/lib.rs`, `src-tauri/src/plugins/system_tray.rs`, Tauri plugin configuration | Useful Tauri 2 single-instance/tray/global-shortcut patterns. Do not import its frontend dependency stack into `apps/web`. Its blanket close-to-tray handler must be narrowed for CreativeStudio multi-window behavior. |
| Companion window lifecycle | `Picrew/QimoBar` | MIT | Adapt | `src-tauri/src/lib.rs`, `src-tauri/src/window.rs`, `src-tauri/src/tray.rs` | Strongest match for transparent desktop-pet behavior: separate window, position recovery, multi-monitor checks, click-through, always-on-top, autostart, single-instance, tray. Remove GIF-specific asset management and settings UI concerns. |
| Pixel-aware click-through | `you-want/CodeWalkers` | MIT | Adapt (algorithm only) | `src/hooks/useAppConfig.ts`, `src-tauri/src/lib.rs` cursor-ignore commands | Useful 1-pixel alpha test pattern for making only visible character pixels interactive. Current implementation polls every 100 ms and performs cursor lookup + Canvas readback, so it must not be copied unchanged into an idle resident app. Prefer event-driven or aggressively throttled checks. |
| Native click-through fallback | `PlayForm/Round` | CC0-1.0 | Reference | `src-tauri/src/main.rs` platform-specific window handling | Reference for macOS `setIgnoresMouseEvents` / window-level behavior and Windows extended styles such as `WS_EX_TRANSPARENT`. Use only after a verified gap in Tauri standard APIs. |
| Python/FastAPI sidecar lifecycle | `dieharders/example-tauri-v2-python-server-sidecar` | Apache-2.0 | Reference, later Adapt | `src-tauri/src/main.rs`, `src/backends/main.py` | Useful `CommandChild` ownership, stdout/stderr monitoring, stdin shutdown, and PyInstaller lifecycle notes. Do not copy startup behavior: the example starts Python automatically, binds API to `0.0.0.0`, uses fixed port 8008, and permits CORS `*`. CreativeStudio requires lazy startup, loopback-only binding, health checks, dynamic endpoint handling, and clean process termination. |
| Live2D renderer | `codetea-ping999/live2d_desktop_app` | project-owned source; dependency licenses tracked separately | Adapt | `src/renderer/src/components/Live2DViewer.tsx` and related renderer assets/config | Reuse React/Pixi/`pixi-live2d-display` rendering and interactions. Do not migrate Electron main/preload runtime. Add idle/deep-idle/generation render modes before treating it as production-resident code. |

## Current adoption map

```text
CreativeStudio-specific
|- existing React/Vite Studio
|- Live2D renderer (adapt existing project code)
`- generation/event integration

Desktop infrastructure
|- Tauri shell patterns          <- kitlib
|- companion lifecycle           <- QimoBar
|- pixel-aware hit testing        <- CodeWalkers
|- native window fallback         <- Round
`- future sidecar supervision     <- dieharders example
```

## Rules for importing code

1. Never copy a repository wholesale merely because it is close to the target architecture.
2. Prefer Tauri official APIs and plugins before importing custom native code.
3. Import the smallest useful implementation unit and rewrite application-specific naming/state boundaries.
4. Preserve copyright/license notices where required.
5. Record substantial copied or adapted code in the future `THIRD_PARTY_NOTICES.md` before distribution.
6. Repositories without an explicit license are reference-only unless separate permission exists.
7. Do not import frontend UI frameworks or styling systems solely because an upstream template uses them.
8. Do not inherit upstream security defaults without review. Bind local services to loopback and keep shell/sidecar permissions narrow.
9. Desktop startup must remain independent of Python/CUDA/model startup regardless of upstream example behavior.
10. New repo exploration resumes only when implementation exposes a concrete unsolved problem. The goal is implementability, not proof that no better repository exists.

## Distribution note

The audited companion implementations use Tauri/macOS behavior that may require `macos-private-api` for transparent windows. The initial macOS distribution plan is signed/notarized direct distribution rather than assuming Mac App Store compatibility.

Live2D Cubism SDK/Core licensing remains a separate release gate. Development may proceed with properly licensed local assets, but desktop redistribution must be reviewed against the applicable Live2D SDK terms before a public binary release.
