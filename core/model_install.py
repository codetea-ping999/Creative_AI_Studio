"""Place model files the user already downloaded into a manifest's directory.

The Studio can be pointed at a folder chosen in Finder / Explorer. The folder is
validated with the same readiness rules the loaders use (``core/model_readiness``)
*before* anything is copied into the models directory. Only runtimes whose files
readiness can really check accept a folder (``supports_local_install``), so an
arbitrary folder is never copied.

Files are copied into a staging directory next to the destination, re-checked,
and then moved into place entry by entry. Anything that fails part-way is rolled
back: moved entries go back where they were and the staging directory is removed.

Some install locations ship placeholder files checked into the repository
(``README.md``, ``LICENSE.md``, ``.gitattributes``, a tracked ``model_index.json``).
Those count as "empty", are never moved or overwritten, and the model's weights
are placed alongside them. A tracked ``model_index.json`` is kept; if the chosen
model's own ``model_index.json`` describes different components the import fails
instead of mixing two pipelines.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.model_readiness import (
    STATUS_INVALID_CONFIGURATION,
    ModelReadiness,
    evaluate_readiness,
    llama_cpp_model_file,
    resolve_repo_path,
    supports_local_install,
)

#: How many directory levels below the chosen folder are searched when the folder
#: itself is not a model (people often pick the parent of the model directory).
_SEARCH_DEPTH = 2
#: Free space kept in reserve on top of the model size.
_DISK_MARGIN_BYTES = 256 * 1024 * 1024
#: Files that are never copied: hidden entries (``.git``, ``.cache``, ``.DS_Store``)
#: and Python sources. No supported runtime loads code from the weights folder, and
#: a ``.py`` file under ``models/`` would also restart the ``--reload`` dev server
#: in the middle of a copy.
_IGNORED_PATTERNS: tuple[str, ...] = (".*", "__pycache__", "*.py", "*.pyc")
#: Placeholder files that may ship in an install location and count as "empty".
_PLACEHOLDER_PATTERNS: tuple[str, ...] = ("README*", "LICENSE*", ".gitattributes")
_MODEL_INDEX = "model_index.json"
_STAGING_MARKER = ".import-"

_IMPORT_LOCK = threading.Lock()

#: Returns the paths git tracks in a directory, or ``None`` when that is unknown.
TrackedFilesLookup = Callable[[Path], "frozenset[str] | None"]
#: Called after the copy is verified and right before anything in the install
#: location moves; raise :class:`ModelInUseError` to stop while nothing changed.
BeforeSwapHook = Callable[[], None]
#: A staging folder without a live owner is removed at startup only once it is
#: this old, so another API process sharing ``models/`` keeps its import.
_STALE_STAGING_SECONDS = 60 * 60
_OWNER_FILE = ".import-owner"


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


class IncompatibleTrackedFileError(ModelInstallError):
    status_code = 409
    code = "incompatible_tracked_file"


class InsufficientSpaceError(ModelInstallError):
    status_code = 507
    code = "insufficient_space"


class InstallBusyError(ModelInstallError):
    status_code = 409
    code = "install_in_progress"


class ModelInUseError(ModelInstallError):
    """The model is loading or generating, so its files must not change now."""

    status_code = 409
    code = "model_in_use"


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


def local_install_destination(
    runtime: str,
    local_path: str | None,
    default_params: Mapping[str, Any] | None,
    *,
    repo_root: Path | None = None,
) -> Path | None:
    """``install_destination`` for runtimes whose files can be validated, else ``None``."""

    if not supports_local_install(runtime):
        return None
    return install_destination(runtime, local_path, default_params, repo_root=repo_root)


def _inside_git_checkout(directory: Path) -> bool:
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def git_tracked_files(directory: Path) -> frozenset[str] | None:
    """Paths (relative to ``directory``) that git tracks there.

    Returns ``None`` when that cannot be known: a release artifact made with
    ``git archive`` has no ``.git``, and git may not be installed. ``git`` is not
    run at all outside a checkout, because on macOS without the Command Line
    Tools that pops up an installer dialog.
    """

    if not directory.is_dir():
        return frozenset()
    if not _inside_git_checkout(directory):
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(directory), "ls-files", "-z", "--", "."],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    names = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return frozenset(name for name in names if name)


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


def _is_ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in _IGNORED_PATTERNS)


def _is_placeholder_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in _PLACEHOLDER_PATTERNS)


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


def _selected_entries(
    runtime: str, model_root: Path, default_params: Mapping[str, Any] | None
) -> list[Path] | None:
    """Relative paths to copy, or ``None`` to copy the whole folder.

    The llama.cpp loader only opens one GGUF file, so only that file is copied;
    a folder such as ``~/Downloads`` that happens to hold one ``.gguf`` must not
    be copied wholesale. Kokoro needs its config, its weight and the voice packs.
    """

    params = default_params or {}
    if runtime == "llama_cpp":
        model_file = llama_cpp_model_file(model_root, params)
        return [model_file.relative_to(model_root)] if model_file is not None else []
    if runtime == "kokoro":
        entries = [Path("config.json")]
        for pattern in ("*.pth", "*.pt"):
            entries += [
                path.relative_to(model_root)
                for path in sorted(model_root.glob(pattern))
                if path.is_file()
            ]
        if (model_root / "voices").is_dir():
            entries.append(Path("voices"))
        return entries
    return None


def _allowed_link_roots(model_root: Path) -> tuple[str, ...]:
    """Where links inside ``model_root`` may point: the folder itself and, for a
    Hugging Face cache snapshot (``<repo>/snapshots/<hash>``), that repo's ``blobs/``."""

    roots = [os.path.realpath(model_root)]
    if model_root.parent.name == "snapshots":
        roots.append(os.path.realpath(model_root.parent.parent / "blobs"))
    return tuple(roots)


