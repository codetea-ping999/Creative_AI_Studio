"""Security contract for the local API development launcher."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "run_api_dev.sh"


class RunApiDevScriptTests(unittest.TestCase):
    def _run_launcher(self, **overrides: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        script_dir = root / "scripts"
        fake_bin = root / "bin"
        script_dir.mkdir()
        fake_bin.mkdir()

        launcher = script_dir / "run_api_dev.sh"
        launcher.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        launcher.chmod(0o755)

        invocation_log = root / "uvicorn-args"
        uvicorn = fake_bin / "uvicorn"
        uvicorn.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$UVICORN_LOG\"\n",
            encoding="utf-8",
        )
        uvicorn.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "UVICORN_LOG": str(invocation_log),
            }
        )
        environment.update(overrides)
        result = subprocess.run(
            ["bash", str(launcher)],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result, invocation_log

    def test_non_loopback_bind_is_rejected_without_explicit_unsafe_opt_in(self) -> None:
        result, invocation_log = self._run_launcher(API_HOST="0.0.0.0")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing non-loopback API_HOST", result.stderr)
        self.assertFalse(invocation_log.exists())

    def test_loopback_bind_remains_allowed(self) -> None:
        result, invocation_log = self._run_launcher(API_HOST="127.0.0.2")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--host 127.0.0.2", invocation_log.read_text(encoding="utf-8"))

    def test_non_loopback_bind_requires_exact_unsafe_opt_in(self) -> None:
        result, invocation_log = self._run_launcher(
            API_HOST="0.0.0.0",
            ALLOW_UNSAFE_API_BIND="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("--host 0.0.0.0", invocation_log.read_text(encoding="utf-8"))

    def test_truthy_but_non_exact_unsafe_opt_in_is_rejected(self) -> None:
        result, invocation_log = self._run_launcher(
            API_HOST="192.168.1.25",
            ALLOW_UNSAFE_API_BIND="true",
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(invocation_log.exists())


if __name__ == "__main__":
    unittest.main()
