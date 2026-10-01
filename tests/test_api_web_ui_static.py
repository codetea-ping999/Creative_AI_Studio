"""Prebuilt web UI static serving contract for the release runtime path.

The frozen v1.0 artifact runs without Node: FastAPI serves the prebuilt
``apps/web/dist`` from the same origin as the API. These tests pin the
coexistence contract between that root static mount and every existing API
route.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from starlette.routing import Mount
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    from apps.api.main import create_app
    from bootstrap import create_application_services
except ModuleNotFoundError as exc:
    IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class WebUiStaticServingTests(unittest.TestCase):
    def _client_with_dist(
        self,
        root: Path,
        dist_entries: dict[str, str],
    ) -> tuple[TestClient, Path]:
        dist = root / "apps/web"
        dist.mkdir(parents=True, exist_ok=True)
        for relative_path, content in dist_entries.items():
            target = dist / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        services = create_application_services(
            db_path=root / "jobs.db",
            output_dir=root / "outputs/images",
        )
        client = TestClient(create_app(services, start_job_runner=False, web_dist_dir=dist))
        return client, dist

    def test_app_can_be_created_and_started_without_a_web_dist(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs/images",
            )
            client = TestClient(
                create_app(services, start_job_runner=False, web_dist_dir=root / "missing-dist")
            )

            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})

    def test_root_serves_index_html_and_static_assets_from_the_dist(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            client, _ = self._client_with_dist(
                root,
                {
                    "index.html": "<!doctype html><title>Release UI 1.0</title>",
                    "assets/app.js": "console.log('release');",
                },
            )

            index_response = client.get("/")
            self.assertEqual(index_response.status_code, 200)
            self.assertIn("Release UI 1.0", index_response.text)

            asset_response = client.get("/assets/app.js")
            self.assertEqual(asset_response.status_code, 200)
            self.assertEqual(asset_response.text, "console.log('release');")

    def test_api_routes_are_not_shadowed_by_the_root_mount(self) -> None:
        # Deliberately hostile fixtures: paths that would collide with real
        # API routes if the root mount were ever registered before them.
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            client, _ = self._client_with_dist(
                root,
                {
                    "index.html": "<!doctype html><title>Release UI</title>",
                    "health": "this file must never be served as /health",
                    "jobs/index.html": "<!doctype html><title>wrong jobs page</title>",
                    "outputs/index.html": "<!doctype html><title>wrong outputs page</title>",
                },
            )

            health_response = client.get("/health")
            self.assertEqual(health_response.status_code, 200)
            self.assertEqual(health_response.json(), {"status": "ok"})

            jobs_response = client.get("/jobs")
            self.assertEqual(jobs_response.status_code, 200)
            self.assertNotIn("wrong jobs page", jobs_response.text)

    def test_outputs_mount_keeps_precedence_over_the_root_mount(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            client, _ = self._client_with_dist(
                root,
                {"index.html": "<!doctype html><title>Release UI</title>"},
            )
            output_root = root / "outputs"
            output_root.mkdir(parents=True, exist_ok=True)
            (output_root / "probe.txt").write_text("output-probe", encoding="utf-8")

            probe_response = client.get("/outputs/probe.txt")
            self.assertEqual(probe_response.status_code, 200)
            self.assertEqual(probe_response.text, "output-probe")

    def test_root_static_mount_is_registered_after_api_routes(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dist = root / "apps/web"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs/images",
            )
            app: FastAPI = create_app(
                services,
                start_job_runner=False,
                web_dist_dir=dist,
            )

            routes = app.routes
            root_mount = routes[-1]
            self.assertIsInstance(root_mount, Mount)
            assert isinstance(root_mount, Mount)
            self.assertEqual(root_mount.name, "web")
            self.assertEqual(root_mount.path_format, "/{path}")

    def test_dist_without_index_html_skips_the_mount(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs/images",
            )
            client = TestClient(
                create_app(
                    services,
                    start_job_runner=False,
                    web_dist_dir=root / "apps/web",
                )
            )

            self.assertEqual(client.get("/").status_code, 404)
            self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()