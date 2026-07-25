#!/usr/bin/env python3
"""Validate the local Creative AI Studio development setup."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.models.readiness import (  # noqa: E402  (path bootstrap above)
    STATUS_SCAFFOLD,
    evaluate_manifest_payload,
    resolve_repo_path,
)

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


def _check_runtime_model_files(manifest_root: Path | None = None) -> bool:
    """Report runtime file readiness with the same rules `GET /models` uses."""

    manifest_root = manifest_root or ROOT / "models" / "manifests"
    success = True
    for manifest_path in sorted(manifest_root.rglob("*.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - reported by _check_manifest_files
            continue
        if not isinstance(payload, dict) or payload.get("enabled") is False:
            continue

        label = f"{payload.get('display_name', payload.get('id', '<unknown>'))}"
        local_path = payload.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            print(f"[WARN] {label}: manifest has no local_path")
            continue

        model_root = resolve_repo_path(local_path, repo_root=ROOT)
        if not model_root.exists():
            print(f"[WARN] {label}: model directory is missing: {local_path}")
            continue

        readiness = evaluate_manifest_payload(payload, repo_root=ROOT)
        if readiness.is_ready:
            print(f"[OK] {label}: runtime files are ready ({local_path})")
            continue
        if readiness.status == STATUS_SCAFFOLD or not _is_required_model(payload):
            print(f"[WARN] {label}: {readiness.message}")
            continue
        print(f"[FAIL] {label}: {readiness.message}")
        success = False
    return success


def _is_required_model(payload: dict[str, Any]) -> bool:
    """Only default production models block local setup validation."""

    if not payload.get("is_default"):
        return False
    if payload.get("runtime") == "learned":
        return False
    tags = payload.get("tags", [])
    return not (isinstance(tags, list) and "experimental" in tags)


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
        checks.append(_check_runtime_model_files())

    if all(checks):
        print("[OK] Local setup looks ready.")
        return 0

    print("[FAIL] Local setup is incomplete. Review the lines above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
