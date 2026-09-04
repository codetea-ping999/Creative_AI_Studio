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
                # "sdxl" now advertises img2img reference_capability (#201
                # follow-up); "ssd-1b" is the shipped manifest that still
                # doesn't, so this exercises the actual no-capability path.
                "model_id": "ssd-1b",
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
                "model_id": "ssd-1b",
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

    def test_post_jobs_accepts_a_reference_for_sdxl_which_advertises_img2img(self) -> None:
        # Regression (#201 follow-up, third Codex round on PR #376): every
        # shipped image manifest omitted reference_capability, so the img2img
        # conditioning path built for #201 was unreachable by any real model
        # -- only a test-only manifest could exercise it. "sdxl" (the
        # is_default manifest) now advertises img2img support.
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
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(self.services.job_repository.list()), 1)

    def test_post_generate_image_rejects_a_reference_from_a_different_project(
        self,
    ) -> None:
        # Regression (#201 follow-up, fifth Codex round on PR #376, P1): a
        # reference asset from a different project must not silently
        # condition a job in this one. apps/api/routes/generate.py already
        # enforces the identical project-membership boundary for
        # assembly-request timeline assets (asset.project_id != project_id);
        # reference-image conditioning had no such check at all, so a job in
        # project B could resolve and condition on project A's image.
        from core.assets import Asset

        client = self._client()
        project_a = self.services.project_repository.create("Project A")
        project_b = self.services.project_repository.create("Project B")
        self.services.asset_repository.create_or_update(
            Asset(
                id="cross_project_ref",
                job_id="job_fixture",
                project_id=project_a.id,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path="/tmp/does-not-need-to-exist.png",
            )
        )
        response = client.post(
            "/generate/image",
            json={
                "prompt": "a knight",
                "model_id": "sdxl",
                "project_id": project_b.id,
                "references": [
                    {"asset_id": "cross_project_ref", "role": "character", "strength": 0.8}
                ],
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("cross_project_ref", response.text)
        self.assertEqual(self.services.job_repository.list(), [])

    def test_post_generate_image_accepts_a_reference_from_the_same_project(
        self,
    ) -> None:
        from core.assets import Asset

        client = self._client()
        project_a = self.services.project_repository.create("Project A")
        self.services.asset_repository.create_or_update(
            Asset(
                id="same_project_ref",
                job_id="job_fixture",
                project_id=project_a.id,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path="/tmp/does-not-need-to-exist.png",
            )
        )
        response = client.post(
            "/generate/image",
            json={
                "prompt": "a knight",
                "model_id": "sdxl",
                "project_id": project_a.id,
                "references": [
                    {"asset_id": "same_project_ref", "role": "character", "strength": 0.8}
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(self.services.job_repository.list()), 1)

    def test_post_generate_image_rejects_a_bible_derived_reference_from_a_different_project(
        self,
    ) -> None:
        # Regression (#201 follow-up, sixth Codex round on PR #376, P1): the
        # request.references check above only covers the documented
        # top-level field. A Bible-derived character/location reference
        # (params.bible_refs) resolves its asset the same way once
        # PromptComposer runs inside the generator, with the identical
        # cross-project exposure risk if left unchecked -- a job in project B
        # could still resolve a Bible entry whose reference_asset_ids point
        # at a project-A image and condition on it.
        from core.assets import Asset

        client = self._client()
        project_a = self.services.project_repository.create("Project A")
        project_b = self.services.project_repository.create("Project B")
        self.services.asset_repository.create_or_update(
            Asset(
                id="bible_cross_project_ref",
                job_id="job_fixture",
                project_id=project_a.id,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path="/tmp/does-not-need-to-exist.png",
            )
        )
        entry = self.services.bible_repository.create(
            kind="character",
            name="Mina",
            reference_asset_ids=["bible_cross_project_ref"],
        )
        response = client.post(
            "/generate/image",
            json={
                "prompt": "a knight",
                "model_id": "sdxl",
                "project_id": project_b.id,
                "params": {"bible_refs": [entry.id]},
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("bible_cross_project_ref", response.text)
        self.assertEqual(self.services.job_repository.list(), [])

    def test_post_generate_image_accepts_a_bible_derived_reference_from_the_same_project(
        self,
    ) -> None:
        from core.assets import Asset

        client = self._client()
        project_a = self.services.project_repository.create("Project A")
        self.services.asset_repository.create_or_update(
            Asset(
                id="bible_same_project_ref",
                job_id="job_fixture",
                project_id=project_a.id,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path="/tmp/does-not-need-to-exist.png",
            )
        )
        entry = self.services.bible_repository.create(
            kind="character",
            name="Mina",
            reference_asset_ids=["bible_same_project_ref"],
        )
        response = client.post(
            "/generate/image",
            json={
                "prompt": "a knight",
                "model_id": "sdxl",
                "project_id": project_a.id,
                "params": {"bible_refs": [entry.id]},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(self.services.job_repository.list()), 1)

    def test_post_generate_image_ignores_an_unknown_bible_ref_in_the_project_check(
        self,
    ) -> None:
        # An unknown bible entry id must not be rejected by this early
        # project-boundary check -- PromptComposer already degrades that to
        # a warning later (see _resolve_entries), and this check must not
        # be stricter than the real resolution it is only a fast-fail for.
        client = self._client()
        response = client.post(
            "/generate/image",
            json={
                "prompt": "a knight",
                "model_id": "sdxl",
                "params": {"bible_refs": ["does-not-exist"]},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_post_generate_audio_accepts_a_bible_ref_with_a_cross_project_image(
        self,
    ) -> None:
        # Regression (#201 follow-up, seventh Codex round on PR #376, P2):
        # the Bible-derived project-boundary check must be gated on image
        # jobs specifically -- no other media type performs reference-image
        # conditioning at all, so an audio (or text/video) job carrying
        # bible_refs that happens to name a character/location entry with a
        # cross-project image reference must not be rejected for a risk
        # that generator can never act on.
        from core.assets import Asset

        client = self._client()
        project_a = self.services.project_repository.create("Project A")
        project_b = self.services.project_repository.create("Project B")
        self.services.asset_repository.create_or_update(
            Asset(
                id="audio_job_bible_ref",
                job_id="job_fixture",
                project_id=project_a.id,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path="/tmp/does-not-need-to-exist.png",
            )
        )
        entry = self.services.bible_repository.create(
            kind="character",
            name="Mina",
            reference_asset_ids=["audio_job_bible_ref"],
        )
        response = client.post(
            "/generate/audio",
            json={
                "prompt": "bright synth loop",
                "model_id": "musicgen-small",
                "project_id": project_b.id,
                "output_format": "wav",
                "params": {"bible_refs": [entry.id], "duration_seconds": 4},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_post_batches_reports_422_not_500_for_a_cross_project_bible_reference(
        self,
    ) -> None:
        # Regression (#201 follow-up, ninth Codex round on PR #376, P2):
        # BatchSpec.bible_refs flows unchanged into each expanded item's
        # params.bible_refs (core/batches/expansion.py), so a batch whose
        # bible_refs resolve to an image outside the batch's own project
        # surfaces the project-boundary rejection JobService.create_job()
        # raises through BatchService.create_batch() -- but this route never
        # caught it, so it fell through as an unhandled 500 instead of 422,
        # exactly like the /jobs/{id}/rerun and gallery reuse gaps from
        # earlier rounds.
        from core.assets import Asset

        client = self._client()
        project_a = self.services.project_repository.create("Project A")
        project_b = self.services.project_repository.create("Project B")
        self.services.asset_repository.create_or_update(
            Asset(
                id="batch_cross_project_ref",
                job_id="job_fixture",
                project_id=project_a.id,
                media_type="image",
                kind="output",
                title="reference fixture",
                prompt="a reference image",
                model_id="sdxl",
                path="/tmp/does-not-need-to-exist.png",
            )
        )
        entry = self.services.bible_repository.create(
            kind="character",
            name="Mina",
            reference_asset_ids=["batch_cross_project_ref"],
        )
        response = client.post(
            "/batches",
            json={
                "spec": {
                    "name": "cross-project batch",
                    "media_type": "image",
                    "model_id": "sdxl",
                    "project_id": project_b.id,
                    "prompt": "a knight",
                    "bible_refs": [entry.id],
                }
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("batch_cross_project_ref", response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
