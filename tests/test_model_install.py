"""Tests for placing already-downloaded model folders (core + API)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import folder_picker
from core.model_install import (
    DestinationNotEmptyError,
    IncompleteSourceError,
    InstallNotSupportedError,
    InvalidSourceError,
    install_destination,
    install_from_local_folder,
)
from tests.test_api_models import (
    _COGVIDEOX_INDEX,
    _DIFFUSERS_INDEX,
    IMPORT_ERROR,
    _write_diffusers_pipeline,
    _write_manifest,
)

if IMPORT_ERROR is None:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services


def _diffusers_kwargs(destination: Path) -> dict[str, object]:
    return {
        "runtime": "diffusers",
        "local_path": str(destination),
        "remote_ref": None,
        "default_params": {},
        "manifest_id": "sdxl-local",
    }


class InstallDestinationTests(unittest.TestCase):
    def test_diffusers_models_install_into_local_path(self) -> None:
        self.assertEqual(
            install_destination("diffusers", "/models/image/sdxl", {}),
            Path("/models/image/sdxl").resolve(),
        )

    def test_learned_runtime_installs_into_pipeline_path(self) -> None:
        destination = install_destination(
            "learned", "./models/video/learned-runtime", {"pipeline_path": "/w/cogvideox-2b"}
        )
        self.assertEqual(destination, Path("/w/cogvideox-2b").resolve())

    def test_endpoint_models_have_no_destination(self) -> None:
        self.assertIsNone(install_destination("voicevox_http", None, {}))


class InstallFromLocalFolderTests(unittest.TestCase):
    def test_copies_a_complete_model_and_follows_symlinks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            # Hugging Face cache layout: the snapshot files are links into blobs/.
            blob = root / "blobs" / "weights"
            blob.parent.mkdir()
            blob.write_bytes(b"real-bytes")
            linked = source / "unet" / "model.safetensors"
            linked.unlink()
            linked.symlink_to(blob)
            destination = root / "models" / "image" / "sdxl"

            result = install_from_local_folder(
                source_path=str(source), **_diffusers_kwargs(destination)
            )

            self.assertTrue(result.readiness.is_ready)
            installed = destination / "unet" / "model.safetensors"
            self.assertFalse(installed.is_symlink())
            self.assertEqual(installed.read_bytes(), b"real-bytes")
            self.assertEqual(
                [p.name for p in destination.parent.iterdir()], ["sdxl"], "no staging leftovers"
            )

    def test_finds_the_model_inside_the_chosen_parent_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "downloads" / "hub" / "snapshots" / "abc"
            _write_diffusers_pipeline(snapshot, _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"

            result = install_from_local_folder(
                source_path=str(root / "downloads" / "hub"),
                **_diffusers_kwargs(destination),
            )

            self.assertTrue(result.readiness.is_ready)
            self.assertEqual(result.source, snapshot.resolve())

    def test_incomplete_source_is_rejected_before_copying(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / "unet" / "model.safetensors").unlink()
            destination = root / "models" / "sdxl"

            with self.assertRaises(IncompleteSourceError) as raised:
                install_from_local_folder(source_path=str(source), **_diffusers_kwargs(destination))

            self.assertIn("unet", str(raised.exception))
            self.assertFalse(destination.exists())

    def test_existing_files_need_replace_and_are_moved_aside(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "partial.bin").write_bytes(b"old")
            kwargs = _diffusers_kwargs(destination)

            with self.assertRaises(DestinationNotEmptyError):
                install_from_local_folder(source_path=str(source), **kwargs)
            self.assertTrue((destination / "partial.bin").exists())

            result = install_from_local_folder(source_path=str(source), replace=True, **kwargs)

            self.assertIsNotNone(result.replaced_to)
            assert result.replaced_to is not None
            self.assertEqual((result.replaced_to / "partial.bin").read_bytes(), b"old")
            self.assertTrue((destination / "model_index.json").exists())

    def test_empty_destination_directory_is_replaced_without_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)

            result = install_from_local_folder(
                source_path=str(source), **_diffusers_kwargs(destination)
            )

            self.assertIsNone(result.replaced_to)
            self.assertTrue(result.readiness.is_ready)

    def test_rejects_relative_missing_and_overlapping_sources(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = _write_diffusers_pipeline(root / "models" / "sdxl", _DIFFUSERS_INDEX)
            kwargs = _diffusers_kwargs(destination)

            for bad in ("relative/path", str(root / "missing"), str(destination)):
                with self.subTest(source=bad), self.assertRaises(InvalidSourceError):
                    install_from_local_folder(source_path=bad, **kwargs)
            with self.assertRaises(InvalidSourceError):
                install_from_local_folder(source_path=str(root), **kwargs)  # contains destination

    def test_learned_runtime_places_weights_under_pipeline_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / "adapter"
            adapter.mkdir()
            (adapter / "runtime.py").write_text("", encoding="utf-8")
            source = _write_diffusers_pipeline(root / "downloads" / "cog", _COGVIDEOX_INDEX)
            pipeline = root / "models" / "cogvideox-2b"

            result = install_from_local_folder(
                source_path=str(source),
                runtime="learned",
                local_path=str(adapter),
                remote_ref=None,
                default_params={"entrypoint": "runtime.py", "pipeline_path": str(pipeline)},
                manifest_id="learned-video-local",
            )

            self.assertEqual(result.destination, pipeline.resolve())
            self.assertTrue(result.readiness.is_ready)

    def test_endpoint_models_cannot_receive_files(self) -> None:
        with self.assertRaises(InstallNotSupportedError):
            install_from_local_folder(
                source_path="/tmp",
                runtime="voicevox_http",
                local_path=None,
                remote_ref="http://127.0.0.1:50021",
                default_params={},
                manifest_id="voicevox",
            )


class FolderPickerTests(unittest.TestCase):
    def test_builds_the_native_command_for_each_platform(self) -> None:
        mac = folder_picker.build_picker_command("darwin")
        self.assertEqual(mac[0], "osascript")
        self.assertIn("choose folder", mac[-1])
        windows = folder_picker.build_picker_command("win32")
        self.assertEqual(windows[:3], ["powershell", "-NoProfile", "-STA"])
        self.assertIn("FolderBrowserDialog", windows[-1])

    def test_prompt_cannot_inject_script_syntax(self) -> None:
        command = folder_picker.build_picker_command("darwin", 'x" & do shell script "id')
        self.assertNotIn('"id', command[-1])
        self.assertNotIn('" &', command[-1])

    def test_linux_without_a_dialog_tool_is_unavailable(self) -> None:
        with patch.object(folder_picker.shutil, "which", return_value=None):
            with self.assertRaises(folder_picker.FolderPickerUnavailableError):
                folder_picker.build_picker_command("linux")

    def test_returns_the_selected_path_and_none_when_cancelled(self) -> None:
        def run(stdout: str, code: int) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")

        selected = run("/Users/a/model/\n", 0)
        with patch.object(folder_picker.subprocess, "run", return_value=selected):
            self.assertEqual(folder_picker.pick_folder(platform="darwin"), "/Users/a/model/")
        with patch.object(folder_picker.subprocess, "run", return_value=run("", 1)):
            self.assertIsNone(folder_picker.pick_folder(platform="darwin"))
        with patch.object(folder_picker.subprocess, "run", return_value=run("", 0)):
            self.assertIsNone(folder_picker.pick_folder(platform="win32"))


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ModelInstallApiTests(unittest.TestCase):
    def _client(self, root: Path, destination: Path) -> TestClient:
        manifest_root = root / "manifests"
        _write_manifest(
            manifest_root / "image" / "custom-sdxl.json",
            {
                "id": "custom-sdxl-local",
                "public_id": "custom-sdxl",
                "display_name": "Custom SDXL",
                "media_type": "image",
                "task_type": "text-to-image",
                "provider": "local",
                "runtime": "diffusers",
                "local_path": str(destination),
                "loader": "diffusers_image_loader",
                "default_params": {},
                "is_default": True,
                "enabled": True,
            },
        )
        services = create_application_services(
            manifest_root=manifest_root,
            db_path=root / "jobs.db",
            output_dir=root / "outputs" / "images",
        )
        return TestClient(create_app(services, start_job_runner=False))

    def test_install_makes_the_model_available(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            client = self._client(root, destination)
            before = client.get("/models").json()["models"][0]
            self.assertFalse(before["is_available"])
            self.assertEqual(before["install_path"], str(destination.resolve()))

            response = client.post(
                "/models/custom-sdxl/install",
                json={"source_path": str(source), "media_type": "image"},
            )
            after = client.get("/models").json()["models"][0]

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["model_id"], "custom-sdxl")
        self.assertTrue(body["is_available"])
        self.assertTrue(after["is_available"])

    def test_errors_carry_a_code_and_the_right_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / "vae" / "model.safetensors").unlink()
            destination = root / "models" / "sdxl"
            client = self._client(root, destination)

            incomplete = client.post(
                "/models/custom-sdxl/install",
                json={"source_path": str(source), "media_type": "image"},
            )
            unknown = client.post(
                "/models/nope/install",
                json={"source_path": str(source), "media_type": "image"},
            )

        self.assertEqual(incomplete.status_code, 422)
        self.assertEqual(incomplete.json()["detail"]["code"], "incomplete_source")
        self.assertTrue(incomplete.json()["detail"]["missing"])
        self.assertEqual(unknown.status_code, 404)

    def test_non_local_clients_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")
            remote = TestClient(client.app, client=("203.0.113.9", 50000))

            picked = remote.post("/models/pick-folder")
            installed = remote.post(
                "/models/custom-sdxl/install",
                json={"source_path": str(root), "media_type": "image"},
            )

        self.assertEqual(picked.status_code, 403)
        self.assertEqual(installed.status_code, 403)

    def test_pick_folder_endpoint_returns_the_chosen_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")
            with patch("apps.api.routes.model_install.pick_folder", return_value="/Users/a/sdxl"):
                chosen = client.post("/models/pick-folder")
            with patch("apps.api.routes.model_install.pick_folder", return_value=None):
                cancelled = client.post("/models/pick-folder")
            with patch(
                "apps.api.routes.model_install.pick_folder",
                side_effect=folder_picker.FolderPickerUnavailableError("no dialog"),
            ):
                unavailable = client.post("/models/pick-folder")

        self.assertEqual(chosen.json(), {"path": "/Users/a/sdxl"})
        self.assertEqual(cancelled.json(), {"path": None})
        self.assertEqual(unavailable.status_code, 501)


if __name__ == "__main__":
    unittest.main()
