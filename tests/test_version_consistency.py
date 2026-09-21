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


class ReleaseToolingUsesTheVersionFileTests(unittest.TestCase):
    """The build and smoke scripts must derive the version, not restate it."""

    def test_build_script_reads_the_version_file(self) -> None:
        body = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('< "${ROOT_DIR}/VERSION"', body)

    def test_smoke_script_reads_the_version_file(self) -> None:
        body = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("from core.version import get_release_tag", body)


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
