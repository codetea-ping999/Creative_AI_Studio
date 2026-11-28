"""Behavioral coverage for the project setup script."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT_PATH = REPOSITORY_ROOT / "setup.sh"
# setup.sh delegates the version policy to this script and its pin files.
TOOLCHAIN_POLICY_FILES = ("scripts/check_local_setup.py", ".python-version", ".nvmrc")
# Fake python3 shims hand the policy script to a real interpreter so the actual
# classification runs, while the versions under test come from the fake
# ``--version`` output (never from the host toolchain).
DELEGATE_POLICY_LINE = (
    "if [[ \"$1\" == *check_local_setup.py ]]; then exec \"$REAL_PYTHON\" \"$@\"; fi\n"
)


def _install_toolchain_policy(root: Path, fake_bin: Path) -> None:
    for relative in TOOLCHAIN_POLICY_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / relative, target)
    fake_python = fake_bin / "python3"
    if fake_python.exists():
        shebang, _, body = fake_python.read_text(encoding="utf-8").partition("\n")
        fake_python.write_text(f"{shebang}\n{DELEGATE_POLICY_LINE}{body}", encoding="utf-8")


def _write_fake_toolchain(fake_bin: Path, *, python_version: str, node_version: str) -> None:
    (fake_bin / "python3").write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"$1\" == \"--version\" ]]; then printf 'Python {python_version}\\n'; exit 0; fi\n"
        "if [[ \"$1\" == \"-m\" && \"$2\" == \"venv\" ]]; then\n"
        "  mkdir -p \"$3/bin\"; : > \"$3/bin/activate\"\n"
        "fi\n"
        "if [[ \"$#\" == \"0\" ]]; then : > data/jobs.db; fi\n",
        encoding="utf-8",
    )
    (fake_bin / "node").write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"$1\" == \"--version\" ]]; then printf 'v{node_version}\\n'; fi\n",
        encoding="utf-8",
    )
    for tool in ("pip", "npm"):
        (fake_bin / tool).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    for executable in fake_bin.iterdir():
        executable.chmod(0o755)


class SetupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"REAL_PYTHON": sys.executable})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_setup_with_versions(
        self, *, python_version: str, node_version: str
    ) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            web_dir = root / "apps" / "web"
            web_dir.mkdir(parents=True)
            (web_dir / ".env.example").write_text("VITE_API_BASE_URL=\n", encoding="utf-8")
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            _write_fake_toolchain(
                fake_bin, python_version=python_version, node_version=node_version
            )
            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            return subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

    def test_migration_python_and_node_floor_warn_but_complete_setup(self) -> None:
        """Python 3.10 / Node 20.19 stay supported for migration with a warning."""

        result = self._run_setup_with_versions(python_version="3.10.0", node_version="20.19.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[WARN] Python version: 3.10.0", result.stdout)
        self.assertIn("[WARN] Node.js version: v20.19.0", result.stdout)
        self.assertIn("推奨バージョン以外で続行します", result.stdout)
        self.assertIn("セットアップが完了しました", result.stdout)

    def test_canonical_versions_complete_setup_without_policy_warning(self) -> None:
        python_pin = (REPOSITORY_ROOT / ".python-version").read_text(encoding="utf-8").strip()
        node_pin = (REPOSITORY_ROOT / ".nvmrc").read_text(encoding="utf-8").strip()

        result = self._run_setup_with_versions(
            python_version=f"{python_pin}.1", node_version=f"{node_pin}.0.0"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[OK] Python version", result.stdout)
        self.assertIn("[OK] Node.js version", result.stdout)
        self.assertNotIn("推奨バージョン以外で続行します", result.stdout)

    def test_python_below_floor_stops_before_creating_a_venv(self) -> None:
        result = self._run_setup_with_versions(python_version="3.9.6", node_version="20.19.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("[FAIL] Python version: 3.9.6", result.stderr)
        self.assertIn("toolchain version check", result.stderr)
        self.assertIn("前提条件を確認しています", result.stderr)
        self.assertNotIn("Python 仮想環境を構築しています", result.stdout)

    def test_successful_setup_keeps_the_existing_completion_flow(self) -> None:
        """The diagnostics do not change a successful setup into an error path."""

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            web_dir = root / "apps" / "web"
            web_dir.mkdir(parents=True)
            (web_dir / ".env.example").write_text("VITE_API_BASE_URL=\n", encoding="utf-8")
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            setup_script.chmod(0o755)

            (fake_bin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'python3 %s\\n' \"$*\" >> \"$CALL_LOG\"\n"
                "if [[ \"$1\" == \"--version\" ]]; then\n"
                "  printf 'Python 3.11.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1\" == \"-c\" ]]; then exit 0; fi\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"venv\" ]]; then\n"
                "  mkdir -p \"$3/bin\"\n"
                "  : > \"$3/bin/activate\"\n"
                "fi\n"
                "if [[ \"$#\" == \"0\" ]]; then : > data/jobs.db; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "node").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then printf 'v20.19.0\\n'; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "pip").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'pip %s\\n' \"$*\" >> \"$CALL_LOG\"\n"
                "printf 'pip success sentinel\\n'\n",
                encoding="utf-8",
            )
            (fake_bin / "npm").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'npm %s\\n' \"$*\" >> \"$CALL_LOG\"\n"
                "printf 'npm success sentinel\\n'\n",
                encoding="utf-8",
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            call_log = root / "calls.log"
            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "CALL_LOG": str(call_log),
                }
            )
            result = subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("セットアップが完了しました", result.stdout)
            self.assertNotIn("セットアップに失敗しました", result.stderr)
            self.assertNotIn("pip success sentinel", result.stdout)
            self.assertNotIn("npm success sentinel", result.stdout)
            self.assertTrue((root / ".env").is_file())
            self.assertTrue((root / "apps" / "web" / ".env").is_file())
            self.assertTrue((root / "outputs" / "images").is_dir())
            self.assertTrue((root / "outputs" / "audio").is_dir())
            self.assertTrue((root / "outputs" / "videos").is_dir())
            self.assertTrue((root / "data" / "projects").is_dir())
            self.assertTrue((root / "data" / "feedback").is_dir())
            self.assertTrue((root / "data" / "jobs.db").is_file())
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("python3 -m venv venv", calls)
            self.assertIn("pip install --upgrade pip setuptools wheel", calls)
            self.assertIn("pip install -r requirements.txt", calls)
            self.assertIn("npm ci", calls)

    def _run_setup_with_failing_installer(self, failing: str) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            web_dir = root / "apps" / "web"
            web_dir.mkdir(parents=True)
            (web_dir / ".env.example").write_text("VITE_API_BASE_URL=\n", encoding="utf-8")
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            setup_script.chmod(0o755)
            (fake_bin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then printf 'Python 3.11.0\\n'; exit 0; fi\n"
                "if [[ \"$1\" == \"-c\" ]]; then exit 0; fi\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"venv\" ]]; then\n"
                "  mkdir -p \"$3/bin\"; : > \"$3/bin/activate\"\n"
                "fi\n"
                "if [[ \"$#\" == \"0\" ]]; then : > data/jobs.db; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "node").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then printf 'v20.19.0\\n'; fi\n",
                encoding="utf-8",
            )
            for tool in ("pip", "npm"):
                if tool == failing:
                    script = (
                        "#!/usr/bin/env bash\n"
                        "if [[ \"$*\" == *requirements.txt* || \"$1\" == \"ci\" ]]; then\n"
                        "  printf 'noise line\\n'\n"
                        f"  printf '{tool} underlying error sentinel\\n' >&2\n"
                        "  exit 23\n"
                        "fi\n"
                    )
                else:
                    script = "#!/usr/bin/env bash\nprintf 'quiet success\\n'\n"
                (fake_bin / tool).write_text(script, encoding="utf-8")
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["TMPDIR"] = str(root)
            return subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    def test_python_dependency_failure_shows_underlying_error_and_phase(self) -> None:
        result = self._run_setup_with_failing_installer("pip")

        self.assertEqual(result.returncode, 23)
        self.assertIn("pip underlying error sentinel", result.stderr)
        self.assertIn("Python パッケージをインストールしています", result.stderr)
        self.assertIn("pip install -r requirements.txt", result.stderr)
        self.assertIn("次の手順", result.stderr)
        self.assertNotIn("requirements.txt からパッケージをインストールしました", result.stdout)
        self.assertNotIn("Web UI のロック済み依存パッケージ", result.stdout)

    def test_web_dependency_failure_shows_underlying_error_and_phase(self) -> None:
        result = self._run_setup_with_failing_installer("npm")

        self.assertEqual(result.returncode, 23)
        self.assertIn("npm underlying error sentinel", result.stderr)
        self.assertIn("Web UI パッケージをインストールしています", result.stderr)
        self.assertIn("npm ci", result.stderr)
        self.assertIn("次の手順", result.stderr)
        self.assertNotIn("Web UI のロック済み依存パッケージをインストールしました", result.stdout)

    def test_python_version_lookup_failure_reports_phase_and_stops(self) -> None:
        """A failed version lookup cannot be hidden inside a success message."""

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            setup_script.chmod(0o755)
            (fake_bin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then exit 17; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "python3").chmod(0o755)

            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            result = subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Python 3 のバージョンを取得できません", result.stderr)
            self.assertIn("前提条件を確認しています", result.stderr)
            self.assertIn("python3 --version", result.stderr)
            self.assertNotIn("Python 3 は インストール済み", result.stdout)

    def test_unsupported_node_version_reports_phase_and_command(self) -> None:
        """An unsupported Node version follows the explicit prerequisite failure path."""

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            setup_script.chmod(0o755)
            (fake_bin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then printf 'Python 3.11.0\\n'; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "node").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then\n"
                "  printf 'v20.18.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            result = subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("[FAIL] Node.js version: v20.18.0", result.stderr)
            self.assertIn("requires Node.js 20.19+ or 22.12+", result.stderr)
            self.assertIn("前提条件を確認しています", result.stderr)
            self.assertIn("toolchain version check", result.stderr)
            self.assertNotIn("Node.js はインストール済み", result.stdout)

    def test_database_initialization_failure_reports_a_concise_command(self) -> None:
        """A heredoc failure names its command without dumping its full source body."""

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            setup_script.chmod(0o755)
            (fake_bin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then\n"
                "  printf 'Python 3.11.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1\" == \"-c\" ]]; then exit 0; fi\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"venv\" ]]; then\n"
                "  mkdir -p \"$3/bin\"\n"
                "  : > \"$3/bin/activate\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 31\n",
                encoding="utf-8",
            )
            (fake_bin / "node").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then printf 'v20.19.0\\n'; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "pip").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            result = subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 31)
            self.assertIn("データベースを初期化しています", result.stderr)
            self.assertIn("失敗したコマンド: python3", result.stderr)
            self.assertIn("<<'EOF'", result.stderr)
            self.assertNotIn("from pathlib import Path", result.stderr)

    def test_dependency_failure_reports_phase_and_command_then_stops(self) -> None:
        """A failing setup command identifies its phase without running later phases."""

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            setup_script = root / "setup.sh"
            setup_script.write_text(SETUP_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            setup_script.chmod(0o755)

            (fake_bin / "python3").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then\n"
                "  printf 'Python 3.11.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1\" == \"-c\" ]]; then\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"venv\" ]]; then\n"
                "  mkdir -p \"$3/bin\"\n"
                "  : > \"$3/bin/activate\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fake_bin / "node").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == \"--version\" ]]; then printf 'v20.19.0\\n'; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "pip").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'simulated pip failure\\n' >&2\n"
                "exit 42\n",
                encoding="utf-8",
            )
            (fake_bin / "npm").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'npm should not run after a pip failure\\n' > \"$NPM_MARKER\"\n",
                encoding="utf-8",
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            npm_marker = root / "npm-ran"
            _install_toolchain_policy(root, fake_bin)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "NPM_MARKER": str(npm_marker),
                }
            )
            result = subprocess.run(
                ["bash", str(setup_script)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 42)
            self.assertIn("Python パッケージをインストールしています", result.stderr)
            self.assertIn("pip install --upgrade pip setuptools wheel", result.stderr)
            self.assertFalse(npm_marker.exists())


if __name__ == "__main__":
    unittest.main()
