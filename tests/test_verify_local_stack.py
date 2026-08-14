"""Tests for the local verification script helpers."""

from __future__ import annotations

import http.server
import importlib.util
import json
import socketserver
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlparse


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_local_stack.py"
SPEC = importlib.util.spec_from_file_location("verify_local_stack", SCRIPT_PATH)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

SETUP_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_local_setup.py"
SETUP_SPEC = importlib.util.spec_from_file_location("check_local_setup", SETUP_SCRIPT_PATH)
assert SETUP_SPEC is not None
SETUP_MODULE = importlib.util.module_from_spec(SETUP_SPEC)
assert SETUP_SPEC.loader is not None
SETUP_SPEC.loader.exec_module(SETUP_MODULE)


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    health_payload: object = {"status": "ok"}
    models_payload: object = {"models": []}
    job_payload: object = {"id": "job_smoke", "status": "succeeded"}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(self.health_payload)
            return
        if parsed.path == "/models":
            self._write_json(self.models_payload)
            return
        if parsed.path == "/jobs/job_smoke":
            self._write_json(self.job_payload)
            return
        if parsed.path == "/gallery":
            self._write_json([{"job_id": "job_smoke"}])
            return
        if parsed.path == "/projects/project_smoke/jobs":
            self._write_json({"jobs": [{"id": "job_smoke"}]})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/projects":
            self._write_json({"id": "project_smoke"}, status_code=201)
            return
        if parsed.path == "/generate/video":
            self._write_json({"job_id": "job_smoke", "status": "queued"}, status_code=201)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _write_json(self, payload: object, *, status_code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class VerifyLocalStackTests(unittest.TestCase):
    def _serve(self, handler: type[_JsonHandler]) -> tuple[socketserver.TCPServer, str]:
        server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        return server, f"http://{host}:{port}"

    def test_run_api_smoke_checks_accepts_expected_payloads(self) -> None:
        class Handler(_JsonHandler):
            health_payload = {"status": "ok"}
            models_payload = {"models": [{"id": "sdxl"}]}

        server, base_url = self._serve(Handler)
        try:
            MODULE.run_api_smoke_checks(base_url, timeout=2.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_run_api_smoke_checks_rejects_invalid_models_payload(self) -> None:
        class Handler(_JsonHandler):
            health_payload = {"status": "ok"}
            models_payload = {"items": []}

        server, base_url = self._serve(Handler)
        try:
            with self.assertRaises(MODULE.VerificationError):
                MODULE.run_api_smoke_checks(base_url, timeout=2.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_build_api_command_uses_base_url_host_and_port(self) -> None:
        command, env = MODULE.build_api_command("http://127.0.0.1:8123")

        self.assertEqual(command[-4:], ["--host", "127.0.0.1", "--port", "8123"])
        self.assertEqual(env["API_HOST"], "127.0.0.1")
        self.assertEqual(env["API_PORT"], "8123")

    def test_isolated_runtime_environment_creates_clean_checkout_directories(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            runtime_root = Path(tmp_dir) / "runtime"

            env = MODULE.build_isolated_environment(runtime_root, base_env={})

            self.assertEqual(Path(env["DB_PATH"]), runtime_root / "data" / "jobs.db")
            self.assertEqual(Path(env["OUTPUT_DIR"]), runtime_root / "outputs")
            self.assertTrue((runtime_root / "data").is_dir())
            self.assertTrue((runtime_root / "outputs" / "images").is_dir())
            self.assertTrue((runtime_root / "outputs" / "audio").is_dir())
            self.assertTrue((runtime_root / "outputs" / "videos").is_dir())

    def test_standard_verification_runs_web_tests(self) -> None:
        commands: list[tuple[list[str], Path]] = []

        def _record_command(
            command: list[str],
            *,
            cwd: Path = MODULE.ROOT,
            env: dict[str, str] | None = None,
        ) -> None:
            del env
            commands.append((command, cwd))

        with (
            TemporaryDirectory() as tmp_dir,
            patch.object(MODULE, "run_command", side_effect=_record_command),
            patch.object(MODULE, "run_api_smoke_checks"),
            patch.object(
                MODULE,
                "parse_args",
                return_value=MODULE.argparse.Namespace(
                    skip_setup_check=True,
                    skip_web_build=False,
                    skip_web_tests=False,
                    skip_tests=True,
                    skip_api_smoke=True,
                    skip_npm_audit=True,
                    skip_eslint=True,
                    skip_ruff=True,
                    skip_mypy=True,
                    skip_coverage=True,
                    start_api=False,
                    api_base_url="http://127.0.0.1:8123",
                    api_timeout=1.0,
                    check_runtime_files=False,
                    runtime_root=Path(tmp_dir),
                ),
            ),
        ):
            self.assertEqual(MODULE.main(), 0)

        self.assertIn((["npm", "test"], MODULE.ROOT / "apps" / "web"), commands)

    def test_standard_verification_runs_npm_audit(self) -> None:
        commands: list[tuple[list[str], Path]] = []

        def _record_command(
            command: list[str],
            *,
            cwd: Path = MODULE.ROOT,
            env: dict[str, str] | None = None,
        ) -> None:
            del env
            commands.append((command, cwd))

        with (
            TemporaryDirectory() as tmp_dir,
            patch.object(MODULE, "run_command", side_effect=_record_command),
            patch.object(MODULE, "run_api_smoke_checks"),
            patch.object(
                MODULE,
                "parse_args",
                return_value=MODULE.argparse.Namespace(
                    skip_setup_check=True,
                    skip_web_build=True,
                    skip_web_tests=True,
                    skip_tests=True,
                    skip_api_smoke=True,
                    skip_npm_audit=False,
                    skip_eslint=True,
                    skip_ruff=True,
                    skip_mypy=True,
                    skip_coverage=True,
                    start_api=False,
                    api_base_url="http://127.0.0.1:8123",
                    api_timeout=1.0,
                    check_runtime_files=False,
                    runtime_root=Path(tmp_dir),
                ),
            ),
        ):
            self.assertEqual(MODULE.main(), 0)

        self.assertIn(
            (["npm", "audit", "--audit-level=high"], MODULE.ROOT / "apps" / "web"),
            commands,
        )

    def _run_with_only(self, tmp_dir: str, **enabled_flags: bool) -> list[tuple[list[str], Path]]:
        commands: list[tuple[list[str], Path]] = []

        def _record_command(
            command: list[str],
            *,
            cwd: Path = MODULE.ROOT,
            env: dict[str, str] | None = None,
        ) -> None:
            del env
            commands.append((command, cwd))

        all_skip_flags = {
            "skip_setup_check": True,
            "skip_web_build": True,
            "skip_web_tests": True,
            "skip_tests": True,
            "skip_api_smoke": True,
            "skip_npm_audit": True,
            "skip_eslint": True,
            "skip_ruff": True,
            "skip_mypy": True,
            "skip_coverage": True,
        }
        for name, enabled in enabled_flags.items():
            all_skip_flags[f"skip_{name}"] = not enabled

        with (
            patch.object(MODULE, "run_command", side_effect=_record_command),
            patch.object(MODULE, "run_api_smoke_checks"),
            patch.object(
                MODULE,
                "parse_args",
                return_value=MODULE.argparse.Namespace(
                    start_api=False,
                    api_base_url="http://127.0.0.1:8123",
                    api_timeout=1.0,
                    check_runtime_files=False,
                    runtime_root=Path(tmp_dir),
                    **all_skip_flags,
                ),
            ),
        ):
            self.assertEqual(MODULE.main(), 0)
        return commands

    def test_standard_verification_runs_eslint(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            commands = self._run_with_only(tmp_dir, eslint=True)
        self.assertIn((["npm", "run", "lint"], MODULE.ROOT / "apps" / "web"), commands)

    def test_standard_verification_runs_ruff(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            commands = self._run_with_only(tmp_dir, ruff=True)
        self.assertIn(
            ([MODULE.venv_executable("ruff"), "check", "core", "generators"], MODULE.ROOT),
            commands,
        )

    def test_standard_verification_runs_mypy(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            commands = self._run_with_only(tmp_dir, mypy=True)
        self.assertIn(
            ([MODULE.venv_executable("mypy"), "core", "generators"], MODULE.ROOT),
            commands,
        )

    def test_standard_verification_runs_coverage(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            commands = self._run_with_only(tmp_dir, coverage=True)
        self.assertIn(
            (
                [MODULE.venv_python(), "-m", "pytest", "--cov=core", "--cov=generators", "-q"],
                MODULE.ROOT,
            ),
            commands,
        )

    def test_python_version_check_rejects_unsupported_runtime(self) -> None:
        self.assertFalse(SETUP_MODULE._check_python_version((3, 9, 18)))
        self.assertTrue(SETUP_MODULE._check_python_version((3, 10, 0)))

    def test_node_version_check_rejects_unsupported_runtime(self) -> None:
        self.assertFalse(SETUP_MODULE._check_node_version("v20.18.1"))
        self.assertTrue(SETUP_MODULE._check_node_version("v20.19.0"))
        self.assertTrue(SETUP_MODULE._check_node_version("v22.12.0"))

    def test_check_manifest_files_rejects_duplicate_ids(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            manifest_root = Path(tmp_dir)
            first = manifest_root / "first.json"
            second = manifest_root / "second.json"
            payload = {
                "id": "duplicate-local",
                "public_id": "duplicate",
                "display_name": "Duplicate",
            }
            first.write_text(json.dumps(payload), encoding="utf-8")
            second.write_text(json.dumps(payload), encoding="utf-8")

            self.assertFalse(SETUP_MODULE._check_manifest_files(manifest_root))

    def test_check_manifest_files_rejects_duplicate_public_aliases(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            manifest_root = Path(tmp_dir)
            (manifest_root / "first.json").write_text(
                json.dumps(
                    {
                        "id": "first-local",
                        "public_id": "first",
                        "aliases": ["shared"],
                    }
                ),
                encoding="utf-8",
            )
            (manifest_root / "second.json").write_text(
                json.dumps(
                    {
                        "id": "second-local",
                        "public_id": "second",
                        "aliases": ["shared"],
                    }
                ),
                encoding="utf-8",
            )

            self.assertFalse(SETUP_MODULE._check_manifest_files(manifest_root))


if __name__ == "__main__":
    unittest.main()
