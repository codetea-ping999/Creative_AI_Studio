"""The release version has exactly one source of truth.

Gate 5 of the release checklist requires tag/artifact/version consistency. That
is only checkable if every place the version appears is derived from, or
verified against, the repository-root ``VERSION`` file. This module is the gate
that keeps the copies from drifting.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from core.version import get_base_version, get_release_tag, get_version


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPOSITORY_ROOT / "VERSION"
WEB_PACKAGE_JSON = REPOSITORY_ROOT / "apps" / "web" / "package.json"
WEB_PACKAGE_LOCK = REPOSITORY_ROOT / "apps" / "web" / "package-lock.json"
DESKTOP_CONFIG = REPOSITORY_ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json"
CHANGELOG = REPOSITORY_ROOT / "CHANGELOG.md"
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts" / "build_release_artifact.sh"
SMOKE_SCRIPT = REPOSITORY_ROOT / "scripts" / "smoke_release_artifact.py"


class VersionSourceOfTruthTests(unittest.TestCase):
    def test_version_file_holds_a_single_semantic_version(self) -> None:
        raw = VERSION_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            raw,
            raw.strip() + "\n",
            "VERSION must be one line with a trailing newline and no padding",
        )
        self.assertRegex(raw.strip(), r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

    def test_release_tag_is_the_v_prefixed_version(self) -> None:
        self.assertEqual(get_release_tag(), f"v{get_version()}")

    def test_base_version_drops_the_pre_release_suffix(self) -> None:
        self.assertEqual(get_base_version(), get_version().split("-", 1)[0])

    def test_web_package_version_matches(self) -> None:
        # The base version, so cutting 1.0.0-rc.1 does not require editing
        # package.json and its lockfile: nothing at release time reads them.
        package = json.loads(WEB_PACKAGE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(
            package["version"],
            get_base_version(),
            "apps/web/package.json is out of sync with VERSION",
        )

    def test_web_lockfile_version_matches(self) -> None:
        lock = json.loads(WEB_PACKAGE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(lock.get("version"), get_base_version())
        self.assertEqual(
            lock.get("packages", {}).get("", {}).get("version"), get_base_version()
        )

    def test_desktop_bundle_version_matches_when_present(self) -> None:
        # The desktop shell lands separately (PR #409); assert only when it is
        # actually in the tree so this gate does not depend on merge order.
        if not DESKTOP_CONFIG.exists():
            self.skipTest("desktop shell is not part of this tree")
        config = json.loads(DESKTOP_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(config["version"], get_base_version())


class ApiReportsTheVersionTests(unittest.TestCase):
    """Every version an API client can read agrees with VERSION.

    ``GET /version`` is the explicit one, but the OpenAPI schema carries a
    version too, and FastAPI defaults it to "0.1.0" when none is passed. A
    client reading /openapi.json or the Swagger UI would then see a different
    release than the one actually running.
    """

    def _client(self):
        from fastapi.testclient import TestClient

        from apps.api.main import create_app

        return TestClient(create_app(start_job_runner=False))

    def test_version_endpoint_reports_the_version_file(self) -> None:
        with self._client() as client:
            payload = client.get("/version").json()
        self.assertEqual(payload["version"], get_version())
        self.assertEqual(payload["release_tag"], get_release_tag())

    def test_openapi_schema_reports_the_same_version(self) -> None:
        with self._client() as client:
            schema = client.get("/openapi.json").json()
        self.assertEqual(
            schema["info"]["version"],
            get_version(),
            "/openapi.json and the Swagger UI would report a different release",
        )


class ReleaseToolingUsesTheVersionFileTests(unittest.TestCase):
    """The build and smoke scripts must derive the version, not restate it."""

    def test_build_script_reads_the_version_file(self) -> None:
        body = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('< "${ROOT_DIR}/VERSION"', body)

    def test_smoke_script_reads_the_version_file(self) -> None:
        body = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("from core.version import get_release_tag", body)


class ChecksumFileTests(unittest.TestCase):
    """SHA256SUMS must be verifiable by whoever downloads the release."""

    def test_checksum_names_the_artifact_not_the_build_machines_path(self) -> None:
        # docs/release/install-from-artifact.md tells the downloader to run
        # `sha256sum -c SHA256SUMS` next to the tarball. sha256sum resolves the
        # name in the file against the current directory, so a line naming the
        # build runner's absolute path fails with "No such file or directory"
        # for everyone but the runner.
        body = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            '(cd "${OUT_DIR}" && sha256_file "${ARTIFACT_NAME}.tar.gz")',
            body,
            "SHA256SUMS must be written from inside the artifacts directory "
            "so it records a bare filename",
        )
        self.assertNotIn(
            'sha256_file "${OUT_DIR}/${ARTIFACT_NAME}.tar.gz"',
            body,
            "passing an absolute path to sha256_file puts that path in SHA256SUMS",
        )


class ChangelogTests(unittest.TestCase):
    def test_changelog_documents_the_current_version(self) -> None:
        # The changelog itself lands in PR #433; assert against it only once it
        # is in the tree so this gate does not depend on merge order.
        if not CHANGELOG.exists():
            self.skipTest("CHANGELOG.md is not part of this tree yet")
        # The base version: a release candidate documents the same contents as
        # the release it is a candidate for, so 1.0.0-rc.1 looks for "## [1.0.0]".
        body = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn(
            f"## [{get_base_version()}]",
            body,
            "CHANGELOG.md has no section for the version in VERSION",
        )


if __name__ == "__main__":
    unittest.main()
