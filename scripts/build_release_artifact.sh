#!/usr/bin/env bash
set -euo pipefail

# build_release_artifact.sh
# Builds the Creative AI Studio release artifact:
#   - tracked source tree (git archive)
#   - prebuilt apps/web/dist with VITE_API_BASE_URL=""
#   - excludes: venv, node_modules, data/, outputs/, .env, apps/desktop, model weights
#   - produces: creative-ai-studio-<version>.tar.gz + SHA256SUMS
#
# The version comes from the repository-root VERSION file (the single source of
# truth shared with core/version.py and scripts/smoke_release_artifact.py), so
# cutting a new release means editing one file.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "${ROOT_DIR}/VERSION" ]]; then
  echo "ERROR: ${ROOT_DIR}/VERSION is missing; it is the single source of truth for the release version." >&2
  exit 1
fi
VERSION="v$(tr -d '[:space:]' < "${ROOT_DIR}/VERSION")"
if [[ ! "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "ERROR: VERSION must hold a single MAJOR.MINOR.PATCH version, got ${VERSION#v}" >&2
  exit 1
fi
ARTIFACT_NAME="creative-ai-studio-${VERSION}"

# macOS ships `shasum`, most Linux distributions ship `sha256sum`. The release
# workflow runs this script on Linux, so accept either rather than assuming.
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1"
  else
    echo "ERROR: neither sha256sum nor shasum is available." >&2
    exit 1
  fi
}
OUT_DIR="${ROOT_DIR}/artifacts"
STAGE_DIR="$(mktemp -d)"
STAGE_ROOT="${STAGE_DIR}/${ARTIFACT_NAME}"

cleanup() {
  rm -rf "${STAGE_DIR}"
}
trap cleanup EXIT

mkdir -p "${OUT_DIR}"

echo "=== Checking working tree clean ==="
if ! git -C "${ROOT_DIR}" diff --quiet || ! git -C "${ROOT_DIR}" diff --cached --quiet; then
  echo "ERROR: working tree has uncommitted changes. Commit or stash before building artifact." >&2
  exit 1
fi
if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain --untracked-files=all)" ]]; then
  echo "ERROR: working tree has untracked files. Add, commit, or remove them before building artifact." >&2
  exit 1
fi

echo "=== Installing web UI dependencies (npm ci) ==="
npm ci --prefix "${ROOT_DIR}/apps/web"

echo "=== Building web UI (VITE_API_BASE_URL=\"\") ==="
VITE_API_BASE_URL="" npm --prefix "${ROOT_DIR}/apps/web" run build

echo "=== Staging source tree from HEAD ==="
mkdir -p "${STAGE_ROOT}"
git -C "${ROOT_DIR}" archive HEAD | tar -x -C "${STAGE_ROOT}"

echo "=== Removing Desktop app from staging ==="
rm -rf "${STAGE_ROOT}/apps/desktop"

echo "=== Copying prebuilt web dist ==="
cp -R "${ROOT_DIR}/apps/web/dist" "${STAGE_ROOT}/apps/web/dist"

echo "=== Verifying exclusions ==="
for p in venv apps/web/node_modules data outputs .env apps/desktop; do
  if [[ -e "${STAGE_ROOT}/${p}" ]]; then
    echo "ERROR: excluded path present: ${p}" >&2
    exit 1
  fi
done

echo "=== Creating tarball ==="
COPYFILE_DISABLE=1 tar -czf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" -C "${STAGE_DIR}" "${ARTIFACT_NAME}"

echo "=== Computing SHA256 ==="
# Record the bare filename rather than this machine's absolute path: the
# tarball and SHA256SUMS land side by side in the Release, so the documented
# `sha256sum -c SHA256SUMS` has to resolve against the downloader's own
# directory, not the build runner's.
(cd "${OUT_DIR}" && sha256_file "${ARTIFACT_NAME}.tar.gz") > "${OUT_DIR}/SHA256SUMS"

echo "=== Verifying artifact contents ==="
# Write the listings to files and assert against those. Piping `tar` into
# `grep -q` lets grep exit on the first match, which SIGPIPEs tar; under
# `set -o pipefail` that non-zero status would make a *matching* forbidden
# path look like "no match" and silently pass the exclusion assertions.
VERBOSE_LISTING="${OUT_DIR}/artifact-listing.txt"
tar -tzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" | sort > "${OUT_DIR}/artifact-manifest.txt"
tar -tvzf "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz" > "${VERBOSE_LISTING}"

echo "=== Exclusion assertions ==="
if grep -Eq '(^|/)(venv|node_modules|data|outputs)/|(^|/)\.env($|/)|apps/desktop' "${OUT_DIR}/artifact-manifest.txt"; then
  echo "ERROR: forbidden paths found in artifact" >&2
  exit 1
fi

if grep -Eqi '\.(safetensors|ckpt|gguf|onnx|pth|pt|bin|model)$' "${OUT_DIR}/artifact-manifest.txt"; then
  echo "ERROR: weight files found in artifact" >&2
  exit 1
fi

if grep -Eq '(^|/)\._' "${OUT_DIR}/artifact-manifest.txt"; then
  echo "ERROR: AppleDouble (._*) entries found in artifact" >&2
  exit 1
fi

echo "=== Required paths present ==="
if ! grep -Fqx "${ARTIFACT_NAME}/apps/web/dist/index.html" "${OUT_DIR}/artifact-manifest.txt"; then
  echo "ERROR: ${ARTIFACT_NAME}/apps/web/dist/index.html missing" >&2
  exit 1
fi
if ! grep -Eq "^-rwx.* ${ARTIFACT_NAME}/scripts/run_studio\.sh$" "${VERBOSE_LISTING}"; then
  echo "ERROR: ${ARTIFACT_NAME}/scripts/run_studio.sh missing or not executable" >&2
  exit 1
fi
if ! grep -Fqx "${ARTIFACT_NAME}/VERSION" "${OUT_DIR}/artifact-manifest.txt"; then
  echo "ERROR: ${ARTIFACT_NAME}/VERSION missing; /version cannot report the release" >&2
  exit 1
fi

echo "=== Build complete ==="
echo "Artifact: ${OUT_DIR}/${ARTIFACT_NAME}.tar.gz"
cat "${OUT_DIR}/SHA256SUMS"
echo "Manifest: ${OUT_DIR}/artifact-manifest.txt ($(wc -l < "${OUT_DIR}/artifact-manifest.txt") entries)"