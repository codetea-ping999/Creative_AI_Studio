"""Contract for the local model download helper."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "download_models.sh"
#: Resolved before the fake PATH is applied, which no longer contains bash.
BASH = shutil.which("bash") or "/bin/bash"

#: External commands the script relies on. The fake PATH holds only these, so a
#: Hugging Face CLI installed on the developer's machine cannot leak into a run.
_REQUIRED_COMMANDS = ("dirname", "df", "awk", "mkdir", "cp", "cat")


class DownloadModelsScriptTests(unittest.TestCase):
    def _prepare_root(self) -> tuple[Path, Path]:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        script_dir = root / "scripts"
        fake_bin = root / "bin"
        script_dir.mkdir()
        fake_bin.mkdir()

        script = script_dir / "download_models.sh"
        script.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        script.chmod(0o755)

        for command in _REQUIRED_COMMANDS:
            resolved = shutil.which(command)
            self.assertIsNotNone(resolved, f"{command} is required to run this test")
            os.symlink(resolved, fake_bin / command)

        return root, fake_bin

    def _install_fake_cli(self, fake_bin: Path, name: str) -> None:
        binary = fake_bin / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)

    def _run(self, root: Path, fake_bin: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = str(fake_bin)
        return subprocess.run(
            [BASH, str(root / "scripts" / "download_models.sh"), *arguments],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_missing_cli_reports_install_instructions(self) -> None:
        root, fake_bin = self._prepare_root()

        result = self._run(root, fake_bin)

        self.assertEqual(result.returncode, 3)
        self.assertIn("No Hugging Face CLI found", result.stderr)
        self.assertIn('huggingface_hub[cli]', result.stderr)

    def test_modern_cli_omits_the_removed_symlink_flag(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "hf")

        result = self._run(root, fake_bin, "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hf download THUDM/CogVideoX-2b", result.stdout)
        self.assertIn("hf download facebook/musicgen-small", result.stdout)
        self.assertNotIn("--local-dir-use-symlinks", result.stdout)

    def test_legacy_cli_keeps_the_symlink_flag(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "huggingface-cli")

        result = self._run(root, fake_bin, "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--local-dir-use-symlinks False", result.stdout)

    def test_venv_cli_is_preferred_over_path(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "huggingface-cli")
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        self._install_fake_cli(venv_bin, "hf")

        result = self._run(root, fake_bin, "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("venv/bin/hf download", result.stdout)
        self.assertNotIn("--local-dir-use-symlinks", result.stdout)

    def test_audio_download_excludes_duplicate_and_optional_weights(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "hf")

        result = self._run(root, fake_bin, "--only", "audio", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CogVideoX-2b", result.stdout)
        for excluded in ("pytorch_model.bin", "state_dict.bin", "compression_state_dict.bin"):
            self.assertIn(f"--exclude {excluded}", result.stdout)

    def test_long_form_keeps_the_audiocraft_state_dicts_and_adds_t5(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "hf")

        result = self._run(root, fake_bin, "--only", "audio", "--with-long-form", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--exclude pytorch_model.bin", result.stdout)
        self.assertNotIn("--exclude state_dict.bin", result.stdout)
        self.assertIn("google-t5/t5-base", result.stdout)
        self.assertIn("audiocraft==1.3.0", result.stdout)

    def test_unknown_target_is_rejected(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "hf")

        result = self._run(root, fake_bin, "--only", "bogus")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown --only target", result.stderr)

    def test_insufficient_disk_space_aborts_before_downloading(self) -> None:
        root, fake_bin = self._prepare_root()
        self._install_fake_cli(fake_bin, "hf")
        fake_df = fake_bin / "df"
        fake_df.unlink()
        fake_df.write_text(
            "#!/bin/sh\n"
            "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
            "printf '/dev/fake 100000000 99000000 1048576 99%% /\\n'\n",
            encoding="utf-8",
        )
        fake_df.chmod(0o755)

        result = self._run(root, fake_bin, "--dry-run")

        self.assertEqual(result.returncode, 4)
        self.assertIn("Not enough free disk space", result.stderr)
        self.assertNotIn("==> hf download", result.stdout)


if __name__ == "__main__":
    unittest.main()
