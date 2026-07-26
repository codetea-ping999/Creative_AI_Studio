#!/usr/bin/env python3
"""Measure the real FLUX.1-dev image path on Apple Silicon.

This script intentionally uses the same model service and image generator as
the API. It prints newline-delimited JSON events so long-running inference can
be monitored without writing a second metrics format to disk.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bootstrap.factories import (  # noqa: E402
    create_default_image_generator,
    create_default_model_service,
)
from core.jobs import GenerationContext  # noqa: E402
from core.schemas import GenerationRequest  # noqa: E402


GIB = 1024**3


def _emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, sort_keys=True), flush=True)


def _mps_bytes(torch: Any) -> dict[str, int | None]:
    if not torch.backends.mps.is_available():
        return {
            "mps_current_bytes": None,
            "mps_driver_bytes": None,
            "mps_recommended_bytes": None,
        }

    def read(name: str) -> int | None:
        callback = getattr(torch.mps, name, None)
        if callback is None:
            return None
        try:
            return int(callback())
        except Exception:  # pragma: no cover - runtime/OS-dependent metric
            return None

    return {
        "mps_current_bytes": read("current_allocated_memory"),
        "mps_driver_bytes": read("driver_allocated_memory"),
        "mps_recommended_bytes": read("recommended_max_memory"),
    }


class MetricSampler:
    """Sample process RSS and MPS allocator usage during one measured stage."""

    def __init__(self, torch: Any, interval_seconds: float = 0.1) -> None:
        self.torch = torch
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._finished_at = 0.0
        self._start: dict[str, int | None] = {}
        self._end: dict[str, int | None] = {}
        self._peak: dict[str, int | None] = {}
        try:
            import psutil
        except ModuleNotFoundError:
            self._process = None
        else:
            self._process = psutil.Process(os.getpid())

    def _snapshot(self) -> dict[str, int | None]:
        rss = (
            int(self._process.memory_info().rss)
            if self._process is not None
            else None
        )
        return {"rss_bytes": rss, **_mps_bytes(self.torch)}

    def _sample(self) -> None:
        snapshot = self._snapshot()
        for key, value in snapshot.items():
            if value is None:
                continue
            previous = self._peak.get(key)
            self._peak[key] = value if previous is None else max(previous, value)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def __enter__(self) -> "MetricSampler":
        self._started_at = time.perf_counter()
        self._start = self._snapshot()
        self._peak = dict(self._start)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._sample()
        self._end = self._snapshot()
        self._finished_at = time.perf_counter()

    def result(self) -> dict[str, float | int | None]:
        values: dict[str, float | int | None] = {
            "seconds": round(self._finished_at - self._started_at, 3),
        }
        for prefix, snapshot in (
            ("start", self._start),
            ("end", self._end),
            ("peak", self._peak),
        ):
            for key, value in snapshot.items():
                values[f"{prefix}_{key}"] = value
                values[f"{prefix}_{key.removesuffix('_bytes')}_gib"] = (
                    round(value / GIB, 3) if value is not None else None
                )
        return values


def _synchronize(torch: Any) -> None:
    if torch.backends.mps.is_available():
        torch.mps.synchronize()


def _measure(
    name: str,
    torch: Any,
    callback: Callable[[], Any],
) -> tuple[Any, dict[str, float | int | None]]:
    _synchronize(torch)
    _emit("stage_started", stage=name)
    with MetricSampler(torch) as sampler:
        value = callback()
        _synchronize(torch)
    metrics = sampler.result()
    _emit("stage_finished", stage=name, metrics=metrics)
    return value, metrics


def _generation_context(stage: str) -> GenerationContext:
    return GenerationContext(
        is_cancelled=lambda: False,
        on_progress=lambda fraction: _emit(
            "progress",
            stage=stage,
            fraction=round(fraction, 4),
        ),
        min_interval_seconds=0.5,
        min_progress_delta=0.0,
    )


def _request(
    *,
    width: int,
    height: int,
    steps: int,
    seed: int,
) -> GenerationRequest:
    return GenerationRequest(
        media_type="image",
        prompt=(
            "A quiet Japanese tea house beside a moss garden at dawn, "
            "soft mist, warm paper lanterns, natural cinematic photography"
        ),
        negative_prompt="low quality, blurry, text",
        model_id="flux-dev",
        seed=seed,
        output_format="png",
        params={
            "width": width,
            "height": height,
            "num_inference_steps": steps,
            "guidance_scale": 3.5,
            "variation_count": 1,
        },
    )


def _run_generation(
    *,
    name: str,
    generator: Any,
    request: GenerationRequest,
    torch: Any,
) -> dict[str, Any]:
    generator.validate_request(request)
    generator.prepare(request)
    result, metrics = _measure(
        name,
        torch,
        lambda: generator.generate(request, context=_generation_context(name)),
    )
    metadata = result.metadata
    summary = {
        "stage": name,
        "output": result.outputs[0],
        "metrics": metrics,
        "metadata": {
            "model_id": metadata.get("model_id"),
            "manifest_id": metadata.get("manifest_id"),
            "pipeline_class": metadata.get("pipeline_class"),
            "pipeline_family": metadata.get("pipeline_family"),
            "negative_prompt_applied": metadata.get("negative_prompt_applied"),
            "device": metadata.get("device"),
            "load_dtype": metadata.get("load_dtype"),
            "torch_dtype": metadata.get("torch_dtype"),
            "seed": metadata.get("seed"),
            "params": metadata.get("params"),
        },
    }
    _emit("generation_finished", **summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--switch-cycles",
        type=int,
        default=1,
        help="Number of SDXL -> FLUX reload cycles after generation.",
    )
    parser.add_argument(
        "--skip-standard",
        action="store_true",
        help="Run only the 512px/4-step smoke before switch measurements.",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the 512px/4-step smoke when it already passed.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "outputs" / "images" / "flux-m1-validation",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional path for the final machine-readable JSON report.",
    )
    parser.add_argument(
        "--sdxl-path",
        type=Path,
        help=(
            "Optional absolute SDXL Diffusers path for switch validation. "
            "Useful when the worktree references a shared model directory."
        ),
    )
    args = parser.parse_args()

    import torch

    os.environ["DEVICE"] = "auto"
    _emit(
        "environment",
        machine=platform.machine(),
        mac_ver=platform.mac_ver()[0],
        python=platform.python_version(),
        torch=torch.__version__,
        mps_available=torch.backends.mps.is_available(),
        memory=_mps_bytes(torch),
    )
    if platform.machine() != "arm64" or not torch.backends.mps.is_available():
        raise RuntimeError("This validation requires Apple Silicon with MPS.")

    model_service = create_default_model_service(max_cached_models=1)
    generator = create_default_image_generator(
        output_dir=args.output_dir,
        model_service=model_service,
    )

    (manifest, runtime), cold_metrics = _measure(
        "cold_flux_load",
        torch,
        lambda: model_service.resolve_runtime(
            "flux-dev",
            media_type="image",
            task_type="text-to-image",
        ),
    )
    _emit(
        "runtime",
        stage="cold_flux_load",
        manifest_id=manifest.id,
        pipeline_class=type(runtime["pipeline"]).__name__,
        pipeline_family=runtime["pipeline_family"],
        device=runtime["device"],
        load_dtype=runtime["load_dtype"],
        torch_dtype=runtime["torch_dtype"],
        metrics=cold_metrics,
    )

    (_, warm_runtime), warm_metrics = _measure(
        "warm_flux_resolve",
        torch,
        lambda: model_service.resolve_runtime(
            "flux-dev",
            media_type="image",
            task_type="text-to-image",
        ),
    )
    if warm_runtime is not runtime:
        raise RuntimeError("Warm resolve did not reuse the cached FLUX runtime.")
    _emit("runtime", stage="warm_flux_resolve", metrics=warm_metrics)
    del warm_runtime
    del runtime
    del manifest

    summaries: list[dict[str, Any]] = []
    if not args.skip_smoke:
        summaries.append(
            _run_generation(
                name="smoke_512x512_4",
                generator=generator,
                request=_request(width=512, height=512, steps=4, seed=21051204),
                torch=torch,
            )
        )
    if not args.skip_standard:
        summaries.append(
            _run_generation(
                name="standard_1024x1024_28",
                generator=generator,
                request=_request(
                    width=1024,
                    height=1024,
                    steps=28,
                    seed=21102428,
                ),
                torch=torch,
            )
        )

    switch_metrics: list[dict[str, Any]] = []

    def resolve_sdxl() -> tuple[Any, Any]:
        if args.sdxl_path is None:
            return model_service.resolve_runtime(
                "sdxl",
                media_type="image",
                task_type="text-to-image",
            )
        manifest = model_service.get_manifest(
            "sdxl",
            media_type="image",
            task_type="text-to-image",
        ).model_copy(update={"local_path": str(args.sdxl_path.resolve())})
        loader = model_service.loader_registry.get(manifest.loader)
        runtime = loader.load(manifest)
        model_service.runtime_cache.put(manifest.id, runtime)
        return manifest, runtime

    for cycle in range(1, max(0, args.switch_cycles) + 1):
        gc.collect()
        (_, sdxl_runtime), sdxl_metrics = _measure(
            f"switch_{cycle}_sdxl_load",
            torch,
            resolve_sdxl,
        )
        sdxl_summary = {
            "cycle": cycle,
            "model_id": "sdxl",
            "pipeline_class": type(sdxl_runtime["pipeline"]).__name__,
            "metrics": sdxl_metrics,
        }
        _emit("switch_finished", **sdxl_summary)
        switch_metrics.append(sdxl_summary)
        del sdxl_runtime

        gc.collect()
        (_, flux_runtime), flux_metrics = _measure(
            f"switch_{cycle}_flux_load",
            torch,
            lambda: model_service.resolve_runtime(
                "flux-dev",
                media_type="image",
                task_type="text-to-image",
            ),
        )
        flux_summary = {
            "cycle": cycle,
            "model_id": "flux-dev",
            "pipeline_class": type(flux_runtime["pipeline"]).__name__,
            "metrics": flux_metrics,
        }
        _emit("switch_finished", **flux_summary)
        switch_metrics.append(flux_summary)
        del flux_runtime

    model_service.unload_all()
    gc.collect()
    torch.mps.empty_cache()
    final_report = {
        "generations": summaries,
        "switches": switch_metrics,
        "final_memory": _mps_bytes(torch),
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(final_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _emit(
        "validation_finished",
        generations=summaries,
        switches=switch_metrics,
        final_memory=final_report["final_memory"],
        report=str(args.report) if args.report is not None else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
