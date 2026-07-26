"""Tests for the local MusicGen smoke command."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import wave

from core.model_readiness import ModelReadiness, STATUS_MISSING_FILES, STATUS_READY
from core.schemas import GenerationResult
from scripts import smoke_musicgen


class _FakeModelService:
    def get_manifest(self, model_id: str, media_type: str, task_type: str):
        return SimpleNamespace(
            id=f"{model_id}-local",
            display_name=model_id,
            runtime="transformers",
            local_path=f"./models/audio/{model_id}",
            default_params={},
        )

    def unload_all(self) -> None:
        return None


class _FakeAudioGenerator:
    def __init__(self, model_service, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)

    def run(self, request) -> GenerationResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / (
            f"{request.model_id}-{request.params['duration_seconds']}-{request.seed}.wav"
        )
        sample_rate = 100
        duration = int(request.params["duration_seconds"])
        parameter_bytes = json.dumps(
            request.params,
            sort_keys=True,
            ensure_ascii=True,
        ).encode("ascii")
        sample_value = (int(request.seed or 0) + sum(parameter_bytes)) % 32767
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(
                sample_value.to_bytes(2, "little", signed=True) * sample_rate * duration
            )
        return GenerationResult(
            job_id="job-smoke",
            status="succeeded",
            outputs=[str(output_path)],
            metadata={
                "conditioning_prompt": request.prompt,
                "device": "cpu",
                "torch_dtype": "float32",
                "load_dtype": "float32",
                "quality_report": {},
            },
        )


def test_missing_weights_skip_with_success_exit(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    with (
        patch.object(
            smoke_musicgen,
            "create_default_model_service",
            return_value=_FakeModelService(),
        ),
        patch.object(
            smoke_musicgen,
            "evaluate_manifest_readiness",
            return_value=ModelReadiness(
                STATUS_MISSING_FILES,
                "weights are missing",
            ),
        ),
        patch.object(smoke_musicgen, "AudioGenerator") as audio_generator,
    ):
        exit_code = smoke_musicgen.main(["--report", str(report_path)])

    assert exit_code == 0
    assert "[SKIP]" in capsys.readouterr().out
    assert not audio_generator.called
    assert json.loads(report_path.read_text())["runs"] == []


def test_repeat_and_changed_seed_are_verified(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    with (
        patch.object(
            smoke_musicgen,
            "create_default_model_service",
            return_value=_FakeModelService(),
        ),
        patch.object(
            smoke_musicgen,
            "evaluate_manifest_readiness",
            return_value=ModelReadiness(STATUS_READY, "ready"),
        ),
        patch.object(smoke_musicgen, "AudioGenerator", _FakeAudioGenerator),
    ):
        exit_code = smoke_musicgen.main(
            [
                "--duration",
                "2",
                "--seed",
                "42",
                "--repeat",
                "2",
                "--compare-seed",
                "43",
                "--output-dir",
                str(tmp_path / "outputs"),
                "--report",
                str(report_path),
            ]
        )

    report = json.loads(report_path.read_text())
    assert exit_code == 0
    assert len(report["runs"]) == 3
    assert report["reproducibility"] == [
        {
            "model_id": "musicgen-small",
            "duration_seconds": 2,
            "seed": 42,
            "same_seed_identical": True,
        },
        {
            "model_id": "musicgen-small",
            "duration_seconds": 2,
            "seed": 43,
            "different_seed_changed": True,
        },
    ]


def test_parameter_sweep_records_every_supported_control(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    with (
        patch.object(
            smoke_musicgen,
            "create_default_model_service",
            return_value=_FakeModelService(),
        ),
        patch.object(
            smoke_musicgen,
            "evaluate_manifest_readiness",
            return_value=ModelReadiness(STATUS_READY, "ready"),
        ),
        patch.object(smoke_musicgen, "AudioGenerator", _FakeAudioGenerator),
    ):
        exit_code = smoke_musicgen.main(
            [
                "--duration",
                "2",
                "--seed",
                "42",
                "--parameter-sweep",
                "--output-dir",
                str(tmp_path / "outputs"),
                "--report",
                str(report_path),
            ]
        )

    report = json.loads(report_path.read_text())
    assert exit_code == 0
    assert len(report["runs"]) == len(smoke_musicgen.PARAMETER_SWEEP)
    assert {
        effect["parameter"] for effect in report["parameter_effects"]
    } == {
        "mood",
        "bpm",
        "genre",
        "instruments",
        "structure",
        "guidance_scale",
        "temperature",
        "top_k",
        "top_p",
    }
    assert all(effect["changed_from_baseline"] for effect in report["parameter_effects"])
