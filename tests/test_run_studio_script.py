"""Security and artifact contract for the production Studio runner.

``scripts/run_studio.sh`` is the Node-free runtime path for the frozen v1.0
artifact. It must keep the loopback-only API binding policy of the dev runner,
require the prebuilt web UI, and never reload.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "run_studio.sh"


class RunStudioScriptTests(unittest.TestCase):
    def _run_launcher(self, **overrides: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        script_dir = root / "scripts"
        fake_bin = root / "bin"
        web_dist = root / "apps" / "web" / "dist"
        script_dir.mkdir()
        fake_bin.mkdir()
        web_dist.mkdir(parents=True)
        (web_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")

        launcher = script_dir / "run_studio.sh"
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

    def test_script_contains_no_reload_flag(self) -> None:
        script_text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("--reload", script_text)

    def test_invocation_runs_without_reload(self) -> None:
        result, invocation_log = self._run_launcher()

        self.assertEqual(result.returncode, 0, result.stderr)
        args = invocation_log.read_text(encoding="utf-8")
        self.assertIn("apps.api.main:app", args)
        self.assertNotIn("--reload", args)
        self.assertNotIn("--reload-dir", args)

    def test_uses_api_host_and_api_port(self) -> None:
        result, invocation_log = self._run_launcher(API_HOST="127.0.0.2", API_PORT="8123")

        self.assertEqual(result.returncode, 0, result.stderr)
        args = invocation_log.read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.2", args)
        self.assertIn("--port 8123", args)

    def test_non_loopback_bind_is_rejected_without_explicit_unsafe_opt_in(self) -> None:
        result, invocation_log = self._run_launcher(API_HOST="0.0.0.0")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing non-loopback API_HOST", result.stderr)
        self.assertFalse(invocation_log.exists())

    def test_non_loopback_bind_requires_exact_unsafe_opt_in(self) -> None:
        result, invocation_log = self._run_launcher(
            API_HOST="0.0.0.0",
            ALLOW_UNSAFE_API_BIND="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("--host 0.0.0.0", invocation_log.read_text(encoding="utf-8"))

    def test_missing_web_dist_fails_clearly(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        script_dir = root / "scripts"
        fake_bin = root / "bin"
        script_dir.mkdir()
        fake_bin.mkdir()

        launcher = script_dir / "run_studio.sh"
        launcher.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        launcher.chmod(0o755)

        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        result = subprocess.run(
            ["bash", str(launcher)],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires the prebuilt web UI", result.stderr)
        self.assertIn("apps/web/dist/index.html", result.stderr)


if __name__ == "__main__":
    unittest.main()