#!/usr/bin/env bash
# Desktop Shell packaged-app smoke.
#
# Verifies the bundled macOS .app against a live backend:
#   - default port (127.0.0.1:8000) resolution with NO env override
#   - CORS read / preflight / JSON write from Origin tauri://localhost
#   - non-default port (default 8123) integration: the backend is started via
#     `API_PORT=8123 ./scripts/run_api_dev.sh` plus a root `.env`, then a
#     normally-launched packaged app connects to that port and must NOT touch
#     8000 (proves DesktopRuntime precedence: STUDIO_BACKEND_URL > root .env
#     API_PORT > API_PORT env > loopback default, matching run_api_dev.sh)
#   - a running second instance just focuses the existing one (single process)
#
# Build the bundle first:
#   cd apps/desktop/src-tauri && cargo tauri build
#
# Then run from the repo root:
#   ./scripts/desktop_smoke.sh
#
# Only processes this script started are stopped on exit. `--force` also
# stops whatever is listening on the two smoke ports and any already-running
# instance of this exact bundle binary before the run. An existing root `.env`
# is moved aside for the whole run (so it cannot override the phase ports) and
# restored on exit; the temporary one is removed. Requires macOS (GUI app)
# plus curl/pgrep/lsof.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_BIN="${APP_BIN:-$ROOT/apps/desktop/src-tauri/target/release/bundle/macos/Creative AI Studio.app/Contents/MacOS/creative-ai-studio-desktop}"
API_SCRIPT="$ROOT/scripts/run_api_dev.sh"
DEFAULT_PORT="${STUDIO_DEFAULT_PORT:-8000}"
NON_DEFAULT_PORT="${API_PORT:-8123}"
LOG_DIR="${DESKTOP_SMOKE_LOG_DIR:-}"
FORCE=0
FAILED=0
# .env bookkeeping: cleanup touches the root .env only after the script has
# actually taken ownership of it (ENV_OWNED=1).
ENV_OWNED=0
ENV_BACKUP=""
# PIDs this script started; cleanup never signals anything else.
OWNED_PIDS=()
APP_PID=""

say() { printf '\n== %s ==\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILED=1; }

stop_owned_pid() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 0
  # uvicorn --reload runs workers as children of the PID we started.
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  # Capture the status that triggered EXIT before running anything else.
  local rc=$?
  trap - EXIT
  set +e
  local pid
  for pid in "${OWNED_PIDS[@]+"${OWNED_PIDS[@]}"}"; do
    stop_owned_pid "$pid"
  done
  # The API lifespan can take several seconds to join its threads after TERM;
  # wait (bounded) for every owned PID, then KILL whatever is still alive so
  # the smoke never exits with its ports occupied.
  local waited=0 alive
  while :; do
    alive=0
    for pid in "${OWNED_PIDS[@]+"${OWNED_PIDS[@]}"}"; do
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    [[ "$alive" -eq 0 || "$waited" -ge 15 ]] && break
    sleep 1
    waited=$((waited + 1))
  done
  if [[ "$alive" -eq 1 ]]; then
    for pid in "${OWNED_PIDS[@]+"${OWNED_PIDS[@]}"}"; do
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill -KILL "$pid" 2>/dev/null || true
    done
  fi
  if [[ "$ENV_OWNED" -eq 1 ]]; then
    rm -f "$ROOT/.env"
    if [[ -n "$ENV_BACKUP" && -f "$ENV_BACKUP" ]]; then
      mv -f "$ENV_BACKUP" "$ROOT/.env"
    fi
  fi
  if [[ -n "$LOG_DIR" ]]; then printf '\nLogs: %s\n' "$LOG_DIR"; fi
  if [[ "$rc" -ne 0 ]]; then exit "$rc"; fi
  if [[ "$FAILED" -ne 0 ]]; then exit 1; fi
  exit 0
}
trap cleanup EXIT

ui_request_seen() { grep -qE 'GET /(catalog/loras|projects|gallery|metrics)' "$1"; }
backend_running_here() { curl -sf "http://127.0.0.1:$1/health" >/dev/null 2>&1; }

