#!/usr/bin/env python3
"""Run reproducible local MusicGen WAV smoke and benchmark cases."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import resource
import sys
import time
import wave
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bootstrap import create_default_model_service
from core.models import evaluate_manifest_readiness
from core.schemas import GenerationRequest
from generators.audio import AudioGenerator

DEFAULT_PROMPT = "Warm lo-fi piano, soft drums, clear intro and outro"
DEFAULT_PARAMS: dict[str, Any] = {
    "genre": "lo-fi",
    "instruments": "piano, soft drums",
    "structure": "intro-outro",
    "mood": "warm",
    "bpm": 92,
    "guidance_scale": 3.0,
    "temperature": 1.0,
    "top_k": 250,
    "top_p": 0.0,
}
PARAMETER_SWEEP: tuple[tuple[str, dict[str, Any]], ...] = (
    ("baseline", {}),
    ("mood", {"mood": "energetic"}),
    ("bpm", {"bpm": 140}),
    ("genre", {"genre": "jazz"}),
    ("instruments", {"instruments": "saxophone, upright bass"}),
    ("structure", {"structure": "verse-chorus"}),
    ("guidance_scale", {"guidance_scale": 5.0}),
    ("temperature", {"temperature": 0.7}),
    ("top_k", {"top_k": 100}),
    ("top_p", {"top_p": 0.8}),
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        choices=("musicgen-small", "musicgen-medium"),
        help="Model to validate; repeat to select both (default: musicgen-small).",
    )
    parser.add_argument(
        "--duration",
        action="append",
        dest="durations",
        type=int,
        help="Duration in seconds; repeat for a matrix (default: 2).",
    )
    parser.add_argument("--seed", type=int, default=260726)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--parameter-sweep",
        action="store_true",
        help="Run a baseline plus one real-model case for every supported control.",
    )
    parser.add_argument(
        "--compare-seed",
        type=int,
        help="Generate one additional case and require a different WAV hash.",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "audio")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    for duration in args.durations or [2]:
        if not 2 <= duration <= 30:
            parser.error("--duration must be between 2 and 30 seconds")
    return args


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _peak_rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        value *= 1024
    return round(value / (1024 * 1024), 1)


def _wav_facts(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as wav_file:
        frames = wav_file.getnframes()
        sample_rate = wav_file.getframerate()
        return {
            "channels": wav_file.getnchannels(),
            "sample_rate": sample_rate,
            "frames": frames,
            "duration_seconds": round(frames / sample_rate, 4),
        }


def _environment() -> dict[str, Any]:
    import torch
    import transformers

    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "device_override": __import__("os").getenv("DEVICE", "auto"),
        "cuda_available": torch.cuda.is_available(),
        "mps_available": bool(
            hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        ),
    }


def _run_case(
    generator: AudioGenerator,
    *,
    model_id: str,
    duration: int,
    seed: int,
    prompt: str,
    repeat_index: int,
    parameters: dict[str, Any],
    case_label: str = "standard",
) -> dict[str, Any]:
    started = time.perf_counter()
    result = generator.run(
        GenerationRequest(
            media_type="audio",
            prompt=prompt,
            model_id=model_id,
            seed=seed,
            output_format="wav",
            params={**parameters, "duration_seconds": duration},
        )
    )
    elapsed = time.perf_counter() - started
    if result.status != "succeeded" or len(result.outputs) != 1:
        raise RuntimeError(f"Unexpected MusicGen result: {result.model_dump(mode='json')}")

    output_path = Path(result.outputs[0]).resolve()
    if output_path.suffix.lower() != ".wav" or not output_path.is_file():
        raise RuntimeError(f"MusicGen did not create a WAV file: {output_path}")

    facts = _wav_facts(output_path)
    if abs(float(facts["duration_seconds"]) - duration) > 0.25:
        raise RuntimeError(
            f"Generated duration {facts['duration_seconds']}s is not close to {duration}s"
        )

    record = {
        "model_id": model_id,
        "requested_duration_seconds": duration,
        "seed": seed,
        "repeat_index": repeat_index,
        "case_label": case_label,
        "parameters": parameters,
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_mb": _peak_rss_mb(),
        "output_path": str(output_path),
        "output_size_bytes": output_path.stat().st_size,
        "output_sha256": _sha256(output_path),
        "conditioning_prompt": result.metadata.get("conditioning_prompt"),
        "device": result.metadata.get("device"),
        "torch_dtype": result.metadata.get("torch_dtype"),
        "load_dtype": result.metadata.get("load_dtype"),
        "wav": facts,
        "quality_report": result.metadata.get("quality_report"),
    }
    print(
        "[OK] "
        f"{model_id} {duration}s seed={seed} repeat={repeat_index}: "
        f"{record['elapsed_seconds']}s, {record['peak_rss_mb']} MiB RSS, "
        f"{record['output_sha256'][:12]}"
    )
    return record


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[OK] MusicGen validation report: {path.resolve()}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    model_ids = args.models or ["musicgen-small"]
    durations = args.durations or [2]
    model_service = create_default_model_service(max_cached_models=1)
    ready_models: list[str] = []
    skipped: list[dict[str, str]] = []

    for model_id in model_ids:
        manifest = model_service.get_manifest(model_id, "audio", "text-to-music")
        readiness = evaluate_manifest_readiness(manifest, repo_root=ROOT)
        if readiness.is_ready:
            ready_models.append(model_id)
            continue
        reason = readiness.message
        skipped.append({"model_id": model_id, "reason": reason})
        print(f"[SKIP] {manifest.display_name} is not ready: {reason}")

    report: dict[str, Any] = {
        "environment": _environment(),
        "prompt": args.prompt,
        "parameters": DEFAULT_PARAMS,
        "runs": [],
        "skipped": skipped,
        "reproducibility": [],
        "parameter_effects": [],
    }
    if not ready_models:
        _write_report(args.report, report)
        return 0

    generator = AudioGenerator(model_service, output_dir=args.output_dir)
    runs: list[dict[str, Any]] = report["runs"]
    if args.parameter_sweep:
        if len(ready_models) != 1 or len(durations) != 1:
            raise ValueError("--parameter-sweep requires exactly one ready model and duration")
        model_id = ready_models[0]
        duration = durations[0]
        baseline_hash = ""
        for case_label, overrides in PARAMETER_SWEEP:
            parameters = {**DEFAULT_PARAMS, **overrides}
            record = _run_case(
                generator,
                model_id=model_id,
                duration=duration,
                seed=args.seed,
                prompt=args.prompt,
                repeat_index=1,
                parameters=parameters,
                case_label=case_label,
            )
            runs.append(record)
            if case_label == "baseline":
                baseline_hash = record["output_sha256"]
                continue
            report["parameter_effects"].append(
                {
                    "parameter": case_label,
                    "value": overrides[case_label],
                    "changed_from_baseline": record["output_sha256"] != baseline_hash,
                    "output_sha256": record["output_sha256"],
                }
            )
        model_service.unload_all()
        gc.collect()
        _write_report(args.report, report)
        return 0

    for model_id in ready_models:
        for duration in durations:
            repeated: list[dict[str, Any]] = []
            for repeat_index in range(1, args.repeat + 1):
                record = _run_case(
                    generator,
                    model_id=model_id,
                    duration=duration,
                    seed=args.seed,
                    prompt=args.prompt,
                    repeat_index=repeat_index,
                    parameters=DEFAULT_PARAMS,
                )
                runs.append(record)
                repeated.append(record)

            if len(repeated) > 1:
                hashes = {record["output_sha256"] for record in repeated}
                reproducible = len(hashes) == 1
                report["reproducibility"].append(
                    {
                        "model_id": model_id,
                        "duration_seconds": duration,
                        "seed": args.seed,
                        "same_seed_identical": reproducible,
                    }
                )
                if not reproducible:
                    raise RuntimeError(
                        f"{model_id} {duration}s was not reproducible for seed {args.seed}"
                    )

            if args.compare_seed is not None:
                different = _run_case(
                    generator,
                    model_id=model_id,
                    duration=duration,
                    seed=args.compare_seed,
                    prompt=args.prompt,
                    repeat_index=1,
                    parameters=DEFAULT_PARAMS,
                    case_label="different-seed",
                )
                runs.append(different)
                changed = different["output_sha256"] != repeated[0]["output_sha256"]
                report["reproducibility"].append(
                    {
                        "model_id": model_id,
                        "duration_seconds": duration,
                        "seed": args.compare_seed,
                        "different_seed_changed": changed,
                    }
                )
                if not changed:
                    raise RuntimeError(
                        f"{model_id} {duration}s did not change for seed {args.compare_seed}"
                    )

        model_service.unload_all()
        gc.collect()

    _write_report(args.report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
