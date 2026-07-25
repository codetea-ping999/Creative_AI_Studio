# CLAUDE.md

Guidance for AI assistants working in this repository.

`AGENTS.md` holds the frontend-specific rules and is **binding** — read it before
touching `apps/web/`. This file covers the whole system.

## What this project is

Creative AI Studio is a **fully local** generative-media workbench. A FastAPI
backend accepts prompts, queues them as jobs, runs media-specific generators
against locally-installed model weights, scores the output with heuristic
quality evaluators, and stores results as reusable assets. A React + Vite
Studio UI drives the loop.

Three media types are supported end to end: `image` (SDXL via diffusers),
`audio` (MusicGen via transformers), and `video` (a procedural storyboard GIF
runtime plus a pilot CogVideoX-2B MP4 runtime).

No cloud calls. Model weights are never committed — only manifests and small
helper files live in Git.

## Commands

Everything is driven from the repo root. The `Makefile` defaults to
`PYTHON ?= ./venv/bin/python`, so either activate the venv or pass
`PYTHON=python3`.

| Command | Purpose |
| --- | --- |
| `./setup.sh` | One-shot bootstrap: venv, pip deps, dirs, SQLite init, `npm install`, `.env` |
| `make verify` | Full local verification (setup check + web test + web build + pytest + API smoke). This is exactly what CI runs. |
| `make verify-lite` | Same minus the temporary-API smoke |
| `make test` | `pytest -q` |
| `make web-test` | `npm --prefix apps/web test` (vitest, jsdom) |
| `make web-build` | `npm --prefix apps/web run build` (`tsc -b && vite build`) |
| `make setup-check` | `scripts/check_local_setup.py --skip-runtime-files` |
| `make api-smoke` | Boot a throwaway API on a free port and hit `/health` + `/models` |
| `make calibration-report` | Build the feedback/quality correlation report |
| `make cogvideox-smoke` | Real MP4 smoke test (needs CogVideoX-2B weights on disk) |
| `./scripts/start_studio.sh` | Start API + Vite together using `.env` ports, open the browser |
| `./scripts/run_api_dev.sh` | API only, with uvicorn reload scoped to source dirs |
| `npm --prefix apps/web run dev` | Web UI only |

**Before calling any change complete, run `make verify`** (or at minimum the
subset that covers what you touched). `scripts/verify_local_stack.py` uses a
temporary DB, temporary output dirs, and a free loopback port, so it never
touches your real `data/` or `outputs/`.

There is no configured Python linter or formatter in CI. `CONTRIBUTING.md`
mentions `flake8`, but it is not installed or enforced — do not add lint churn
to a functional change.

## Prerequisites

- Python **3.10+** (`check_local_setup.py` enforces this)
- Node **20.19+** or **22.12+** (enforced by `engines` and the setup check)
- Copy `.env.example` → `.env` and `apps/web/.env.example` → `apps/web/.env`

Note: this container may not have deps installed. Python tests are written so
that missing heavyweight deps (`torch`, `fastapi`, `PIL`) cause **skips**, not
failures — see "Testing conventions".

## Architecture

### Layering

```
apps/web/       React + Vite Studio UI
apps/api/       FastAPI routers, thin — parse, delegate, shape response
bootstrap/      Dependency composition (the only place services are wired)
core/           Domain: jobs, models, assets, projects, feedback, quality, storage
generators/     Media-specific generation (image / audio / video)
models/         Manifests (committed) + weights (gitignored)
scripts/        Setup, verification, model download, smoke tests
tests/          pytest-run unittest suites
docs/           Design + contract docs (mostly Japanese)
```

The dependency direction is strictly one-way:
`apps → bootstrap → core/generators`. Nothing in `core/` imports from `apps/`.

### Request flow

1. `POST /generate/{image,audio,video}` (or `POST /jobs`) hits a router in
   `apps/api/routes/`.
2. The router calls `JobService.create_job()`, which persists a `JobRecord` to
   SQLite, publishes `job_created`, and enqueues the job id.
3. `JobRunner` (a daemon thread started in the FastAPI `lifespan`, see
   `apps/api/main.py`) dequeues and calls `process_job`.
