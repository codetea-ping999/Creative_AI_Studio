#!/usr/bin/env python3
"""Export a local feedback calibration dataset and correlation report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bootstrap import create_application_services
from core.quality import (
    build_calibration_records,
    build_calibration_report,
    count_calibration_eligible_jobs,
    count_calibration_eligible_segments,
)
from core.storage.json_files import write_json_atomic, write_jsonl_atomic


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/jobs.db")
    parser.add_argument(
        "--dataset-output",
        default="data/calibration/feedback-quality.jsonl",
    )
    parser.add_argument(
        "--report-output",
        default="data/calibration/feedback-quality-report.json",
    )
    parser.add_argument("--media-type", choices=("image", "audio", "video"))
    parser.add_argument("--model-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    services = create_application_services(db_path=args.db_path)
    jobs = [
        job
        for job in services.job_service.list_jobs()
        if (args.media_type is None or job.media_type == args.media_type)
        and (args.model_id is None or job.request.model_id == args.model_id)
    ]
    records = build_calibration_records(
        jobs,
        services.asset_repository.list_all(),
        services.feedback_repository.list_all(),
    )
    report = build_calibration_report(
        records,
        eligible_job_count=count_calibration_eligible_jobs(jobs),
        eligible_segments=count_calibration_eligible_segments(jobs),
    )
    write_jsonl_atomic(args.dataset_output, records)
    write_json_atomic(args.report_output, report)
    print(
        json.dumps(
            {
                "dataset_output": str(Path(args.dataset_output)),
                "report_output": str(Path(args.report_output)),
                "sample_count": report["sample_count"],
                "recommendation_status": report["recommendation_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
