"""Place model files the user already downloaded into a manifest's directory.

The Studio can be pointed at a folder chosen in Finder / Explorer. The folder is
validated with the same readiness rules the loaders use (``core/model_readiness``)
*before* anything is copied into the models directory, then copied into a staging
directory next to the destination and moved into place, so a failed or interrupted
copy never leaves a half-written model where the loader looks.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.model_readiness import (
    ModelReadiness,
    evaluate_readiness,
    resolve_repo_path,
)

#: How many directory levels below the chosen folder are searched when the folder
#: itself is not a model (people often pick the parent of the model directory).
_SEARCH_DEPTH = 2
#: Free space kept in reserve on top of the model size.
_DISK_MARGIN_BYTES = 256 * 1024 * 1024

_IMPORT_LOCK = threading.Lock()


class ModelInstallError(Exception):
    """Base class for import failures that the API maps to an HTTP status."""

    status_code = 400
    code = "install_failed"


class InstallNotSupportedError(ModelInstallError):
    status_code = 422
    code = "not_supported"


class InvalidSourceError(ModelInstallError):
    status_code = 422
    code = "invalid_source"


class IncompleteSourceError(ModelInstallError):
    status_code = 422
    code = "incomplete_source"

    def __init__(self, message: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing


class DestinationNotEmptyError(ModelInstallError):
    status_code = 409
    code = "destination_not_empty"


class InsufficientSpaceError(ModelInstallError):
    status_code = 507
    code = "insufficient_space"


class InstallBusyError(ModelInstallError):
    status_code = 409
    code = "install_in_progress"


@dataclass(frozen=True)
class InstallResult:
    destination: Path
    source: Path
    #: Where the previous contents of the destination were moved, if ``replace`` set any aside.
    replaced_to: Path | None
    readiness: ModelReadiness
    copied_bytes: int


def install_destination(
    runtime: str,
    local_path: str | None,
    default_params: Mapping[str, Any] | None,
    *,
    repo_root: Path | None = None,
) -> Path | None:
    """Directory the loader reads model files from, or ``None`` for remote-only models."""

    params = default_params or {}
    value: Any = local_path
    if runtime == "learned":
        # ``local_path`` is the adapter that ships with the repo; the weights live
        # in ``pipeline_path``.
        value = params.get("pipeline_path")
    if not isinstance(value, str) or not value.strip():
        return None
    return resolve_repo_path(value, repo_root=repo_root)


def _readiness_for(
    *,
    runtime: str,
    local_path: str | None,
    remote_ref: str | None,
    default_params: Mapping[str, Any] | None,
    manifest_id: str | None,
    candidate: Path,
    repo_root: Path | None,
) -> ModelReadiness:
    """Readiness of the manifest as if ``candidate`` were its model directory."""

    params = dict(default_params or {})
    if runtime == "learned":
        params["pipeline_path"] = str(candidate)
        effective_local_path = local_path
    else:
        effective_local_path = str(candidate)
    return evaluate_readiness(
        runtime=runtime,
        local_path=effective_local_path,
        remote_ref=remote_ref,
        default_params=params,
        manifest_id=manifest_id,
        repo_root=repo_root,
    )


def _candidate_directories(source: Path) -> list[Path]:
    """``source`` first, then its sub-directories breadth-first (bounded depth)."""

    found = [source]
    frontier = [source]
    for _ in range(_SEARCH_DEPTH):
        next_frontier: list[Path] = []
        for directory in frontier:
            try:
                children = sorted(
                    child
                    for child in directory.iterdir()
                    if child.is_dir() and not child.name.startswith(".")
                )
            except OSError:
                continue
            next_frontier.extend(children)
        found.extend(next_frontier)
        frontier = next_frontier
    return found


def _directory_size(root: Path) -> int:
    total = 0
    for current, _dirs, files in os.walk(root, followlinks=True):
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def locate_model_root(
    source: Path,
    *,
    runtime: str,
    local_path: str | None,
    remote_ref: str | None,
    default_params: Mapping[str, Any] | None,
    manifest_id: str | None,
    repo_root: Path | None = None,
) -> tuple[Path, ModelReadiness]:
    """Find the directory under ``source`` that satisfies the manifest.

    Returns the first complete model found (the chosen folder itself, otherwise a
    sub-folder such as a Hugging Face ``snapshots/<hash>`` directory). Raises
    :class:`IncompleteSourceError` listing what the best candidate lacks.
    """

    first_failure: ModelReadiness | None = None
    for candidate in _candidate_directories(source):
        readiness = _readiness_for(
            runtime=runtime,
            local_path=local_path,
            remote_ref=remote_ref,
            default_params=default_params,
            manifest_id=manifest_id,
            candidate=candidate,
            repo_root=repo_root,
        )
        if readiness.is_ready:
            return candidate, readiness
        if first_failure is None:
            first_failure = readiness
    assert first_failure is not None
    raise IncompleteSourceError(
        "The selected folder does not contain a complete model: " + first_failure.message,
        first_failure.missing,
    )


def install_from_local_folder(
    *,
    source_path: str,
    runtime: str,
    local_path: str | None,
    remote_ref: str | None,
    default_params: Mapping[str, Any] | None,
    manifest_id: str | None,
    replace: bool = False,
    repo_root: Path | None = None,
) -> InstallResult:
    """Copy a complete model folder into the manifest's model directory."""

    destination = install_destination(runtime, local_path, default_params, repo_root=repo_root)
    if destination is None:
        raise InstallNotSupportedError(
            "This model is served from an endpoint, so there are no local files to place."
        )

    raw = Path(source_path).expanduser()
    if not raw.is_absolute():
        raise InvalidSourceError("The source folder must be an absolute path.")
    try:
        source = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidSourceError(f"The source folder does not exist: {source_path}") from exc
    if not source.is_dir():
        raise InvalidSourceError("The source must be a folder, not a file.")

    destination = destination.resolve()
    if source == destination:
        raise InvalidSourceError("The selected folder is already the model's install location.")
    model_root, _ = locate_model_root(
        source,
        runtime=runtime,
        local_path=local_path,
        remote_ref=remote_ref,
        default_params=default_params,
        manifest_id=manifest_id,
        repo_root=repo_root,
    )
    if _is_within(model_root, destination):
        raise InvalidSourceError("The selected folder is already inside the install location.")
    if _is_within(destination, model_root):
        raise InvalidSourceError(
            "The selected folder contains the install location. Choose the model folder itself."
        )

    if not _IMPORT_LOCK.acquire(blocking=False):
        raise InstallBusyError("Another model import is already running.")
    try:
        return _copy_into_place(
            model_root=model_root,
            destination=destination,
            runtime=runtime,
            local_path=local_path,
            remote_ref=remote_ref,
            default_params=default_params,
            manifest_id=manifest_id,
            replace=replace,
            repo_root=repo_root,
        )
    finally:
        _IMPORT_LOCK.release()


