#!/usr/bin/env python3
"""Run one opt-in local CogVideoX-2B MP4 generation smoke test."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bootstrap import create_default_model_service
from core.schemas import GenerationRequest
from generators.video import VideoGenerator


def main() -> int:
    model_root = ROOT / "models" / "video" / "cogvideox-2b"
    required_configs = [
        model_root / "model_index.json",
        model_root / "scheduler" / "scheduler_config.json",
        model_root / "text_encoder" / "config.json",
        model_root / "tokenizer" / "tokenizer_config.json",
        model_root / "transformer" / "config.json",
        model_root / "vae" / "config.json",
    ]
    weights_ready = all(
        any((model_root / component).glob("*.safetensors"))
        for component in ("text_encoder", "transformer", "vae")
    )
    if not all(path.exists() for path in required_configs) or not weights_ready:
        print(f"[SKIP] CogVideoX-2B weight set is incomplete at {model_root}")
        return 2

    output_dir = ROOT / "outputs" / "videos"
    generator = VideoGenerator(create_default_model_service(), output_dir=output_dir)
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
