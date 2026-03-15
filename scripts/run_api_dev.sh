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

if [[ -x "$ROOT_DIR/venv/bin/uvicorn" ]]; then
  UVICORN_BIN="$ROOT_DIR/venv/bin/uvicorn"
else
  UVICORN_BIN="uvicorn"
fi

exec "$UVICORN_BIN" apps.api.main:app \
  --host "${API_HOST:-127.0.0.1}" \
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
