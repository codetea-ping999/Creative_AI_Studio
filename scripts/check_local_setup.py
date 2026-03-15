#!/usr/bin/env python3
"""Validate the local Creative AI Studio development setup."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_RUNTIME_FILES = "--skip-runtime-files" in sys.argv


def _check_path(path: Path, label: str, *, required: bool = True) -> bool:
    exists = path.exists()
    status = "OK" if exists else ("WARN" if not required else "FAIL")
    print(f"[{status}] {label}: {path}")
    return exists or not required


def _check_manifest_files() -> bool:
    manifest_root = ROOT / "models" / "manifests"
    if not _check_path(manifest_root, "Model manifest root"):
        return False

    manifest_files = sorted(manifest_root.rglob("*.json"))
    if not manifest_files:
        print(f"[FAIL] No manifest files found under: {manifest_root}")
        return False

    print(f"[OK] Found {len(manifest_files)} manifest file(s)")
    success = True
    for manifest_path in manifest_files:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive CLI path
            print(f"[FAIL] Invalid JSON: {manifest_path} ({exc})")
            success = False
            continue

        manifest_id = payload.get("id", "<missing id>")
        public_id = payload.get("public_id", manifest_id)
        print(f"[OK] Manifest: {manifest_path.relative_to(ROOT)} id={manifest_id} public_id={public_id}")
    return success


def _check_sdxl_runtime_files() -> bool:
    model_root = ROOT / "models" / "image" / "sdxl"
    if not model_root.exists():
        print(f"[WARN] SDXL model directory is missing: {model_root}")
        return True

    required_files = [
        model_root / "model_index.json",
        model_root / "text_encoder" / "model.fp16.safetensors",
        model_root / "text_encoder_2" / "model.fp16.safetensors",
        model_root / "unet" / "diffusion_pytorch_model.fp16.safetensors",
        model_root / "vae" / "diffusion_pytorch_model.fp16.safetensors",
    ]
    success = True
    for path in required_files:
        if not _check_path(path, f"Runtime file {path.relative_to(ROOT)}"):
            success = False
    return success


def main() -> int:
    print("Creative AI Studio local setup check")
    print(f"Repository: {ROOT}")

    checks = [
        _check_path(Path(sys.executable), "Python executable"),
        _check_path(ROOT / "venv", "Python virtual environment", required=False),
        _check_path(ROOT / ".env", "Root environment file", required=False),
        _check_path(ROOT / "apps" / "web" / ".env", "Web environment file", required=False),
        _check_path(ROOT / "data", "Data directory"),
        _check_path(ROOT / "data" / "projects", "Project data directory", required=False),
        _check_path(ROOT / "data" / "feedback", "Feedback data directory", required=False),
        _check_path(ROOT / "outputs" / "images", "Image output directory"),
        _check_path(ROOT / "outputs" / "audio", "Audio output directory", required=False),
        _check_path(ROOT / "outputs" / "videos", "Video output directory", required=False),
        _check_path(ROOT / "apps" / "web" / "node_modules", "Web dependencies", required=False),
    ]
    checks.append(_check_manifest_files())
    if SKIP_RUNTIME_FILES:
        print("[WARN] Runtime file checks skipped by --skip-runtime-files")
    else:
        checks.append(_check_sdxl_runtime_files())

    if all(checks):
        print("[OK] Local setup looks ready.")
        return 0

    print("[FAIL] Local setup is incomplete. Review the lines above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
