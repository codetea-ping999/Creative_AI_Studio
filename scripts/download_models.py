#!/usr/bin/env python3
"""Download the local model weights that ``GET /models`` reports as missing.

Covers the two models tracked in issue #421: CogVideoX-2B (``learned-video``)
and MusicGen Small (``musicgen-small``). Weights are gitignored runtime
artifacts, so this has to run on the machine that serves the Studio.
See docs/model-download-guide.md.

Destinations come from the manifests themselves rather than from hard-coded
paths, so an installation that moved its models with ``MODELS_ROOT`` /
``MODELS_MANIFEST_ROOT`` downloads to the directory its own manifests name.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.model_readiness import (  # noqa: E402  (path bootstrap above)
    STATUS_READY,
    evaluate_manifest_payload,
)

EXIT_NO_CLI = 3
EXIT_NO_SPACE = 4
EXIT_NOT_READY = 5

#: Published file sizes on the Hugging Face repositories, used for the
#: free-space preflight only. Kept as bytes so the arithmetic stays exact.
VIDEO_BYTES = 13_774_687_212
AUDIO_BYTES = 2_367_700_000
AUDIOCRAFT_STATE_DICT_BYTES = 1_076_845_798
T5_BYTES = 893_900_000

#: pytorch_model.bin duplicates model.safetensors, and the AudioCraft state
#: dicts are only read by the optional long-form runtime.
AUDIO_DUPLICATE_WEIGHTS = ("pytorch_model.bin",)
AUDIOCRAFT_STATE_DICTS = ("state_dict.bin", "compression_state_dict.bin")
T5_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "spiece.model",
    "tokenizer.json",
)


@dataclass(frozen=True)
class HuggingFaceCli:
    """A Hugging Face CLI found on this machine."""

    path: str
    #: True for ``huggingface-cli``, removed in huggingface_hub 1.0.
    legacy: bool


@dataclass(frozen=True)
class Download:
    """One repository to place into one directory."""

    repo_id: str
    destination: Path
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    estimated_bytes: int = 0


@dataclass(frozen=True)
class Target:
    """A model this script can install, with the manifest that describes it."""

    key: str
    manifest_path: Path
    payload: Mapping[str, object]
    downloads: tuple[Download, ...]
    #: Extra bytes written after downloading (staged copies, for instance).
    extra_bytes: int = 0
    #: Files copied into place once the downloads finish.
    copies: tuple[tuple[Path, Path], ...] = ()
    notes: str = ""


def load_dotenv(root: Path = ROOT) -> None:
    """Apply the root .env, letting the real environment win.

    The launcher scripts source .env, so a MODELS_ROOT set only there would
    otherwise be invisible to this script.
    """

    env_file = root / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _resolve_path(value: str, root: Path = ROOT) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def manifest_root(root: Path = ROOT) -> Path:
    """Mirror bootstrap/factories.py so both find the same manifests."""

    configured = os.getenv("MODELS_MANIFEST_ROOT")
    if configured:
        return _resolve_path(configured, root)
    models_root = os.getenv("MODELS_ROOT")
    if models_root:
        return _resolve_path(models_root, root) / "manifests"
    return root / "models" / "manifests"


def read_manifest(relative_path: str, root: Path = ROOT) -> tuple[Path, Mapping[str, object]]:
    manifest_path = manifest_root(root) / relative_path
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{manifest_path} does not contain a manifest object")
    return manifest_path, payload


def manifest_directory(payload: Mapping[str, object], key: str, root: Path = ROOT) -> Path:
    """Read a directory out of a manifest, resolving it like the loaders do."""

    if key in payload:
        value = payload.get(key)
    else:
        default_params = payload.get("default_params")
        value = default_params.get(key) if isinstance(default_params, Mapping) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"manifest {payload.get('id')!r} has no usable {key!r}")
    return _resolve_path(value, root)


def build_targets(*, with_long_form: bool, root: Path = ROOT) -> dict[str, Target]:
    """Describe what each --only selection downloads, and where."""

    video_manifest, video_payload = read_manifest("video/learned-local.json", root)
    video_destination = manifest_directory(video_payload, "pipeline_path", root)
    video_repo = video_payload.get("default_params")
    video_repo_id = (
        video_repo.get("pipeline_id") if isinstance(video_repo, Mapping) else None
    ) or "THUDM/CogVideoX-2b"

    audio_manifest, audio_payload = read_manifest("audio/musicgen-small.json", root)
    audio_destination = manifest_directory(audio_payload, "local_path", root)

    audio_exclude = AUDIO_DUPLICATE_WEIGHTS
    audio_bytes = AUDIO_BYTES
    audio_downloads: list[Download] = []
    audio_copies: list[tuple[Path, Path]] = []
    audio_extra_bytes = 0
    audio_notes = ""

    if with_long_form:
        audio_bytes += AUDIOCRAFT_STATE_DICT_BYTES
        _, long_form_payload = read_manifest("audio/musicgen-long-form.json", root)
        long_form_destination = manifest_directory(long_form_payload, "local_path", root)
        audio_copies = [
            (audio_destination / name, long_form_destination / name)
            for name in AUDIOCRAFT_STATE_DICTS
        ]
        audio_extra_bytes = AUDIOCRAFT_STATE_DICT_BYTES
        audio_downloads.append(
            Download(
                repo_id="google-t5/t5-base",
                destination=long_form_destination / "t5-base",
                include=T5_FILES,
                estimated_bytes=T5_BYTES,
            )
        )
        audio_notes = (
            "musicgen-long-form also needs the AudioCraft package, which this script does\n"
            "not install because it changes the venv:\n\n"
            "  ./venv/bin/pip install --no-deps audiocraft==1.3.0\n"
            "  ./venv/bin/pip install av soundfile einops flashy hydra-core hydra-colorlog \\\n"
            '    julius num2words "spacy>=3.6.1" librosa torchmetrics encodec demucs'
        )
    else:
        audio_exclude = AUDIO_DUPLICATE_WEIGHTS + AUDIOCRAFT_STATE_DICTS

    audio_downloads.insert(
        0,
        Download(
            repo_id="facebook/musicgen-small",
            destination=audio_destination,
            exclude=audio_exclude,
            estimated_bytes=audio_bytes,
        ),
    )

    return {
        "video": Target(
            key="video",
            manifest_path=video_manifest,
            payload=video_payload,
            downloads=(
                Download(
                    repo_id=str(video_repo_id),
                    destination=video_destination,
                    estimated_bytes=VIDEO_BYTES,
                ),
            ),
        ),
        "audio": Target(
            key="audio",
            manifest_path=audio_manifest,
            payload=audio_payload,
            downloads=tuple(audio_downloads),
            extra_bytes=audio_extra_bytes,
            copies=tuple(audio_copies),
            notes=audio_notes,
        ),
    }


def find_cli(root: Path = ROOT) -> HuggingFaceCli | None:
    """Prefer the project venv, and record which CLI generation it is.

    huggingface_hub 1.0 renamed huggingface-cli to hf and removed
    --local-dir-use-symlinks, so the binary name decides the flags.
    """

    candidates = (
        root / "venv" / "bin" / "hf",
        root / "venv" / "bin" / "huggingface-cli",
        Path("hf"),
        Path("huggingface-cli"),
    )
    for candidate in candidates:
        resolved = shutil.which(str(candidate))
        if resolved:
            return HuggingFaceCli(path=resolved, legacy=Path(resolved).name == "huggingface-cli")
    return None


def download_command(cli: HuggingFaceCli, download: Download) -> list[str]:
    """Build the CLI call, in the pattern grammar that CLI actually parses."""

    command = [cli.path, "download", download.repo_id, "--local-dir", str(download.destination)]
    if cli.legacy:
        # argparse nargs="*": a repeated flag keeps only its last occurrence,
        # so every pattern has to follow a single flag.
        if download.include:
            command += ["--include", *download.include]
        if download.exclude:
            command += ["--exclude", *download.exclude]
        command += ["--local-dir-use-symlinks", "False"]
    else:
        # typer list[str]: the flag repeats, and a bare pattern after one would
        # be read as a filename to download instead.
        for pattern in download.include:
            command += ["--include", pattern]
        for pattern in download.exclude:
            command += ["--exclude", pattern]
    return command


def directory_size(path: Path) -> int:
    """Bytes already on disk under path, counting each inode once."""

    if not path.exists():
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    for entry in path.rglob("*"):
        try:
            stat_result = entry.stat()
        except OSError:
            continue
        if not entry.is_file():
            continue
        key = (stat_result.st_dev, stat_result.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += stat_result.st_size
    return total


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or ".")


def missing_bytes(downloads: Iterable[Download]) -> dict[Path, int]:
    """What each destination still needs, so a resumed download is not blocked.

    hf download skips files it already has, so the preflight asks only for the
    remainder rather than for the whole payload again.
    """

    remaining: dict[Path, int] = {}
    for download in downloads:
        outstanding = max(download.estimated_bytes - directory_size(download.destination), 0)
        remaining[download.destination] = remaining.get(download.destination, 0) + outstanding
    return remaining


def check_free_space(requirements: Mapping[Path, int]) -> list[str]:
    """Return one problem line per filesystem that cannot hold its share.

    Destinations are grouped by device, so two directories on the same disk are
    weighed against that disk's free space together rather than one at a time.
    """

    per_device: dict[int, tuple[Path, int]] = {}
    for destination, needed in sorted(requirements.items()):
        anchor = _existing_ancestor(destination)
        device = anchor.stat().st_dev
        known_anchor, known_needed = per_device.get(device, (anchor, 0))
        per_device[device] = (known_anchor, known_needed + needed)

    problems: list[str] = []
    for anchor, needed in sorted(per_device.values()):
        # Downloads stage next to the target before being moved into place, so
        # ask for headroom on top of the payload itself.
        with_headroom = needed + needed // 10
        free = shutil.disk_usage(anchor).free
        print(
            f"{anchor}: need {_gib(with_headroom)} (payload {_gib(needed)}), free {_gib(free)}"
        )
        if free < with_headroom:
            problems.append(
                f"{anchor} is short by {_gib(with_headroom - free)}"
            )
    return problems


def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GiB"


def verify(targets: Sequence[Target]) -> int:
    """Check the selected manifests only, rather than the whole workstation."""

    failures = 0
    for target in targets:
        readiness = evaluate_manifest_payload(target.payload)
        ready = readiness.status == STATUS_READY
        print(f"{target.payload.get('public_id', target.key)}: {readiness.status}")
        if ready:
            continue
        print(f"  {readiness.message}")
        for missing in readiness.missing:
            print(f"  missing: {missing}")
        failures += 1
    return failures


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the CogVideoX-2B and MusicGen Small weights into the "
            "directories this installation's manifests name."
        )
    )
    parser.add_argument(
        "--only",
        choices=("video", "audio"),
        help="Download one model instead of both.",
    )
    parser.add_argument(
        "--with-long-form",
        action="store_true",
        help="Also stage the optional AudioCraft long-form assets.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands without downloading.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    load_dotenv()

    cli = find_cli()
    if cli is None:
        print(
            "No Hugging Face CLI found. Install it into the project venv first:\n\n"
            '  ./venv/bin/pip install "huggingface_hub[cli]"\n\n'
            "Then re-run this script.",
            file=sys.stderr,
        )
        return EXIT_NO_CLI

    print(f"Hugging Face CLI: {cli.path}")

    targets = build_targets(with_long_form=arguments.with_long_form)
    selected = [targets[arguments.only]] if arguments.only else [targets["video"], targets["audio"]]

    downloads = [download for target in selected for download in target.downloads]
    requirements = missing_bytes(downloads)
    for target in selected:
        if target.extra_bytes and target.copies:
            staged = target.copies[0][1].parent
            requirements[staged] = requirements.get(staged, 0) + target.extra_bytes
    problems = check_free_space(requirements)
    if problems:
        for problem in problems:
            print(f"Not enough free disk space: {problem}", file=sys.stderr)
        return EXIT_NO_SPACE

    for download in downloads:
        command = download_command(cli, download)
        print(f"\n==> {' '.join(command)}")
        if arguments.dry_run:
            continue
        download.destination.mkdir(parents=True, exist_ok=True)
        subprocess.run(command, check=True)

    for target in selected:
        for source, destination in target.copies:
            print(f"\n==> copy {source} -> {destination}")
            if arguments.dry_run:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        if target.notes:
            print(f"\n{target.notes}")

    if arguments.dry_run:
        print("\nDry run complete. Nothing was downloaded.")
        return 0

    print("\n==> verifying readiness")
    if verify(selected):
        return EXIT_NOT_READY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