wait_for_health() {
  local port="$1"
  for _ in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

wait_for_ui_requests() {
  local log="$1"
  for _ in $(seq 1 30); do
    if [[ -f "$log" ]] && ui_request_seen "$log"; then return 0; fi
    sleep 1
  done
  return 1
}

# --force only: stop exactly the listener on a smoke port (never a pattern kill).
force_stop_port_listener() {
  local port="$1" pids
  pids="$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    say "stopping pre-existing listener on :$port (--force): $(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
    sleep 2
  fi
}

start_backend() {
  local port="$1" log="$2" pid
  if backend_running_here "$port"; then
    fail "a backend already answers on 127.0.0.1:$port; stop it or pass --force"
    exit 1
  fi
  API_PORT="$port" "$API_SCRIPT" >"$log" 2>&1 &
  pid=$!
  OWNED_PIDS+=("$pid")
  if ! wait_for_health "$port"; then
    fail "backend did not become healthy on :$port (see $log)"
    exit 1
  fi
  pass "backend healthy on 127.0.0.1:$port (pid $pid, log: $log)"
}

launch_app() {
  # Normal-launch equivalent: no STUDIO_BACKEND_URL, no API_PORT inherited.
  # A bundle inside this checkout finds the root .env next to its executable;
  # an APP_BIN elsewhere is pointed at this checkout explicitly.
  local root_env=(-u CREATIVE_AI_STUDIO_ROOT)
  if [[ "$APP_BIN" != "$ROOT"/* ]]; then root_env=("CREATIVE_AI_STUDIO_ROOT=$ROOT"); fi
  env -u API_PORT -u STUDIO_BACKEND_URL "${root_env[@]}" "$APP_BIN" >"$LOG_DIR/app-$1.log" 2>&1 &
  APP_PID=$!
  OWNED_PIDS+=("$APP_PID")
}

stop_app() {
  if [[ -n "$APP_PID" ]]; then stop_owned_pid "$APP_PID"; fi
  sleep 2
}

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --logs=*) LOG_DIR="${arg#--logs=}" ;;
    -h|--help)
      grep -E '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) fail "unknown argument: $arg"; exit 1 ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || { fail "packaged-app smoke requires macOS"; exit 1; }
[[ -x "$APP_BIN" ]] || {
  fail "packaged app binary not found: $APP_BIN"
  printf 'Build it first: (cd apps/desktop/src-tauri && cargo tauri build)\n'
  exit 1
}

if [[ "$FORCE" -eq 1 ]]; then
  force_stop_port_listener "$DEFAULT_PORT"
  force_stop_port_listener "$NON_DEFAULT_PORT"
  EXISTING_APP="$(pgrep -f -x "$APP_BIN" 2>/dev/null || true)"
  if [[ -n "$EXISTING_APP" ]]; then
    say "stopping running instance of this bundle (--force): $(echo "$EXISTING_APP" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $EXISTING_APP 2>/dev/null || true
    sleep 2
  fi
fi
if backend_running_here "$DEFAULT_PORT" || backend_running_here "$NON_DEFAULT_PORT"; then
  fail "a Studio backend already appears to be running; stop it or pass --force"
  exit 1
fi
if pgrep -f -x "$APP_BIN" >/dev/null 2>&1; then
  fail "this desktop bundle is already running; quit it or pass --force"
  exit 1
fi

if [[ -z "$LOG_DIR" ]]; then LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/desktop-smoke.XXXX")"; fi
mkdir -p "$LOG_DIR"

# Take ownership of the root .env: move a user-provided one aside so it can
# neither override the per-phase API_PORT (run_api_dev.sh sources it after the
# environment) nor be clobbered. Restored by cleanup.
if [[ -e "$ROOT/.env" ]]; then
  ENV_BACKUP="$(mktemp "$LOG_DIR/real-env.backup.XXXX")"
  mv -f "$ROOT/.env" "$ENV_BACKUP"
fi
ENV_OWNED=1

LOG_DEFAULT="$LOG_DIR/api-${DEFAULT_PORT}.log"
LOG_NON_DEFAULT="$LOG_DIR/api-${NON_DEFAULT_PORT}.log"

say "1. default port ($DEFAULT_PORT) without env override"
start_backend "$DEFAULT_PORT" "$LOG_DEFAULT"
launch_app "default-port"
if wait_for_ui_requests "$LOG_DEFAULT"; then
  pass "packaged app loaded Studio data from 127.0.0.1:$DEFAULT_PORT (no env override)"
else
  fail "no app requests observed on :$DEFAULT_PORT (app log: $LOG_DIR/app-default-port.log)"
fi

say "2. CORS read / preflight / JSON write from tauri://localhost"
ORIGIN="tauri://localhost"
READ_ACAO="$(curl -s -H "Origin: $ORIGIN" -D - "http://127.0.0.1:$DEFAULT_PORT/health" -o /dev/null | tr -d '\r' | grep -i '^access-control-allow-origin:' || true)"
PREFLIGHT_200="$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type" "http://127.0.0.1:$DEFAULT_PORT/projects" || true)"
CREATE_CODE="$(curl -s -o "$LOG_DIR/create.json" -w '%{http_code}' -H "Origin: $ORIGIN" -H "Content-Type: application/json" -X POST "http://127.0.0.1:$DEFAULT_PORT/projects" -d '{"name":"__desktop_smoke_cors__","status":"active"}' || true)"
if [[ "$READ_ACAO" == *"access-control-allow-origin: $ORIGIN"* ]] && [[ "$PREFLIGHT_200" == "200" ]] && [[ "$CREATE_CODE" == "201" ]]; then
  pass "CORS read + preflight + JSON write (201) accepted from $ORIGIN"
else
  fail "CORS detail: read_acao='$READ_ACAO' preflight=$PREFLIGHT_200 create=$CREATE_CODE"
fi
if [[ "$CREATE_CODE" == "201" ]]; then
  PROJ_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$LOG_DIR/create.json" 2>/dev/null || true)"
  if [[ -z "$PROJ_ID" ]]; then
    fail "could not read the smoke project id from $LOG_DIR/create.json; remove __desktop_smoke_cors__ manually"
  else
    DELETE_CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Origin: $ORIGIN" -X DELETE "http://127.0.0.1:$DEFAULT_PORT/projects/$PROJ_ID" || true)"
    if [[ "$DELETE_CODE" =~ ^2[0-9][0-9]$ ]]; then
      pass "smoke project $PROJ_ID removed (HTTP $DELETE_CODE)"
    else
      fail "DELETE /projects/$PROJ_ID returned HTTP '${DELETE_CODE}'; remove it manually"
    fi
  fi
fi

say "3. non-default port ($NON_DEFAULT_PORT) via root .env, no env override"
stop_app
printf 'API_PORT=%s\n' "$NON_DEFAULT_PORT" > "$ROOT/.env"
start_backend "$NON_DEFAULT_PORT" "$LOG_NON_DEFAULT"
DEFAULT_BEFORE="$(wc -l < "$LOG_DEFAULT" | tr -d ' ')"
launch_app "non-default-port"
if wait_for_ui_requests "$LOG_NON_DEFAULT"; then
  pass "packaged app loaded Studio data from 127.0.0.1:$NON_DEFAULT_PORT via .env (no export)"
else
  fail "no app requests observed on :$NON_DEFAULT_PORT (app log: $LOG_DIR/app-non-default-port.log)"
fi
DEFAULT_AFTER="$(wc -l < "$LOG_DEFAULT" | tr -d ' ')"
if [[ "$DEFAULT_AFTER" == "$DEFAULT_BEFORE" ]]; then
  pass "app never touched :$DEFAULT_PORT while this instance ran (line count frozen at $DEFAULT_BEFORE)"
else
  fail "app touched :$DEFAULT_PORT during the non-default run ($DEFAULT_BEFORE -> $DEFAULT_AFTER lines)"
fi

say "4. second instance focuses the existing one"
stop_app
launch_app "second-instance-1"
FIRST_PID="$APP_PID"
sleep 5
launch_app "second-instance-2"
SECOND_PID="$APP_PID"
sleep 8
if kill -0 "$FIRST_PID" 2>/dev/null && ! kill -0 "$SECOND_PID" 2>/dev/null; then
  pass "second launch exited and focused the existing instance (pid $FIRST_PID still running)"
else
  fail "expected only pid $FIRST_PID alive; second instance pid $SECOND_PID is $(kill -0 "$SECOND_PID" 2>/dev/null && echo alive || echo gone)"
fi

if [[ "$FAILED" -eq 0 ]]; then
  say "desktop smoke: ALL PASS"
else
  say "desktop smoke: FAILURES DETECTED"
fi
