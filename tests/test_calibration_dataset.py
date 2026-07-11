from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from apps.api.main import create_app
from bootstrap import create_application_services
from core.quality import (
    build_calibration_records,
    build_calibration_report,
    count_calibration_eligible_jobs,
)
from core.schemas import GenerationRequest, GenerationResult


def _succeed_job(services, *, prompt: str, model_id: str, quality_score: float):
    job = services.job_service.create_job(
        GenerationRequest(
            media_type="image",
            prompt=prompt,
            model_id=model_id,
            output_format="png",
            params={},
        )
    )
    output_path = services.output_dir / f"{job.id}.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"image")
    services.job_service.mark_succeeded(
        job.id,
        GenerationResult(
            job_id=job.id,
            status="succeeded",
            outputs=[str(output_path)],
            previews=[str(output_path)],
            metadata={
                "quality_report": {
                    "quality_score": quality_score,
                    "semantic_alignment_score": quality_score - 5,
                    "creative_alignment_score": quality_score - 10,
                }
            },
            error_message=None,
        ),
    )
    return job


def test_calibration_dataset_is_deterministic_and_does_not_apply_updates():
    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        services = create_application_services(
            db_path=root / "jobs.db",
            output_dir=root / "outputs" / "images",
        )
        lower = _succeed_job(
            services,
            prompt="lower quality",
            model_id="sdxl",
            quality_score=40.0,
        )
        higher = _succeed_job(
            services,
            prompt="higher quality",
            model_id="sdxl",
            quality_score=80.0,
        )
        services.feedback_repository.create(
            lower.id,
            2,
            semantic_rating=2,
            creative_rating=2,
            issue_tags=["blur", "motion"],
        )
        services.feedback_repository.create(
            higher.id,
            4,
            semantic_rating=4,
            creative_rating=4,
            export_ready=True,
        )

        records = build_calibration_records(
            services.job_service.list_jobs(),
            services.asset_repository.list_all(),
            services.feedback_repository.list_all(),
        )
        repeated = build_calibration_records(
            services.job_service.list_jobs(),
            services.asset_repository.list_all(),
            services.feedback_repository.list_all(),
        )
        report = build_calibration_report(
            records,
            eligible_job_count=count_calibration_eligible_jobs(
                services.job_service.list_jobs()
            ),
        )

    assert records == repeated
    assert [record["job_id"] for record in records] == [lower.id, higher.id]
    assert records[0]["issue_tags"] == ["blur", "motion"]
    assert report["sample_count"] == 2
    assert report["coverage_rate"] == 100.0
    assert report["recommendation_status"] == "insufficient_data"
    assert report["metrics"]["quality"]["pearson_correlation"] == 1.0
    assert report["automatic_updates_applied"] is False


def test_calibration_metrics_endpoint_supports_model_filter():
    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        services = create_application_services(
            db_path=root / "jobs.db",
            output_dir=root / "outputs" / "images",
        )
        included = _succeed_job(
            services,
            prompt="included",
            model_id="sdxl",
            quality_score=75.0,
        )
        excluded = _succeed_job(
            services,
            prompt="excluded",
            model_id="anime-sdxl",
            quality_score=65.0,
        )
        services.feedback_repository.create(included.id, 4)
        services.feedback_repository.create(excluded.id, 3)
        client = TestClient(create_app(services, start_job_runner=False))

        response = client.get("/metrics/calibration?media_type=image&model_id=sdxl")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sample_count"] == 1
    assert payload["eligible_job_count"] == 1
    assert list(payload["by_model"]) == ["sdxl"]
    assert payload["automatic_updates_applied"] is False
