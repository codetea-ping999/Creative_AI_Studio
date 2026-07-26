#!/usr/bin/env python3
"""Run one opt-in local CogVideoX-2B MP4 generation smoke test."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bootstrap import create_default_model_service
from core.models import evaluate_manifest_readiness
from core.schemas import GenerationRequest
from generators.video import VideoGenerator


def main() -> int:
    model_service = create_default_model_service()
    manifest = model_service.get_manifest("learned-video", "video")
    readiness = evaluate_manifest_readiness(manifest, repo_root=ROOT)
    if not readiness.is_ready:
        print(f"[SKIP] {manifest.display_name} is not ready: {readiness.message}")
        return 2

    output_dir = ROOT / "outputs" / "videos"
    generator = VideoGenerator(model_service, output_dir=output_dir)
    result = generator.run(
        GenerationRequest(
            media_type="video",
            prompt="A calm cinematic ocean at sunrise, slow camera movement",
            negative_prompt="blurry, jittery, distorted",
            model_id="learned-video",
            seed=42,
            output_format="mp4",
            params={
                "width": 720,
                "height": 480,
                "num_frames": 49,
                "fps": 8,
                "num_inference_steps": 20,
                "guidance_scale": 6.0,
            },
        )
    )
    output_path = Path(result.outputs[0])
    if result.status != "succeeded" or output_path.suffix.lower() != ".mp4":
        raise RuntimeError(f"Unexpected CogVideoX smoke result: {result.model_dump(mode='json')}")
    print(f"[OK] CogVideoX MP4 generated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
