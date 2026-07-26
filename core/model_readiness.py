"""Shared readiness rules for locally installed model weights and processor assets.

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

import importlib.util
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_REPO_ROOT = Path(__file__).resolve().parents[1]

STATUS_READY = "ready"
STATUS_CONFIGURED = "configured"
STATUS_MISSING_FILES = "missing_files"
STATUS_INVALID_CONFIGURATION = "invalid_configuration"
STATUS_SCAFFOLD = "scaffold"

#: File patterns accepted as "real weights" for a component directory.
WEIGHT_PATTERNS: tuple[str, ...] = ("*.safetensors", "*.bin")
_WEIGHT_INDEX_PATTERNS: tuple[str, ...] = (
    "*.safetensors.index.json",
    "*.bin.index.json",
)
_SHARD_NAME_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<part>\d{5})-of-(?P<total>\d{5})"
    r"(?P<suffix>\.(?:safetensors|bin))$"
)

#: Components that ship configuration only and never carry weight files.
_CONFIG_ONLY_COMPONENTS: frozenset[str] = frozenset({"scheduler"})
_CONFIG_ONLY_PREFIXES: tuple[str, ...] = ("tokenizer", "feature_extractor", "image_processor")
_TRANSFORMERS_PROCESSOR_CONFIGS: tuple[str, ...] = (
    "preprocessor_config.json",
    "tokenizer_config.json",
)
_AUDIOCRAFT_MODEL_FILES: tuple[str, ...] = (
    "state_dict.bin",
    "compression_state_dict.bin",
)
_AUDIOCRAFT_T5_FILES: tuple[str, ...] = (
    "config.json",
    "model.safetensors",
    "spiece.model",
)


class ManifestLike(Protocol):
    """Structural view of the manifest fields readiness rules depend on."""

    runtime: str
    local_path: str | None
    remote_ref: str | None
    default_params: Mapping[str, Any]


@dataclass(frozen=True)
class ModelReadiness:
    """Whether a manifest's local files satisfy its runtime requirements."""

    status: str
    message: str
    missing: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.status in {STATUS_READY, STATUS_CONFIGURED}


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
        remote_ref=getattr(manifest, "remote_ref", None),
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
    remote_ref = payload.get("remote_ref")
    default_params = payload.get("default_params")
    return evaluate_readiness(
        runtime=str(payload.get("runtime") or ""),
        local_path=local_path if isinstance(local_path, str) else None,
        remote_ref=remote_ref if isinstance(remote_ref, str) else None,
        default_params=default_params if isinstance(default_params, Mapping) else None,
        repo_root=repo_root,
    )


