"""API tests for manifest-backed model listing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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


_DIFFUSERS_INDEX = {
    "_class_name": "StableDiffusionXLPipeline",
    "scheduler": ["diffusers", "EulerDiscreteScheduler"],
    "text_encoder": ["transformers", "CLIPTextModel"],
    "tokenizer": ["transformers", "CLIPTokenizer"],
    "unet": ["diffusers", "UNet2DConditionModel"],
    "vae": ["diffusers", "AutoencoderKL"],
}

_COGVIDEOX_INDEX = {
    "_class_name": "CogVideoXPipeline",
    "scheduler": ["diffusers", "CogVideoXDDIMScheduler"],
    "text_encoder": ["transformers", "T5EncoderModel"],
    "tokenizer": ["transformers", "T5Tokenizer"],
    "transformer": ["diffusers", "CogVideoXTransformer3DModel"],
    "vae": ["diffusers", "AutoencoderKLCogVideoX"],
}

_CONFIG_NAMES = {"scheduler": "scheduler_config.json", "tokenizer": "tokenizer_config.json"}
_WEIGHTLESS_COMPONENTS = {"scheduler", "tokenizer"}


def _write_model_index(model_path: Path, index: dict[str, object]) -> None:
    model_path.mkdir(parents=True, exist_ok=True)
    (model_path / "model_index.json").write_text(json.dumps(index), encoding="utf-8")


def _write_component_files(model_path: Path, index: dict[str, object]) -> None:
    """Write the component configs and weights a real pipeline load needs."""

    for component, spec in index.items():
        if component.startswith("_"):
            continue
        component_root = model_path / component
        component_root.mkdir(parents=True, exist_ok=True)
        config_name = _CONFIG_NAMES.get(component, "config.json")
        (component_root / config_name).write_text("{}", encoding="utf-8")
        if component == "tokenizer":
            tokenizer_class = str(spec[1]) if isinstance(spec, list) else ""
            if "T5Tokenizer" in tokenizer_class:
                (component_root / "spiece.model").write_bytes(b"stub")
            else:
                (component_root / "vocab.json").write_text("{}", encoding="utf-8")
                (component_root / "merges.txt").write_text("", encoding="utf-8")
        if component not in _WEIGHTLESS_COMPONENTS:
            (component_root / "model.safetensors").write_bytes(b"stub")


def _write_diffusers_pipeline(model_path: Path, index: dict[str, object]) -> Path:
    _write_model_index(model_path, index)
    _write_component_files(model_path, index)
    return model_path


def _write_transformers_model(model_path: Path) -> Path:
    model_path.mkdir(parents=True, exist_ok=True)
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    (model_path / "model.safetensors").write_bytes(b"stub")
    _write_transformers_processor_files(model_path)
    return model_path


def _write_transformers_processor_files(model_path: Path) -> None:
    (model_path / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (model_path / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (model_path / "tokenizer.json").write_text("{}", encoding="utf-8")


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ModelsApiTests(unittest.TestCase):
    def test_models_endpoint_uses_configured_manifest_root(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_path = _write_diffusers_pipeline(root / "runtime" / "sdxl", _DIFFUSERS_INDEX)
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
                        "runtime_status": "ready",
                        "availability_message": "Diffusers model files are ready.",
                    }
                ]
            },
        )

    def test_models_endpoint_filters_by_media_type(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            image_path = _write_diffusers_pipeline(root / "runtime" / "sdxl", _DIFFUSERS_INDEX)
            audio_path = _write_transformers_model(root / "runtime" / "musicgen")
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

    def test_voicevox_endpoint_is_configured_without_local_model_files(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            _write_manifest(
                manifest_root / "audio" / "voicevox.json",
                {
                    "id": "voicevox-local",
                    "public_id": "voicevox",
                    "display_name": "VOICEVOX",
                    "media_type": "audio",
                    "task_type": "text-to-speech",
                    "provider": "local",
                    "runtime": "voicevox_http",
                    "remote_ref": "http://127.0.0.1:50021/private/path",
                    "loader": "voicevox_http_loader",
                    "default_params": {"speaker_id": 3},
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            with patch.dict(os.environ, {"VOICEVOX_BASE_URL": ""}):
                response = client.get("/models?media_type=audio")

        self.assertEqual(response.status_code, 200)
        model = response.json()["models"][0]
        self.assertTrue(model["is_available"])
        self.assertEqual(model["runtime_status"], "configured")
        self.assertIn("http://127.0.0.1:50021", model["availability_message"])
        self.assertNotIn("private/path", model["availability_message"])
        self.assertIn("fail that job", model["availability_message"])

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
        self.assertEqual(response.json()["models"][0]["runtime_status"], "scaffold")
        self.assertIn("scaffold", response.json()["models"][0]["availability_message"])

    def test_learned_model_requires_adapter_and_pipeline_files(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            adapter_root = root / "runtime" / "learned-video"
            pipeline_root = root / "models" / "cogvideox-2b"
            adapter_root.mkdir(parents=True)
            pipeline_root.mkdir(parents=True)
            (adapter_root / "runtime.py").write_text(
                "def load_runtime(manifest):\n    return {'runtime_adapter': 'learned_text_to_video'}\n",
                encoding="utf-8",
            )
            _write_manifest(
                manifest_root / "video" / "learned.json",
                {
                    "id": "learned-video-local",
                    "public_id": "learned-video",
                    "display_name": "CogVideoX-2B",
                    "media_type": "video",
                    "task_type": "text-to-video",
                    "provider": "local",
                    "runtime": "learned",
                    "local_path": str(adapter_root),
                    "loader": "learned_video_loader",
                    "default_params": {
                        "entrypoint": "runtime.py",
                        "runtime_status": "pilot",
                        "pipeline_path": str(pipeline_root),
                        "output_format": "mp4",
                    },
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            missing_response = client.get("/models?media_type=video")
            _write_model_index(pipeline_root, _COGVIDEOX_INDEX)
            index_only_response = client.get("/models?media_type=video")
            _write_component_files(pipeline_root, _COGVIDEOX_INDEX)
            ready_response = client.get("/models?media_type=video")

        self.assertFalse(missing_response.json()["models"][0]["is_available"])
        self.assertEqual(missing_response.json()["models"][0]["runtime_status"], "missing_files")
        index_only_model = index_only_response.json()["models"][0]
        self.assertFalse(index_only_model["is_available"])
        self.assertEqual(index_only_model["runtime_status"], "missing_files")
        self.assertIn("transformer/*.safetensors", index_only_model["availability_message"])
        self.assertTrue(ready_response.json()["models"][0]["is_available"])
        self.assertEqual(ready_response.json()["models"][0]["runtime_status"], "ready")

    def test_diffusers_model_without_weights_is_not_available(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_path = root / "runtime" / "sdxl"
            _write_model_index(model_path, _DIFFUSERS_INDEX)
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
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            index_only_response = client.get("/models?media_type=image")
            _write_component_files(model_path, _DIFFUSERS_INDEX)
            ready_response = client.get("/models?media_type=image")

        index_only_model = index_only_response.json()["models"][0]
        self.assertFalse(index_only_model["is_available"])
        self.assertEqual(index_only_model["runtime_status"], "missing_files")
        self.assertIn("unet/*.safetensors", index_only_model["availability_message"])
        self.assertTrue(ready_response.json()["models"][0]["is_available"])

    def test_transformers_model_without_weights_is_not_available(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_root = root / "manifests"
            model_path = root / "runtime" / "musicgen"
            model_path.mkdir(parents=True)
            (model_path / "config.json").write_text("{}", encoding="utf-8")
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
                    "local_path": str(model_path),
                    "loader": "transformers_musicgen_loader",
                    "enabled": True,
                },
            )
            services = create_application_services(
                manifest_root=manifest_root,
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            config_only_response = client.get("/models?media_type=audio")
            (model_path / "model.safetensors").write_bytes(b"stub")
            _write_transformers_processor_files(model_path)
            ready_response = client.get("/models?media_type=audio")

        config_only_model = config_only_response.json()["models"][0]
        self.assertFalse(config_only_model["is_available"])
        self.assertEqual(config_only_model["runtime_status"], "missing_files")
        self.assertTrue(ready_response.json()["models"][0]["is_available"])


if __name__ == "__main__":
    unittest.main()
