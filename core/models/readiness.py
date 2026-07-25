"""Shared readiness rules for locally installed model weights.

`GET /models`, `scripts/check_local_setup.py`, `scripts/smoke_cogvideox.py`,
the CogVideoX adapter, and the runtime loaders all answer the same question:
are the files a loader will actually open present on disk? Keeping the rules
in one module stops `/models` from advertising a model as available when only
its `model_index.json` or `config.json` is checked into the repository.

Requirements are derived from the pipeline's own `model_index.json` rather
than hard-coded per model, so a new Diffusers pipeline is validated without
touching this module.

This module depends on the standard library only, so scripts can import it
without pulling in the FastAPI/pydantic stack.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_REPO_ROOT = Path(__file__).resolve().parents[2]

STATUS_READY = "ready"
STATUS_MISSING_FILES = "missing_files"
STATUS_SCAFFOLD = "scaffold"

#: File patterns accepted as "real weights" for a component directory.
WEIGHT_PATTERNS: tuple[str, ...] = ("*.safetensors", "*.bin")

#: Components that ship configuration only and never carry weight files.
_CONFIG_ONLY_COMPONENTS: frozenset[str] = frozenset({"scheduler"})
_CONFIG_ONLY_PREFIXES: tuple[str, ...] = ("tokenizer", "feature_extractor", "image_processor")


class ManifestLike(Protocol):
    """Structural view of the manifest fields readiness rules depend on."""

    runtime: str
    local_path: str | None
    default_params: Mapping[str, Any]


@dataclass(frozen=True)
class ModelReadiness:
    """Whether a manifest's local files satisfy its runtime requirements."""

    status: str
    message: str
    missing: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.status == STATUS_READY


