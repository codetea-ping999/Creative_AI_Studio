# Desktop Architecture Decision

Status: Accepted  
Date: 2026-09-04

## Context

Creative AI Studio is currently a browser-first React/Vite frontend backed by FastAPI and Python generation workers. The desktop version must reduce launch friction and support an always-available companion experience without reducing generation throughput or occupying GPU memory while idle.

The desktop layer therefore needs to remain lightweight, reuse the existing web UI, preserve the Python AI ecosystem, and isolate resident UI concerns from model/runtime lifecycle concerns.

## Decision

### 1. Use Tauri 2 for the desktop shell

The desktop application will use Tauri 2 with a Rust core process.

The Rust/Tauri layer owns only desktop concerns:

- system tray;
- global shortcut;
- single-instance handling;
- autostart configuration;
- window lifecycle;
- notifications;
- desktop IPC/event routing;
- future backend process supervision;
- performance-mode coordination.

The desktop core must not directly load AI models or initialize CUDA.

### 2. Keep React, TypeScript, and Vite

`apps/web` remains the UI source of truth. Desktop-specific surfaces should reuse the existing React/Vite implementation rather than create a second application stack.

Desktop integration should be introduced through narrow runtime adapters and separate entry surfaces where necessary. The browser build must remain independently usable.

Do not add a new UI framework for desktop work. Existing repository UI rules in `AGENTS.md` continue to apply.

### 3. Keep Python and FastAPI for generation

Python/FastAPI remains the backend and AI integration layer. Rewriting the generation stack in Rust is explicitly out of scope.

The initial desktop shell will connect to an already-running backend. Desktop Shell v0.1 must nevertheless include the minimum runtime integration required for a packaged WebView to reach that backend reliably:

- the packaged desktop bundle must not depend solely on build-time `VITE_API_BASE_URL`;
- `DesktopRuntime` resolves the backend endpoint from Tauri-managed runtime configuration/IPC, with the existing loopback default used only as an explicit fallback;
- non-default supported API ports must remain usable without rebuilding the desktop bundle;
- FastAPI CORS configuration must explicitly allow the packaged Tauri WebView origins used by supported platforms;
- CORS must remain an exact allowlist rather than a wildcard.

A later phase may package the backend as a Tauri sidecar behind a `BackendSupervisor` abstraction.

Desktop startup must not automatically start Python, FastAPI, CUDA, or model runtimes.

### 4. Separate the Live2D companion from the Studio window

The Live2D companion is a separate transparent, frameless desktop window rather than part of the full Studio window.

This separation allows the companion to remain available while the heavier Studio WebView is hidden or, after draft-safety requirements are implemented, destroyed.

The companion may support:

- always-on-top mode;
- click-through mode;
- persisted screen position;
- multi-monitor boundary recovery;
- lightweight interaction and quick-open actions;
- generation-state reactions;
- idle/deep-idle render throttling.

The existing `codetea-ping999/live2d_desktop_app` React/Pixi renderer is the primary migration source. Its Electron main/preload layer will not be carried forward.

### 5. Treat GPU ownership as a hard process boundary

The resident desktop application does not own generation GPU state.

The design invariant is:

```text
Desktop resident != Python resident != CUDA resident != model resident
```

Only the generation backend/worker may own model runtimes and CUDA state.

When the application is merely resident in the tray, the expected state is:

```text
Python process spawned by Desktop = 0
CUDA context created by Desktop = 0
AI models loaded by Desktop = 0
```

The companion WebView may use ordinary OS/WebView graphics acceleration, but must support a reduced render mode during generation.

If a future `BackendSupervisor` owns a packaged Python sidecar, tray-only residency must still converge back to the zero-process state when generation is idle. The supervisor lifecycle is therefore explicit:

```text
Stopped -> Starting -> Ready -> Busy -> Stopping -> Stopped
```

A transition to tray-only requests backend shutdown. If a generation job is active, shutdown is deferred until that job reaches a terminal state; the supervisor records the pending stop rather than killing an in-flight generation. Once idle, it performs graceful shutdown with a bounded timeout and escalates to process termination if needed. Hiding or destroying a WebView never counts as backend cleanup by itself.

### 6. Use an explicit desktop lifecycle

The target lifecycle is:

```text
Tray Only
  -> Companion
  -> Full Studio
```

The full Studio WebView is not required to remain resident indefinitely. A later power-management phase may destroy an inactive Studio window and recreate it on demand, but only after user-edit state is protected.

Before WebView destruction is enabled, at least one of the following must exist for every editable Studio surface:

- durable draft persistence with restoration after recreation; or
- a reliable dirty-state guard that prevents destruction while unsaved input exists.

Until that gate is satisfied, power management may hide/suspend the Studio window but must not destroy a React tree containing unsaved composer, project, Story, Matrix, or other form state.

Close-to-tray behavior applies only where explicitly configured. It must not be implemented as a blanket handler that accidentally prevents auxiliary windows from closing normally.

### 7. Introduce a runtime adapter between Web and Desktop

The existing web client currently resolves the API endpoint from `VITE_API_BASE_URL` with a loopback fallback. Desktop packaging must not make the web build depend directly on Tauri APIs.

Introduce a small platform boundary such as:

```text
StudioRuntime
  |- BrowserRuntime
  `- DesktopRuntime
