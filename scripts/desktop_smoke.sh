#!/usr/bin/env bash
# Desktop Shell packaged-app smoke (v0.1).
#
# Verifies the bundled macOS .app against a live backend:
#   - default port (127.0.0.1:8000) resolution with NO env override
#   - CORS read / preflight / JSON write from Origin tauri://localhost
#   - non-default port (default 8123) integration: theme started via
#     `API_PORT=8123 ./scripts/run_api_dev.sh` plus a root `.env`, then a
#     normally-launched packaged app connects to that port and must NOT touch
#     8000 (proves DesktopRuntime precedence: STUDIO_BACKEND_URL > API_PORT
#     env > root .env API_PORT > loopback default)
#   - a running second instance just focuses the existing one (single process)
#
# Build the bundle first:
#   cd apps/desktop/src-tauri && cargo tauri build
#
# Then run from the repo root:
#   ./scripts/desktop_smoke.sh
#
# `--force` stops a pre-existing Studio desktop/backend first. A root `.env`
# is created only temporarily and restored on exit. Requires macOS (GUI app)
# plus curl/pgrep/pkill.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_BIN="${APP_BIN:-$ROOT/apps/desktop/src-tauri/target/release/bundle/macos/Creative AI Studio.app/Contents/MacOS/creative-ai-studio-desktop}"
API_SCRIPT="$ROOT/scripts/run_api_dev.sh"
DEFAULT_PORT="${STUDIO_DEFAULT_PORT:-8000}"
NON_DEFAULT_PORT="${API_PORT:-8123}"
LOG_DIR="${DESKTOP_SMOKE_LOG_DIR:-$(mktemp -d /tmp/desktop-smoke.XXXX)}"
FORCE=0
FAILED=0
ENV_BACKUP=""

say() { printf '\n== %s ==\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILED=1; }

cleanup() {
  [[ -n "${APP_PID:-}" ]] && kill "$APP_PID" 2>/dev/null || true
  pkill -f "creative-ai-studio-desktop" 2>/dev/null || true
  pkill -f "uvicorn apps.api.main:app" 2>/dev/null || true
  sleep 1
  if [[ -n "$ENV_BACKUP" ]]; then
    if [[ -f "$ENV_BACKUP" ]]; then mv -f "$ENV_BACKUP" "$ROOT/.env"; else rm -f "$ROOT/.env"; fi
  else
    rm -f "$ROOT/.env"
  fi
  printf '\nLogs: %s\n' "$LOG_DIR"
  if [[ "$FAILED" -eq 0 ]]; then exit 0; else exit 1; fi
}
trap cleanup EXIT

ui_request_seen() { grep -qE 'GET /(catalog/loras|projects|gallery|metrics)' "$1"; }
backend_running_here() { curl -sf "http://127.0.0.1:$1/health" >/dev/null 2>&1; }

wait_for_health() {
  local port="$1" i
  for i in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

wait_for_ui_requests() {
  local log="$1" i
  for i in $(seq 1 30); do
    if [[ -f "$log" ]] && ui_request_seen "$log"; then return 0; fi
    sleep 1
  done
  return 1
}

start_backend() {
  local port="$1" log="$2"
  if backend_running_here "$port" && [[ "$FORCE" -eq 0 ]]; then
    fail "a backend already answers on 127.0.0.1:$port; stop it or pass --force"
    exit 1
  fi
  if backend_running_here "$port"; then
    say "stopping pre-existing backend on :$port (--force)"
    pkill -f "uvicorn apps.api.main:app" 2>/dev/null || true
    sleep 2
  fi
  (cd "$ROOT" && API_PORT="$port" nohup "$API_SCRIPT" >"$log" 2>&1 &) 
  if ! wait_for_health "$port"; then
    fail "backend did not become healthy on :$port (see $log)"
    exit 1
  fi
  pass "backend healthy on 127.0.0.1:$port (log: $log)"
}

launch_app() {
  # Normal-launch equivalent: no STUDIO_BACKEND_URL, no API_PORT inherited.
  env -u API_PORT -u STUDIO_BACKEND_URL "$APP_BIN" >"$LOG_DIR/app-$1.log" 2>&1 &
  APP_PID=$!
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

if ( backend_running_here "$DEFAULT_PORT" || backend_running_here "$NON_DEFAULT_PORT" ) && [[ "$FORCE" -eq 0 ]]; then
  fail "a Studio backend already appears to be running; stop it or pass --force"
  exit 1
fi

# Preserve any user-provided root .env so the temporary one never clobbers it.
if [[ -f "$ROOT/.env" ]]; then
  ENV_BACKUP="$(mktemp "$LOG_DIR/real-env.backup.XXXX")"
  cp "$ROOT/.env" "$ENV_BACKUP"
fi

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
PREFLIGHT_200="$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type" "http://127.0.0.1:$DEFAULT_PORT/projects")"
CREATE_CODE="$(curl -s -o "$LOG_DIR/create.json" -w '%{http_code}' -H "Origin: $ORIGIN" -H "Content-Type: application/json" -X POST "http://127.0.0.1:$DEFAULT_PORT/projects" -d '{"name":"__desktop_smoke_cors__","status":"active"}')"
if [[ "$READ_ACAO" == *"access-control-allow-origin: $ORIGIN"* ]] && [[ "$PREFLIGHT_200" == "200" ]] && [[ "$CREATE_CODE" == "201" ]]; then
  pass "CORS read + preflight + JSON write (201) accepted from $ORIGIN"
else
  fail "CORS detail: read_acao='$READ_ACAO' preflight=$PREFLIGHT_200 create=$CREATE_CODE"
fi
if [[ "$CREATE_CODE" == "201" ]]; then
  PROJ_ID="$(python3 -c "import json,sys;print(json.load(open('$LOG_DIR/create.json'))['id'])" 2>/dev/null || true)"
  [[ -n "$PROJ_ID" ]] && curl -sf -H "Origin: $ORIGIN" -X DELETE "http://127.0.0.1:$DEFAULT_PORT/projects/$PROJ_ID" >/dev/null 2>&1 && pass "smoke project removed"
fi

say "3. non-default port ($NON_DEFAULT_PORT) via root .env, no env override"
kill "$APP_PID" 2>/dev/null || true
sleep 2
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
kill "$APP_PID" 2>/dev/null || true
sleep 2
launch_app "second-instance-1"
sleep 5
"$APP_BIN" >"$LOG_DIR/app-second-instance-2.log" 2>&1 &
sleep 8
PROC_COUNT="$(pgrep -f 'creative-ai-studio-desktop' | wc -l | tr -d ' ')"
if [[ "$PROC_COUNT" -eq 1 ]]; then
  pass "second launch focused the existing instance (single desktop process: $PROC_COUNT)"
else
  fail "expected 1 desktop process, got $PROC_COUNT"
fi

if [[ "$FAILED" -eq 0 ]]; then
  say "desktop smoke: ALL PASS"
else
  say "desktop smoke: FAILURES DETECTED"
fi