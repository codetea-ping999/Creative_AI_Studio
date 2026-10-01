"""Unit coverage for the third-party notices collector.

The collector walks ``node_modules`` and reads package metadata. Both steps
have quiet failure modes that would produce a *plausible but wrong* legal
notice: counting a package's internal ``esm/package.json`` as a dependency, or
reporting no license for a package that declares one in an older format. The
walking and license-reading rules are pure functions, so they are covered here
rather than only by looking at the generated file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "collect_third_party_notices.py"


def _load_collector():
    spec = importlib.util.spec_from_file_location("collect_third_party_notices", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so dataclasses can resolve the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


class PackageRootTests(unittest.TestCase):
    def test_accepts_package_roots(self) -> None:
        for path in (
            "react/package.json",
            "@types/node/package.json",
            "foo/node_modules/bar/package.json",
            "foo/node_modules/@scope/bar/package.json",
            "a/node_modules/b/node_modules/c/package.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(collector._is_package_root(Path(path)))

    def test_rejects_manifests_inside_a_package(self) -> None:
        for path in (
            "foo/esm/package.json",           # {"type": "module"} marker
            "@babel/core/lib/package.json",   # build output
            "foo/test/fixtures/package.json",  # test fixture
            "foo/node_modules/bar/dist/package.json",
        ):
            with self.subTest(path=path):
                self.assertFalse(collector._is_package_root(Path(path)))


class NodeLicenseTests(unittest.TestCase):
    def test_reads_a_plain_license_string(self) -> None:
        self.assertEqual(collector._node_license({"license": "MIT"}), "MIT")

    def test_reads_the_legacy_object_form(self) -> None:
        self.assertEqual(
            collector._node_license({"license": {"type": "Apache-2.0"}}), "Apache-2.0"
        )

    def test_reads_the_legacy_list_form(self) -> None:
        self.assertEqual(
            collector._node_license({"licenses": [{"type": "MIT"}, {"type": "GPL-2.0"}]}),
            "MIT, GPL-2.0",
        )

    def test_reports_unknown_rather_than_guessing(self) -> None:
        self.assertEqual(collector._node_license({}), collector.UNKNOWN_LICENSE)


if __name__ == "__main__":
    unittest.main()
