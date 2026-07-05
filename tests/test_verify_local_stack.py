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