def evaluate_readiness(
    *,
    runtime: str,
    local_path: str | None,
    remote_ref: str | None = None,
    default_params: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> ModelReadiness:
    """Evaluate readiness from the manifest fields that describe local files."""

    params = dict(default_params or {})
    if runtime == "voicevox_http":
        return _voicevox_readiness(remote_ref)
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
    if runtime == "audiocraft":
        return audiocraft_model_readiness(model_root)
    if runtime == "learned":
        return _learned_runtime_readiness(model_root, params, repo_root=repo_root)
    return ModelReadiness(STATUS_READY, "Local runtime files are ready.")


def _voicevox_readiness(remote_ref: str | None) -> ModelReadiness:
    """Report endpoint configuration without probing the service or leaking its path."""

    import os

    from core.models.audio_runtimes import audio_endpoint_origin

    configured_base_url = os.getenv("VOICEVOX_BASE_URL", "").strip()
    base_url = configured_base_url or remote_ref
    if not base_url:
        return ModelReadiness(
            STATUS_INVALID_CONFIGURATION,
            "VOICEVOX endpoint URL is not configured.",
        )
    try:
        origin = audio_endpoint_origin(base_url)
    except ValueError:
        return ModelReadiness(
            STATUS_INVALID_CONFIGURATION,
            "VOICEVOX endpoint configuration is invalid or disallowed.",
        )
    return ModelReadiness(
        STATUS_CONFIGURED,
        f"VOICEVOX endpoint is configured at {origin}. Availability is checked "
        "when generation starts; an engine that is not running will fail that job.",
    )


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


def audiocraft_model_readiness(
    model_path: Path,
    *,
    label: str = "AudioCraft",
) -> ModelReadiness:
    """Check the optional AudioCraft dependency and local exported checkpoint."""

    if importlib.util.find_spec("audiocraft") is None:
        return ModelReadiness(
            STATUS_MISSING_FILES,
            "AudioCraft dependency is not installed. "
            "Install the optional AudioCraft 1.3 runtime described in "
            "docs/model-download-guide.md.",
            ("python:audiocraft",),
        )

    missing = [
        name for name in _AUDIOCRAFT_MODEL_FILES if not (model_path / name).is_file()
    ]
    t5_root = model_path / "t5-base"
    missing.extend(
        f"t5-base/{name}"
        for name in _AUDIOCRAFT_T5_FILES
        if not (t5_root / name).is_file()
    )
    if missing:
        return ModelReadiness(
            STATUS_MISSING_FILES,
            f"{label} model files are missing: " + ", ".join(missing),
            tuple(missing),
        )
    return ModelReadiness(
        STATUS_READY,
        f"{label} dependency, checkpoint, and local T5 files are ready.",
    )


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
        if component.startswith("tokenizer"):
            missing.extend(
                f"{component}/{name}"
                for name in _missing_tokenizer_assets(component_root, class_name=spec[1])
            )
        if _component_needs_weights(component):
            missing.extend(
                f"{component}/{name}" for name in _missing_weight_files(component_root)
            )
    return missing


def missing_transformers_files(model_path: Path) -> list[str]:
    """Return model-relative paths that a Transformers load would need."""

    missing: list[str] = []
    if not (model_path / "config.json").exists():
        missing.append("config.json")
    missing.extend(_missing_weight_files(model_path))
    missing.extend(
        name
        for name in _TRANSFORMERS_PROCESSOR_CONFIGS
        if not (model_path / name).exists()
    )
    missing.extend(_missing_tokenizer_assets(model_path))
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


def _missing_weight_files(component_root: Path) -> list[str]:
    """Return missing files unless one complete local weight set is present."""

    index_missing: list[str] = []
    for index_path in sorted(
        path
        for pattern in _WEIGHT_INDEX_PATTERNS
        for path in component_root.glob(pattern)
    ):
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            weight_map = payload.get("weight_map") if isinstance(payload, dict) else None
        except (OSError, ValueError):
            weight_map = None
        if not isinstance(weight_map, Mapping) or not weight_map:
            index_missing.append(index_path.name)
            continue
        referenced_files = sorted(
            {
                filename
                for filename in weight_map.values()
                if isinstance(filename, str) and filename
            }
        )
        if not referenced_files:
            index_missing.append(index_path.name)
            continue
        missing = [
            filename for filename in referenced_files if not (component_root / filename).is_file()
        ]
        if not missing:
            return []
        index_missing.extend(missing)

    weight_files = sorted(
        path
        for pattern in WEIGHT_PATTERNS
        for path in component_root.glob(pattern)
        if path.is_file() and _is_candidate_weight_file(path)
    )
    shard_groups: dict[tuple[str, str, int], set[int]] = {}
    for path in weight_files:
        shard_match = _SHARD_NAME_RE.fullmatch(path.name)
        if shard_match is None:
            return []
        total = int(shard_match.group("total"))
        key = (
            shard_match.group("prefix"),
            shard_match.group("suffix"),
            total,
        )
        shard_groups.setdefault(key, set()).add(int(shard_match.group("part")))

    shard_missing: list[str] = []
    for (prefix, suffix, total), present_parts in sorted(shard_groups.items()):
        expected_parts = set(range(1, total + 1))
        if present_parts == expected_parts:
            return []
        shard_missing.extend(
            f"{prefix}-{part:05d}-of-{total:05d}{suffix}"
            for part in sorted(expected_parts - present_parts)
        )

    if index_missing:
        return sorted(set(index_missing))
    if shard_missing:
        return shard_missing
    return [WEIGHT_PATTERNS[0]]


def _is_candidate_weight_file(path: Path) -> bool:
    if path.suffix == ".safetensors":
        return True
    if path.suffix != ".bin":
        return False
    return path.name.startswith(("pytorch_model", "diffusion_pytorch_model", "model"))


def _missing_tokenizer_assets(
    tokenizer_root: Path,
    *,
    class_name: str | None = None,
) -> list[str]:
    """Require one vocabulary format that the configured tokenizer can load."""

    if (tokenizer_root / "tokenizer.json").is_file():
        return []

    normalized_class = (class_name or "").lower()
    if "cliptokenizer" in normalized_class:
        return [
            name
            for name in ("vocab.json", "merges.txt")
            if not (tokenizer_root / name).is_file()
        ]

    if "t5tokenizer" in normalized_class or "sentencepiece" in normalized_class:
        return [] if (tokenizer_root / "spiece.model").is_file() else ["spiece.model"]

    if any(
        (tokenizer_root / name).is_file()
        for name in ("spiece.model", "sentencepiece.bpe.model", "vocab.txt")
    ):
        return []
    if all((tokenizer_root / name).is_file() for name in ("vocab.json", "merges.txt")):
        return []
    return ["tokenizer.json|spiece.model|vocab.json+merges.txt|vocab.txt"]


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
