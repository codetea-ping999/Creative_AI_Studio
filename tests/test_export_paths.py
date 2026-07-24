"""Tests for export destination validation."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

try:
    from fastapi import HTTPException

    from apps.api.export_paths import (
        exports_root,
        resolve_export_dir,
        sanitize_export_name,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    IMPORT_ERROR: Exception | None = exc
else:
    IMPORT_ERROR = None


def _services(root: Path) -> SimpleNamespace:
    # resolve_export_dir only needs ``output_dir``; exports root is its parent.
    return SimpleNamespace(output_dir=root / "outputs" / "images")


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ExportPathValidationTests(unittest.TestCase):
    def test_default_subpath_lands_under_exports_root(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            services = _services(Path(tmp_dir))
            resolved = resolve_export_dir(services, None, default_subpath="video")
            self.assertEqual(resolved, exports_root(services) / "video")

    def test_relative_dir_is_anchored_to_exports_root(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            services = _services(Path(tmp_dir))
            resolved = resolve_export_dir(services, "batch-01", default_subpath="image")
            self.assertEqual(resolved, exports_root(services) / "batch-01")

    def test_absolute_dir_outside_root_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            services = _services(Path(tmp_dir))
            with self.assertRaises(HTTPException) as ctx:
                resolve_export_dir(
                    services,
                    str(Path(tmp_dir) / "escape"),
                    default_subpath="image",
                )
            self.assertEqual(ctx.exception.status_code, 400)

    def test_traversal_dir_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            services = _services(Path(tmp_dir))
            with self.assertRaises(HTTPException) as ctx:
                resolve_export_dir(services, "../../etc", default_subpath="image")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_sanitize_name_allows_bare_file_name(self) -> None:
        self.assertEqual(sanitize_export_name("render.png"), "render.png")
        self.assertIsNone(sanitize_export_name(None))

    def test_sanitize_name_rejects_paths_and_traversal(self) -> None:
        for bad in ("../secret", "nested/name.png", "..", ".", ""):
            with self.subTest(name=bad):
                with self.assertRaises(HTTPException) as ctx:
                    sanitize_export_name(bad)
                self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
