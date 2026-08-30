# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Creative AI Studio is a locally-run generative AI studio: a FastAPI backend plus a React/Vite
Studio UI that queue and run **image**, **audio (music)**, **video (storyboard)**, and **text
(story engine)** generation jobs against pluggable local models. Everything is designed to work
with zero model weights installed (deterministic/template fallbacks), and to upgrade in quality
(not in capability) once real weights are placed under `models/`.

Primary docs, in recommended reading order, live in `docs/README.md`. The most load-bearing ones
for code changes are `docs/codebase-guide.md` (where to read/edit for a given change),
`docs/model-system.md` (manifest/resolver/loader/runtime-cache contract), and
`docs/domain-model.md` (Job vs Asset vs Project vs Bible vs Batch vs StoryDocument). Read those
before making non-trivial changes — this file only summarizes what they say.

## Commands

Bootstrap (first time / when deps change):

```bash
./setup.sh                    # venv, deps, dirs, sqlite init, web install, .env
```

Run the stack:

```bash
./scripts/start_studio.sh     # starts API + Web UI together, using root .env ports
./scripts/run_api_dev.sh      # API only (reads root .env, excludes venv/node_modules from reload watch)
cd apps/web && npm run dev    # Web UI only (http://localhost:5173)
```

Verification (mirrors CI — `python scripts/verify_local_stack.py --start-api`):

```bash
make verify        # setup-check + web build/test + eslint/ruff/mypy + npm audit + pytest (+coverage) + API smoke, via verify_local_stack.py
make verify-lite   # same set of gates, without starting/smoke-testing the API
make setup-check   # scripts/check_local_setup.py --skip-runtime-files
make test          # pytest -q
make web-build     # npm --prefix apps/web run build
make web-test      # npm --prefix apps/web test
make lint          # eslint (apps/web) + ruff (core/, generators/)
make typecheck     # mypy (core/, generators/)
make test-coverage # pytest --cov=core --cov=generators (fails under the pyproject.toml floor)
make npm-audit     # npm --prefix apps/web audit --audit-level=high
make api-smoke     # /health + /models smoke check only (skips lint/typecheck/coverage/audit)
```

Single test:

```bash
./venv/bin/python -m pytest tests/test_job_pipeline.py -v
./venv/bin/python -m pytest tests/test_job_pipeline.py::test_specific_case -v
npm --prefix apps/web test -- PromptForm      # vitest, filters by filename/testname
```

Optional model-specific smoke tests (require weights placed under `models/`):

```bash
make cogvideox-smoke
make musicgen-smoke
make calibration-report   # builds feedback/quality correlation report
```

`verify_local_stack.py` uses a temp DB/output dir and a free loopback port — it does not touch
`data/`, `outputs/`, or a port-8000 process you already have running.

## Architecture

### Layers and the request lifecycle

```
apps/api/        FastAPI HTTP entrypoint (apps/api/main.py, apps/api/routes/*)
apps/web/         React + Vite Studio UI (apps/web/src/App.tsx + src/components/*)
bootstrap/        Wires everything together (bootstrap/factories.py) — start here to see how
                   ModelService, GeneratorRegistry, JobRepository/Service/Runner, and the
                   Asset/Project/Feedback repositories are constructed and injected.
core/              Media-agnostic domain logic: jobs, schemas, models, assets, projects,
                   feedback, bible, batches, story, prompting, quality.
generators/        Media-specific generation logic (image/audio/video/text), each following
                   validate_request -> prepare -> generate -> cleanup (generators/base.py).
models/            Model manifests (models/manifests/**/*.json) plus local model directories.
```

Every generation request — whether from `POST /generate/{image,audio,video,text}` (convenience
routes) or `POST /jobs` (low-level entry) — is normalized into a `GenerationRequest`
(`core/schemas/generation.py`) and flows through one path:

1. `apps/api/routes/generate.py` builds a `GenerationRequest`.
2. `core/jobs/service.py` `JobService.create_job()` persists the job and enqueues it.
3. `core/jobs/runner.py` `JobRunner` pulls from the queue and looks up the generator via
   `generators/registry.py` by `media_type`.
4. The `generators/<media>/generator.py` implementation runs and returns a `GenerationResult`.
5. `core/assets/__init__.py` syncs successful jobs into `Asset` records (the reusable/exportable
   unit — distinct from `Job`, which is the execution record and can fail).
6. `core/quality/evaluators.py` attaches a heuristic quality report; optional semantic judge
   (`core/quality/semantic.py`, gated by `QUALITY_ENABLE_SEMANTIC_JUDGE`) can add alignment
   scoring.
7. The Web UI polls `/jobs/{id}` and reads `/gallery` to reflect state.

**Do not conflate `Job` and `Asset`.** Jobs are SQLite-backed (`data/jobs.db` via
`core/storage/repositories/job_repository.py`); Projects, Feedback, Bible, Batches, and
StoryDocuments are JSON-backed under `data/*/`, written with replace-on-success atomic saves.
`GET /models` only reads manifests — it never loads a runtime.

### Model system (manifest -> registry -> resolver -> loader -> runtime cache)

Manifests (`models/manifests/{image,audio,video,text}/*.json`) declare models; they contain no
model code. The resolution chain: `ModelService` -> `ModelResolver` (resolves aliases/public_id
to an internal manifest id, checked in that order) -> `ModelRuntimeCache` -> `LoaderRegistry`
(runtime-specific loader creates the runtime if not cached). Generators depend only on
`ModelService` and treat the returned runtime as opaque — they must not depend on a loader's
internal return structure.