```

Browser runtime resolves the configured web API URL. Desktop runtime resolves the current backend endpoint through a narrow Tauri IPC/configuration boundary from Desktop Shell v0.1 onward. Future backend supervision may provide a dynamically allocated endpoint through the same interface without changing React feature code.

The v0.1 integration is not accepted until a packaged WebView can perform both a simple read and a preflighted JSON write against an already-running backend, including when the backend uses a supported non-default port.

### 8. Use OSS bricolage for infrastructure

CreativeStudio-specific user experience should be implemented locally. Commodity desktop infrastructure should be adapted from audited OSS instead of rewritten without reason.

Adoption policy:

- `Adopt`: reuse with minimal local changes;
- `Adapt`: reuse the design/code after narrowing it to CreativeStudio requirements;
- `Reference`: study the implementation but implement locally;
- `Reject`: do not use.

Current sources and decisions are recorded in `docs/desktop/upstream-sources.md`.

No repository is copied wholesale. Only the smallest useful implementation units are migrated.

### 9. Track third-party provenance and licenses

Creative AI Studio is MIT-licensed. Imported or substantially adapted code must retain required notices and provenance.

Preferred source licenses are MIT, Apache-2.0, BSD, ISC, and CC0. Repositories without an explicit license are reference-only unless permission is separately established.

A future distributable desktop build must include an appropriate third-party notices file covering copied/substantially adapted code and packaged runtime dependencies.

### 10. Prefer Tauri standard APIs before native fallback code

Transparent windows, click-through, always-on-top, tray, and related behavior should use Tauri 2 APIs first.

OS-specific native code is a fallback only when required for a verified platform behavior gap. `PlayForm/Round` is currently retained as a reference for macOS and Windows behavior rather than a direct base implementation.

### 11. macOS distribution is initially direct, not Mac App Store

The audited companion implementations use Tauri's `macos-private-api` feature for transparent desktop behavior. This creates a distribution constraint for Mac App Store submission.

The initial macOS desktop distribution target is therefore signed/notarized direct distribution. The separate native iOS/iPadOS application is not coupled to this choice.

## Performance budget

Desktop work is accepted only if it preserves the generation-first performance model.

Initial invariants:

### Tray-only

- no Desktop-spawned Python process after any active supervised generation completes and deferred shutdown finishes;
- no Desktop-created CUDA context;
- no AI model runtime loaded;
- effectively idle GPU compute use.

### Companion idle

- no Python/CUDA/model runtime ownership;
- render loop can throttle substantially below interactive frame rate;
- hidden companion can suspend rendering.

### Generation

A later performance gate will compare generation with and without the desktop UI resident. Initial target budgets are:

- generation latency regression <= 2%;
- additional peak VRAM <= 128 MiB;
- companion switches to generation performance mode while heavy generation is active.

Absolute RSS/CPU budgets will be fixed after baseline measurements on the supported platforms.

## Initial implementation sequence

1. Desktop Shell v0.1
   - `apps/desktop`;
   - Tauri 2;
   - tray;
   - single instance;
   - global shortcut;
   - autostart setting;
   - existing Studio UI display;
   - minimal `BrowserRuntime`/`DesktopRuntime` API endpoint boundary;
   - runtime backend endpoint resolution, including supported non-default ports;
   - exact FastAPI CORS allowlisting for packaged Tauri WebView origins;
   - packaged read + preflighted JSON-write connection smoke tests;
   - no backend spawn.

2. Runtime adapter hardening
   - configuration precedence and error handling;
   - backend health/availability UX;
   - preserve the same React feature API for browser and desktop.

3. Live2D companion migration
   - migrate React/Pixi renderer;
   - transparent companion window;
   - position persistence and click-through.

4. Power management
   - idle/deep-idle rendering;
   - generation performance mode;
   - draft persistence or dirty-state protection;
   - optional Studio WebView destruction only after the draft-safety gate passes.

5. Backend supervisor
   - external backend first;
   - packaged sidecar later;
   - health checks and dynamic endpoint publication;
   - explicit busy/deferred-stop behavior;
   - graceful shutdown with bounded forced-termination fallback;
   - tray-only convergence to zero Desktop-spawned Python processes.

6. Performance gate
   - RSS/CPU/GPU/VRAM/generation-latency measurements.

## Non-goals for Desktop Shell v0.1

The first desktop implementation must not include:

- Live2D migration;
- bundled Python;
- automatic FastAPI startup;
- CUDA/model initialization;
- GPU worker process refactoring;
- Pixi or Live2D library upgrades;
- a new frontend UI library;
- broad API redesign.

## Consequences

### Positive

- Desktop residency remains independent of expensive AI runtime state.
- The existing browser application remains reusable and independently deployable.
- Python AI ecosystem compatibility is preserved.
- Live2D can evolve independently of the full Studio window.
- Infrastructure work can reuse audited OSS patterns.
- Packaged desktop networking is treated as an explicit v0.1 integration concern instead of an accidental build-time assumption.
- WebView power management cannot silently discard user drafts.

### Costs and risks

- Tauri/WebView differences, including packaged origins and CORS behavior, must be tested across Windows and macOS.
- Transparent/click-through behavior may require platform-specific fallback code.
- Desktop runtime endpoint discovery requires a small web-platform abstraction in the initial shell rather than as a later cleanup.
- Sidecar packaging and process cleanup remain a separate engineering problem, with explicit deferred-stop semantics required before shipping a supervised backend.
- macOS transparent-window behavior affects App Store distribution choices.

## Revisit conditions

Revisit this ADR if any of the following becomes true:

- Tauri cannot satisfy required companion behavior on a supported platform without unacceptable private/native integration;
- measured desktop residency materially reduces generation performance despite power-management controls;
- the existing web UI cannot be shared without creating a worse maintenance boundary than a dedicated desktop frontend;
- Python packaging becomes operationally prohibitive and a different backend deployment model is required.