def resolve_repo_path(path_value: str, *, repo_root: Path | None = None) -> Path:
    """Resolve a manifest path relative to the repository root."""

    candidate = Path(path_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return ((repo_root or _REPO_ROOT) / candidate).resolve()


def evaluate_manifest_readiness(
    manifest: ManifestLike,
    *,
    repo_root: Path | None = None,
) -> ModelReadiness:
    """Evaluate readiness for a parsed :class:`ModelManifest`."""

    return evaluate_readiness(
        runtime=manifest.runtime,
        local_path=manifest.local_path,
        default_params=manifest.default_params,
        repo_root=repo_root,
    )


def evaluate_manifest_payload(
    payload: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
) -> ModelReadiness:
    """Evaluate readiness for a raw manifest JSON payload."""

    local_path = payload.get("local_path")
    default_params = payload.get("default_params")
    return evaluate_readiness(
        runtime=str(payload.get("runtime") or ""),
        local_path=local_path if isinstance(local_path, str) else None,
        default_params=default_params if isinstance(default_params, Mapping) else None,
        repo_root=repo_root,
    )


def evaluate_readiness(
    *,
    runtime: str,
    local_path: str | None,
    default_params: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> ModelReadiness:
    """Evaluate readiness from the manifest fields that describe local files."""

    params = dict(default_params or {})
    if not local_path:
        return ModelReadiness(
            STATUS_MISSING_FILES,
            "Manifest does not define a local model path.",
        )

    model_root = resolve_repo_path(local_path, repo_root=repo_root)
    if not model_root.exists():
        return ModelReadiness(
            STATUS_MISSING_FILES,
            f"Local model path is missing: {local_path}",
            (local_path,),
        )

    if runtime == "diffusers":
        return diffusers_pipeline_readiness(model_root)
    if runtime == "transformers":
        return transformers_model_readiness(model_root)
    if runtime == "learned":
        return _learned_runtime_readiness(model_root, params, repo_root=repo_root)
    return ModelReadiness(STATUS_READY, "Local runtime files are ready.")


def diffusers_pipeline_readiness(
    pipeline_path: Path,
    *,
    label: str = "Diffusers",
) -> ModelReadiness:
    """Check a Diffusers pipeline directory against its own `model_index.json`."""

    missing = missing_diffusers_files(pipeline_path)
    if missing:
        return ModelReadiness(
            STATUS_MISSING_FILES,
            f"{label} model files are missing: " + ", ".join(missing),
            tuple(missing),
        )
    return ModelReadiness(STATUS_READY, f"{label} model files are ready.")


def transformers_model_readiness(
    model_path: Path,
    *,
    label: str = "Transformers",
) -> ModelReadiness:
    """Check a Transformers checkpoint directory for its config and weights."""

    missing = missing_transformers_files(model_path)
    if missing:
        return ModelReadiness(
            STATUS_MISSING_FILES,
            f"{label} model files are missing: " + ", ".join(missing),
            tuple(missing),
        )
    return ModelReadiness(STATUS_READY, f"{label} model files are ready.")


def missing_diffusers_files(pipeline_path: Path) -> list[str]:
    """Return pipeline-relative paths that a Diffusers load would need."""

    index_payload = _read_model_index(pipeline_path)
    if index_payload is None:
        return ["model_index.json"]

    missing: list[str] = []
    for component, spec in sorted(index_payload.items()):
        if component.startswith("_") or not _is_component_spec(spec):
            continue
        component_root = pipeline_path / component
        config_name = _component_config_name(component)
        if not (component_root / config_name).exists():
            missing.append(f"{component}/{config_name}")
        if _component_needs_weights(component) and not _has_weight_file(component_root):
            missing.append(f"{component}/{WEIGHT_PATTERNS[0]}")
    return missing


def missing_transformers_files(model_path: Path) -> list[str]:
    """Return model-relative paths that a Transformers load would need."""

    missing: list[str] = []
    if not (model_path / "config.json").exists():
        missing.append("config.json")
    if not _has_weight_file(model_path):
        missing.append(WEIGHT_PATTERNS[0])
    return missing


def _learned_runtime_readiness(
    adapter_root: Path,
    params: Mapping[str, Any],
    *,
    repo_root: Path | None,
) -> ModelReadiness:
    if params.get("runtime_status") == "scaffold":
        return ModelReadiness(STATUS_SCAFFOLD, "Learned runtime adapter is still a scaffold.")

    entrypoint_name = str(params.get("entrypoint") or "runtime.py")
    if not (adapter_root / entrypoint_name).exists():
        return ModelReadiness(
            STATUS_MISSING_FILES,
            f"Learned runtime entrypoint is missing: {entrypoint_name}",
            (entrypoint_name,),
        )

    pipeline_value = params.get("pipeline_path")
    if not isinstance(pipeline_value, str) or not pipeline_value.strip():
        return ModelReadiness(
            STATUS_MISSING_FILES,
            "Learned runtime pipeline_path is not configured.",
        )

    pipeline_path = resolve_repo_path(pipeline_value, repo_root=repo_root)
    missing = missing_diffusers_files(pipeline_path)
    if missing:
        return ModelReadiness(
            STATUS_MISSING_FILES,
            f"Learned runtime weights are incomplete under {pipeline_value}: "
            + ", ".join(missing),
            tuple(missing),
        )
    return ModelReadiness(
        STATUS_READY,
        "Learned runtime adapter and local model files are ready.",
    )


def _read_model_index(pipeline_path: Path) -> dict[str, Any] | None:
    index_path = pipeline_path / "model_index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_component_spec(spec: Any) -> bool:
    # Diffusers records each component as [library, class]; optional slots that
    # were saved as [null, null] carry no files.
    return (
        isinstance(spec, (list, tuple))
        and len(spec) == 2
        and all(isinstance(entry, str) and entry for entry in spec)
    )


def _component_config_name(component: str) -> str:
    if component in _CONFIG_ONLY_COMPONENTS:
        return "scheduler_config.json"
    if component.startswith("tokenizer"):
        return "tokenizer_config.json"
    if component.startswith(("feature_extractor", "image_processor")) or component.endswith(
        "processor"
    ):
        return "preprocessor_config.json"
    return "config.json"


def _component_needs_weights(component: str) -> bool:
    if component in _CONFIG_ONLY_COMPONENTS:
        return False
    if component.startswith(_CONFIG_ONLY_PREFIXES) or component.endswith("processor"):
        return False
    return True


def _has_weight_file(component_root: Path) -> bool:
    return any(
        any(component_root.glob(pattern)) for pattern in WEIGHT_PATTERNS
    )


__all__ = [
    "STATUS_MISSING_FILES",
    "STATUS_READY",
    "STATUS_SCAFFOLD",
    "WEIGHT_PATTERNS",
    "ManifestLike",
    "ModelReadiness",
    "diffusers_pipeline_readiness",
    "evaluate_manifest_payload",
    "evaluate_manifest_readiness",
    "evaluate_readiness",
    "missing_diffusers_files",
    "missing_transformers_files",
    "resolve_repo_path",
    "transformers_model_readiness",
]
