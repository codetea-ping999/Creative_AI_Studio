"""Tests for the reference-image capability and request contract (#198)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models.manifest import ModelManifest  # noqa: E402
from core.reference_capabilities import (  # noqa: E402
    ReferenceCapability,
    ReferenceImageInput,
    UnsupportedReferenceError,
    validate_reference_inputs,
)
from core.schemas import GenerationRequest  # noqa: E402


def _manifest(**overrides: object) -> ModelManifest:
    payload: dict[str, object] = {
        "id": "sdxl-local",
        "public_id": "sdxl",
        "display_name": "SDXL Local",
        "media_type": "image",
        "task_type": "text-to-image",
        "provider": "local",
        "runtime": "diffusers",
        "local_path": "./models/image/sdxl",
        "loader": "diffusers_image_loader",
    }
    payload.update(overrides)
    return ModelManifest(**payload)


class ReferenceImageInputSchemaTests(unittest.TestCase):
    """Field-level validation: roles and strengths are constrained at parse time."""

    def test_accepts_valid_role_strength_and_preprocessing(self) -> None:
        reference = ReferenceImageInput(
            asset_id="asset-1",
            role="character",
            strength=0.75,
            preprocessing="face_crop",
        )
        self.assertEqual(reference.role, "character")
        self.assertEqual(reference.strength, 0.75)
        self.assertEqual(reference.preprocessing, "face_crop")

    def test_defaults_strength_and_preprocessing_when_omitted(self) -> None:
        reference = ReferenceImageInput(asset_id="asset-1", role="location")
        self.assertGreaterEqual(reference.strength, 0.0)
        self.assertLessEqual(reference.strength, 1.0)
        self.assertEqual(reference.preprocessing, "none")

    def test_rejects_unknown_role(self) -> None:
        with self.assertRaises(ValidationError):
            ReferenceImageInput(asset_id="asset-1", role="prop")  # type: ignore[arg-type]

    def test_rejects_strength_above_one(self) -> None:
        with self.assertRaises(ValidationError):
            ReferenceImageInput(asset_id="asset-1", role="character", strength=1.5)

    def test_rejects_strength_below_zero(self) -> None:
        with self.assertRaises(ValidationError):
            ReferenceImageInput(asset_id="asset-1", role="character", strength=-0.1)

    def test_rejects_empty_asset_id(self) -> None:
        with self.assertRaises(ValidationError):
            ReferenceImageInput(asset_id="", role="character")

    def test_rejects_unknown_extra_field(self) -> None:
        with self.assertRaises(ValidationError):
            ReferenceImageInput(
                asset_id="asset-1",
                role="character",
                unexpected_field="nope",
            )


class GenerationRequestReferenceSerializationTests(unittest.TestCase):
    """Role and strength must survive request serialization round-trips."""

    def test_generation_request_defaults_to_no_references(self) -> None:
        request = GenerationRequest(media_type="image", prompt="a cat", model_id="sdxl")
        self.assertEqual(request.references, [])

    def test_references_round_trip_through_model_dump_and_validate(self) -> None:
        request = GenerationRequest(
            media_type="image",
            prompt="a knight on a rooftop",
            model_id="sdxl",
            references=[
                ReferenceImageInput(asset_id="char-1", role="character", strength=0.8),
                ReferenceImageInput(
                    asset_id="loc-1",
                    role="location",
                    strength=0.4,
                    preprocessing="auto",
                ),
            ],
        )
        dumped = request.model_dump()
        rebuilt = GenerationRequest.model_validate(dumped)

        self.assertEqual(len(rebuilt.references), 2)
        self.assertEqual(rebuilt.references[0].role, "character")
        self.assertEqual(rebuilt.references[0].strength, 0.8)
        self.assertEqual(rebuilt.references[1].role, "location")
        self.assertEqual(rebuilt.references[1].strength, 0.4)
        self.assertEqual(rebuilt.references[1].preprocessing, "auto")

    def test_references_round_trip_through_json(self) -> None:
        request = GenerationRequest(
            media_type="image",
            prompt="a knight",
            model_id="sdxl",
            references=[ReferenceImageInput(asset_id="char-1", role="character", strength=0.55)],
        )
        rebuilt = GenerationRequest.model_validate_json(request.model_dump_json())
        self.assertEqual(rebuilt.references[0].asset_id, "char-1")
        self.assertEqual(rebuilt.references[0].role, "character")
        self.assertEqual(rebuilt.references[0].strength, 0.55)


class ReferenceCapabilityTests(unittest.TestCase):
    def test_capability_with_no_modes_or_roles_is_disabled(self) -> None:
        capability = ReferenceCapability()
        self.assertFalse(capability.enabled)

    def test_capability_with_modes_and_roles_is_enabled(self) -> None:
        capability = ReferenceCapability(
            supported_modes=["ip_adapter"],
            supported_roles=["character"],
        )
        self.assertTrue(capability.enabled)

    def test_rejects_max_strength_below_min_strength(self) -> None:
        with self.assertRaises(ValidationError):
            ReferenceCapability(min_strength=0.8, max_strength=0.2)

    def test_manifest_defaults_to_no_reference_capability(self) -> None:
        # Existing manifests (JSON on disk today) omit this field entirely; a new
        # model must not be treated as reference-capable unless it opts in.
        manifest = _manifest()
        self.assertIsNone(manifest.reference_capability)

    def test_manifest_can_declare_reference_capability(self) -> None:
        manifest = _manifest(
            reference_capability={
                "supported_modes": ["ip_adapter"],
                "supported_roles": ["character", "location"],
                "max_references_per_role": 2,
            }
        )
        assert manifest.reference_capability is not None
        self.assertTrue(manifest.reference_capability.enabled)
        self.assertEqual(manifest.reference_capability.max_references_per_role, 2)


class ValidateReferenceInputsTests(unittest.TestCase):
    """Unsupported references must fail before generation with an actionable message."""

    def test_no_references_never_raises_even_without_capability(self) -> None:
        validate_reference_inputs([], capability=None, model_id="template-writer")

    def test_raises_when_model_has_no_capability_at_all(self) -> None:
        references = [ReferenceImageInput(asset_id="char-1", role="character")]
        with self.assertRaises(UnsupportedReferenceError) as ctx:
            validate_reference_inputs(references, capability=None, model_id="sdxl")
        message = str(ctx.exception)
        self.assertIn("sdxl", message)
        self.assertIn("does not support reference-image conditioning", message)

    def test_raises_when_capability_is_present_but_empty(self) -> None:
        # A manifest could declare an empty ReferenceCapability() explicitly; it
        # must be treated the same as "no reference support", never guessed-on.
        references = [ReferenceImageInput(asset_id="char-1", role="character")]
        with self.assertRaises(UnsupportedReferenceError):
            validate_reference_inputs(references, capability=ReferenceCapability(), model_id="sdxl")

    def test_raises_on_unsupported_role(self) -> None:
        capability = ReferenceCapability(
            supported_modes=["img2img"],
            supported_roles=["location"],
        )
        references = [ReferenceImageInput(asset_id="char-1", role="character")]
        with self.assertRaises(UnsupportedReferenceError) as ctx:
            validate_reference_inputs(references, capability=capability, model_id="sdxl")
        message = str(ctx.exception)
        self.assertIn("character", message)
        self.assertIn("sdxl", message)

    def test_raises_on_strength_outside_capability_bounds(self) -> None:
        capability = ReferenceCapability(
            supported_modes=["ip_adapter"],
            supported_roles=["character"],
            min_strength=0.2,
            max_strength=0.6,
        )
        references = [ReferenceImageInput(asset_id="char-1", role="character", strength=0.9)]
        with self.assertRaises(UnsupportedReferenceError) as ctx:
            validate_reference_inputs(references, capability=capability, model_id="sdxl")
        self.assertIn("outside", str(ctx.exception))

    def test_raises_on_unsupported_preprocessing(self) -> None:
        capability = ReferenceCapability(
            supported_modes=["ip_adapter"],
            supported_roles=["character"],
        )
        references = [
            ReferenceImageInput(asset_id="char-1", role="character", preprocessing="depth")
        ]
        with self.assertRaises(UnsupportedReferenceError):
            validate_reference_inputs(references, capability=capability, model_id="sdxl")

    def test_raises_when_role_count_exceeds_capability_limit(self) -> None:
        capability = ReferenceCapability(
            supported_modes=["ip_adapter"],
            supported_roles=["character"],
            max_references_per_role=1,
        )
        references = [
            ReferenceImageInput(asset_id="char-1", role="character"),
            ReferenceImageInput(asset_id="char-2", role="character"),
        ]
        with self.assertRaises(UnsupportedReferenceError) as ctx:
            validate_reference_inputs(references, capability=capability, model_id="sdxl")
        self.assertIn("at most 1", str(ctx.exception))

    def test_accepts_references_within_a_matching_capability(self) -> None:
        capability = ReferenceCapability(
            supported_modes=["ip_adapter"],
            supported_roles=["character", "location"],
            max_references_per_role=1,
        )
        references = [
            ReferenceImageInput(asset_id="char-1", role="character", strength=0.7),
            ReferenceImageInput(asset_id="loc-1", role="location", strength=0.3),
        ]
        # Must not raise.
        validate_reference_inputs(references, capability=capability, model_id="sdxl")


class UnsupportedReferenceRequestApiTests(unittest.TestCase):
    """#198 acceptance criterion: unsupported references fail before generation.

    validate_reference_inputs() is exercised above only as a standalone unit;
    none of those tests would notice if it were never actually called from a
    real request path. These tests hit the real HTTP surface.
    """

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _client(self):
        from pathlib import Path

        from fastapi.testclient import TestClient

        from apps.api.main import create_app
        from bootstrap import create_application_services

        root = Path(self._tmp.name)
        self.services = create_application_services(
            db_path=root / "jobs.db",
            output_dir=root / "outputs" / "images",
        )
        return TestClient(create_app(self.services, start_job_runner=False))

    def test_post_jobs_rejects_references_for_a_model_without_reference_capability(self) -> None:
        client = self._client()
        response = client.post(
            "/jobs",
            json={
                "media_type": "image",
                "prompt": "a knight",
                "model_id": "sdxl",
                "references": [
                    {"asset_id": "char-1", "role": "character", "strength": 0.8}
                ],
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("reference-image conditioning", response.text)
        self.assertEqual(self.services.job_repository.list(), [])

    def test_post_generate_image_rejects_references_for_a_model_without_reference_capability(
        self,
    ) -> None:
        client = self._client()
        response = client.post(
            "/generate/image",
            json={
                "prompt": "a knight",
                "model_id": "sdxl",
                "references": [
                    {"asset_id": "char-1", "role": "character", "strength": 0.8}
                ],
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("reference-image conditioning", response.text)
        self.assertEqual(self.services.job_repository.list(), [])

    def test_post_jobs_accepts_a_request_with_no_references(self) -> None:
        client = self._client()
        response = client.post(
            "/jobs",
            json={"media_type": "image", "prompt": "a knight", "model_id": "sdxl"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(self.services.job_repository.list()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
