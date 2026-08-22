"""AudioCraft MusicGen long-form validation and job lifecycle tests."""

from __future__ import annotations

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from bootstrap import create_application_services, create_default_model_service
from core.model_readiness import STATUS_MISSING_FILES, STATUS_READY, evaluate_readiness
from core.models.loader import AudioCraftMusicgenLoader
from core.schemas import GenerationRequest
from generators.audio import AudioGenerator


def _write_manifest_root(root: Path) -> Path:
    manifest_root = root / "manifests"
    audio_root = manifest_root / "audio"
    audio_root.mkdir(parents=True)
    manifests = (
        {
            "id": "musicgen-small-test",
            "public_id": "musicgen-small",
            "display_name": "MusicGen Small",
            "media_type": "audio",
            "task_type": "text-to-music",
            "provider": "test",
            "runtime": "transformers",
            "local_path": str(root / "short-model"),
            "loader": "transformers_musicgen_loader",
            "dtype": "float32",
            "default_params": {"duration_seconds": 8},
            "tags": ["audio", "music"],
            "is_default": True,
            "enabled": True,
        },
        {
            "id": "musicgen-long-form-test",
            "public_id": "musicgen-long-form",
            "display_name": "MusicGen Small Long-form",
            "media_type": "audio",
            "task_type": "text-to-music",
            "provider": "test",
            "runtime": "audiocraft",
            "local_path": str(root / "long-model"),
            "loader": "audiocraft_musicgen_loader",
            "dtype": "float32",
            "default_params": {
                "duration_seconds": 45,
                "extend_stride_seconds": 18,
                "guidance_scale": 3.0,
                "temperature": 1.0,
                "top_k": 250,
                "top_p": 0.0,
            },
            "tags": ["audio", "music", "long-form", "optional"],
            "is_default": False,
            "enabled": True,
        },
    )
    for index, payload in enumerate(manifests):
        (audio_root / f"model-{index}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    return manifest_root


class _FakeAudioCraftModel:
    sample_rate = 32_000
    frame_rate = 50
    max_duration = 30.0

    def __init__(
        self,
        *,
        fail_segment: int | None = None,
        before_segment=None,
    ) -> None:
        self.fail_segment = fail_segment
        self.before_segment = before_segment
        self.progress_callback = None
        self.params: dict[str, object] = {}
        self.prompts: list[str] = []

    def set_generation_params(self, **kwargs: object) -> None:
        self.params = kwargs

    def set_custom_progress_callback(self, callback=None) -> None:
        self.progress_callback = callback

    def generate(self, prompts: list[str], *, progress: bool):
        import torch

        self.prompts = prompts
        duration = float(self.params["duration"])
        stride = float(self.params["extend_stride"])
        segment_count = 1 + math.ceil(max(0.0, duration - self.max_duration) / stride)
        boundaries = [
            min(duration, self.max_duration + index * stride)
            for index in range(segment_count)
        ]
        for segment, boundary in enumerate(boundaries, start=1):
            if self.before_segment is not None:
                self.before_segment(segment)
            if self.fail_segment == segment:
                raise RuntimeError(f"segment {segment} failed")
            if progress and self.progress_callback is not None:
                self.progress_callback(
                    round(boundary * self.frame_rate),
                    round(duration * self.frame_rate),
                )
        sample_count = round(duration * self.sample_rate)
        time = torch.arange(sample_count, dtype=torch.float32) / self.sample_rate
        audio = 0.05 * torch.sin(2 * torch.pi * 220 * time)
        return audio.reshape(1, 1, -1)


def _runtime_for(manifest, model: _FakeAudioCraftModel) -> dict[str, object]:
    return {
        "stub": False,
        "loader": "AudioCraftMusicgenLoader",
        "manifest_id": manifest.id,
        "display_name": manifest.display_name,
        "runtime": manifest.runtime,
        "provider": manifest.provider,
        "local_path": manifest.local_path,
        "remote_ref": manifest.remote_ref,
        "dtype": manifest.dtype,
        "device": "cpu",
        "default_params": dict(manifest.default_params),
        "path_exists": True,
        "model": model,
        "sampling_rate": model.sample_rate,
        "frame_rate": model.frame_rate,
        "max_duration": model.max_duration,
    }


def _write_audiocraft_files(root: Path, *, include_t5: bool = True) -> None:
    root.mkdir(parents=True)
    (root / "state_dict.bin").write_bytes(b"lm")
    (root / "compression_state_dict.bin").write_bytes(b"codec")
    if include_t5:
        t5_root = root / "t5-base"
        t5_root.mkdir()
        for name in ("config.json", "model.safetensors", "spiece.model"):
            (t5_root / name).write_bytes(b"t5")


class MusicgenLongFormTests(unittest.TestCase):
    def test_model_specific_duration_and_stride_validation(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service = create_default_model_service(_write_manifest_root(root))
            generator = AudioGenerator(service)

            accepted = (
                ("musicgen-small", {"duration_seconds": 30}),
                (
                    "musicgen-long-form",
                    {"duration_seconds": 31, "extend_stride_seconds": 5},
                ),
                (
                    "musicgen-long-form",
                    {"duration_seconds": 120, "extend_stride_seconds": 29},
                ),
            )
            for model_id, params in accepted:
                with self.subTest(model_id=model_id, params=params):
                    generator.validate_request(
                        GenerationRequest(
                            media_type="audio",
                            prompt="boundary cue",
                            model_id=model_id,
                            output_format="wav",
                            params=params,
                        )
                    )

            rejected = (
                ("musicgen-small", {"duration_seconds": 31}, "between 2 and 30"),
                (
                    "musicgen-small",
                    {"duration_seconds": 30, "extend_stride_seconds": 18},
                    "only supported",
                ),
                (
                    "musicgen-long-form",
                    {"duration_seconds": 30, "extend_stride_seconds": 18},
                    "between 31 and 120",
                ),
                (
                    "musicgen-long-form",
                    {"duration_seconds": 121, "extend_stride_seconds": 18},
                    "between 31 and 120",
                ),
                (
                    "musicgen-long-form",
                    {"duration_seconds": 45, "extend_stride_seconds": 4},
                    "between 5 and 29",
                ),
                (
                    "musicgen-long-form",
                    {"duration_seconds": 45, "extend_stride_seconds": 30},
                    "between 5 and 29",
                ),
            )
            for model_id, params, message in rejected:
                with self.subTest(model_id=model_id, params=params):
                    with self.assertRaisesRegex(ValueError, message):
                        generator.validate_request(
                            GenerationRequest(
                                media_type="audio",
                                prompt="invalid boundary cue",
                                model_id=model_id,
                                output_format="wav",
                                params=params,
                            )
                        )

    def test_api_rejects_invalid_long_form_before_creating_a_job(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                manifest_root=_write_manifest_root(root),
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            response = client.post(
                "/generate/audio",
                json={
                    "prompt": "too long",
                    "model_id": "musicgen-long-form",
                    "params": {
                        "duration_seconds": 121,
                        "extend_stride_seconds": 18,
                    },
                },
            )

            self.assertEqual(response.status_code, 422)
            self.assertEqual(services.job_repository.list(), [])
            self.assertEqual(services.job_queue.size(), 0)

    def test_45_second_job_reports_segments_and_reaches_gallery_and_export(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            model = _FakeAudioCraftModel()
            services = create_application_services(
                manifest_root=_write_manifest_root(root),
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            with (
                patch(
                    "core.models.loader.AudioCraftMusicgenLoader.load",
                    new=lambda _loader, manifest: _runtime_for(manifest, model),
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                response = client.post(
                    "/generate/audio",
                    json={
                        "prompt": "evolving orchestral cue",
                        "model_id": "musicgen-long-form",
                        "output_format": "wav",
                        "seed": 28,
                        "params": {
                            "duration_seconds": 45,
                            "extend_stride_seconds": 10,
                        },
                    },
                )
                self.assertEqual(response.status_code, 201)
                completed = services.job_runner.run_once()

            self.assertIsNotNone(completed)
            assert completed is not None and completed.result is not None
            self.assertEqual(completed.status, "succeeded")
            self.assertAlmostEqual(
                completed.result.metadata["final_duration_seconds"],
                45.0,
                places=3,
            )
            self.assertEqual(completed.result.metadata["segment_count"], 3)
            self.assertEqual(completed.result.metadata["extend_stride_seconds"], 10.0)
            self.assertEqual(model.params["duration"], 45.0)
            self.assertEqual(model.params["extend_stride"], 10.0)

            segment_events = [
                event
                for event in services.event_bus.list_events()
                if event.type == "job_segment_progress"
            ]
            self.assertEqual(
                [event.payload["segment"] for event in segment_events],
                [1, 2, 3],
            )
            asset = services.asset_repository.get_primary_by_job(completed.id)
            self.assertIsNotNone(asset)
            assert asset is not None
            gallery_response = client.get(f"/gallery/job/{completed.id}")
            self.assertEqual(gallery_response.status_code, 200)
            export_response = client.post(
                f"/gallery/{asset.id}/export",
                json={
                    "destination_dir": str(root / "outputs" / "exports" / "audio"),
                    "destination_name": "long-form-cue.wav",
                    "include_metadata": True,
                },
            )
            self.assertEqual(export_response.status_code, 200)
            self.assertTrue(Path(export_response.json()["export_path"]).is_file())
            self.assertTrue(Path(export_response.json()["metadata_path"]).is_file())

    def test_120_second_generation_uses_default_stride_18(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            model = _FakeAudioCraftModel()
            service = create_default_model_service(_write_manifest_root(root))
            generator = AudioGenerator(service, output_dir=root / "audio")

            with (
                patch(
                    "core.models.loader.AudioCraftMusicgenLoader.load",
                    new=lambda _loader, manifest: _runtime_for(manifest, model),
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="two minute evolving cue",
                        model_id="musicgen-long-form",
                        output_format="wav",
                        params={"duration_seconds": 120},
                    )
                )

            self.assertAlmostEqual(
                result.metadata["final_duration_seconds"],
                120.0,
                places=3,
            )
            self.assertEqual(result.metadata["extend_stride_seconds"], 18.0)
            self.assertEqual(result.metadata["segment_count"], 6)
            self.assertEqual(model.params["extend_stride"], 18.0)

    def test_long_form_generation_applies_shared_music_postprocessing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            model = _FakeAudioCraftModel()
            service = create_default_model_service(_write_manifest_root(root))
            generator = AudioGenerator(service, output_dir=root / "audio")

            with (
                patch(
                    "core.models.loader.AudioCraftMusicgenLoader.load",
                    new=lambda _loader, manifest: _runtime_for(manifest, model),
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="evolving orchestral cue",
                        model_id="musicgen-long-form",
                        output_format="wav",
                        params={"duration_seconds": 45},
                    )
                )

            audio_postprocess = result.metadata["audio_postprocess"]
            self.assertEqual(audio_postprocess["preset"], "music")
            self.assertTrue(audio_postprocess["enabled"])
            self.assertEqual(
                audio_postprocess["chain"],
                ["normalize_rms", "apply_fades", "normalize_peak"],
            )
            self.assertEqual(result.metadata["params"]["postprocess"], True)
            # Music post-processing never trims, so segment duration is unaffected.
            self.assertAlmostEqual(
                result.metadata["final_duration_seconds"], 45.0, places=3
            )

    def test_long_form_generation_can_disable_postprocessing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            model = _FakeAudioCraftModel()
            service = create_default_model_service(_write_manifest_root(root))
            generator = AudioGenerator(service, output_dir=root / "audio")

            with (
                patch(
                    "core.models.loader.AudioCraftMusicgenLoader.load",
                    new=lambda _loader, manifest: _runtime_for(manifest, model),
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                result = generator.run(
                    GenerationRequest(
                        media_type="audio",
                        prompt="evolving orchestral cue",
                        model_id="musicgen-long-form",
                        output_format="wav",
                        params={"duration_seconds": 45, "postprocess": False},
                    )
                )

            audio_postprocess = result.metadata["audio_postprocess"]
            self.assertEqual(audio_postprocess["preset"], "music")
            self.assertFalse(audio_postprocess["enabled"])
            self.assertEqual(audio_postprocess["chain"], [])
            self.assertEqual(result.metadata["params"]["postprocess"], False)
            self.assertTrue(Path(result.outputs[0]).exists())

    def test_segment_failure_publishes_no_wav_or_gallery_asset(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            model = _FakeAudioCraftModel(fail_segment=2)
            services = create_application_services(
                manifest_root=_write_manifest_root(root),
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            generator = services.generator_registry.get("audio")

            with patch(
                "core.models.loader.AudioCraftMusicgenLoader.load",
                new=lambda _loader, manifest: _runtime_for(manifest, model),
            ):
                job = services.job_service.create_job(
                    GenerationRequest(
                        media_type="audio",
                        prompt="failing long cue",
                        model_id="musicgen-long-form",
                        output_format="wav",
                        params={
                            "duration_seconds": 45,
                            "extend_stride_seconds": 10,
                        },
                    )
                )
                failed = services.job_runner.run_once()

            self.assertIsNotNone(failed)
            assert failed is not None
            self.assertEqual(failed.status, "failed")
            self.assertIn("segment 2 failed", failed.error_message or "")
            self.assertIsNone(services.asset_repository.get_primary_by_job(job.id))
            self.assertEqual(list(generator.output_dir.glob("*.wav")), [])

    def test_boundary_cancellation_publishes_no_partial_asset(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            holder: dict[str, object] = {}

            def cancel_before_second_segment(segment: int) -> None:
                if segment == 2:
                    services = holder["services"]
                    job_id = holder["job_id"]
                    services.job_service.cancel_job(job_id)

            model = _FakeAudioCraftModel(before_segment=cancel_before_second_segment)
            services = create_application_services(
                manifest_root=_write_manifest_root(root),
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            holder["services"] = services
            generator = services.generator_registry.get("audio")

            with patch(
                "core.models.loader.AudioCraftMusicgenLoader.load",
                new=lambda _loader, manifest: _runtime_for(manifest, model),
            ):
                job = services.job_service.create_job(
                    GenerationRequest(
                        media_type="audio",
                        prompt="cancelled long cue",
                        model_id="musicgen-long-form",
                        output_format="wav",
                        params={
                            "duration_seconds": 45,
                            "extend_stride_seconds": 10,
                        },
                    )
                )
                holder["job_id"] = job.id
                cancelled = services.job_runner.run_once()

            self.assertIsNotNone(cancelled)
            assert cancelled is not None
            self.assertEqual(cancelled.status, "cancelled")
            self.assertIsNone(services.asset_repository.get_primary_by_job(job.id))
            self.assertEqual(list(generator.output_dir.glob("*.wav")), [])

    def test_readiness_requires_dependency_checkpoints_and_local_t5(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "audiocraft"
            _write_audiocraft_files(root)

            with patch("core.model_readiness.importlib.util.find_spec", return_value=None):
                missing_dependency = evaluate_readiness(
                    runtime="audiocraft",
                    local_path=str(root),
                )
            self.assertEqual(missing_dependency.status, STATUS_MISSING_FILES)
            self.assertIn("python:audiocraft", missing_dependency.missing)

            with patch(
                "core.model_readiness.importlib.util.find_spec",
                return_value=object(),
            ):
                (root / "state_dict.bin").unlink()
                missing_weights = evaluate_readiness(
                    runtime="audiocraft",
                    local_path=str(root),
                )
                self.assertEqual(missing_weights.status, STATUS_MISSING_FILES)
                self.assertIn("state_dict.bin", missing_weights.missing)

                (root / "state_dict.bin").write_bytes(b"lm")
                (root / "t5-base" / "spiece.model").unlink()
                missing_t5 = evaluate_readiness(
                    runtime="audiocraft",
                    local_path=str(root),
                )
                self.assertIn("t5-base/spiece.model", missing_t5.missing)

                (root / "t5-base" / "spiece.model").write_bytes(b"t5")
                ready = evaluate_readiness(runtime="audiocraft", local_path=str(root))
                self.assertEqual(ready.status, STATUS_READY)

    def test_models_endpoint_exposes_long_form_unavailability_reason(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                manifest_root=_write_manifest_root(root),
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            response = client.get("/models", params={"media_type": "audio"})

            self.assertEqual(response.status_code, 200)
            long_form = next(
                model
                for model in response.json()["models"]
                if model["id"] == "musicgen-long-form"
            )
            self.assertFalse(long_form["is_available"])
            self.assertEqual(long_form["runtime_status"], "missing_files")
            self.assertIn("Local model path is missing", long_form["availability_message"])

    def test_loader_defaults_to_cpu_without_cuda_and_installs_xformers_shim(self) -> None:
        loader = AudioCraftMusicgenLoader()

        class _Cuda:
            @staticmethod
            def is_available() -> bool:
                return False

        class _Torch:
            cuda = _Cuda()

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(loader._resolve_device(_Torch()), "cpu")
        loader._install_xformers_import_shim()
        self.assertIn("xformers.ops", __import__("sys").modules)


if __name__ == "__main__":
    unittest.main()