def _check_link(entry: Path, allowed: tuple[str, ...]) -> None:
    if not entry.is_symlink():
        return
    target = os.path.realpath(entry)
    if not any(target == root or target.startswith(root + os.sep) for root in allowed):
        raise InvalidSourceError(
            f"The selected folder links to files outside it ({entry} -> {target}). "
            "Copy the real files into the model folder and try again."
        )


def _walk_files(root: Path, allowed: tuple[str, ...]) -> Iterable[Path]:
    """Files under ``root`` (following links), skipping ignored names.

    Raises :class:`InvalidSourceError`, before anything is copied, on a link that
    points outside the model folder (other than a Hugging Face cache's ``blobs/``)
    or on a folder link that loops back to one of its parents.
    """

    stack = [root]
    while stack:
        directory = stack.pop()
        real = os.path.realpath(directory)
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if _is_ignored(entry.name):
                continue
            _check_link(entry, allowed)
            if entry.is_dir():
                target = os.path.realpath(entry)
                if real == target or real.startswith(target + os.sep):
                    raise InvalidSourceError(
                        "The selected folder contains a folder link that loops back on "
                        f"itself: {entry}"
                    )
                stack.append(entry)
            elif entry.is_file():
                yield entry


def _selection_size(model_root: Path, selection: list[Path] | None) -> int:
    roots = [model_root] if selection is None else [model_root / item for item in selection]
    allowed = _allowed_link_roots(model_root)
    total = 0
    for item in roots:
        if item is not model_root:
            _check_link(item, allowed)
        if item.is_file():
            files: Iterable[Path] = [item]
        else:
            files = _walk_files(item, allowed)
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _copy_selection(model_root: Path, staging: Path, selection: list[Path] | None) -> None:
    ignore = shutil.ignore_patterns(*_IGNORED_PATTERNS)
    if selection is None:
        # symlinks=False copies the real files, which matters for Hugging Face cache
        # folders where every file is a link into ``blobs/``.
        shutil.copytree(
            model_root, staging, symlinks=False, ignore=ignore, dirs_exist_ok=True
        )
        return
    for item in selection:
        source = model_root / item
        target = staging / item
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False, ignore=ignore)
        else:
            shutil.copy2(source, target)


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
        # Prefer a failure that explains itself (e.g. "more than one .gguf in
        # snapshots/<hash>") over the chosen folder's plain "files are missing".
        if first_failure is None or (
            readiness.status == STATUS_INVALID_CONFIGURATION
            and first_failure.status != STATUS_INVALID_CONFIGURATION
        ):
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
    tracked_files: TrackedFilesLookup = git_tracked_files,
    before_swap: BeforeSwapHook | None = None,
) -> InstallResult:
    """Copy a complete model folder into the manifest's model directory.

    ``before_swap`` runs once the copy is verified, right before anything in the
    install location moves; the API uses it to refuse while the model is in use.
    """

    destination = install_destination(runtime, local_path, default_params, repo_root=repo_root)
    if destination is None:
        raise InstallNotSupportedError(
            "This model is served from an endpoint, so there are no local files to place."
        )
    if not supports_local_install(runtime):
        raise InstallNotSupportedError(
            f"Files for the {runtime!r} runtime cannot be checked before they are placed, "
            "so they cannot be placed from a folder. Copy them by hand instead."
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
            tracked_files=tracked_files,
            before_swap=before_swap,
        )
    finally:
        _IMPORT_LOCK.release()


