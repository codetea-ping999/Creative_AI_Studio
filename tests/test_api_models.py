"""API tests for manifest-backed model listing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

IMPORT_ERROR: Exception | None = None

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from core.models import ModelManifest
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc


class _StubRegistry:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.load_all_called = False

    def load_all(self) -> None:
        self.load_all_called = True

    def list_all(self, *, enabled_only: bool = True) -> list[ModelManifest]:
        if not self.load_all_called:
            raise AssertionError("Registry should load manifests before listing.")
        if not enabled_only:
            raise AssertionError("/models must request enabled manifests only.")

        return [
            ModelManifest(
                id="sdxl-local",
                public_id="sdxl",
                display_name="SDXL Local",
                media_type="image",
                task_type="text-to-image",
                provider="local",
                runtime="diffusers",
                local_path="./models/image/sdxl",
                loader="diffusers_image_loader",
                default_params={"width": 1024},
                aliases=["sdxl-local"],
                tags=["image", "base"],
                is_default=True,
                enabled=True,
            ),
            ModelManifest(
                id="musicgen-small-local",
                public_id="musicgen-small",
                display_name="MusicGen Small Local",
                media_type="audio",
                task_type="text-to-music",
                provider="local",
                runtime="transformers",
                local_path="./models/audio/musicgen-small",
                loader="transformers_musicgen_loader",
                default_params={"duration_seconds": 8},
                aliases=["musicgen-small-local"],
                tags=["audio", "music"],
                is_default=True,
                enabled=True,
            ),
        ]


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class ModelsApiTests(unittest.TestCase):
    def test_models_endpoint_returns_ui_safe_manifest_metadata(self) -> None:
        with patch("apps.api.routes.models.ModelRegistry", _StubRegistry):
            client = TestClient(create_app())

            response = client.get("/models")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "models": [
                    {
                        "id": "sdxl",
                        "internal_id": "sdxl-local",
                        "display_name": "SDXL Local",
                        "media_type": "image",
                        "task_type": "text-to-image",
                        "provider": "local",
                        "default_params": {"width": 1024},
                        "tags": ["image", "base"],
                        "is_default": True,
                        "is_available": True,
                    },
                    {
                        "id": "musicgen-small",
                        "internal_id": "musicgen-small-local",
                        "display_name": "MusicGen Small Local",
                        "media_type": "audio",
                        "task_type": "text-to-music",
                        "provider": "local",
                        "default_params": {"duration_seconds": 8},
                        "tags": ["audio", "music"],
                        "is_default": True,
                        "is_available": True,  # Now available after download
                    }
                ]
            },
        )

    def test_models_endpoint_filters_by_media_type(self) -> None:
        with patch("apps.api.routes.models.ModelRegistry", _StubRegistry):
            client = TestClient(create_app())

            response = client.get("/models?media_type=video")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"models": []})

    def test_models_endpoint_returns_audio_models_when_filtered(self) -> None:
        with patch("apps.api.routes.models.ModelRegistry", _StubRegistry):
            client = TestClient(create_app())

            response = client.get("/models?media_type=audio")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "models": [
                    {
                        "id": "musicgen-small",
                        "internal_id": "musicgen-small-local",
                        "display_name": "MusicGen Small Local",
                        "media_type": "audio",
                        "task_type": "text-to-music",
                        "provider": "local",
                        "default_params": {"duration_seconds": 8},
                        "tags": ["audio", "music"],
                        "is_default": True,
                        "is_available": True,  # Now available after download
                    }
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
