"""Toolchain version policy: canonical pins, supported floors, and classification.

Classification tests pass the canonical version explicitly so they never
depend on the host toolchain; the pin-file tests check that the default
canonical value is read from ``.python-version`` / ``.nvmrc`` only.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import check_local_setup as policy


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "check_local_setup.py"
# Fixture inputs mirroring the approved policy (Python 3.12 / Node 24 canonical).
PYTHON_PIN = (3, 12)
NODE_PIN = (24,)


class PythonClassificationTests(unittest.TestCase):
    def _status(self, version: tuple[int, int, int]) -> str:
        return policy.classify_python_version(version, canonical=PYTHON_PIN).status

    def test_canonical_minor_passes(self) -> None:
        classification = policy.classify_python_version((3, 12, 4), canonical=PYTHON_PIN)
        self.assertEqual(classification.status, policy.CANONICAL)
        self.assertEqual(classification.level, "OK")
        self.assertFalse(classification.is_blocking)

    def test_migration_versions_warn(self) -> None:
        for version in ((3, 10, 0), (3, 11, 9)):
            with self.subTest(version=version):
                classification = policy.classify_python_version(version, canonical=PYTHON_PIN)
                self.assertEqual(classification.status, policy.SUPPORTED)
                self.assertEqual(classification.level, "WARN")
                self.assertIn("supported for migration, not canonical", classification.summary)

    def test_newer_than_canonical_warns_as_untested(self) -> None:
        self.assertEqual(self._status((3, 13, 0)), policy.UNTESTED)
        self.assertEqual(self._status((3, 14, 2)), policy.UNTESTED)
        self.assertEqual(
            policy.classify_python_version((3, 13, 0), canonical=PYTHON_PIN).level, "WARN"
        )

    def test_below_floor_fails(self) -> None:
        classification = policy.classify_python_version((3, 9, 18), canonical=PYTHON_PIN)
        self.assertEqual(classification.status, policy.UNSUPPORTED)
        self.assertEqual(classification.level, "FAIL")
        self.assertTrue(classification.is_blocking)
        self.assertIn("requires Python 3.10+", classification.summary)


class NodeClassificationTests(unittest.TestCase):
    def _classify(self, version: tuple[int, int, int]) -> policy.ToolchainClassification:
        return policy.classify_node_version(version, canonical=NODE_PIN)

    def test_canonical_major_passes(self) -> None:
        for version in ((24, 0, 0), (24, 11, 1)):
            with self.subTest(version=version):
                self.assertEqual(self._classify(version).level, "OK")

    def test_current_minimums_warn_as_not_canonical(self) -> None:
        for version in ((20, 19, 0), (22, 12, 0), (23, 0, 0)):
            with self.subTest(version=version):
                classification = self._classify(version)
                self.assertEqual(classification.status, policy.SUPPORTED)
                self.assertEqual(classification.level, "WARN")

    def test_newer_majors_warn_as_untested(self) -> None:
        for version in ((25, 0, 0), (26, 1, 0)):
            with self.subTest(version=version):
                classification = self._classify(version)
                self.assertEqual(classification.status, policy.UNTESTED)
                self.assertEqual(classification.level, "WARN")
                self.assertIn("untested", classification.summary)

    def test_below_minimums_fail(self) -> None:
        for version in ((18, 20, 0), (20, 18, 1), (21, 7, 0), (22, 11, 0)):
            with self.subTest(version=version):
                classification = self._classify(version)
                self.assertEqual(classification.status, policy.UNSUPPORTED)
                self.assertEqual(classification.level, "FAIL")
                self.assertIn("requires Node.js 20.19+ or 22.12+", classification.summary)


class CanonicalPinFileTests(unittest.TestCase):
    def test_pin_files_are_the_default_canonical_source(self) -> None:
        with TemporaryDirectory() as temporary:
            python_pin = Path(temporary) / ".python-version"
            node_pin = Path(temporary) / ".nvmrc"
            python_pin.write_text("3.11\n", encoding="utf-8")
            node_pin.write_text("v22\n", encoding="utf-8")
            with patch.object(policy, "PYTHON_VERSION_FILE", python_pin), patch.object(
                policy, "NODE_VERSION_FILE", node_pin
            ):
                self.assertEqual(policy.canonical_python_version(), (3, 11))
                self.assertEqual(policy.canonical_node_version(), (22,))
                self.assertEqual(policy.classify_python_version((3, 11, 2)).level, "OK")
                self.assertEqual(policy.classify_node_version((22, 12, 0)).level, "OK")

    def test_repository_pins_are_numeric_and_above_the_floors(self) -> None:
        python_pin = policy.canonical_python_version()
        node_pin = policy.canonical_node_version()

        self.assertEqual(len(python_pin), 2)
        self.assertEqual(len(node_pin), 1)
        self.assertEqual(
            policy.classify_python_version((*python_pin, 0)).status, policy.CANONICAL
        )
        self.assertEqual(policy.classify_node_version((*node_pin, 0, 0)).status, policy.CANONICAL)

    def test_non_numeric_pin_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            pin = Path(temporary) / ".nvmrc"
            pin.write_text("lts/*\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                policy.read_canonical_version(pin)


class ToolchainOnlyCliTests(unittest.TestCase):
    def _run(self, python_version: str, node_version: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--toolchain-only",
                "--python-version",
                python_version,
                "--node-version",
                node_version,
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def test_supported_non_canonical_versions_exit_zero_with_warnings(self) -> None:
        result = self._run("Python 3.10.0", "v20.19.0")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("[WARN] Python version: 3.10.0", result.stdout)
        self.assertIn("[WARN] Node.js version: v20.19.0", result.stdout)

    def test_unsupported_versions_exit_one(self) -> None:
        result = self._run("Python 3.9.6", "v18.20.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("[FAIL] Python version: 3.9.6", result.stdout)
        self.assertIn("[FAIL] Node.js version: v18.20.0", result.stdout)

    def test_unparseable_python_version_fails(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(policy.check_toolchain("Python", "v20.19.0"))
        self.assertIn("could not be parsed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
