"""Contract for the local model download helper."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "download_models.py"
SPEC = importlib.util.spec_from_file_location("download_models", SCRIPT_PATH)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
# Registered before execution so the module's dataclasses resolve their own types.
sys.modules["download_models"] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

MODERN_CLI = MODULE.HuggingFaceCli(path="/somewhere/hf", legacy=False)
LEGACY_CLI = MODULE.HuggingFaceCli(path="/somewhere/huggingface-cli", legacy=True)


def _manifest_root(root: Path) -> Path:
    return root / "models" / "manifests"


def _write_manifests(root: Path, models_directory: str) -> None:
    """Lay out the three manifests this script reads, pointing at a chosen root."""

    manifests = _manifest_root(root)
    (manifests / "video").mkdir(parents=True)
    (manifests / "audio").mkdir(parents=True)
    (manifests / "video" / "learned-local.json").write_text(
        json.dumps(
            {
                "id": "learned-video-local",
                "public_id": "learned-video",
                "runtime": "learned",
                "local_path": f"{models_directory}/video/learned-runtime",
                "default_params": {
                    "entrypoint": "runtime.py",
                    "pipeline_path": f"{models_directory}/video/cogvideox-2b",
                    "pipeline_id": "THUDM/CogVideoX-2b",
                },
            }
        ),
        encoding="utf-8",
    )
    (manifests / "audio" / "musicgen-small.json").write_text(
        json.dumps(
            {
                "id": "musicgen-small-local",
                "public_id": "musicgen-small",
                "runtime": "transformers",
                "local_path": f"{models_directory}/audio/musicgen-small",
            }
        ),
        encoding="utf-8",
    )
    (manifests / "audio" / "musicgen-long-form.json").write_text(
        json.dumps(
            {
                "id": "musicgen-long-form-local",
                "public_id": "musicgen-long-form",
                "runtime": "audiocraft",
                "local_path": f"{models_directory}/audio/musicgen-long-form",
            }
        ),
        encoding="utf-8",
    )


class DownloadCommandTests(unittest.TestCase):
    """The two CLI generations parse repeated patterns differently."""

    def _audio_download(self, *, with_long_form: bool = False) -> object:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_manifests(root, "./models")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MODELS_ROOT", None)
                os.environ.pop("MODELS_MANIFEST_ROOT", None)
                targets = MODULE.build_targets(with_long_form=with_long_form, root=root)
            return targets["audio"].downloads[0]

    def test_modern_cli_repeats_the_flag_and_drops_the_removed_option(self) -> None:
        command = MODULE.download_command(MODERN_CLI, self._audio_download())

        self.assertNotIn("--local-dir-use-symlinks", command)
        # typer reads list[str] options as repeatable; a bare second pattern
        # would be taken for a filename to download.
        self.assertEqual(command.count("--exclude"), 3)
        self.assertEqual(
            command[command.index("--exclude") : command.index("--exclude") + 2],
            ["--exclude", "pytorch_model.bin"],
        )

    def test_legacy_cli_groups_patterns_under_one_flag(self) -> None:
        command = MODULE.download_command(LEGACY_CLI, self._audio_download())

        # argparse nargs="*" keeps only the last occurrence of a repeated flag,
        # so all three patterns have to follow a single --exclude.
        self.assertEqual(command.count("--exclude"), 1)
        excluded = command[command.index("--exclude") + 1 : command.index("--exclude") + 4]
        self.assertEqual(
            excluded,
            ["pytorch_model.bin", "state_dict.bin", "compression_state_dict.bin"],
        )
        self.assertIn("--local-dir-use-symlinks", command)

    def test_legacy_cli_groups_include_patterns_too(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_manifests(root, "./models")
            targets = MODULE.build_targets(with_long_form=True, root=root)
        t5_download = targets["audio"].downloads[1]

        command = MODULE.download_command(LEGACY_CLI, t5_download)

        self.assertEqual(command.count("--include"), 1)
        self.assertEqual(
            command[command.index("--include") + 1 : command.index("--include") + 6],
            list(MODULE.T5_FILES),
        )


class TargetTests(unittest.TestCase):
    def test_destinations_come_from_the_manifests(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            external = Path(directory) / "external-disk" / "models"
            _write_manifests(root, str(external))

            targets = MODULE.build_targets(with_long_form=False, root=root)

        self.assertEqual(
            targets["video"].downloads[0].destination,
            external / "video" / "cogvideox-2b",
        )
        self.assertEqual(
            targets["audio"].downloads[0].destination,
            external / "audio" / "musicgen-small",
        )

    def test_models_root_moves_the_manifest_lookup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            relocated = root / "external-disk"
            _write_manifests(relocated, str(relocated / "models"))

            with patch.dict(
                os.environ, {"MODELS_ROOT": str(relocated / "models")}, clear=False
            ):
                os.environ.pop("MODELS_MANIFEST_ROOT", None)
                resolved = MODULE.manifest_root(root)
                targets = MODULE.build_targets(with_long_form=False, root=root)

        self.assertEqual(resolved, _manifest_root(relocated))
        self.assertEqual(
            targets["audio"].downloads[0].destination,
            relocated / "models" / "audio" / "musicgen-small",
        )

    def test_long_form_keeps_the_state_dicts_and_stages_them(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_manifests(root, "./models")

            targets = MODULE.build_targets(with_long_form=True, root=root)

        audio = targets["audio"]
        self.assertEqual(audio.downloads[0].exclude, ("pytorch_model.bin",))
        self.assertEqual(audio.downloads[1].repo_id, "google-t5/t5-base")
        self.assertEqual(
            [destination.name for _, destination in audio.copies],
            ["state_dict.bin", "compression_state_dict.bin"],
        )
        self.assertIn("audiocraft==1.3.0", audio.notes)

    def test_default_audio_download_skips_the_duplicate_and_optional_weights(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_manifests(root, "./models")

            targets = MODULE.build_targets(with_long_form=False, root=root)

        self.assertEqual(len(targets["audio"].downloads), 1)
        self.assertEqual(
            targets["audio"].downloads[0].exclude,
            ("pytorch_model.bin", "state_dict.bin", "compression_state_dict.bin"),
        )


class SpacePreflightTests(unittest.TestCase):
    def test_already_downloaded_bytes_are_not_demanded_again(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "cogvideox-2b"
            destination.mkdir()
            (destination / "part.safetensors").write_bytes(b"x" * 1024)
            download = MODULE.Download(
                repo_id="THUDM/CogVideoX-2b",
                destination=destination,
                estimated_bytes=4096,
            )

            remaining = MODULE.missing_bytes([download])

        self.assertEqual(remaining[destination], 3072)

    def test_a_complete_download_needs_no_free_space(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "musicgen-small"
            destination.mkdir()
            (destination / "model.safetensors").write_bytes(b"x" * 8192)
            download = MODULE.Download(
                repo_id="facebook/musicgen-small",
                destination=destination,
                estimated_bytes=4096,
            )

            remaining = MODULE.missing_bytes([download])

        self.assertEqual(remaining[destination], 0)

    def test_short_filesystem_is_reported(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "models" / "video"
            usage = shutil.disk_usage(directory)
            with patch.object(
                MODULE.shutil,
                "disk_usage",
                return_value=usage._replace(free=1024),
            ):
                problems = MODULE.check_free_space({destination: 10_000})

        self.assertEqual(len(problems), 1)
        self.assertIn("short by", problems[0])

    def test_destinations_on_one_disk_are_weighed_together(self) -> None:
        with TemporaryDirectory() as directory:
            video = Path(directory) / "models" / "video"
            audio = Path(directory) / "models" / "audio"
            usage = shutil.disk_usage(directory)
            with patch.object(
                MODULE.shutil,
                "disk_usage",
                return_value=usage._replace(free=15_000),
            ):
                problems = MODULE.check_free_space({video: 10_000, audio: 4_000})

        # Either one fits on its own; together with headroom they do not.
        self.assertEqual(len(problems), 1)

    def test_sufficient_filesystem_reports_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "models" / "video"
            usage = shutil.disk_usage(directory)
            with patch.object(
                MODULE.shutil,
                "disk_usage",
                return_value=usage._replace(free=1_000_000),
            ):
                problems = MODULE.check_free_space({destination: 10_000})

        self.assertEqual(problems, [])


class VerifyTests(unittest.TestCase):
    """Readiness is answered per selected manifest, not per workstation."""

    def _target(self, payload: dict[str, object]) -> object:
        return MODULE.Target(
            key="audio",
            manifest_path=Path("manifest.json"),
            payload=payload,
            downloads=(),
        )

    def test_a_missing_checkpoint_fails(self) -> None:
        with TemporaryDirectory() as directory:
            payload = {
                "id": "musicgen-small-local",
                "public_id": "musicgen-small",
                "runtime": "transformers",
                "local_path": str(Path(directory) / "musicgen-small"),
            }

            failures = MODULE.verify([self._target(payload)])

        self.assertEqual(failures, 1)

    def test_a_complete_checkpoint_passes(self) -> None:
        with TemporaryDirectory() as directory:
            model_directory = Path(directory) / "musicgen-small"
            model_directory.mkdir()
            for name in (
                "config.json",
                "preprocessor_config.json",
                "tokenizer_config.json",
                "tokenizer.json",
                "spiece.model",
                "model.safetensors",
            ):
                (model_directory / name).write_text("{}", encoding="utf-8")
            payload = {
                "id": "musicgen-small-local",
                "public_id": "musicgen-small",
                "runtime": "transformers",
                "local_path": str(model_directory),
            }

            failures = MODULE.verify([self._target(payload)])

        self.assertEqual(failures, 0)


class CliDiscoveryTests(unittest.TestCase):
    def test_the_venv_cli_wins_over_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            venv_bin = root / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            venv_cli = venv_bin / "hf"
            venv_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            venv_cli.chmod(0o755)

            found = MODULE.find_cli(root)

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(Path(found.path), venv_cli)
        self.assertFalse(found.legacy)

    def test_a_legacy_venv_cli_is_recognised(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            venv_bin = root / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            legacy = venv_bin / "huggingface-cli"
            legacy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            legacy.chmod(0o755)

            found = MODULE.find_cli(root)

        assert found is not None
        self.assertTrue(found.legacy)

    def test_missing_cli_is_reported_with_an_exit_code(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_manifests(root, "./models")
            with patch.object(MODULE, "load_dotenv"), patch.object(
                MODULE, "find_cli", return_value=None
            ):
                exit_code = MODULE.main(["--dry-run"])

        self.assertEqual(exit_code, MODULE.EXIT_NO_CLI)


class DotenvTests(unittest.TestCase):
    def test_dotenv_fills_gaps_without_overriding_the_environment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                '# comment\nexport MODELS_ROOT="/Volumes/AI/models"\nALREADY_SET=from-file\n',
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ALREADY_SET": "from-environment"}, clear=False):
                os.environ.pop("MODELS_ROOT", None)
                MODULE.load_dotenv(root)
                models_root = os.environ.get("MODELS_ROOT")
                already_set = os.environ["ALREADY_SET"]
                os.environ.pop("MODELS_ROOT", None)

        self.assertEqual(models_root, "/Volumes/AI/models")
        self.assertEqual(already_set, "from-environment")


if __name__ == "__main__":
    unittest.main()