def _copy_into_place(
    *,
    model_root: Path,
    destination: Path,
    runtime: str,
    local_path: str | None,
    remote_ref: str | None,
    default_params: Mapping[str, Any] | None,
    manifest_id: str | None,
    replace: bool,
    repo_root: Path | None,
) -> InstallResult:
    destination_has_files = destination.exists() and any(destination.iterdir())
    if destination_has_files and not replace:
        raise DestinationNotEmptyError(
            f"The install location already has files: {destination}. "
            "Confirm to move them aside and place the selected model."
        )

    size = _directory_size(model_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination.parent).free
    if free < size + _DISK_MARGIN_BYTES:
        raise InsufficientSpaceError(
            f"Not enough free disk space: the model needs about {size / 2**30:.1f} GiB "
            f"and {free / 2**30:.1f} GiB is free."
        )

    staging = destination.parent / f".{destination.name}.import-{uuid.uuid4().hex[:8]}"
    try:
        # symlinks=False copies the real files, which matters for Hugging Face cache
        # folders where every file is a link into ``blobs/``.
        shutil.copytree(model_root, staging, symlinks=False)
        readiness = _readiness_for(
            runtime=runtime,
            local_path=local_path,
            remote_ref=remote_ref,
            default_params=default_params,
            manifest_id=manifest_id,
            candidate=staging,
            repo_root=repo_root,
        )
        if not readiness.is_ready:
            raise IncompleteSourceError(
                "The copied files did not pass the readiness check: " + readiness.message,
                readiness.missing,
            )

        replaced_to: Path | None = None
        if destination.exists():
            if destination_has_files:
                replaced_to = destination.with_name(
                    f"{destination.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}"
                )
                destination.rename(replaced_to)
            else:
                destination.rmdir()
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    final = _readiness_for(
        runtime=runtime,
        local_path=local_path,
        remote_ref=remote_ref,
        default_params=default_params,
        manifest_id=manifest_id,
        candidate=destination,
        repo_root=repo_root,
    )
    return InstallResult(
        destination=destination,
        source=model_root,
        replaced_to=replaced_to,
        readiness=final,
        copied_bytes=size,
    )


__all__ = [
    "DestinationNotEmptyError",
    "IncompleteSourceError",
    "InsufficientSpaceError",
    "InstallBusyError",
    "InstallNotSupportedError",
    "InstallResult",
    "InvalidSourceError",
    "ModelInstallError",
    "install_destination",
    "install_from_local_folder",
    "locate_model_root",
]
