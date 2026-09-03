"""API contract tests for the CreativeStudio remote agent handshake."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

IMPORT_ERROR: Exception | None = None

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class AgentApiTests(unittest.TestCase):
    def test_agent_info_returns_stable_v1_handshake(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                manifest_root=root / "manifests",
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            first = client.get("/v1/agent/info")
            second = client.get("/v1/agent/info")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        payload = first.json()
        self.assertEqual(
            set(payload),
            {"protocol_version", "agent_version", "instance_id", "capabilities"},
        )
        self.assertEqual(payload["protocol_version"], "1")
        self.assertTrue(payload["agent_version"])
        self.assertTrue(payload["instance_id"])
        self.assertEqual(payload["capabilities"], sorted(payload["capabilities"]))
        self.assertEqual(second.json(), payload)


if __name__ == "__main__":
    unittest.main()
