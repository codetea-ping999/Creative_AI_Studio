#!/usr/bin/env python3
"""Validate the local Creative AI Studio development setup."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This module must stay importable by a bare system ``python3`` before the
# repository venv exists: ``setup.sh`` runs ``--toolchain-only`` first so an
# old interpreter is rejected with a clear message.  Keep repository imports
# lazy and stdlib-only.

# --- Toolchain version policy -------------------------------------------------
# Canonical versions live only in ``.python-version`` / ``.nvmrc``.  The floors
# below are the single place for the oldest still-supported versions.
PYTHON_VERSION_FILE = ROOT / ".python-version"
NODE_VERSION_FILE = ROOT / ".nvmrc"
MIN_PYTHON_VERSION = (3, 10)
MIN_NODE_20_VERSION = (20, 19, 0)
MIN_NODE_22_VERSION = (22, 12, 0)

CANONICAL = "canonical"
SUPPORTED = "supported"
UNTESTED = "untested"
UNSUPPORTED = "unsupported"
VERSION_TEXT_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")


class ToolchainClassification(NamedTuple):
    """How one installed toolchain version relates to the repository policy.

    A ``NamedTuple`` (not a dataclass) so the module also works when loaded
    from a file path without being registered in ``sys.modules``.
    """

    tool: str
    version: tuple[int, int, int]
    status: str
    canonical: str
    requirement: str

    @property
    def is_blocking(self) -> bool:
        return self.status == UNSUPPORTED

    @property
    def level(self) -> str:
        """``OK`` for canonical, ``FAIL`` for unsupported, otherwise ``WARN``."""

        if self.status == CANONICAL:
            return "OK"
        return "FAIL" if self.is_blocking else "WARN"

    @property
    def summary(self) -> str:
        if self.status == CANONICAL:
            return f"canonical {self.canonical}"
        if self.status == SUPPORTED:
            return f"supported for migration, not canonical; canonical is {self.canonical}"
        if self.status == UNTESTED:
            return f"newer than canonical {self.canonical}; untested"
        return f"unsupported; requires {self.requirement}, canonical is {self.canonical}"


def _format_version(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def parse_version_text(text: str) -> tuple[int, int, int] | None:
    """Extract ``major.minor[.patch]`` from ``Python X.Y.Z`` / ``vX.Y.Z`` style text."""

    match = VERSION_TEXT_RE.search(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def read_canonical_version(path: Path) -> tuple[int, ...]:
    """Read a numeric version pin such as ``X.Y`` or ``X`` (optional ``v`` prefix)."""

    text = path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text)
    if match is None:
        raise ValueError(f"{path.name} must contain a numeric version, got {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def canonical_python_version() -> tuple[int, ...]:
    """Canonical ``(major, minor)`` read from ``.python-version``."""

    return read_canonical_version(PYTHON_VERSION_FILE)[:2]


def canonical_node_version() -> tuple[int, ...]:
    """Canonical ``(major,)`` read from ``.nvmrc``."""

    return read_canonical_version(NODE_VERSION_FILE)[:1]


def python_requirement() -> str:
    return f"Python {_format_version(MIN_PYTHON_VERSION)}+"


def node_requirement() -> str:
    return (
        f"Node.js {_format_version(MIN_NODE_20_VERSION[:2])}+ "
        f"or {_format_version(MIN_NODE_22_VERSION[:2])}+"
    )


def meets_python_floor(version: Sequence[int]) -> bool:
    return tuple(version[:3]) >= (*MIN_PYTHON_VERSION, 0)


def meets_node_floor(version: Sequence[int]) -> bool:
    current = tuple(version[:3])
    on_node_20_line = current[0] == MIN_NODE_20_VERSION[0] and current >= MIN_NODE_20_VERSION
    return on_node_20_line or current >= MIN_NODE_22_VERSION


def _pad(version: Sequence[int]) -> tuple[int, int, int]:
    padded = (tuple(version) + (0, 0, 0))[:3]
    return padded[0], padded[1], padded[2]


def classify_python_version(
    version: Sequence[int],
    canonical: Sequence[int] | None = None,
) -> ToolchainClassification:
    """Classify Python as canonical (OK), supported/untested (WARN) or unsupported (FAIL)."""

    current = _pad(version)
    pinned = tuple(canonical if canonical is not None else canonical_python_version())[:2]
    if not meets_python_floor(current):
        status = UNSUPPORTED
    elif current[:2] == pinned:
        status = CANONICAL
    elif current[:2] > pinned:
        status = UNTESTED
    else:
        status = SUPPORTED
    return ToolchainClassification(
        "Python", current, status, f"Python {_format_version(pinned)}", python_requirement()
    )


def classify_node_version(
    version: Sequence[int],
    canonical: Sequence[int] | None = None,
) -> ToolchainClassification:
    """Classify Node.js as canonical (OK), supported/untested (WARN) or unsupported (FAIL)."""

    current = _pad(version)
    pinned = tuple(canonical if canonical is not None else canonical_node_version())[:1]
    if not meets_node_floor(current):
        status = UNSUPPORTED
    elif current[:1] == pinned:
        status = CANONICAL
    elif current[:1] > pinned:
        status = UNTESTED
    else:
        status = SUPPORTED
    return ToolchainClassification(
        "Node.js", current, status, f"Node.js {_format_version(pinned)}.x", node_requirement()
    )


def _report_toolchain(classification: ToolchainClassification, version_text: str) -> bool:
    print(
        f"[{classification.level}] {classification.tool} version: {version_text} "
        f"({classification.summary})"
    )
    return not classification.is_blocking


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _environment_path(name: str, default: str) -> Path:
    return _resolve_path(os.getenv(name, default))


def _models_root() -> Path:
    return _environment_path("MODELS_ROOT", "models")


def _manifest_root() -> Path:
    configured = os.getenv("MODELS_MANIFEST_ROOT")
    return _resolve_path(configured) if configured else _models_root() / "manifests"


def _check_python_version(version_info: Sequence[int] | None = None) -> bool:
    """Report the Python policy level; only an unsupported version blocks."""

    if version_info is None:
        version_info = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    version = tuple(version_info[:3])
    return _report_toolchain(classify_python_version(version), _format_version(version))


def _check_node_version(version_text: str) -> bool:
    """Report the Node.js policy level; only an unsupported version blocks."""

    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version_text.strip())
    if match is None:
        print(f"[FAIL] Node.js version could not be parsed: {version_text!r}")
        return False
    version = tuple(int(part) for part in match.groups())
    return _report_toolchain(classify_node_version(version), version_text.strip())


def _check_node_runtime() -> bool:
    try:
        completed = subprocess.run(
            ["node", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(f"[FAIL] Node.js executable was not found (required: {node_requirement()})")
        return False
    if completed.returncode != 0:
        print(f"[FAIL] Node.js version check failed: {completed.stderr.strip()}")
        return False
    return _check_node_version(completed.stdout)


def check_toolchain(
    python_version_text: str | None = None,
    node_version_text: str | None = None,
) -> bool:
    """Check only the Python/Node.js policy; ``None`` inspects the live toolchain."""

    if python_version_text is None:
        python_ok = _check_python_version()
    else:
        version = parse_version_text(python_version_text)
        if version is None:
            print(f"[FAIL] Python version could not be parsed: {python_version_text!r}")
            python_ok = False
        else:
            python_ok = _check_python_version(version)
    node_ok = (
        _check_node_runtime() if node_version_text is None
        else _check_node_version(node_version_text)
    )
    return python_ok and node_ok


def _check_path(path: Path, label: str, *, required: bool = True) -> bool:
    exists = path.exists()
    status = "OK" if exists else ("WARN" if not required else "FAIL")
    print(f"[{status}] {label}: {path}")
    return exists or not required


def _check_manifest_files(manifest_root: Path | None = None) -> bool:
    manifest_root = manifest_root or _manifest_root()
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

    from core.model_readiness import (
        STATUS_SCAFFOLD,
        evaluate_manifest_payload,
        resolve_repo_path,
    )

    manifest_root = manifest_root or _manifest_root()
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the local development setup.")
    parser.add_argument(
        "--skip-runtime-files", action="store_true", help="do not inspect model runtime files"
    )
    parser.add_argument(
        "--toolchain-only",
        action="store_true",
        help="only classify Python/Node.js against .python-version and .nvmrc",
    )
    parser.add_argument(
        "--python-version",
        help="classify this Python version text instead of the running interpreter",
    )
    parser.add_argument(
        "--node-version",
        help="classify this Node.js version text instead of running `node --version`",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.toolchain_only:
        if check_toolchain(args.python_version, args.node_version):
            return 0
        print("[FAIL] Toolchain versions are unsupported. Review the lines above.")
        return 1

    print("Creative AI Studio local setup check")
    print(f"Repository: {ROOT}")

    db_path = _environment_path("DB_PATH", "data/jobs.db")
    output_root = _environment_path("OUTPUT_DIR", "outputs")
    image_output_dir = _environment_path(
        "OUTPUT_IMAGE_DIR",
        str(output_root / "images"),
    )
    audio_output_dir = _environment_path(
        "OUTPUT_AUDIO_DIR",
        str(output_root / "audio"),
    )
    video_output_dir = _environment_path(
        "OUTPUT_VIDEO_DIR",
        str(output_root / "videos"),
    )
    checks = [
        _check_path(Path(sys.executable), "Python executable"),
        check_toolchain(args.python_version, args.node_version),
        _check_path(ROOT / "venv", "Python virtual environment", required=False),
        _check_path(ROOT / ".env", "Root environment file", required=False),
        _check_path(ROOT / "apps" / "web" / ".env", "Web environment file", required=False),
        _check_path(db_path.parent, "Data directory"),
        _check_path(db_path.parent / "projects", "Project data directory", required=False),
        _check_path(db_path.parent / "feedback", "Feedback data directory", required=False),
        _check_path(image_output_dir, "Image output directory"),
        _check_path(audio_output_dir, "Audio output directory", required=False),
        _check_path(video_output_dir, "Video output directory", required=False),
        _check_path(ROOT / "apps" / "web" / "node_modules", "Web dependencies", required=False),
    ]
    checks.append(_check_manifest_files())
    if args.skip_runtime_files:
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
