#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"

is_valid_port() {
  [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && (( 10#$1 <= 65535 ))
}

if ! is_valid_port "$API_PORT" || ! is_valid_port "$WEB_PORT"; then
  printf 'API_PORT and WEB_PORT must be valid TCP port numbers.\n' >&2
  exit 2
fi

export WEB_PORT
API_URL="http://127.0.0.1:${API_PORT}"
WEB_URL="http://localhost:${WEB_PORT}"
API_LOG=""
WEB_LOG=""

is_available() {
  curl --silent --fail --connect-timeout 1 --max-time 2 "$1" >/dev/null 2>&1
}

api_allows_web_origin() {
  local headers
  if ! headers="$(
    curl --silent --fail --connect-timeout 1 --max-time 2 \
      --request OPTIONS \
      --header "Origin: $WEB_URL" \
      --header "Access-Control-Request-Method: GET" \
      --dump-header - \
      --output /dev/null \
      "${API_URL}/health"
  )"; then
    return 1
  fi

  printf '%s\n' "$headers" | awk -v expected="$WEB_URL" '
    {
      sub(/\r$/, "")
    }
    tolower($0) ~ /^access-control-allow-origin:/ {
      value = $0
      sub(/^[^:]*:[[:space:]]*/, "", value)
      if (value == expected) {
        allowed = 1
      }
    }
    END {
      exit allowed ? 0 : 1
    }
  '
}

web_uses_expected_api() {
  curl --silent --fail --connect-timeout 1 --max-time 2 \
    "${WEB_URL}/src/studioClient.ts" | \
    grep -Fq "\"VITE_API_BASE_URL\": \"${API_URL}\""
}

create_log() {
  local service_name="$1"
  mktemp "${TMPDIR:-/tmp}/creative-ai-studio-${service_name}.XXXXXX"
}

start_api() {
  API_LOG="$(create_log api)"
  chmod 600 "$API_LOG"
  nohup "$ROOT_DIR/scripts/run_api_dev.sh" >"$API_LOG" 2>&1 < /dev/null &
}

start_web() {
  WEB_LOG="$(create_log web)"
  chmod 600 "$WEB_LOG"
  VITE_API_BASE_URL="$API_URL" nohup npm --prefix "$ROOT_DIR/apps/web" run dev -- \
    --host localhost --port "$WEB_PORT" --strictPort >"$WEB_LOG" 2>&1 < /dev/null &
}

open_studio() {
  if command -v open >/dev/null 2>&1; then
    open "$WEB_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$WEB_URL" >/dev/null 2>&1 || true
  fi
}

if is_available "${WEB_URL}"; then
  if ! web_uses_expected_api; then
    printf 'Existing Web UI at %s is not configured to use %s.\n' "$WEB_URL" "$API_URL" >&2
    printf 'Stop the existing Vite server, then run ./scripts/start_studio.sh again.\n' >&2
    exit 1
  fi
fi

if is_available "${API_URL}/health"; then
  if ! api_allows_web_origin; then
    printf 'Existing API at %s does not allow the configured Web origin %s.\n' \
      "$API_URL" "$WEB_URL" >&2
    printf 'Stop and restart the API with ./scripts/start_studio.sh after checking WEB_PORT.\n' >&2
    exit 1
  fi
else
  start_api
fi

if ! is_available "${WEB_URL}"; then
  start_web
fi

deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
  if is_available "${API_URL}/health" && \
    api_allows_web_origin && \
    is_available "${WEB_URL}" && \
    web_uses_expected_api; then
    open_studio
    printf 'Creative AI Studio is ready at %s\n' "$WEB_URL"
    exit 0
  fi
  sleep 1
done

printf 'Studio did not become ready.\n' >&2
if [[ -n "$API_LOG" ]]; then
  printf 'API log: %s\n' "$API_LOG" >&2
fi
if [[ -n "$WEB_LOG" ]]; then
  printf 'Web log: %s\n' "$WEB_LOG" >&2
fi
exit 1
