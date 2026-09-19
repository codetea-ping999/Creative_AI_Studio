#!/usr/bin/env bash
set -euo pipefail

# build_release_artifact.sh
# Builds the Creative AI Studio v1.0.0 release artifact:
#   - tracked source tree (git archive)
#   - prebuilt apps/web/dist with VITE_API_BASE_URL=""
#   - excludes: venv, node_modules, data/, outputs/, .env, apps/desktop, model weights
#   - produces: creative-ai-studio-v1.0.0.tar.gz + SHA256SUMS

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="v1.0.0"
ARTIFACT_NAME="creative-ai-studio-${VERSION}"
OUT_DIR="${ROOT_DIR}/artifacts"
STAGE_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${STAGE_DIR}"
}
trap cleanup EXIT

mkdir -p "${OUT_DIR}"

echo "=== Building web UI (VITE_API_BASE_URL=\"\") ==="
VITE_API_BASE_URL="" npm --prefix "${ROOT_DIR}/apps/web" run build

echo "=== Staging source tree from HEAD ==="
git -C "${ROOT_DIR}" archive HEAD | tar -x -C "${STAGE_DIR}"

echo "=== Removing Desktop app from staging ==="
rm -rf "${STAGE_DIR}/apps/desktop"

echo "=== Copying prebuilt web dist ==="
cp -R "${ROOT_DIR}/apps/web/dist" "${STAGE_DIR}/apps/web/dist"

echo "=== Verifying exclusions ==="
for p in venv apps/web/node_modules data outputs .env apps/desktop; do
  if [[ -e "${STAGE_DIR}/${p}" ]]; then
    echo "ERROR: excluded path present: ${p}" >&2
    exit 1
  fi
done

echo "=== Creating tarball ==="
tar -czf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" -C "${STAGE_DIR}" .

echo "=== Computing SHA256 ==="
shasum -a 256 "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" > "${OUT_DIR}/SHA256SUMS"

echo "=== Verifying artifact contents ==="
tar -tzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" | sort > "${OUT_DIR}/artifact-manifest.txt"

echo "=== Exclusion assertions ==="
if tar -tzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" | rg '(^|/)(venv|node_modules|data|outputs)/|(^|/)\.env($|/)|apps/desktop' >/dev/null; then
  echo "ERROR: forbidden paths found in artifact" >&2
  exit 1
fi

if tar -tzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" | rg -i '\.(safetensors|ckpt|gguf|onnx|pth|pt|bin|model)$' >/dev/null; then
  echo "ERROR: weight files found in artifact" >&2
  exit 1
fi

echo "=== Required paths present ==="
if ! tar -tzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" | rg 'apps/web/dist/index\.html' >/dev/null; then
  echo "ERROR: apps/web/dist/index.html missing" >&2
  exit 1
fi
if ! tar -tvzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" | rg 'scripts/run_studio\.sh$' | rg '^-rwx' >/dev/null; then
  echo "ERROR: scripts/run_studio.sh missing or not executable" >&2
  exit 1
fi

echo "=== Build complete ==="
echo "Artifact: ${OUT_DIR}/${ARTIFACT_NAME}.tar.gz"
cat "${OUT_DIR}/SHA256SUMS"
echo "Manifest: ${OUT_DIR}/artifact-manifest.txt ($(wc -l < "${OUT_DIR}/artifact-manifest.txt") entries)"