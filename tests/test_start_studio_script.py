"""Coverage for the one-command local Studio launcher."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "start_studio.sh"


class StartStudioScriptTests(unittest.TestCase):
    def test_launcher_uses_dotenv_ports_for_api_web_and_cors(self) -> None:
        """A custom .env port reaches both children through one launcher contract."""

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_dir = root / "scripts"
            web_dir = root / "apps" / "web"
            fake_bin = root / "bin"
            runtime_dir = root / "runtime"
            script_dir.mkdir(parents=True)
            web_dir.mkdir(parents=True)
            fake_bin.mkdir()
            runtime_dir.mkdir()

            launcher = script_dir / "start_studio.sh"
            launcher.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            launcher.chmod(0o755)
            (root / ".env").write_text("API_PORT=8123\nWEB_PORT=5174\n", encoding="utf-8")
            (script_dir / "run_api_dev.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            (fake_bin / "curl").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *\"--request OPTIONS\"* ]]; then\n"
                "  printf 'HTTP/1.1 200 OK\\r\\naccess-control-allow-origin: %s\\r\\n\\r\\n' \"$EXPECTED_WEB_URL\"\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$*\" == *\"/src/studioClient.ts\"* ]]; then\n"
                "  printf 'import.meta.env = {\"VITE_API_BASE_URL\": \"%s\"};' \"$EXPECTED_API_URL\"\n"
                "  exit 0\n"
                "fi\n"
                "count=0\n"
                "if [[ -f \"$CURL_STATE\" ]]; then count=$(<\"$CURL_STATE\"); fi\n"
                "count=$((count + 1))\n"
                "printf '%s' \"$count\" > \"$CURL_STATE\"\n"
                "if (( count <= 3 )); then exit 1; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "nohup").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s|%s|%s\\n' \"${VITE_API_BASE_URL:-}\" \"${WEB_PORT:-}\" \"$*\" >> \"$NOHUP_LOG\"\n",
                encoding="utf-8",
            )
            (fake_bin / "open").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (fake_bin / "sleep").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            nohup_log = root / "nohup.log"
            environment = os.environ.copy()
            environment.pop("VITE_API_BASE_URL", None)
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "TMPDIR": str(runtime_dir),
                    "CURL_STATE": str(root / "curl-state"),
                    "NOHUP_LOG": str(nohup_log),
                    "EXPECTED_API_URL": "http://127.0.0.1:8123",
                    "EXPECTED_WEB_URL": "http://localhost:5174",
                },
            )
            result = subprocess.run(
                ["bash", str(launcher)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("http://localhost:5174", result.stdout)
            for _ in range(20):
                if nohup_log.exists() and len(nohup_log.read_text(encoding="utf-8").splitlines()) == 2:
                    break
                time.sleep(0.05)
            launches = nohup_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(launches), 2)
            api_launch = next(line for line in launches if "run_api_dev.sh" in line)
            web_launch = next(line for line in launches if "npm --prefix" in line)
            self.assertIn("|5174|", api_launch)
            self.assertIn("http://127.0.0.1:8123|5174|", web_launch)
            self.assertIn("--host localhost --port 5174 --strictPort", web_launch)

    def test_launcher_rejects_an_existing_api_with_stale_cors(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_dir = root / "scripts"
            fake_bin = root / "bin"
            script_dir.mkdir(parents=True)
            fake_bin.mkdir()

            launcher = script_dir / "start_studio.sh"
            launcher.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            launcher.chmod(0o755)
            (root / ".env").write_text("API_PORT=8123\nWEB_PORT=5174\n", encoding="utf-8")
            (fake_bin / "curl").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *\"--request OPTIONS\"* ]]; then\n"
                "  printf 'HTTP/1.1 200 OK\\r\\naccess-control-allow-origin: http://localhost:5173\\r\\n\\r\\n'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$*\" == *\"/src/studioClient.ts\"* ]]; then\n"
                "  printf 'import.meta.env = {\"VITE_API_BASE_URL\": \"http://127.0.0.1:8123\"};'\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fake_bin / "nohup").write_text(
                "#!/usr/bin/env bash\nprintf 'unexpected launch\\n' >> \"$NOHUP_LOG\"\n",
                encoding="utf-8",
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            nohup_log = root / "nohup.log"
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "NOHUP_LOG": str(nohup_log),
                }
            )
            result = subprocess.run(
                ["bash", str(launcher)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("does not allow the configured Web origin", result.stderr)
            self.assertFalse(nohup_log.exists())

    def test_launcher_rejects_an_existing_web_ui_with_a_different_api(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_dir = root / "scripts"
            fake_bin = root / "bin"
            script_dir.mkdir(parents=True)
            fake_bin.mkdir()

            launcher = script_dir / "start_studio.sh"
            launcher.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            launcher.chmod(0o755)
            (root / ".env").write_text("API_PORT=8123\nWEB_PORT=5174\n", encoding="utf-8")
            (fake_bin / "curl").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *\"--request OPTIONS\"* ]]; then\n"
                "  printf 'HTTP/1.1 200 OK\\r\\naccess-control-allow-origin: http://localhost:5174\\r\\n\\r\\n'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$*\" == *\"/src/studioClient.ts\"* ]]; then\n"
                "  printf 'import.meta.env = {\"VITE_API_BASE_URL\": \"http://127.0.0.1:8000\"};'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$*\" == *\"/health\"* ]]; then\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fake_bin / "nohup").write_text(
                "#!/usr/bin/env bash\nprintf 'unexpected launch\\n' >> \"$NOHUP_LOG\"\n",
                encoding="utf-8",
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            nohup_log = root / "nohup.log"
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "NOHUP_LOG": str(nohup_log),
                }
            )
            result = subprocess.run(
                ["bash", str(launcher)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("is not configured to use", result.stderr)
            time.sleep(0.1)
            self.assertFalse(nohup_log.exists())


if __name__ == "__main__":
    unittest.main()
