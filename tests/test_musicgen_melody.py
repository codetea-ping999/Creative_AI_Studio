"""MusicGen Melody validation, conditioning, and Gallery lineage tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient

from apps.api.main import create_app
from bootstrap import create_application_services
from core.assets import Asset
from core.audio_conditioning import inspect_wav_reference, prepare_wav_reference


class _FakeMelodyProcessor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        payload = {
            "input_ids": torch.ones((1, 4), dtype=torch.long),
            "attention_mask": torch.ones((1, 4), dtype=torch.long),
        }
        if "audio" in kwargs:
            payload["input_features"] = torch.ones((1, 12, 8), dtype=torch.float32)
        return payload


class _FakeMelodyModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs):
        import torch

        self.calls.append(kwargs)
        return torch.zeros((1, 1, 64_000), dtype=torch.float32)


def _fake_musicgen_runtime(self, manifest):
    return {
        "stub": False,
        "loader": self.__class__.__name__,
        "manifest_id": manifest.id,
        "display_name": manifest.display_name,
        "runtime": manifest.runtime,
        "provider": manifest.provider,
        "local_path": manifest.local_path,
        "remote_ref": manifest.remote_ref,
        "dtype": manifest.dtype,
        "load_dtype": "float32",
        "torch_dtype": "float32",
        "weight_dtype": "float32",
        "device": "cpu",
        "default_params": dict(manifest.default_params),
        "path_exists": True,
        "processor": _FakeMelodyProcessor(),
        "model": _FakeMelodyModel(),
        "sampling_rate": 32_000,
        "frame_rate": 50,
    }


def _write_pcm_wav(
    path: Path,
    *,
    channels: int = 1,
    sampling_rate: int = 8_000,
    duration_seconds: float = 1.0,
) -> None:
    frame_count = round(sampling_rate * duration_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sampling_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count * channels)


class MusicgenMelodyTests(unittest.TestCase):
    def test_wav_reference_is_validated_mixed_to_mono_and_resampled(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "stereo.wav"
            _write_pcm_wav(
                wav_path,
                channels=2,
                sampling_rate=8_000,
                duration_seconds=2,
            )

            info = inspect_wav_reference(wav_path, max_duration_seconds=30)
            import torch

            prepared, prepared_info = prepare_wav_reference(
                wav_path,
                target_sampling_rate=32_000,
                max_duration_seconds=30,
                torch=torch,
            )

            self.assertEqual(info.channels, 2)
            self.assertEqual(prepared_info, info)
            self.assertEqual(tuple(prepared.shape), (64_000,))

    def test_wav_reference_rejects_non_wav_multichannel_and_too_long(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            non_wav = root / "reference.mp3"
            non_wav.write_bytes(b"not an mp3")
            multichannel = root / "three-channel.wav"
            _write_pcm_wav(multichannel, channels=3)
            empty = root / "empty.wav"
            _write_pcm_wav(empty, duration_seconds=0)
            too_short = root / "too-short.wav"
            _write_pcm_wav(too_short, duration_seconds=0.5)
            too_long = root / "too-long.wav"
            _write_pcm_wav(
                too_long,
                sampling_rate=1_000,
                duration_seconds=31,
            )

            cases = (
                (non_wav, "Gallery WAV"),
                (multichannel, "one or two channels"),
                (empty, "too short"),
                (too_short, "too short"),
                (too_long, "too long"),
            )
            for path, message in cases:
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(ValueError, message):
                        inspect_wav_reference(path, max_duration_seconds=30)

    def test_gallery_melody_reuse_validates_before_queue_and_persists_lineage(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            client = TestClient(create_app(services, start_job_runner=False))

            with patch(
                "core.models.loader.TransformersMusicgenLoader.load",
                new=_fake_musicgen_runtime,
            ):
                create_response = client.post(
                    "/generate/audio",
                    json={
                        "prompt": "simple piano motif",
                        "model_id": "musicgen-small",
                        "output_format": "wav",
                        "seed": 17,
                        "params": {"duration_seconds": 2},
                    },
                )
                self.assertEqual(create_response.status_code, 201)
                source_job = services.job_runner.run_once()

            self.assertIsNotNone(source_job)
            source_asset = services.asset_repository.get_primary_by_job(source_job.id)
            self.assertIsNotNone(source_asset)
            assert source_asset is not None

            initial_job_count = len(services.job_repository.list())
            unsupported_response = client.post(
                f"/gallery/{source_asset.id}/reuse",
                json={
                    "action": "melody",
                    "model_id": "musicgen-small",
                },
            )
            self.assertEqual(unsupported_response.status_code, 400)
            self.assertIn("does not support melody", unsupported_response.json()["detail"])
            self.assertEqual(len(services.job_repository.list()), initial_job_count)

            non_wav_path = root / "reference.mp3"
            non_wav_path.write_bytes(b"invalid")
            non_wav_asset = Asset(
                id="asset_non_wav",
                job_id=source_job.id,
                project_id=None,
                media_type="audio",
                kind="output",
                title="invalid reference",
                prompt="invalid reference",
                model_id="musicgen-small",
                path=str(non_wav_path),
            )
            services.asset_repository.create_or_update(non_wav_asset)
            invalid_reference_response = client.post(
                "/gallery/asset_non_wav/reuse",
                json={
                    "action": "melody",
                    "model_id": "musicgen-melody",
                },
            )
            self.assertEqual(invalid_reference_response.status_code, 400)
            self.assertIn("Gallery WAV", invalid_reference_response.json()["detail"])
            self.assertEqual(len(services.job_repository.list()), initial_job_count)

            reuse_response = client.post(
                f"/gallery/{source_asset.id}/reuse",
                json={
                    "action": "melody",
                    "model_id": "musicgen-melody",
                    "prompt": "orchestral variation following the piano motif",
                    "seed": 19,
                    "params": {"duration_seconds": 2},
                },
            )
            self.assertEqual(reuse_response.status_code, 201)
            derived_job_id = reuse_response.json()["job_id"]

            with (
                patch(
                    "core.models.loader.TransformersMusicgenMelodyLoader.load",
                    new=_fake_musicgen_runtime,
                ),
                patch(
                    "generators.audio.generator.evaluate_audio_semantics",
                    return_value={"status": "skipped"},
                ),
            ):
                derived_job = services.job_runner.run_once()

            self.assertIsNotNone(derived_job)
            assert derived_job is not None and derived_job.result is not None
            self.assertEqual(derived_job.id, derived_job_id)
            self.assertEqual(derived_job.status, "succeeded")
            self.assertEqual(derived_job.result.metadata["reuse_action"], "melody")
            self.assertEqual(
                derived_job.result.metadata["conditioning"]["reference_asset_id"],
                source_asset.id,
            )
            self.assertEqual(
                derived_job.result.metadata["conditioning"]["prepared_sampling_rate"],
                32_000,
            )

            derived_asset = services.asset_repository.get_primary_by_job(derived_job_id)
            self.assertIsNotNone(derived_asset)
            assert derived_asset is not None
            self.assertEqual(derived_asset.parent_asset_id, source_asset.id)
            self.assertIn(source_asset.id, derived_asset.lineage)
            self.assertEqual(
                derived_asset.metadata["conditioning"]["type"],
                "melody",
            )


if __name__ == "__main__":
    unittest.main()
