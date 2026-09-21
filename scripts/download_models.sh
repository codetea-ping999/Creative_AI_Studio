#!/usr/bin/env bash

# Download the local model weights that GET /models reports as missing_files.
#
# Covers the two models tracked in issue #421:
#   - CogVideoX-2B  -> ./models/video/cogvideox-2b   (learned-video)
#   - MusicGen Small -> ./models/audio/musicgen-small (musicgen-small)
#
# Weights are gitignored runtime artifacts, so this has to run on the machine
# that serves the Studio. See docs/model-download-guide.md.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VIDEO_REPO="THUDM/CogVideoX-2b"
VIDEO_DIR="./models/video/cogvideox-2b"
AUDIO_REPO="facebook/musicgen-small"
AUDIO_DIR="./models/audio/musicgen-small"
T5_REPO="google-t5/t5-base"
LONG_FORM_DIR="./models/audio/musicgen-long-form"

# Estimated download size per target, in MiB, from the file sizes published on
# the Hugging Face repositories. Used for the free-space check only.
VIDEO_SIZE_MB=13200
AUDIO_SIZE_MB=2300
LONG_FORM_SIZE_MB=1950

DOWNLOAD_VIDEO=1
DOWNLOAD_AUDIO=1
WITH_LONG_FORM=0
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage: scripts/download_models.sh [options]

Options:
  --only video         Download CogVideoX-2B only (~13.2 GiB)
  --only audio         Download MusicGen Small only (~2.3 GiB)
  --with-long-form     Also stage the optional AudioCraft long-form assets
                       (AudioCraft state dicts + a local t5-base, ~1.9 GiB)
  --dry-run            Print the commands without downloading
  -h, --help           Show this message

Without --only, both CogVideoX-2B and MusicGen Small are downloaded.
Re-running is safe: completed files are skipped and interrupted ones resume.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      [[ $# -ge 2 ]] || { printf 'Missing value for --only.\n' >&2; exit 2; }
      case "$2" in
        video) DOWNLOAD_VIDEO=1; DOWNLOAD_AUDIO=0 ;;
        audio) DOWNLOAD_VIDEO=0; DOWNLOAD_AUDIO=1 ;;
        *) printf 'Unknown --only target %q. Expected video or audio.\n' "$2" >&2; exit 2 ;;
      esac
      shift 2
      ;;
    --with-long-form) WITH_LONG_FORM=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option %q.\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

# huggingface_hub 1.0 renamed huggingface-cli to hf and dropped
# --local-dir-use-symlinks, so the flag is decided by which binary is found.
HF_BIN=""
LEGACY_CLI=0
for candidate in "$ROOT_DIR/venv/bin/hf" "$ROOT_DIR/venv/bin/huggingface-cli" hf huggingface-cli; do
  if command -v "$candidate" >/dev/null 2>&1; then
    HF_BIN="$candidate"
    case "$candidate" in
      *huggingface-cli) LEGACY_CLI=1 ;;
    esac
    break
  fi
done

if [[ -z "$HF_BIN" ]]; then
  # printf rather than a heredoc so the diagnosis still prints when PATH is bare.
  printf 'No Hugging Face CLI found. Install it into the project venv first:\n\n' >&2
  printf '  ./venv/bin/pip install "huggingface_hub[cli]"\n\n' >&2
  printf 'Then re-run this script.\n' >&2
  exit 3
fi

required_mb=0
if [[ "$DOWNLOAD_VIDEO" == "1" ]]; then
  required_mb=$((required_mb + VIDEO_SIZE_MB))
fi
if [[ "$DOWNLOAD_AUDIO" == "1" ]]; then
  required_mb=$((required_mb + AUDIO_SIZE_MB))
  if [[ "$WITH_LONG_FORM" == "1" ]]; then
    required_mb=$((required_mb + LONG_FORM_SIZE_MB))
  fi
fi
# Downloads land in a staging directory next to the target before being moved
# into place, so ask for headroom on top of the payload itself.
required_with_headroom_mb=$((required_mb + required_mb / 10))

mkdir -p ./models/video ./models/audio
available_mb=$(df -Pk ./models | awk 'NR == 2 { print int($4 / 1024) }')
printf 'Hugging Face CLI: %s\n' "$HF_BIN"
printf 'Estimated download: %s MiB (recommended free space %s MiB)\n' \
  "$required_mb" "$required_with_headroom_mb"
printf 'Free space on %s: %s MiB\n' "$ROOT_DIR/models" "$available_mb"

if [[ "$available_mb" -lt "$required_with_headroom_mb" ]]; then
  printf 'Not enough free disk space. Free up %s MiB and re-run.\n' \
    "$((required_with_headroom_mb - available_mb))" >&2
  exit 4
fi

run_download() {
  local repo="$1"
  local target="$2"
  shift 2

  local command=("$HF_BIN" download "$repo" --local-dir "$target" "$@")
  if [[ "$LEGACY_CLI" == "1" ]]; then
    command+=(--local-dir-use-symlinks False)
  fi

  printf '\n==> %s\n' "${command[*]}"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "${command[@]}"
}

if [[ "$DOWNLOAD_VIDEO" == "1" ]]; then
  run_download "$VIDEO_REPO" "$VIDEO_DIR"
fi

if [[ "$DOWNLOAD_AUDIO" == "1" ]]; then
  # pytorch_model.bin duplicates model.safetensors, and the AudioCraft state
  # dicts are only read by the optional long-form runtime.
  audio_excludes=(--exclude "pytorch_model.bin")
  if [[ "$WITH_LONG_FORM" != "1" ]]; then
    audio_excludes+=(--exclude "state_dict.bin" --exclude "compression_state_dict.bin")
  fi
  run_download "$AUDIO_REPO" "$AUDIO_DIR" "${audio_excludes[@]}"

  if [[ "$WITH_LONG_FORM" == "1" ]]; then
    run_download "$T5_REPO" "$LONG_FORM_DIR/t5-base" \
      --include "config.json" \
      --include "generation_config.json" \
      --include "model.safetensors" \
      --include "spiece.model" \
      --include "tokenizer.json"

    printf '\n==> staging AudioCraft checkpoints in %s\n' "$LONG_FORM_DIR"
    if [[ "$DRY_RUN" != "1" ]]; then
      mkdir -p "$LONG_FORM_DIR"
      cp "$AUDIO_DIR/state_dict.bin" "$LONG_FORM_DIR/state_dict.bin"
      cp "$AUDIO_DIR/compression_state_dict.bin" "$LONG_FORM_DIR/compression_state_dict.bin"
    fi
    cat <<'LONGFORM'

musicgen-long-form also needs the AudioCraft package, which this script does
not install because it changes the venv:

  ./venv/bin/pip install --no-deps audiocraft==1.3.0
  ./venv/bin/pip install av soundfile einops flashy hydra-core hydra-colorlog \
    julius num2words "spacy>=3.6.1" librosa torchmetrics encodec demucs
LONGFORM
  fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
  printf '\nDry run complete. Nothing was downloaded.\n'
  exit 0
fi

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/venv/bin/python"
else
  PYTHON_BIN="python3"
fi

printf '\n==> verifying readiness\n'
"$PYTHON_BIN" scripts/check_local_setup.py
