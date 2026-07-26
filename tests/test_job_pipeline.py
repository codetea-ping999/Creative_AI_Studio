"""Tests for the initial job execution pipeline."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

CORE_IMPORT_ERROR: Exception | None = None

try:
    from bootstrap import create_application_services
    from core.jobs import CancellationRegistry, EventBus, JobQueue, JobRunner, JobService
    from core.jobs.context import GenerationCancelled
    from core.schemas import GenerationRequest, GenerationResult
    from core.storage.repositories.job_repository import JobRepository
    from generators.base import BaseGenerator
    from generators.registry import GeneratorRegistry
except ModuleNotFoundError as exc:
    CORE_IMPORT_ERROR = exc

API_IMPORT_ERROR: Exception | None = None

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
except ModuleNotFoundError as exc:
    API_IMPORT_ERROR = exc


if CORE_IMPORT_ERROR is None:
    class _FailingImageGenerator(BaseGenerator):
        def validate_request(self, request: GenerationRequest) -> None:
            return None

        def prepare(self, request: GenerationRequest) -> None:
            return None

        def generate(self, request: GenerationRequest, context=None):
            raise RuntimeError("stub generation failure")

        def cleanup(self, request: GenerationRequest) -> None:
            return None

    class _StubAudioGenerator(BaseGenerator):
        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir

        def validate_request(self, request: GenerationRequest) -> None:
            return None

        def prepare(self, request: GenerationRequest) -> None:
            self.output_dir.mkdir(parents=True, exist_ok=True)

        def generate(self, request: GenerationRequest, context=None):
            output_path = self.output_dir / "stub.wav"
            output_path.write_bytes(b"RIFFstub")
            from core.schemas import GenerationResult

            return GenerationResult(
                job_id="audio-stub",
                status="succeeded",
                outputs=[str(output_path)],
                previews=[],
                metadata={"media_type": "audio"},
                error_message=None,
            )

        def cleanup(self, request: GenerationRequest) -> None:
            return None

    class _CancelDuringGenerateGenerator(BaseGenerator):
        """Cancels the in-flight job while generating, then returns success."""

        def __init__(self, output_dir: Path, on_generate) -> None:
            self.output_dir = output_dir
            self._on_generate = on_generate

        def validate_request(self, request: GenerationRequest) -> None:
            return None

        def prepare(self, request: GenerationRequest) -> None:
            self.output_dir.mkdir(parents=True, exist_ok=True)

        def generate(self, request: GenerationRequest, context=None):
            self._on_generate()
            output_path = self.output_dir / "stub.wav"
            output_path.write_bytes(b"RIFFstub")
            from core.schemas import GenerationResult

            return GenerationResult(
                job_id="cancel-stub",
                status="succeeded",
                outputs=[str(output_path)],
                previews=[],
                metadata={"media_type": "audio"},
                error_message=None,
            )

        def cleanup(self, request: GenerationRequest) -> None:
            return None

    class _StepReportingGenerator(BaseGenerator):
        """Mimics a Diffusers-style step loop that reports progress via context
        and checks for cancellation after every step, like ImageGenerator's
        callback_on_step_end integration."""

        def __init__(
            self,
            output_dir: Path,
            total_steps: int,
            cancel_after_step: int | None = None,
            on_cancel_step=None,
        ) -> None:
            self.output_dir = output_dir
            self.total_steps = total_steps
            self.cancel_after_step = cancel_after_step
            self.on_cancel_step = on_cancel_step
            self.steps_run = 0
            self.reported_fractions: list[float] = []

        def validate_request(self, request: GenerationRequest) -> None:
            return None

        def prepare(self, request: GenerationRequest) -> None:
            self.output_dir.mkdir(parents=True, exist_ok=True)

        def generate(self, request: GenerationRequest, context=None):
            for step in range(1, self.total_steps + 1):
                self.steps_run = step
                fraction = step / self.total_steps
                if context is not None:
                    context.report_progress(fraction)
                    self.reported_fractions.append(fraction)
                if self.cancel_after_step is not None and step == self.cancel_after_step:
                    # Simulate an external cancel request landing exactly here.
                    assert self.on_cancel_step is not None
                    self.on_cancel_step()
                if context is not None:
                    context.raise_if_cancelled()

            output_path = self.output_dir / "step-stub.wav"
            output_path.write_bytes(b"RIFFstub")
            return GenerationResult(
                job_id="step-stub",
                status="succeeded",
                outputs=[str(output_path)],
                previews=[],
                metadata={"media_type": "audio"},
                error_message=None,
            )

        def cleanup(self, request: GenerationRequest) -> None:
            return None


class _FakePipelineResult:
    def __init__(self, image: Image.Image) -> None:
        self.images = [image]


class _FakePipeline:
    def __init__(self) -> None:
        self.loaded_loras: list[dict[str, object]] = []
        self.adapter_calls: list[dict[str, object]] = []
        self.unload_calls = 0

    def __call__(self, **kwargs: object) -> _FakePipelineResult:
        width = int(kwargs.get("width", 64))
        height = int(kwargs.get("height", 64))
        return _FakePipelineResult(Image.new("RGB", (width, height), color=(98, 88, 77)))

    def load_lora_weights(self, source: str, **kwargs: object) -> None:
        self.loaded_loras.append({"source": source, **kwargs})

    def set_adapters(
        self,
        adapter_names: str | list[str],
        adapter_weights: float | list[float] | None = None,
    ) -> None:
        self.adapter_calls.append(
            {"adapter_names": adapter_names, "adapter_weights": adapter_weights}
        )

    def unload_lora_weights(self) -> None:
        self.unload_calls += 1

    def delete_adapters(self, adapter_names: str | list[str]) -> None:
        return None


def _fake_diffusers_load(self, manifest):
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
        "weight_dtype": "float16",
        "variant": "fp16",
        "device": "cpu",
        "default_params": dict(manifest.default_params),
        "path_exists": True,
        "pipeline": _FakePipeline(),
    }


@unittest.skipIf(CORE_IMPORT_ERROR is not None, f"missing dependency: {CORE_IMPORT_ERROR}")
class JobPipelineTests(unittest.TestCase):
    @unittest.skipIf(API_IMPORT_ERROR is not None, f"missing dependency: {API_IMPORT_ERROR}")
    def test_generate_image_job_runs_end_to_end(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            with patch("core.models.loader.DiffusersImageLoader.load", new=_fake_diffusers_load):
                root = Path(tmp_dir)
                services = create_application_services(
                    db_path=root / "jobs.db",
                    output_dir=root / "outputs" / "images",
                )
                client = TestClient(create_app(services, start_job_runner=False))

                create_response = client.post(
                    "/generate/image",
                    json={
                        "prompt": "A cinematic skyline",
                        "model_id": "sdxl",
                        "params": {"steps": 10},
                    },
                )

                self.assertEqual(create_response.status_code, 201)
                payload = create_response.json()
                job_id = payload["job_id"]
                self.assertEqual(payload["status"], "queued")
                self.assertEqual(services.job_queue.size(), 1)

                processed_job = services.job_runner.run_once()

                self.assertIsNotNone(processed_job)
                assert processed_job is not None
                self.assertEqual(processed_job.id, job_id)
                self.assertEqual(processed_job.status, "succeeded")
                self.assertEqual(processed_job.result.job_id, job_id)
                self.assertTrue(Path(processed_job.result.outputs[0]).exists())

                get_response = client.get(f"/jobs/{job_id}")
                self.assertEqual(get_response.status_code, 200)
                job_payload = get_response.json()
                self.assertEqual(job_payload["status"], "succeeded")
                self.assertEqual(job_payload["result"]["job_id"], job_id)
                self.assertEqual(job_payload["progress"], 1.0)

                list_response = client.get("/jobs")
                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(len(list_response.json()), 1)

    def test_runner_marks_job_failed_when_generator_raises(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repository = JobRepository(Path(tmp_dir) / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            service = JobService(repository, queue, event_bus)
            registry = GeneratorRegistry({"image": _FailingImageGenerator()})
            runner = JobRunner(repository, queue, registry, event_bus)

            job = service.create_job(
                GenerationRequest(
                    media_type="image",
                    prompt="Failure case",
                    model_id="sdxl",
                    params={},
                )
            )

            failed_job = runner.run_once()

            self.assertIsNotNone(failed_job)
            assert failed_job is not None
            self.assertEqual(failed_job.id, job.id)
            self.assertEqual(failed_job.status, "failed")
            self.assertEqual(failed_job.error_message, "stub generation failure")

            persisted = repository.get(job.id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "failed")
            self.assertEqual(persisted.error_message, "stub generation failure")

    def test_runner_processes_audio_jobs_with_registered_generator(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repository = JobRepository(root / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            service = JobService(repository, queue, event_bus)
            registry = GeneratorRegistry({"audio": _StubAudioGenerator(root / "outputs" / "audio")})
            runner = JobRunner(repository, queue, registry, event_bus)

            job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="dreamy synth loop",
                    model_id="",
                    output_format="wav",
                    params={"duration_seconds": 4},
                )
            )

            completed_job = runner.run_once()

            self.assertIsNotNone(completed_job)
            assert completed_job is not None
            self.assertEqual(completed_job.id, job.id)
            self.assertEqual(completed_job.status, "succeeded")
            self.assertEqual(completed_job.result.metadata["media_type"], "audio")
            self.assertTrue(Path(completed_job.result.outputs[0]).exists())

    @unittest.skipIf(API_IMPORT_ERROR is not None, f"missing dependency: {API_IMPORT_ERROR}")
    def test_cancel_endpoint_marks_queued_job_and_runner_skips_it(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repository = JobRepository(root / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            service = JobService(repository, queue, event_bus)
            registry = GeneratorRegistry({"audio": _StubAudioGenerator(root / "outputs" / "audio")})
            runner = JobRunner(repository, queue, registry, event_bus)

            from bootstrap import ApplicationServices, create_default_model_service
            from core.assets import AssetRepository
            from core.feedback import FeedbackRepository
            from core.projects import ProjectRepository

            asset_repository = AssetRepository(root / "assets")
            services = ApplicationServices(
                output_dir=root / "outputs" / "images",
                model_service=create_default_model_service(),
                generator_registry=registry,
                job_repository=repository,
                job_queue=queue,
                event_bus=event_bus,
                job_service=service,
                job_runner=runner,
                project_repository=ProjectRepository(root / "projects"),
                feedback_repository=FeedbackRepository(root / "feedback"),
                asset_repository=asset_repository,
            )
            client = TestClient(create_app(services, start_job_runner=False))
            job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="cancel this queued job",
                    model_id="",
                    output_format="wav",
                    params={},
                )
            )

            response = client.post(f"/jobs/{job.id}/cancel")
            skipped_job = runner.run_once()

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "cancelled")
            self.assertIsNotNone(skipped_job)
            assert skipped_job is not None
            self.assertEqual(skipped_job.status, "cancelled")
            self.assertFalse((root / "outputs" / "audio" / "stub.wav").exists())

    def test_runner_honors_cancellation_that_lands_during_generation(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repository = JobRepository(root / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            service = JobService(repository, queue, event_bus)

            job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="cancel me mid-flight",
                    model_id="",
                    output_format="wav",
                    params={},
                )
            )

            def _cancel_mid_run() -> None:
                service.cancel_job(job.id)

            registry = GeneratorRegistry(
                {
                    "audio": _CancelDuringGenerateGenerator(
                        root / "outputs" / "audio",
                        _cancel_mid_run,
                    )
                }
            )
            runner = JobRunner(repository, queue, registry, event_bus)

            final_job = runner.run_once()

            self.assertIsNotNone(final_job)
            assert final_job is not None
            # A cancel that lands while generating must not be clobbered by the
            # success transition.
            self.assertEqual(final_job.status, "cancelled")
            persisted = repository.get(job.id)
            assert persisted is not None
            self.assertEqual(persisted.status, "cancelled")
            self.assertNotEqual(persisted.status, "succeeded")

    def test_runner_honors_cancellation_at_postprocessing_transition(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repository = JobRepository(root / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            service = JobService(repository, queue, event_bus)
            registry = GeneratorRegistry(
                {"audio": _StubAudioGenerator(root / "outputs" / "audio")}
            )
            runner = JobRunner(
                repository,
                queue,
                registry,
                event_bus,
                job_service=service,
            )
            job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="cancel at the completion boundary",
                    model_id="",
                    output_format="wav",
                    params={},
                )
            )
            original_update_status = runner._update_status

            def _cancel_before_postprocessing(
                job_id: str,
                status: str,
                *,
                progress: float | None = None,
            ):
                if status == "postprocessing":
                    service.cancel_job(job_id)
                return original_update_status(job_id, status, progress=progress)

            with patch.object(
                runner,
                "_update_status",
                side_effect=_cancel_before_postprocessing,
            ):
                final_job = runner.run_once()

            self.assertIsNotNone(final_job)
            assert final_job is not None
            self.assertEqual(final_job.status, "cancelled")
            persisted = repository.get(job.id)
            assert persisted is not None
            self.assertEqual(persisted.status, "cancelled")

    def test_runner_reports_step_progress_through_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repository = JobRepository(root / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            cancellation_registry = CancellationRegistry()
            service = JobService(
                repository,
                queue,
                event_bus,
                cancellation_registry=cancellation_registry,
            )
            generator = _StepReportingGenerator(root / "outputs" / "audio", total_steps=5)
            registry = GeneratorRegistry({"audio": generator})
            runner = JobRunner(
                repository,
                queue,
                registry,
                event_bus,
                job_service=service,
                cancellation_registry=cancellation_registry,
            )

            job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="watch me climb",
                    model_id="",
                    output_format="wav",
                    params={},
                )
            )

            final_job = runner.run_once()

            self.assertIsNotNone(final_job)
            assert final_job is not None
            self.assertEqual(final_job.status, "succeeded")
            self.assertEqual(generator.reported_fractions, [0.2, 0.4, 0.6, 0.8, 1.0])

            progress_events = [
                event.payload["progress"]
                for event in event_bus.list_events()
                if event.type == "job_progress"
            ]
            # Progress climbs monotonically within the running band (0.1-0.9)
            # and every write lands strictly inside that band since the
            # reported fractions never reach 0.0 or 1.0 at step boundaries
            # that would exceed it.
            self.assertEqual(len(progress_events), 5)
            self.assertEqual(progress_events, sorted(progress_events))
            for value in progress_events:
                self.assertGreaterEqual(value, 0.1)
                self.assertLessEqual(value, 0.9)

    def test_runner_cancels_mid_generation_via_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repository = JobRepository(root / "jobs.db")
            queue = JobQueue()
            event_bus = EventBus()
            cancellation_registry = CancellationRegistry()
            service = JobService(
                repository,
                queue,
                event_bus,
                cancellation_registry=cancellation_registry,
            )

            job_holder: dict[str, str] = {}

            def _cancel_via_service() -> None:
                service.cancel_job(job_holder["id"])

            generator = _StepReportingGenerator(
                root / "outputs" / "audio",
                total_steps=5,
                cancel_after_step=2,
                on_cancel_step=_cancel_via_service,
            )
            registry = GeneratorRegistry({"audio": generator})
            runner = JobRunner(
                repository,
                queue,
                registry,
                event_bus,
                job_service=service,
                cancellation_registry=cancellation_registry,
            )

            job = service.create_job(
                GenerationRequest(
                    media_type="audio",
                    prompt="cancel me at step 2 of 5",
                    model_id="",
                    output_format="wav",
                    params={},
                )
            )
            job_holder["id"] = job.id

            final_job = runner.run_once()

            self.assertIsNotNone(final_job)
            assert final_job is not None
            self.assertEqual(final_job.status, "cancelled")
            # The generator observed the cancel via context.raise_if_cancelled()
            # right after step 2 and never ran steps 3-5.
            self.assertEqual(generator.steps_run, 2)
            self.assertFalse((root / "outputs" / "audio" / "step-stub.wav").exists())

            persisted = repository.get(job.id)
            assert persisted is not None
            self.assertEqual(persisted.status, "cancelled")
            self.assertNotEqual(persisted.status, "succeeded")

    @unittest.skipIf(API_IMPORT_ERROR is not None, f"missing dependency: {API_IMPORT_ERROR}")
    def test_gallery_job_recovers_when_success_precedes_asset_sync(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            services = create_application_services(
                db_path=root / "data" / "jobs.db",
                output_dir=root / "outputs" / "images",
            )
            output_path = root / "outputs" / "audio" / "late-sync.wav"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"RIFFstub")

            with TestClient(create_app(services, start_job_runner=False)) as client:
                job = services.job_service.create_job(
                    GenerationRequest(
                        media_type="audio",
                        prompt="visible before gallery sync",
                        model_id="musicgen-small",
                        output_format="wav",
                        params={},
                    )
                )
                persisted = services.job_repository.update(
                    job.id,
                    status="succeeded",
                    progress=1.0,
                    result=GenerationResult(
                        job_id=job.id,
                        status="succeeded",
                        outputs=[str(output_path)],
                        previews=[],
                        metadata={"media_type": "audio"},
                        error_message=None,
                    ),
                )
                self.assertIsNotNone(persisted)
                self.assertIsNone(services.asset_repository.get_primary_by_job(job.id))

                response = client.get(f"/gallery/job/{job.id}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job_id"], job.id)
            self.assertIsNotNone(services.asset_repository.get_primary_by_job(job.id))


if __name__ == "__main__":
    unittest.main()
