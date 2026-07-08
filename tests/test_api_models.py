"""API tests for manifest-backed model listing."""

from __future__ import annotations

import json
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


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ModelsApiTests(unittest.TestCase):
    def test_models_endpoint_uses_configured_manifest_root(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_path = root / "runtime" / "sdxl"
            model_path.mkdir(parents=True)
            (model_path / "model_index.json").write_text("{}", encoding="utf-8")
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
                    "local_path": str(model_path),
                    "loader": "diffusers_image_loader",
                    "default_params": {"width": 768},
                    "aliases": ["custom"],
                    "tags": ["image", "custom"],
                    "is_default": True,
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            response = client.get("/models")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "models": [
                    {
                        "id": "custom-sdxl",
                        "internal_id": "custom-sdxl-local",
                        "display_name": "Custom SDXL",
                        "media_type": "image",
                        "task_type": "text-to-image",
                        "provider": "local",
                        "default_params": {"width": 768},
                        "tags": ["image", "custom"],
                        "is_default": True,
                        "is_available": True,
                    }
                ]
            },
        )

    def test_models_endpoint_filters_by_media_type(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            image_path = root / "runtime" / "sdxl"
            audio_path = root / "runtime" / "musicgen"
            image_path.mkdir(parents=True)
            audio_path.mkdir(parents=True)
            (image_path / "model_index.json").write_text("{}", encoding="utf-8")
            (audio_path / "config.json").write_text("{}", encoding="utf-8")
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
                    "local_path": str(image_path),
                    "loader": "diffusers_image_loader",
                    "default_params": {"width": 768},
                    "enabled": True,
                },
            )
            _write_manifest(
                manifest_root / "audio" / "custom-musicgen.json",
                {
                    "id": "custom-musicgen-local",
                    "public_id": "custom-musicgen",
                    "display_name": "Custom MusicGen",
                    "media_type": "audio",
                    "task_type": "text-to-music",
                    "provider": "local",
                    "runtime": "transformers",
                    "local_path": str(audio_path),
                    "loader": "transformers_musicgen_loader",
                    "default_params": {"duration_seconds": 6},
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            response = client.get("/models?media_type=audio")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["models"]), 1)
        self.assertEqual(response.json()["models"][0]["id"], "custom-musicgen")

    def test_learned_scaffold_model_is_not_reported_available(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            learned_path = root / "runtime" / "learned-video"
            learned_path.mkdir(parents=True)
            (learned_path / "runtime.py").write_text(
                "def load_runtime(manifest):\n    return {'load_error': 'scaffold'}\n",
                encoding="utf-8",
            )
            _write_manifest(
                manifest_root / "video" / "learned.json",
                {
                    "id": "learned-video-local",
                    "public_id": "learned-video",
                    "display_name": "Learned Video",
                    "media_type": "video",
                    "task_type": "text-to-video",
                    "provider": "local",
                    "runtime": "learned",
                    "local_path": str(learned_path),
                    "loader": "learned_video_loader",
                    "default_params": {"entrypoint": "runtime.py", "runtime_status": "scaffold"},
                    "tags": ["video", "learned", "fallback"],
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            response = client.get("/models?media_type=video")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"][0]["id"], "learned-video")
        self.assertFalse(response.json()["models"][0]["is_available"])


if __name__ == "__main__":
    unittest.main()
