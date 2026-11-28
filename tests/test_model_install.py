"""Tests for placing already-downloaded model folders (core + API)."""

from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import folder_picker
from core import model_install
from core.model_install import (
    DestinationNotEmptyError,
    IncompatibleTrackedFileError,
    IncompleteSourceError,
    InstallBusyError,
    InstallNotSupportedError,
    InsufficientSpaceError,
    InvalidSourceError,
    ModelInUseError,
    install_destination,
    install_from_local_folder,
    remove_stale_staging,
)
from core.models import RuntimeBusyError
from tests.test_api_models import (
    _COGVIDEOX_INDEX,
    _DIFFUSERS_INDEX,
    IMPORT_ERROR,
    _write_diffusers_pipeline,
    _write_manifest,
)

if IMPORT_ERROR is None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services

    def _local_client(app: FastAPI, host: str = "127.0.0.1") -> TestClient:
        """A client that looks like the user's own browser talking to a loopback API."""

        return TestClient(app, base_url="http://127.0.0.1:8000", client=(host, 50000))


def _completed(stdout: str, code: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def _no_tracked_files(_directory: Path) -> frozenset[str]:
    return frozenset()


def _diffusers_kwargs(destination: Path) -> dict[str, object]:
    return {
        "runtime": "diffusers",
        "local_path": str(destination),
        "remote_ref": None,
        "default_params": {},
        "manifest_id": "sdxl-local",
        "tracked_files": _no_tracked_files,
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
            repo = root / "hub" / "models--stabilityai--sdxl"
            source = _write_diffusers_pipeline(repo / "snapshots" / "abc123", _DIFFUSERS_INDEX)
            # Hugging Face cache layout: the snapshot files are links into blobs/.
            blob = repo / "blobs" / "weights"
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


def _kwargs(runtime: str, destination: Path, **params: object) -> dict[str, object]:
    return {
        "runtime": runtime,
        "local_path": str(destination),
        "remote_ref": None,
        "default_params": dict(params),
        "manifest_id": f"{runtime}-local",
        "tracked_files": _no_tracked_files,
    }


def _listing(directory: Path) -> list[str]:
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


class ValidatedRuntimeTests(unittest.TestCase):
    """B1: only folders that pass a real readiness check are ever copied."""

    def _personal_folder(self, root: Path) -> Path:
        folder = root / "Documents"
        (folder / "taxes").mkdir(parents=True)
        (folder / "taxes" / "2025.pdf").write_text("secret", encoding="utf-8")
        (folder / "notes.txt").write_text("private", encoding="utf-8")
        return folder

    def test_arbitrary_folder_is_refused_for_gguf_and_kokoro(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = self._personal_folder(root)
            for runtime in ("llama_cpp", "kokoro"):
                destination = root / "models" / runtime
                with self.subTest(runtime=runtime):
                    with self.assertRaises(IncompleteSourceError):
                        install_from_local_folder(
                            source_path=str(folder), **_kwargs(runtime, destination)
                        )
                    self.assertFalse(destination.exists())

    def test_runtimes_without_file_checks_cannot_receive_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = self._personal_folder(root)
            for runtime in ("template", "procedural"):
                destination = root / "models" / runtime
                with self.subTest(runtime=runtime):
                    with self.assertRaises(InstallNotSupportedError):
                        install_from_local_folder(
                            source_path=str(folder), **_kwargs(runtime, destination)
                        )
                    self.assertFalse(destination.exists())

    def test_gguf_copies_only_the_weight_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = self._personal_folder(root)
            (downloads / "qwen-q4_k_m.gguf").write_bytes(b"gguf")
            destination = root / "models" / "qwen-writer"

            result = install_from_local_folder(
                source_path=str(downloads), **_kwargs("llama_cpp", destination)
            )

            self.assertTrue(result.readiness.is_ready)
            self.assertEqual(_listing(destination), ["qwen-q4_k_m.gguf"])

    def test_gguf_in_a_hugging_face_cache_uses_the_snapshot_not_the_blobs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "hub" / "models--Qwen--Writer-GGUF"
            blob = cache / "blobs" / "0123abcd"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"gguf-bytes")
            snapshot = cache / "snapshots" / "deadbeef"
            snapshot.mkdir(parents=True)
            (snapshot / "writer.gguf").symlink_to(blob)
            destination = root / "models" / "qwen-writer"

            result = install_from_local_folder(
                source_path=str(cache), **_kwargs("llama_cpp", destination)
            )

            self.assertEqual(result.source, snapshot.resolve())
            self.assertEqual(_listing(destination), ["writer.gguf"])
            self.assertFalse((destination / "writer.gguf").is_symlink())
            self.assertEqual((destination / "writer.gguf").read_bytes(), b"gguf-bytes")

    def test_more_than_one_gguf_is_refused_with_the_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "gguf"
            source.mkdir()
            (source / "a.gguf").write_bytes(b"a")
            (source / "b.gguf").write_bytes(b"b")

            with self.assertRaises(IncompleteSourceError) as raised:
                install_from_local_folder(
                    source_path=str(source), **_kwargs("llama_cpp", root / "models" / "q")
                )

            self.assertIn("More than one .gguf", str(raised.exception))

    def test_kokoro_copies_config_weight_and_voices(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Kokoro-82M"
            (source / "voices").mkdir(parents=True)
            (source / "config.json").write_text("{}", encoding="utf-8")
            (source / "kokoro-v1_0.pth").write_bytes(b"pth")
            (source / "voices" / "jf_alpha.pt").write_bytes(b"pt")
            (source / "samples").mkdir()
            (source / "samples" / "demo.wav").write_bytes(b"wav")
            destination = root / "models" / "kokoro-tts"

            result = install_from_local_folder(
                source_path=str(source), **_kwargs("kokoro", destination)
            )

            self.assertTrue(result.readiness.is_ready)
            self.assertEqual(
                _listing(destination),
                ["config.json", "kokoro-v1_0.pth", "voices", "voices/jf_alpha.pt"],
            )


def _tracked_placeholders(_directory: Path) -> frozenset[str]:
    return frozenset({"README.md", "model_index.json"})


class RepositoryPlaceholderTests(unittest.TestCase):
    """B2: files the repository ships in an install location stay where they are."""

    def test_readme_and_license_count_as_empty_and_are_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / "README.md").write_text("upstream readme", encoding="utf-8")
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "README.md").write_text("repo readme", encoding="utf-8")
            (destination / "LICENSE.md").write_text("repo license", encoding="utf-8")

            result = install_from_local_folder(
                source_path=str(source), **_diffusers_kwargs(destination)
            )

            self.assertIsNone(result.replaced_to)
            self.assertTrue(result.readiness.is_ready)
            self.assertEqual((destination / "README.md").read_text(), "repo readme")
            self.assertEqual((destination / "LICENSE.md").read_text(), "repo license")
            self.assertTrue((destination / "unet").is_dir())

    def _tracked_destination(self, root: Path, index: dict[str, object]) -> Path:
        destination = root / "models" / "sdxl"
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("repo readme", encoding="utf-8")
        (destination / "model_index.json").write_text(
            json.dumps(index, indent=2), encoding="utf-8"
        )
        return destination

    def test_tracked_model_index_is_kept_when_it_describes_the_same_pipeline(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(
                root / "downloads" / "sdxl",
                {**_DIFFUSERS_INDEX, "_diffusers_version": "0.30.0"},
            )
            destination = self._tracked_destination(
                root, {**_DIFFUSERS_INDEX, "_diffusers_version": "0.19.0"}
            )
            tracked_text = (destination / "model_index.json").read_text()

            result = install_from_local_folder(
                source_path=str(source),
                **{**_diffusers_kwargs(destination), "tracked_files": _tracked_placeholders},
            )

            self.assertIsNone(result.replaced_to)
            self.assertTrue(result.readiness.is_ready)
            self.assertEqual((destination / "model_index.json").read_text(), tracked_text)

    def test_incompatible_tracked_model_index_fails_and_changes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "cog", _COGVIDEOX_INDEX)
            destination = self._tracked_destination(root, _DIFFUSERS_INDEX)
            before = _listing(destination)

            with self.assertRaises(IncompatibleTrackedFileError):
                install_from_local_folder(
                    source_path=str(source),
                    **{**_diffusers_kwargs(destination), "tracked_files": _tracked_placeholders},
                )

            self.assertEqual(_listing(destination), before)
            self.assertEqual(sorted(p.name for p in destination.parent.iterdir()), ["sdxl"])

    def test_replace_moves_old_weights_but_never_tracked_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = self._tracked_destination(root, _DIFFUSERS_INDEX)
            (destination / "unet").mkdir()
            (destination / "unet" / "old.safetensors").write_bytes(b"old")
            kwargs = {**_diffusers_kwargs(destination), "tracked_files": _tracked_placeholders}

            with self.assertRaises(DestinationNotEmptyError):
                install_from_local_folder(source_path=str(source), **kwargs)
            result = install_from_local_folder(source_path=str(source), replace=True, **kwargs)

            assert result.replaced_to is not None
            self.assertEqual(_listing(result.replaced_to), ["unet", "unet/old.safetensors"])
            self.assertEqual((destination / "README.md").read_text(), "repo readme")
            self.assertFalse((destination / "unet" / "old.safetensors").exists())
            self.assertTrue(result.readiness.is_ready)


class FailurePathTests(unittest.TestCase):
    """S2 / S7: a failure part-way leaves the install location exactly as it was."""

    def test_failed_move_puts_the_replaced_files_back(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "partial.bin").write_bytes(b"old")
            real_rename = Path.rename

            def failing_rename(self: Path, target: Path) -> Path:
                if self.parent.name.startswith(".sdxl.import-") and self.name == "vae":
                    raise OSError("simulated failure while moving into place")
                return real_rename(self, target)

            with patch.object(Path, "rename", failing_rename):
                with self.assertRaises(OSError):
                    install_from_local_folder(
                        source_path=str(source), replace=True, **_diffusers_kwargs(destination)
                    )

            self.assertEqual(_listing(destination), ["partial.bin"])
            self.assertEqual((destination / "partial.bin").read_bytes(), b"old")
            self.assertEqual(sorted(p.name for p in destination.parent.iterdir()), ["sdxl"])

    def test_interrupted_copy_leaves_no_staging_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"

            def copy_then_fail(src: Path, dst: Path, **_: object) -> None:
                Path(dst).mkdir(parents=True)
                (Path(dst) / "half.bin").write_bytes(b"half")
                raise OSError(28, "No space left on device")

            with patch.object(model_install.shutil, "copytree", copy_then_fail):
                with self.assertRaises(OSError):
                    install_from_local_folder(
                        source_path=str(source), **_diffusers_kwargs(destination)
                    )

            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])

    def test_not_enough_disk_space_is_refused_before_copying(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            usage = model_install.shutil.disk_usage(root)

            with patch.object(
                model_install.shutil, "disk_usage", return_value=usage._replace(free=0)
            ):
                with self.assertRaises(InsufficientSpaceError):
                    install_from_local_folder(
                        source_path=str(source), **_diffusers_kwargs(destination)
                    )

            self.assertFalse(destination.exists())

    def test_a_second_import_while_one_runs_is_busy(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            self.assertTrue(model_install._IMPORT_LOCK.acquire(blocking=False))
            try:
                with self.assertRaises(InstallBusyError):
                    install_from_local_folder(
                        source_path=str(source), **_diffusers_kwargs(root / "models" / "sdxl")
                    )
            finally:
                model_install._IMPORT_LOCK.release()

    def test_a_folder_link_loop_is_refused_before_copying(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / "unet" / "loop").symlink_to(source)
            destination = root / "models" / "sdxl"

            with self.assertRaises(InvalidSourceError):
                install_from_local_folder(
                    source_path=str(source), **_diffusers_kwargs(destination)
                )

            self.assertFalse(destination.parent.exists())

    def test_hidden_entries_and_python_files_are_not_copied(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / ".git").mkdir()
            (source / ".git" / "HEAD").write_text("ref", encoding="utf-8")
            (source / "pipeline.py").write_text("import os", encoding="utf-8")
            (source / "unet" / "convert.py").write_text("import os", encoding="utf-8")
            destination = root / "models" / "sdxl"

            install_from_local_folder(source_path=str(source), **_diffusers_kwargs(destination))

            listing = _listing(destination)
            self.assertFalse([name for name in listing if name.endswith(".py")], listing)
            self.assertNotIn(".git", listing)

    def test_stale_staging_folders_are_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            stale = root / "models" / ".sdxl.import-1234abcd"
            stale.mkdir()
            (stale / "half.bin").write_bytes(b"half")
            other = root / "models" / ".other.import-1234abcd"
            other.mkdir()

            fresh = remove_stale_staging([destination])
            removed = remove_stale_staging([destination], now=time.time() + 2 * 3600)

            self.assertEqual(fresh, [], "a recent folder without an owner may still be in use")
            self.assertEqual(removed, [stale])
            self.assertFalse(stale.exists())
            self.assertTrue(other.exists())
            self.assertTrue(destination.exists())


def _git_unknown(_directory: Path) -> frozenset[str] | None:
    return None


class ReleaseArtifactTests(unittest.TestCase):
    """Without ``.git`` (``git archive`` releases) the shipped files are still kept."""

    def _shipped_destination(self, root: Path) -> Path:
        destination = root / "models" / "sdxl"
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("repo readme", encoding="utf-8")
        (destination / "LICENSE.md").write_text("repo license", encoding="utf-8")
        (destination / "model_index.json").write_text(
            json.dumps({**_DIFFUSERS_INDEX, "_diffusers_version": "0.19.0"}), encoding="utf-8"
        )
        return destination

    def test_shipped_model_index_counts_as_empty_and_is_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = self._shipped_destination(root)
            shipped = (destination / "model_index.json").read_text()

            result = install_from_local_folder(
                source_path=str(source),
                **{**_diffusers_kwargs(destination), "tracked_files": _git_unknown},
            )

            self.assertIsNone(result.replaced_to)
            self.assertTrue(result.readiness.is_ready)
            self.assertEqual((destination / "model_index.json").read_text(), shipped)

    def test_replace_keeps_the_shipped_model_index(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = self._shipped_destination(root)
            (destination / "unet").mkdir()
            (destination / "unet" / "old.safetensors").write_bytes(b"old")

            result = install_from_local_folder(
                source_path=str(source),
                replace=True,
                **{**_diffusers_kwargs(destination), "tracked_files": _git_unknown},
            )

            assert result.replaced_to is not None
            self.assertNotIn("model_index.json", _listing(result.replaced_to))
            self.assertTrue((destination / "model_index.json").is_file())

    def test_git_is_not_run_outside_a_checkout(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "models" / "sdxl"
            directory.mkdir(parents=True)
            with patch.object(model_install.subprocess, "run") as run:
                self.assertIsNone(model_install.git_tracked_files(directory))
            run.assert_not_called()

    def test_missing_git_binary_means_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "models" / "sdxl"
            directory.mkdir(parents=True)
            (Path(tmp) / ".git").mkdir()
            with patch.object(model_install.subprocess, "run", side_effect=FileNotFoundError):
                self.assertIsNone(model_install.git_tracked_files(directory))


class FollowUpReviewTests(unittest.TestCase):
    def test_stale_staging_with_a_live_owner_is_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            live = root / "models" / ".sdxl.import-aaaaaaaa"
            live.mkdir()
            (live / ".import-owner").write_text(f"{os.getppid()}\n", encoding="utf-8")
            dead = root / "models" / ".sdxl.import-bbbbbbbb"
            dead.mkdir()
            (dead / ".import-owner").write_text("999999999\n", encoding="utf-8")

            with patch.object(model_install.os, "kill") as kill:
                kill.side_effect = lambda pid, _sig: (
                    None if pid == os.getppid() else (_ for _ in ()).throw(ProcessLookupError())
                )
                removed = remove_stale_staging([destination], now=time.time() + 2 * 3600)

            self.assertEqual(removed, [dead])
            self.assertTrue(live.exists())

    def test_staging_records_its_owner_and_it_is_not_placed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            seen: list[str] = []

            def look_at_staging() -> None:
                for staging in destination.parent.glob(".sdxl.import-*"):
                    seen.append((staging / ".import-owner").read_text().strip())

            install_from_local_folder(
                source_path=str(source),
                **{**_diffusers_kwargs(destination), "before_swap": look_at_staging},
            )

            self.assertEqual(seen, [str(os.getpid())])
            self.assertFalse((destination / ".import-owner").exists())

    def test_before_swap_refusal_changes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "old.safetensors").write_bytes(b"old")

            def busy() -> None:
                raise ModelInUseError("in use")

            with self.assertRaises(ModelInUseError):
                install_from_local_folder(
                    source_path=str(source),
                    replace=True,
                    **{**_diffusers_kwargs(destination), "before_swap": busy},
                )

            self.assertEqual(_listing(destination.parent), ["sdxl", "sdxl/old.safetensors"])

    def test_model_file_must_stay_inside_the_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside.gguf").write_bytes(b"gguf")
            source = root / "model"
            source.mkdir()
            for model_file in ("../outside.gguf", str(root / "outside.gguf")):
                with self.subTest(model_file=model_file):
                    with self.assertRaises(IncompleteSourceError):
                        install_from_local_folder(
                            source_path=str(source),
                            **_kwargs("llama_cpp", root / "models" / "q", model_file=model_file),
                        )

    def test_a_gguf_named_folder_makes_the_choice_ambiguous_like_the_loader(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "model"
            (source / "extra.gguf").mkdir(parents=True)
            (source / "writer.gguf").write_bytes(b"gguf")

            with self.assertRaises(IncompleteSourceError) as raised:
                install_from_local_folder(
                    source_path=str(source), **_kwargs("llama_cpp", root / "models" / "q")
                )

            self.assertIn("More than one .gguf", str(raised.exception))

    def test_kokoro_ignores_a_folder_named_like_a_voice_pack(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Kokoro-82M"
            source.mkdir()
            (source / "config.json").write_text("{}", encoding="utf-8")
            (source / "kokoro.pth").write_bytes(b"pth")
            (source / "backup.pt").mkdir()
            (source / "backup.pt" / "big.bin").write_bytes(b"x")
            destination = root / "models" / "kokoro"

            install_from_local_folder(source_path=str(source), **_kwargs("kokoro", destination))

            self.assertEqual(_listing(destination), ["config.json", "kokoro.pth"])

    def test_links_outside_the_model_folder_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret.safetensors"
            secret.write_bytes(b"not a model")
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            weight = source / "unet" / "model.safetensors"
            weight.unlink()
            weight.symlink_to(secret)
            destination = root / "models" / "sdxl"

            with self.assertRaises(InvalidSourceError):
                install_from_local_folder(
                    source_path=str(source), **_diffusers_kwargs(destination)
                )

            self.assertFalse(destination.parent.exists())

    def test_replacing_a_model_moves_its_own_licence_aside(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / "LICENSE.md").write_text("new licence", encoding="utf-8")
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "LICENSE.md").write_text("old licence", encoding="utf-8")
            (destination / "old.safetensors").write_bytes(b"old")

            result = install_from_local_folder(
                source_path=str(source), replace=True, **_diffusers_kwargs(destination)
            )

            assert result.replaced_to is not None
            self.assertEqual((destination / "LICENSE.md").read_text(), "new licence")
            self.assertEqual((result.replaced_to / "LICENSE.md").read_text(), "old licence")

    def test_a_tracked_licence_is_never_moved(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            (source / "LICENSE.md").write_text("new licence", encoding="utf-8")
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "LICENSE.md").write_text("repo licence", encoding="utf-8")
            (destination / "old.safetensors").write_bytes(b"old")

            install_from_local_folder(
                source_path=str(source),
                replace=True,
                **{
                    **_diffusers_kwargs(destination),
                    "tracked_files": lambda _d: frozenset({"LICENSE.md"}),
                },
            )

            self.assertEqual((destination / "LICENSE.md").read_text(), "repo licence")


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
        selected = _completed("/Users/a/model/\n", 0)
        with patch.object(folder_picker.subprocess, "run", return_value=selected):
            self.assertEqual(folder_picker.pick_folder(platform="darwin"), "/Users/a/model/")
        cancelled = _completed("", 1, "execution error: User canceled. (-128)")
        with patch.object(folder_picker.subprocess, "run", return_value=cancelled):
            self.assertIsNone(folder_picker.pick_folder(platform="darwin"))
        with patch.object(folder_picker.subprocess, "run", return_value=_completed("", 0)):
            self.assertIsNone(folder_picker.pick_folder(platform="win32"))
        with patch.object(
            folder_picker.subprocess, "run", return_value=_completed("", 1)
        ), patch.object(folder_picker.shutil, "which", return_value="/usr/bin/zenity"):
            self.assertIsNone(folder_picker.pick_folder(platform="linux"))

    def test_other_failures_are_errors_not_cancels(self) -> None:
        cases = {
            "darwin": _completed("", 1, "execution error: some other failure (-1708)"),
            "win32": _completed("", 1, "Add-Type : failed"),
            "linux": _completed("", 5, "zenity: cannot open display"),
        }
        for platform, completed in cases.items():
            with self.subTest(platform=platform), patch.object(
                folder_picker.subprocess, "run", return_value=completed
            ), patch.object(folder_picker.shutil, "which", return_value="/usr/bin/zenity"):
                with self.assertRaises(folder_picker.FolderPickerFailedError):
                    folder_picker.pick_folder(platform=platform)

    def test_macos_apple_event_timeout_is_a_timeout_not_a_permission_problem(self) -> None:
        timed_out = _completed(
            "",
            1,
            "72:152: execution error: Safariでエラーが起きました: "
            "AppleEventがタイムアウトしました。 (-1712)",
        )
        with patch.object(folder_picker.subprocess, "run", return_value=timed_out):
            with self.assertRaises(folder_picker.FolderPickerTimeoutError):
                folder_picker.pick_folder(platform="darwin")

    def test_a_hung_picker_process_is_a_timeout(self) -> None:
        with patch.object(
            folder_picker.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["osascript"], 630),
        ):
            with self.assertRaises(folder_picker.FolderPickerTimeoutError):
                folder_picker.pick_folder(platform="darwin")

    def test_macos_script_waits_as_long_as_the_server(self) -> None:
        script = folder_picker.build_picker_command("darwin")[-1]
        timeout_line = f"with timeout of {folder_picker.PICKER_TIMEOUT_SECONDS} seconds"
        self.assertTrue(script.startswith(timeout_line), script)
        self.assertTrue(script.rstrip().endswith("end timeout"), script)
        self.assertLess(script.index(timeout_line), script.index("choose folder"))

    def test_macos_script_shows_its_own_chooser_in_front(self) -> None:
        lines = folder_picker.build_picker_command("darwin")[-1].splitlines()
        activate = lines.index("tell me to activate")
        choose = next(i for i, line in enumerate(lines) if "choose folder" in line)
        self.assertLess(activate, choose)
        # No other app is told to do anything, so no Automation permission is needed.
        self.assertEqual([line for line in lines if line.startswith("tell ")], [lines[activate]])


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
        return _local_client(create_app(services, start_job_runner=False))

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

    def _picker_status(self, client: TestClient, **headers: str) -> tuple[int, bool]:
        with patch("apps.api.routes.model_install.pick_folder", return_value="/x") as picker:
            response = client.post("/models/pick-folder", headers=headers)
        return response.status_code, picker.called

    def test_other_web_pages_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")
            body = {"source_path": str(root), "media_type": "image"}

            self.assertEqual(
                self._picker_status(client, origin="https://evil.example"), (403, False)
            )
            self.assertEqual(self._picker_status(client, origin="null"), (403, False))
            installed = client.post(
                "/models/custom-sdxl/install",
                json=body,
                headers={"origin": "https://evil.example"},
            )
            self.assertEqual(installed.status_code, 403)

    def test_dns_rebinding_host_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")

            self.assertEqual(
                self._picker_status(client, host="rebind.evil.example:8000"), (403, False)
            )
            self.assertEqual(
                self._picker_status(
                    client,
                    host="rebind.evil.example:8000",
                    origin="http://rebind.evil.example:8000",
                ),
                (403, False),
            )

    def test_studio_ui_origins_and_loopback_hosts_are_allowed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")
            allowed: list[dict[str, str]] = [
                {},
                {"origin": "http://localhost:5173"},
                {"origin": "http://127.0.0.1:5173"},
                {"origin": "tauri://localhost"},
                {"origin": "http://127.0.0.1:8000"},  # UI served by the API itself
                {"host": "localhost:8000", "origin": "http://localhost:8000"},
                {"host": "[::1]:8000"},
            ]
            for headers in allowed:
                with self.subTest(headers=headers):
                    self.assertEqual(self._picker_status(client, **headers), (200, True))
            mapped = _local_client(client.app, host="::ffff:127.0.0.1")
            self.assertEqual(self._picker_status(mapped), (200, True))
            # Starlette's default test client address is not an IP and is refused.
            unknown = TestClient(client.app, base_url="http://127.0.0.1:8000")
            self.assertEqual(self._picker_status(unknown), (403, False))

    def test_picker_failure_is_reported_with_its_hint(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")
            with patch(
                "apps.api.routes.model_install.pick_folder",
                side_effect=folder_picker.FolderPickerFailedError("chooser failed"),
            ):
                response = client.post("/models/pick-folder")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "picker_failed")
        self.assertIn("chooser failed", response.json()["detail"]["message"])

    def test_busy_model_reports_restart_required(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            client = self._client(root, root / "models" / "sdxl")
            services = client.app.state.services
            # The check before the swap passes; a job loads the model right after.
            with patch.object(
                services.model_service,
                "unload_model",
                side_effect=[None, RuntimeBusyError("busy")],
            ):
                response = client.post(
                    "/models/custom-sdxl/install",
                    json={"source_path": str(source), "media_type": "image"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["restart_required"])

    def test_model_in_use_is_refused_before_any_file_moves(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_diffusers_pipeline(root / "downloads" / "sdxl", _DIFFUSERS_INDEX)
            destination = root / "models" / "sdxl"
            destination.mkdir(parents=True)
            (destination / "old.safetensors").write_bytes(b"old")
            client = self._client(root, destination)
            services = client.app.state.services
            with patch.object(
                services.model_service, "unload_model", side_effect=RuntimeBusyError("busy")
            ):
                response = client.post(
                    "/models/custom-sdxl/install",
                    json={"source_path": str(source), "media_type": "image", "replace": True},
                )
            listing = _listing(destination.parent)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "model_in_use")
        self.assertEqual(listing, ["sdxl", "sdxl/old.safetensors"])

    def test_picker_timeout_is_its_own_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._client(root, root / "models" / "sdxl")
            with patch(
                "apps.api.routes.model_install.pick_folder",
                side_effect=folder_picker.FolderPickerTimeoutError("open too long"),
            ):
                response = client.post("/models/pick-folder")

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"]["code"], "picker_timeout")

    def test_models_without_file_checks_do_not_offer_local_install(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_root = root / "manifests"
            _write_manifest(
                manifest_root / "text" / "template.json",
                {
                    "id": "template-writer-local",
                    "public_id": "template-writer",
                    "display_name": "Template",
                    "media_type": "text",
                    "task_type": "story",
                    "provider": "local",
                    "runtime": "template",
                    "local_path": str(root / "models" / "template-writer"),
                    "loader": "template_text_loader",
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
            client = _local_client(create_app(services, start_job_runner=False))
            model = client.get("/models", params={"media_type": "text"}).json()["models"][0]
            refused = client.post(
                "/models/template-writer/install",
                json={"source_path": str(root), "media_type": "text"},
            )

        self.assertFalse(model["supports_local_install"])
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["detail"]["code"], "not_supported")


if __name__ == "__main__":
    unittest.main()