4. The runner resolves a generator from `GeneratorRegistry` by `media_type`,
   walking the job through `preparing → running → postprocessing`.
5. `BaseGenerator.run()` executes `validate_request → prepare → generate →
   cleanup` (cleanup always runs, in a `finally`).
6. The generator resolves a model runtime via `ModelService`, generates, writes
   the file, scores it with `core.quality`, and returns a `GenerationResult`.
7. `JobService.mark_succeeded()` persists the result **and** syncs the asset
   registry. The UI polls `GET /jobs/{id}` and reads `/gallery`.

`/outputs` is mounted as static files, so generated media is served directly.

### Where state lives

- **Jobs** → SQLite at `DB_PATH` (default `data/jobs.db`), via
  `core/storage/repositories/job_repository.py`.
- **Assets, projects, feedback** → JSON files under `data/` (`data/assets/`,
  `data/projects/`, `data/feedback/`), each repository built on
  `core/storage/json_files.py`.
- **Generated media** → `outputs/{images,audio,videos}`; exports go to
  `outputs/exports`.

This split trips people up: an *asset* is the reusable generated artifact, a
*job* is the execution record. They are different lifecycles with different
stores.

## Conventions that matter

### Pydantic

Every request/response/record model sets `model_config = ConfigDict(extra="forbid")`.
Adding a field to a payload means adding it to the model — unknown keys are a
422, by design. Use `from __future__ import annotations` and modern unions
(`str | None`), which the whole codebase does.

### Job status

Status strings are constants in `core/jobs/statuses.py` — never hardcode
`"running"` etc. The literal set is also mirrored in
`core.schemas.generation.GenerationStatus`; both must stay in sync.

In-flight transitions (`preparing`, `running`, `postprocessing`) use
`update_if_status()` compare-and-set so a concurrent cancel is never clobbered.
Terminal transitions (`succeeded`, `failed`, `cancelled`) live **only** in
`JobService` — `JobRunner` delegates to it so the completion path exists in one
place. Preserve that; do not add a second terminal-write path.

Cancellation is cooperative: generation itself is a blocking call, so the runner
checks for cancellation at completion boundaries only.

### Generators

Subclass `generators/base.py:BaseGenerator` and implement all four lifecycle
methods. Register in `bootstrap/factories.py:create_default_generator_registry`
keyed by media type. Generators must:

- validate `media_type` and reject unsupported `output_format` in
  `validate_request`;
- merge params as `{**manifest.default_params, **request.params}` so the
  manifest supplies defaults and the request overrides;
- return rich `metadata` (model id, manifest id, loader, device, dtype, params
  actually used) — the UI and metrics read it;
- attach a `quality_report` via `core.quality` evaluators, then
  `enrich_quality_report(...)` with the optional semantic score.

### Model system

Models are declared as JSON manifests in `models/manifests/{media_type}/`,
validated by `core/models/manifest.py:ModelManifest`. Resolution order:
`public_id` → `alias` → manifest `id`; a request with no `model_id` falls back
to the manifest flagged `is_default` for that media/task type. Duplicate public
ids or aliases raise at index time and are caught by the setup check.

`ModelService` is the facade over registry → resolver → loader → runtime cache.
The cache is LRU with `MAX_CACHED_MODELS` entries (default **1**, because these
are multi-GB pipelines).

Loaders live in `core/models/loader.py` and are registered by name in
`create_default_loader_registry()`: `diffusers_image_loader`,
`transformers_musicgen_loader`, `procedural_video_loader`,
`learned_video_loader`. Adding a model type means adding a loader there and
referencing it from the manifest's `loader` field.

To add a model: drop weights under `models/<media>/<name>/` (gitignored), write
a manifest, run `make setup-check`. See `docs/model-download-guide.md`.

### Storage

JSON repositories write through `write_json_atomic()` — serialize to a temp file
in the same directory, `fsync`, then `os.replace`. Never write JSON state with a
plain `open().write()`. Timestamps go through `utc_now()` / `ensure_utc()`;
legacy naive datetimes are coerced to UTC so comparisons never mix aware and
naive values.

### Security posture

The API is local and unauthenticated, which makes path handling load-bearing:

