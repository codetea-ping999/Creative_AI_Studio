"""Tests for local quality evaluation and operational metrics routes."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from PIL import Image

IMPORT_ERROR: Exception | None = None

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services
    from core.quality import evaluate_audio_output, evaluate_image_output
    from core.quality.semantic import evaluate_audio_semantics, evaluate_image_semantics
    from core.schemas import GenerationRequest, GenerationResult
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class QualityMetricsTests(unittest.TestCase):
    def test_image_quality_evaluator_returns_score(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "sample.png"
            Image.new("RGB", (1024, 1024), color=(120, 90, 180)).save(image_path)

            report = evaluate_image_output(image_path)

            self.assertEqual(report["method"], "heuristic_local_v1")
            self.assertIn("quality_score", report)
            self.assertIn("metrics", report)

    def test_audio_quality_evaluator_returns_score(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "sample.wav"
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes((b"\x00\x10" * 16000))

            report = evaluate_audio_output(audio_path)

            self.assertEqual(report["method"], "heuristic_local_v1")
            self.assertIn("quality_score", report)
            self.assertIn("metrics", report)

    def test_semantic_judge_returns_disabled_status_by_default(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "sample.png"
            audio_path = Path(tmp_dir) / "sample.wav"
            Image.new("RGB", (512, 512), color=(90, 120, 150)).save(image_path)
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes((b"\x00\x10" * 16000))

            image_report = evaluate_image_semantics(image_path, "editorial portrait")
            audio_report = evaluate_audio_semantics(audio_path, "dreamy loop")

            self.assertEqual(image_report["status"], "disabled")
            self.assertEqual(audio_report["status"], "disabled")

    def test_metrics_summary_and_lora_catalog_routes(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )

            image_output = root / "outputs" / "images" / "sample.png"
            image_output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 896), color=(110, 130, 160)).save(image_output)

            audio_output = root / "outputs" / "audio" / "sample.wav"
            audio_output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(audio_output), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes((b"\x00\x20" * 16000))

            image_job = services.job_service.create_job(
                GenerationRequest(
                    media_type="image",
                    prompt="hero illustration",
                    model_id="sdxl",
                    output_format="png",
                    params={},
                )
            )
            audio_job = services.job_service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="dreamy synth loop",
                    model_id="musicgen-small",
                    output_format="wav",
                    params={},
                )
            )

            services.job_service.mark_succeeded(
                image_job.id,
                GenerationResult(
                    job_id=image_job.id,
                    status="succeeded",
                    outputs=[str(image_output)],
                    previews=[str(image_output)],
                    metadata={"quality_report": evaluate_image_output(image_output)},
                    error_message=None,
                ),
            )
            services.job_service.mark_succeeded(
                audio_job.id,
                GenerationResult(
                    job_id=audio_job.id,
                    status="succeeded",
                    outputs=[str(audio_output)],
                    previews=[],
                    metadata={"quality_report": evaluate_audio_output(audio_output)},
                    error_message=None,
                ),
            )

            lora_root = root / "models" / "loras"
            lora_root.mkdir(parents=True, exist_ok=True)
            lora_file = lora_root / "mai_style.safetensors"
            lora_file.write_bytes(b"stub")

            client = TestClient(create_app(services, start_job_runner=False))

            with patch.dict("os.environ", {"LORA_ROOT": str(lora_root)}):
                metrics_response = client.get("/metrics/summary")
                lora_response = client.get("/catalog/loras")

            self.assertEqual(metrics_response.status_code, 200)
            metrics_payload = metrics_response.json()
            self.assertEqual(metrics_payload["total_jobs"], 2)
            self.assertEqual(metrics_payload["success_rate"], 100.0)
            self.assertIn("image", metrics_payload["by_media"])
            self.assertIn("audio", metrics_payload["by_media"])
            self.assertIsNotNone(metrics_payload["average_quality_score"])

            self.assertEqual(lora_response.status_code, 200)
            lora_payload = lora_response.json()
            self.assertEqual(len(lora_payload["items"]), 1)
            self.assertEqual(lora_payload["items"][0]["relative_path"], "mai_style.safetensors")

    def test_metrics_summary_excludes_cancelled_jobs_from_running_counts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "jobs.db",
                output_dir=root / "outputs" / "images",
            )

            image_output = root / "outputs" / "images" / "sample.png"
            image_output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1024, 1024), color=(80, 110, 150)).save(image_output)

            succeeded_job = services.job_service.create_job(
                GenerationRequest(
                    media_type="image",
                    prompt="poster illustration",
                    model_id="sdxl",
                    output_format="png",
                    params={},
                )
            )
            failed_job = services.job_service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="noisy loop",
                    model_id="musicgen-small",
                    output_format="wav",
                    params={},
                )
            )
            running_job = services.job_service.create_job(
                GenerationRequest(
                    media_type="image",
                    prompt="work in progress",
                    model_id="sdxl",
                    output_format="png",
                    params={},
                )
            )
            cancelled_job = services.job_service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="cancel me",
                    model_id="musicgen-small",
                    output_format="wav",
                    params={},
                )
            )

            services.job_service.mark_succeeded(
                succeeded_job.id,
                GenerationResult(
                    job_id=succeeded_job.id,
                    status="succeeded",
                    outputs=[str(image_output)],
                    previews=[str(image_output)],
                    metadata={
                        "quality_report": {
                            "quality_score": 91.0,
                            "business_readiness_score": 83.0,
                            "quality_level": "excellent",
                            "semantic_alignment_score": 77.0,
                        }
                    },
                    error_message=None,
                ),
            )
            services.job_service.mark_failed(failed_job.id, "stub failure")
            services.job_service.update_status(running_job.id, "running", progress=0.5)
            services.job_service.cancel_job(cancelled_job.id)
            services.feedback_repository.create(
                succeeded_job.id,
                5,
                semantic_rating=4,
                creative_rating=4,
            )

            client = TestClient(create_app(services, start_job_runner=False))
            response = client.get("/metrics/summary?window_size=2")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["total_jobs"], 4)
            self.assertEqual(payload["succeeded_jobs"], 1)
            self.assertEqual(payload["failed_jobs"], 1)
            self.assertEqual(payload["running_jobs"], 1)
            self.assertEqual(payload["success_rate"], 25.0)
            self.assertEqual(payload["save_success_rate"], 100.0)
            self.assertEqual(payload["average_quality_score"], 91.0)
            self.assertEqual(payload["average_quality_score_calibrated"], 92.1)
            self.assertEqual(payload["average_business_readiness_score"], 83.0)
            self.assertEqual(payload["average_semantic_alignment_score"], 77.0)
            self.assertEqual(payload["average_semantic_alignment_score_calibrated"], 77.4)
            self.assertEqual(payload["latest_quality_level"], "excellent")
            self.assertEqual(payload["recent_window_size"], 2)
            self.assertEqual(payload["recent_success_rate"], 0.0)
            self.assertIsNone(payload["recent_average_quality_score"])

            self.assertEqual(payload["by_media"]["image"]["total_jobs"], 2)
            self.assertEqual(payload["by_media"]["image"]["succeeded_jobs"], 1)
            self.assertEqual(payload["by_media"]["image"]["running_jobs"], 1)
            self.assertEqual(payload["by_media"]["image"]["average_quality_score"], 91.0)
            self.assertEqual(
                payload["by_media"]["image"]["average_quality_score_calibrated"],
                92.1,
            )

            self.assertEqual(payload["by_media"]["audio"]["total_jobs"], 2)
            self.assertEqual(payload["by_media"]["audio"]["failed_jobs"], 1)
            self.assertEqual(payload["by_media"]["audio"]["running_jobs"], 0)
            self.assertEqual(payload["by_media"]["audio"]["success_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
