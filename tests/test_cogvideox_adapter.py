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

    with pytest.raises(FileNotFoundError, match="model_index.json"):
        module.load_runtime(
            {
                "default_params": {
                    "pipeline_path": str(missing_pipeline),
                    "device": "auto",
                    "dtype": "float16",
                }
            }
        )


def test_cogvideox_adapter_rejects_pipeline_without_component_weights(tmp_path):
    module = _load_adapter_module()
    pipeline_root = tmp_path / "cogvideox-2b"
    pipeline_root.mkdir()
    (pipeline_root / "model_index.json").write_text(
        '{"transformer": ["diffusers", "CogVideoXTransformer3DModel"]}',
        encoding="utf-8",
    )
    (pipeline_root / "transformer").mkdir()
    (pipeline_root / "transformer" / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match=r"transformer/\*\.safetensors"):
        module.load_runtime(
            {
                "default_params": {
                    "pipeline_path": str(pipeline_root),
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


MPS_FLOAT64_MESSAGE = (
    "Cannot convert a MPS Tensor to float64 dtype as the MPS framework doesn't "
    "support float64. Please use float32 instead."
)


class _FakeTensor:
    def __init__(self, dtype):
        self.dtype = dtype

    def to(self, dtype):
        return _FakeTensor(dtype)


def _install_fake_cogvideox(monkeypatch, pipeline_cls):
    class FakeGenerator:
        def __init__(self, device):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    torch_module = ModuleType("torch")
    torch_module.cuda = SimpleNamespace(is_available=lambda: False)
    torch_module.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True))
    torch_module.float16 = "float16"
    torch_module.bfloat16 = "bfloat16"
    torch_module.float32 = "float32"
    torch_module.float64 = "float64"
    torch_module.Tensor = _FakeTensor
    torch_module.Generator = FakeGenerator

    diffusers_module = ModuleType("diffusers")
    diffusers_module.CogVideoXPipeline = pipeline_cls
    utils_module = ModuleType("diffusers.utils")
    utils_module.export_to_video = lambda _frames, path, fps: Path(path).write_bytes(b"mp4")
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers_module)
    monkeypatch.setitem(sys.modules, "diffusers.utils", utils_module)


def _fake_pipeline_class(*, move_error=None, call_error_on_mps=None):
    class FakePipeline:
        def __init__(self):
            self.vae = SimpleNamespace(enable_tiling=lambda: None, enable_slicing=lambda: None)
            # CogVideoXDDIMScheduler's "scaled_linear" tables are float64.
            self.scheduler = SimpleNamespace(
                alphas_cumprod=_FakeTensor("float64"),
                final_alpha_cumprod=_FakeTensor("float64"),
                timesteps=_FakeTensor("int64"),
                num_inference_steps=None,
            )
            self.device = None
            self.calls = []

        @classmethod
        def from_pretrained(cls, *_args, **_kwargs):
            return cls()

        def to(self, device):
            if device == "mps" and move_error is not None:
                raise move_error
            self.device = device
            return self

        def __call__(self, **kwargs):
            self.calls.append((self.device, kwargs.get("generator")))
            if self.device == "mps" and call_error_on_mps is not None:
                raise call_error_on_mps
            return SimpleNamespace(frames=[[object()]])

    return FakePipeline


def _load_fake_runtime(module, tmp_path):
    pipeline_root = tmp_path / "cogvideox-2b"
    pipeline_root.mkdir()
    (pipeline_root / "model_index.json").write_text("{}", encoding="utf-8")
    return module.load_runtime(
        {"default_params": {"pipeline_path": str(pipeline_root), "device": "auto"}}
    )


def _render(runtime, tmp_path):
    return runtime["renderer"](
        output_dir=tmp_path / "outputs",
        output_format="mp4",
        prompt="coastline",
        seed=42,
        num_frames=49,
    )


def test_cogvideox_adapter_casts_float64_scheduler_tables_on_mps(tmp_path, monkeypatch):
    module = _load_adapter_module()
    _install_fake_cogvideox(monkeypatch, _fake_pipeline_class())

    runtime = _load_fake_runtime(module, tmp_path)

    scheduler = runtime["pipeline"].scheduler
    assert runtime["device"] == "mps"
    assert scheduler.alphas_cumprod.dtype == "float32"
    assert scheduler.final_alpha_cumprod.dtype == "float32"
    assert scheduler.timesteps.dtype == "int64"


def test_cogvideox_adapter_leaves_scheduler_tables_alone_off_mps(tmp_path, monkeypatch):
    module = _load_adapter_module()
    _install_fake_cogvideox(monkeypatch, _fake_pipeline_class())
    sys.modules["torch"].backends.mps.is_available = lambda: False

    runtime = _load_fake_runtime(module, tmp_path)

    assert runtime["device"] == "cpu"
    assert runtime["pipeline"].scheduler.alphas_cumprod.dtype == "float64"


def test_cogvideox_adapter_retries_on_cpu_after_mps_float64_error(tmp_path, monkeypatch):
    module = _load_adapter_module()
    _install_fake_cogvideox(
        monkeypatch, _fake_pipeline_class(call_error_on_mps=TypeError(MPS_FLOAT64_MESSAGE))
    )
    runtime = _load_fake_runtime(module, tmp_path)

    rendered = _render(runtime, tmp_path)

    calls = runtime["pipeline"].calls
    assert [device for device, _generator in calls] == ["mps", "cpu"]
    assert calls[1][1].device == "cpu" and calls[1][1].seed == 42
    assert rendered["metadata"]["device"] == "cpu"
    assert rendered["metadata"]["cpu_fallback_reason"] == MPS_FLOAT64_MESSAGE


def test_cogvideox_adapter_does_not_mask_unrelated_type_errors_on_mps(tmp_path, monkeypatch):
    module = _load_adapter_module()
    _install_fake_cogvideox(
        monkeypatch,
        _fake_pipeline_class(call_error_on_mps=TypeError("unexpected keyword argument 'fps'")),
    )
    runtime = _load_fake_runtime(module, tmp_path)

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        _render(runtime, tmp_path)
    assert [device for device, _generator in runtime["pipeline"].calls] == ["mps"]


def test_cogvideox_adapter_loads_on_cpu_when_mps_move_hits_float64(tmp_path, monkeypatch):
    module = _load_adapter_module()
    _install_fake_cogvideox(
        monkeypatch, _fake_pipeline_class(move_error=TypeError(MPS_FLOAT64_MESSAGE))
    )

    runtime = _load_fake_runtime(module, tmp_path)

    assert runtime["device"] == "cpu"
    assert runtime["pipeline"].device == "cpu"