- Exports are constrained to `outputs/exports` by `apps/api/export_paths.py`;
  absolute paths and `..` traversal are rejected with 400. Any new endpoint that
  accepts a filesystem destination must route through it.
- CORS allow-list is derived from `WEB_PORT` and limited to loopback origins.

### Frontend

`AGENTS.md` is the authority. In short:

- Design source of truth: `docs/design-system.md`, `docs/ui-principles.md`,
  `docs/design-directions.md`.
- **No UI library** without explicit approval. Plain React + CSS.
- All colors come from semantic CSS custom properties in
  `apps/web/src/styles.css` — no color literals in components.
- 4px spacing scale; state must have a non-color cue as well as color.
- Before calling a UI change done: check loading/empty/error/disabled/long-text/
  large-list states, keyboard focus and ARIA, and the rendered page at 390 /
  768 / 1280 / 1440 px. Source review alone is not sufficient.

Structure: `App.tsx` is the orchestrator (state, polling, API calls) and is
large; presentational pieces live in `src/components/`, payload builders in
`src/lib/payloads.ts`, shared types and formatters in `src/studio.ts`, and all
HTTP goes through `requestJson()` in `src/studioClient.ts`. TypeScript is
`strict` with `noUnusedLocals`/`noUnusedParameters`, so `tsc -b` will fail the
build on dead bindings.

UI strings are Japanese. Match the surrounding language when adding copy.

## Testing conventions

Python tests are `unittest.TestCase` classes executed by pytest. The dominant
pattern is an import guard:

```python
try:
    from apps.api.main import create_app
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc

@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class SomethingTests(unittest.TestCase):
    ...
```

Follow it for anything that imports torch/diffusers/fastapi, so the suite stays
runnable in minimal environments. Tests build their own services with
`create_application_services(db_path=..., output_dir=..., manifest_root=...)`
inside a `TemporaryDirectory` — never point a test at the real `data/` or
`outputs/`.

Frontend tests are vitest + Testing Library, colocated as `*.test.ts(x)`.

## Docs map

Read in this order when you need background (`docs/README.md` is the index):

1. `docs/codebase-guide.md` — where to start reading code, per directory
2. `docs/model-system.md` — manifests, resolver, runtime cache
3. `docs/api-contract.md` — every endpoint, request/response shapes, error format
4. `docs/architecture.md`, `docs/domain-model.md` — system and domain structure
5. `docs/setup-guide.md`, `docs/model-download-guide.md` — environment and weights
6. `docs/next-tasks.md` — current priorities and what is already done
7. `docs/checklists/` — per-layer review checklists (core, api, generator, ui, integration)

`docs/history/` is archival. Do not treat it as current spec.

**Keep docs in sync**: `docs/api-contract.md` mirrors `apps/api/routes/` — change
an endpoint, update the contract. Same for `docs/model-system.md` when manifest
fields change.

## Gotchas

- The job runner starts inside the API process. `create_app(start_job_runner=False)`
  exists for tests that want no background thread.
- `EventBus` is an in-memory list, not a real bus. Events are not delivered to
  the UI; the UI polls.
- `apps/web/vite.config.js` is the emitted output of `vite.config.ts` (both are
  tracked; `tsconfig.node.json` compiles the `.ts`). Edit the `.ts` and keep the
  `.js` consistent.
- Quality scores are `heuristic_local_v1`: proxies for technical quality
  (resolution, exposure, clipping, frame delta), *not* aesthetic or semantic
  judgment. The semantic judge is optional and off unless
  `QUALITY_ENABLE_SEMANTIC_JUDGE=true`.
- `start_studio.sh` refuses to run if an API or Vite server is already up on a
  different port/URL — stop the old process rather than working around it.
- Weights are gitignored per-directory with explicit `!` re-includes (e.g.
  `models/video/cogvideox-2b/model_index.json`). Check `.gitignore` before
  adding files under `models/`.

## Git workflow

Branch naming: `feature/...` or `fix/...`. Commit messages start with a verb and
say what changed (`Add image generation endpoint with quality scoring`, not
`fixed stuff`). Update `docs/next-tasks.md` when you complete a tracked item.
