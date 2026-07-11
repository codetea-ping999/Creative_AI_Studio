import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


RUNTIME_PATH = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "video"
    / "learned-runtime"
    / "runtime.py"
)


def _load_adapter_module():
    spec = importlib.util.spec_from_file_location("test_cogvideox_runtime", RUNTIME_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cogvideox_adapter_requires_local_diffusers_weights(tmp_path):
    module = _load_adapter_module()
    missing_pipeline = tmp_path / "cogvideox-2b"

    with pytest.raises(FileNotFoundError, match="CogVideoX model_index.json is missing"):
        module.load_runtime(
            {
                "default_params": {
                    "pipeline_path": str(missing_pipeline),
                    "device": "auto",
                    "dtype": "float16",
                }
            }
        )


def test_cogvideox_adapter_filters_studio_only_parameters():
    module = _load_adapter_module()

    normalized = module._normalize_generation_kwargs(
        {
            "prompt": "cinematic coast",
            "negative_prompt": "blur",
            "width": 720,
            "height": 480,
            "num_frames": 49,
            "num_inference_steps": 20,
            "guidance_scale": 6.0,
            "fps": 8,
            "camera_motion": "push-in",
            "pipeline_path": "ignored",
        }
    )

    assert normalized == {
        "prompt": "cinematic coast",
        "negative_prompt": "blur",
        "width": 720,
        "height": 480,
        "num_frames": 49,
        "num_inference_steps": 20,
        "guidance_scale": 6.0,
    }


def test_cogvideox_adapter_returns_renderer_and_mp4_metadata(tmp_path, monkeypatch):
    module = _load_adapter_module()
    pipeline_root = tmp_path / "cogvideox-2b"
    pipeline_root.mkdir()
    (pipeline_root / "model_index.json").write_text("{}", encoding="utf-8")

    class FakeGenerator:
        def __init__(self, device):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    class FakePipeline:
        def __init__(self):
            self.vae = SimpleNamespace(enable_tiling=lambda: None, enable_slicing=lambda: None)
            self.device = None

        @classmethod
        def from_pretrained(cls, *_args, **_kwargs):
            return cls()

        def to(self, device):
            self.device = device
            return self

        def __call__(self, **_kwargs):
            return SimpleNamespace(frames=[[object(), object()]])

    torch_module = ModuleType("torch")
    torch_module.cuda = SimpleNamespace(is_available=lambda: False)
    torch_module.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True))
    torch_module.float16 = "float16"
    torch_module.bfloat16 = "bfloat16"
    torch_module.float32 = "float32"
    torch_module.Generator = FakeGenerator

    diffusers_module = ModuleType("diffusers")
    diffusers_module.CogVideoXPipeline = FakePipeline
    utils_module = ModuleType("diffusers.utils")
    utils_module.export_to_video = lambda _frames, path, fps: Path(path).write_bytes(
        f"mp4:{fps}".encode()
    )
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers_module)
    monkeypatch.setitem(sys.modules, "diffusers.utils", utils_module)

    runtime = module.load_runtime(
        {
            "default_params": {
                "pipeline_path": str(pipeline_root),
                "pipeline_id": "THUDM/CogVideoX-2b",
                "device": "auto",
                "dtype": "float16",
                "fps": 8,
            }
        }
    )
    rendered = runtime["renderer"](
        output_dir=tmp_path / "outputs",
        output_format="mp4",
        prompt="coastline",
        negative_prompt=None,
        seed=42,
        width=720,
        height=480,
        num_frames=49,
        num_inference_steps=20,
        guidance_scale=6.0,
        fps=8,
    )

    assert runtime["runtime_adapter"] == "learned_text_to_video"
    assert runtime["device"] == "mps"
    assert Path(rendered["output_path"]).read_bytes() == b"mp4:8"
    assert rendered["metadata"]["pipeline_id"] == "THUDM/CogVideoX-2b"
    assert rendered["metadata"]["frame_count"] == 2