Key rules from `docs/model-system.md` worth internalizing before touching this layer:
- API requests and generators use the manifest's `public_id`; `internal manifest id` is private to
  the model-system layer.
- Alias/public_id resolution is centralized in the resolver — never re-implemented in a route or
  generator.
- "Is this model usable?" is decided in exactly one place, `core/model_readiness.py`; API, loaders,
  and scripts all call it rather than re-deriving readiness.
- Text runtimes normalize to one calling contract (`runtime["generate"](...)`,
  `runtime["context_window"]`, `runtime["supports_json_schema"]`) across `template_text_loader`
  (default, no weights needed), `llama_cpp_text_loader` (GGUF), and
  `openai_compatible_text_loader` (local endpoint).
- `openai_compatible_text_loader` only allows loopback hosts unless
  `ALLOW_REMOTE_TEXT_ENDPOINTS=true`; API keys are resolved from an env var named in
  `default_params.api_key_env`, never written into the manifest.
- **Security-relevant:** `LearnedVideoLoader` imports and executes `runtime.py`/`adapter.py` from
  inside a model directory (arbitrary code execution by design). Only place trusted model bundles
  under `MODELS_ROOT`.
- Story/task structured output is schema-constrained; on validation failure the generator asks the
  runtime to repair once, then persists the raw response and raises rather than passing malformed
  JSON downstream.

### Domain concepts (see `docs/domain-model.md` for full detail)

- `GenerationRequest` → `Job` (execution, can fail) → `GenerationResult` → `Asset` (reusable
  output, gallery/export/reuse unit).
- `Project` groups jobs/assets (JSON-backed; deleting a project does not delete its jobs/assets).
- `Feedback` is human rating tied to job/asset/project, feeding calibration.
- `BibleEntry` (`core/bible/`) is reusable creative-continuity config (character/style/brand/
  location/prop) compiled deterministically into prompts by `core/prompting/composer.py`
  (`PromptSpec -> ComposedPrompt`). Conflicts (unknown bible id, seed-lock collision, LoRA
  collision, locked-field override attempt) are recorded in `ComposedPrompt.conflicts` rather than
  raised — a bad reference in one axis value must not fail an entire batch.
- `Batch` (`core/batches/`) expands a `BatchSpec` (axes × stages) into child jobs via a pure
  `expand_items()`; batch item state is *re-derived from the job repository*, not stored
  independently, so it stays correct across process restarts.
- `StoryDocument` / `Timeline` (`core/story/`) hold beats/scenes/chapters and derive an assembly
  timeline; `apply_text_result()` is a pure merge that preserves existing `Scene.asset_ids`/
  `job_ids` when a scene list is regenerated, so a text tweak doesn't discard prior generations.

### Frontend

`apps/web/src/App.tsx` owns most state (initial state, API fetch calls, polling, gallery/metrics/
project state). `apps/web/src/components/PromptForm.tsx` + `styles.css` own form UI/layout;
`App.tsx` owns API-response handling. Other panels under `apps/web/src/components/` are split by
surface (gallery, matrix/batch, story, asset detail, models summary).

## Cross-agent harness (Claude Code ⇄ Codex)

This repo supports delegating work between Claude Code and OpenAI's Codex CLI in both
directions, plus a headless fallback broker for when one is out of quota, logged out, or
down. See `docs/cross-agent-harness.md` first, then `.agents/protocol/v1/worker-policy.md`
for the exact rules (fallback triggers on quota/auth/service-down only, never on an
ordinary task failure; every task runs in a fresh dedicated worktree; a dirty worktree
after a failure blocks rather than falling back). The Claude Code → Codex plugin
(`codex@openai-codex`) is enabled per project *path* — check `claude plugin list` before
assuming `/codex:*` commands are available in whichever worktree you're in.

## Conventions

- Follow `AGENTS.md` for any Web UI change: source-of-truth docs are `docs/design-system.md`,
  `docs/ui-principles.md`, `docs/design-directions.md`. Do not add a UI library without explicit
  approval — reuse the existing React + CSS implementation and semantic design tokens. Before
  calling a frontend change complete, verify spacing/control height/radius/typography/borders,
  loading/empty/error/disabled/long-text/large-list states, keyboard focus/ARIA/non-color status
  cues, and inspect the rendered page at 390px/768px/1280px/1440px widths — do not judge visual
  completion from source alone; run the frontend tests and production build.
- Extending a media type touches, in order: `core/schemas/generation.py` (`MediaType`) →
  `generators/` implementation → `generators/registry.py` + `bootstrap/factories.py` →
  `models/manifests/` → `apps/web/src/App.tsx` + `PromptForm.tsx`.
- Adding an API route: add to `apps/api/routes/`, wire any new service through
  `bootstrap/factories.py`, register with `include_router()` in `apps/api/main.py`, add a test
  under `tests/`, and update `docs/api-contract.md`.
- Egress guards default closed: `ALLOW_REMOTE_TEXT_ENDPOINTS`, `ALLOW_REMOTE_AUDIO_ENDPOINTS`, and
  `ALLOW_CLOUD_PROVIDERS` are all `false` by default in `.env.example` — keep new network-calling
  code local-only unless a guard is explicitly threaded through.
- Quality scores are `heuristic_local_v1` technical-quality proxies (resolution/exposure/contrast/
  clipping/silence-ratio/etc., varying by media type), not semantic or artistic judgments; the
  optional semantic judge is a separate, explicitly-gated scorer.
- Commit message style (`CONTRIBUTING.md`): start with a verb (Add/Fix/Update/Remove/Refactor), be
  specific, reference issue numbers where relevant.
