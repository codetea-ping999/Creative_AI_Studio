#!/usr/bin/env python3
"""Validate the local Creative AI Studio development setup."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SKIP_RUNTIME_FILES = "--skip-runtime-files" in sys.argv


def _check_path(path: Path, label: str, *, required: bool = True) -> bool:
    exists = path.exists()
    status = "OK" if exists else ("WARN" if not required else "FAIL")
    print(f"[{status}] {label}: {path}")
    return exists or not required


def _check_manifest_files(manifest_root: Path | None = None) -> bool:
    manifest_root = manifest_root or ROOT / "models" / "manifests"
    if not _check_path(manifest_root, "Model manifest root"):
        return False

    manifest_files = sorted(manifest_root.rglob("*.json"))
    if not manifest_files:
        print(f"[FAIL] No manifest files found under: {manifest_root}")
        return False

    print(f"[OK] Found {len(manifest_files)} manifest file(s)")
    success = True
    manifest_ids: dict[str, Path] = {}
    public_identifiers: dict[str, Path] = {}
    for manifest_path in manifest_files:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive CLI path
            print(f"[FAIL] Invalid JSON: {manifest_path} ({exc})")
            success = False
            continue

        manifest_id = payload.get("id", "<missing id>")
        public_id = payload.get("public_id") or manifest_id
        relative_path = _relative_to_root(manifest_path)
        print(f"[OK] Manifest: {relative_path} id={manifest_id} public_id={public_id}")

        if not isinstance(manifest_id, str) or not manifest_id:
            print(f"[FAIL] Manifest is missing a valid id: {relative_path}")
            success = False
        elif manifest_id in manifest_ids:
            print(
                "[FAIL] Duplicate manifest id "
                f"{manifest_id!r}: {_relative_to_root(manifest_ids[manifest_id])} vs {relative_path}"
            )
            success = False
        else:
            manifest_ids[manifest_id] = manifest_path

        identifiers = _public_identifiers(payload, fallback_id=manifest_id)
        for identifier in identifiers:
            existing_path = public_identifiers.get(identifier)
            if existing_path is not None:
                print(
                    "[FAIL] Duplicate public model id or alias "
                    f"{identifier!r}: {_relative_to_root(existing_path)} vs {relative_path}"
                )
                success = False
                continue
            public_identifiers[identifier] = manifest_path
    return success


def _public_identifiers(payload: dict[str, Any], *, fallback_id: object) -> list[str]:
    identifiers: list[str] = []
    public_id = payload.get("public_id") or fallback_id
    if isinstance(public_id, str) and public_id:
        identifiers.append(public_id)
    aliases = payload.get("aliases", [])
    if isinstance(aliases, list):
        identifiers.extend(alias for alias in aliases if isinstance(alias, str) and alias)
    return identifiers


def _relative_to_root(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


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


def _check_cogvideox_runtime_files() -> bool:
    model_root = ROOT / "models" / "video" / "cogvideox-2b"
    if not model_root.exists():
        print(f"[WARN] Optional CogVideoX-2B model directory is missing: {model_root}")
        return True
    component_configs = [
        model_root / "scheduler" / "scheduler_config.json",
        model_root / "text_encoder" / "config.json",
        model_root / "tokenizer" / "tokenizer_config.json",
        model_root / "transformer" / "config.json",
        model_root / "vae" / "config.json",
    ]
    if not (model_root / "model_index.json").exists():
        print(f"[WARN] Optional CogVideoX model_index.json is missing: {model_root}")
        return True
    missing_configs = [path for path in component_configs if not path.exists()]
    missing_weights = [
        component
        for component in ("text_encoder", "transformer", "vae")
        if not any((model_root / component).glob("*.safetensors"))
    ]
    if missing_configs or missing_weights:
        details = [str(path.relative_to(ROOT)) for path in missing_configs]
        details.extend(f"models/video/cogvideox-2b/{name}/*.safetensors" for name in missing_weights)
        print("[WARN] Optional CogVideoX-2B weights are incomplete: " + ", ".join(details))
        return True
    success = all(
        _check_path(path, f"Runtime file {path.relative_to(ROOT)}")
        for path in [model_root / "model_index.json", *component_configs]
    )
    for component in ("text_encoder", "transformer", "vae"):
        component_root = model_root / component
        if not any(component_root.glob("*.safetensors")):
            print(f"[FAIL] Runtime weights missing: {component_root.relative_to(ROOT)}/*.safetensors")
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
        checks.append(_check_cogvideox_runtime_files())

    if all(checks):
        print("[OK] Local setup looks ready.")
        return 0

    print("[FAIL] Local setup is incomplete. Review the lines above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