def _protected_entries(destination: Path, tracked: frozenset[str] | None) -> set[str]:
    """Top-level names in ``destination`` that must stay where they are.

    Placeholder names (``README*``, ``LICENSE*``, ``.gitattributes``) always count.
    When git cannot say what the repository ships (a release artifact has no
    ``.git``), a ``model_index.json`` is assumed to be the shipped one too.
    """

    protected: set[str] = set()
    if not destination.is_dir():
        return protected
    known = tracked if tracked is not None else frozenset()
    tracked_tops = {Path(name).parts[0] for name in known if Path(name).parts}
    for entry in destination.iterdir():
        if entry.name in tracked_tops or (entry.is_file() and _is_placeholder_name(entry.name)):
            protected.add(entry.name)
        elif tracked is None and entry.name == _MODEL_INDEX and entry.is_file():
            protected.add(entry.name)
    return protected


def _model_index_signature(path: Path) -> dict[str, Any] | None:
    """The parts of a ``model_index.json`` that decide which files a pipeline loads."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        key: value
        for key, value in payload.items()
        if key == "_class_name" or not key.startswith("_")
    }


def _reconcile_with_protected(
    staging: Path,
    destination: Path,
    protected: set[str],
    *,
    tracked: frozenset[str] | None,
    replacing: bool,
) -> list[str]:
    """Settle files that both the install location and the new model have.

    - ``model_index.json``: the kept one wins; a different pipeline is refused.
    - ``README*`` / ``LICENSE*`` / ``.gitattributes`` that git does not track,
      while a previous model is being replaced: they belong to that model, so
      they are returned to be moved aside with it and the new model's copy is
      used (a licence must not be mixed up with another model's weights).
    - anything else protected: the kept one wins and the staged copy is dropped.
    """

    known = tracked if tracked is not None else frozenset()
    tracked_tops = {Path(name).parts[0] for name in known if Path(name).parts}
    swap: list[str] = []
    for name in sorted(protected):
        staged = staging / name
        if not staged.exists() and not staged.is_symlink():
            continue
        kept = destination / name
        if staged.is_dir() or kept.is_dir():
            raise IncompatibleTrackedFileError(
                f"The install location keeps a folder named {name!r} that is part of the "
                "repository, and the selected model has one too. Place this model by hand."
            )
        if name == _MODEL_INDEX:
            ours = _model_index_signature(kept)
            theirs = _model_index_signature(staged)
            if ours is None or theirs is None or ours != theirs:
                raise IncompatibleTrackedFileError(
                    f"The selected model's {_MODEL_INDEX} lists different components than "
                    f"the one kept in {destination}. The repository's file is kept, so this "
                    "model cannot be placed here. Check that you chose the right model."
                )
        elif replacing and _is_placeholder_name(name) and name not in tracked_tops:
            swap.append(name)
            continue
        staged.unlink()
    return swap


def _unique_sibling(destination: Path, suffix: str) -> Path:
    candidate = destination.with_name(f"{destination.name}{suffix}")
    counter = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}{suffix}-{counter}")
        counter += 1
    return candidate


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
    tracked_files: TrackedFilesLookup,
    before_swap: BeforeSwapHook | None,
) -> InstallResult:
    tracked = tracked_files(destination)
    protected = _protected_entries(destination, tracked)
    existing = (
        sorted(entry.name for entry in destination.iterdir() if entry.name not in protected)
        if destination.is_dir()
        else []
    )
    if existing and not replace:
        raise DestinationNotEmptyError(
            f"The install location already has files: {destination}. "
            "Confirm to move them aside and place the selected model."
        )

    selection = _selected_entries(runtime, model_root, default_params)
    size = _selection_size(model_root, selection)
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination.parent).free
    if free < size + _DISK_MARGIN_BYTES:
        raise InsufficientSpaceError(
            f"Not enough free disk space: the model needs about {size / 2**30:.1f} GiB "
            f"and {free / 2**30:.1f} GiB is free."
        )

    staging = destination.parent / f".{destination.name}{_STAGING_MARKER}{uuid.uuid4().hex[:8]}"
    created_destination = False
    replaced_to: Path | None = None
    moved_aside: list[str] = []
    placed: list[str] = []
    try:
        staging.mkdir()
        _write_owner(staging)
        _copy_selection(model_root, staging, selection)
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
        swap = _reconcile_with_protected(
            staging, destination, protected, tracked=tracked, replacing=bool(existing)
        )
        if before_swap is not None:
            before_swap()

        if not destination.exists():
            destination.mkdir()
            created_destination = True
        if existing:
            replaced_to = _unique_sibling(
                destination, f".replaced-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            replaced_to.mkdir()
            for name in [*existing, *swap]:
                (destination / name).rename(replaced_to / name)
                moved_aside.append(name)
        for entry in sorted(staging.iterdir()):
            if entry.name == _OWNER_FILE:
                continue
            entry.rename(destination / entry.name)
            placed.append(entry.name)

        final = _readiness_for(
            runtime=runtime,
            local_path=local_path,
            remote_ref=remote_ref,
            default_params=default_params,
            manifest_id=manifest_id,
            candidate=destination,
            repo_root=repo_root,
        )
        if not final.is_ready:
            raise IncompleteSourceError(
                "The placed files did not pass the readiness check: " + final.message,
                final.missing,
            )
    except BaseException:
        _roll_back(
            destination=destination,
            staging=staging,
            placed=placed,
            replaced_to=replaced_to,
            moved_aside=moved_aside,
            created_destination=created_destination,
        )
        raise
    shutil.rmtree(staging, ignore_errors=True)

    return InstallResult(
        destination=destination,
        source=model_root,
        replaced_to=replaced_to,
        readiness=final,
        copied_bytes=size,
    )


def _roll_back(
    *,
    destination: Path,
    staging: Path,
    placed: list[str],
    replaced_to: Path | None,
    moved_aside: list[str],
    created_destination: bool,
) -> None:
    """Undo a partial placement: the destination ends up exactly as it was."""

    for name in reversed(placed):
        target = destination / name
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError:
            pass
    if replaced_to is not None:
        for name in reversed(moved_aside):
            try:
                (replaced_to / name).rename(destination / name)
            except OSError:
                pass
        try:
            replaced_to.rmdir()
        except OSError:
            pass
    if created_destination:
        try:
            destination.rmdir()
        except OSError:
            pass
    shutil.rmtree(staging, ignore_errors=True)


def _write_owner(staging: Path) -> None:
    (staging / _OWNER_FILE).write_text(f"{os.getpid()}\n", encoding="utf-8")


def _owner_is_alive(staging: Path) -> bool | None:
    """Whether the process that created ``staging`` still runs; ``None`` if unknown."""

    try:
        pid = int((staging / _OWNER_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return False  # this process is starting up, so it has no import running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def remove_stale_staging(
    destinations: Iterable[Path],
    *,
    now: float | None = None,
) -> list[Path]:
    """Delete staging folders an interrupted import left next to ``destinations``.

    Called once when the API starts. Another API process may share ``models/``
    (``make verify`` starts a second one), so a staging folder is removed only
    when the process that wrote it is gone, or, if that cannot be told, when it
    has not changed for an hour.
    """

    current = time.time() if now is None else now
    removed: list[Path] = []
    for destination in destinations:
        parent = destination.parent
        if not parent.is_dir():
            continue
        for stale in parent.glob(f".{destination.name}{_STAGING_MARKER}*"):
            if not stale.is_dir() or stale.is_symlink():
                continue
            alive = _owner_is_alive(stale)
            if alive:
                continue
            if alive is None:
                try:
                    age = current - stale.stat().st_mtime
                except OSError:
                    continue
                if age < _STALE_STAGING_SECONDS:
                    continue
            shutil.rmtree(stale, ignore_errors=True)
            removed.append(stale)
    return removed

__all__ = [
    "DestinationNotEmptyError",
    "IncompatibleTrackedFileError",
    "IncompleteSourceError",
    "InsufficientSpaceError",
    "InstallBusyError",
    "InstallNotSupportedError",
    "InstallResult",
    "InvalidSourceError",
    "ModelInUseError",
    "ModelInstallError",
    "git_tracked_files",
    "install_destination",
    "install_from_local_folder",
    "local_install_destination",
    "locate_model_root",
    "remove_stale_staging",
]
