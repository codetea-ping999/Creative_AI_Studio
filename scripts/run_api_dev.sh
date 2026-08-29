#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

API_HOST_VALUE="${API_HOST:-127.0.0.1}"
API_HOST_IS_LOOPBACK=0
if [[ "$API_HOST_VALUE" == "localhost" || "$API_HOST_VALUE" == "::1" ]]; then
  API_HOST_IS_LOOPBACK=1
elif [[ "$API_HOST_VALUE" =~ ^127\.([0-9]{1,3}\.){2}[0-9]{1,3}$ ]]; then
  API_HOST_IS_LOOPBACK=1
fi

if [[ "$API_HOST_IS_LOOPBACK" != "1" ]]; then
  if [[ "${ALLOW_UNSAFE_API_BIND:-0}" != "1" ]]; then
    printf 'Refusing non-loopback API_HOST=%q. The development API is unauthenticated; set ALLOW_UNSAFE_API_BIND=1 only after accepting the exposure risk.\n' "$API_HOST_VALUE" >&2
    exit 2
  fi
  printf 'WARNING: binding the unauthenticated development API to non-loopback host %q.\n' "$API_HOST_VALUE" >&2
fi

if [[ -x "$ROOT_DIR/venv/bin/uvicorn" ]]; then
  UVICORN_BIN="$ROOT_DIR/venv/bin/uvicorn"
else
  UVICORN_BIN="uvicorn"
fi

exec "$UVICORN_BIN" apps.api.main:app \
  --host "$API_HOST_VALUE" \
  --port "${API_PORT:-8000}" \
  --reload \
  --reload-dir apps \
  --reload-dir bootstrap \
  --reload-dir core \
  --reload-dir generators \
  --reload-dir models \
  --reload-exclude 'venv/*' \
  --reload-exclude 'apps/web/node_modules/*' \
  --reload-exclude 'apps/web/dist/*'
