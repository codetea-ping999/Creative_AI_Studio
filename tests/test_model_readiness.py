"""Tests for the shared model readiness rules."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from core.model_readiness import (
    STATUS_CONFIGURED,
    STATUS_INVALID_CONFIGURATION,
    STATUS_MISSING_FILES,
    STATUS_READY,
    STATUS_SCAFFOLD,
    evaluate_manifest_payload,
    evaluate_readiness,
    missing_diffusers_files,
    missing_transformers_files,
    resolve_repo_path,
)


SDXL_INDEX = {
    "_class_name": "StableDiffusionXLPipeline",
    "scheduler": ["diffusers", "EulerDiscreteScheduler"],
    "text_encoder": ["transformers", "CLIPTextModel"],
    "tokenizer": ["transformers", "CLIPTokenizer"],
    "unet": ["diffusers", "UNet2DConditionModel"],
    "vae": ["diffusers", "AutoencoderKL"],
    "safety_checker": [None, None],
}

COGVIDEOX_INDEX = {
    "_class_name": "CogVideoXPipeline",
    "scheduler": ["diffusers", "CogVideoXDDIMScheduler"],
    "text_encoder": ["transformers", "T5EncoderModel"],
    "tokenizer": ["transformers", "T5Tokenizer"],
    "transformer": ["diffusers", "CogVideoXTransformer3DModel"],
    "vae": ["diffusers", "AutoencoderKLCogVideoX"],
}

_CONFIG_NAMES = {
    "scheduler": "scheduler_config.json",
    "tokenizer": "tokenizer_config.json",
    "tokenizer_2": "tokenizer_config.json",
}
_WEIGHTLESS_COMPONENTS = {"scheduler", "tokenizer", "tokenizer_2"}


def _write_pipeline(
    root: Path,
    index: dict[str, object],
    *,
    with_weights: bool = True,
    weight_name: str = "model.safetensors",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "model_index.json").write_text(json.dumps(index), encoding="utf-8")
    for component, spec in index.items():
        if component.startswith("_") or not isinstance(spec, list):
            continue
        if not all(isinstance(entry, str) for entry in spec):
            continue
        component_root = root / component
        component_root.mkdir(exist_ok=True)
        config_name = _CONFIG_NAMES.get(component, "config.json")
        (component_root / config_name).write_text("{}", encoding="utf-8")
        if component.startswith("tokenizer"):
            tokenizer_class = str(spec[1])
            if "T5Tokenizer" in tokenizer_class:
                (component_root / "spiece.model").write_bytes(b"stub")
            else:
                (component_root / "vocab.json").write_text("{}", encoding="utf-8")
                (component_root / "merges.txt").write_text("", encoding="utf-8")
        if with_weights and component not in _WEIGHTLESS_COMPONENTS:
            (component_root / weight_name).write_bytes(b"stub")
    return root


class DiffusersReadinessTests(unittest.TestCase):
    def test_model_index_alone_is_not_ready(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "sdxl"
            root.mkdir()
            (root / "model_index.json").write_text(json.dumps(SDXL_INDEX), encoding="utf-8")

            readiness = evaluate_readiness(runtime="diffusers", local_path=str(root))

        self.assertEqual(readiness.status, STATUS_MISSING_FILES)
        self.assertFalse(readiness.is_ready)
        self.assertIn("unet/*.safetensors", readiness.missing)
        self.assertIn("vae/config.json", readiness.missing)

    def test_complete_pipeline_is_ready(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(Path(tmp_dir) / "sdxl", SDXL_INDEX)

            readiness = evaluate_readiness(runtime="diffusers", local_path=str(root))

        self.assertEqual(readiness.status, STATUS_READY)
        self.assertEqual(readiness.missing, ())

    def test_variant_and_bin_weight_names_are_accepted(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            fp16_root = _write_pipeline(
                Path(tmp_dir) / "fp16",
                SDXL_INDEX,
                weight_name="model.fp16.safetensors",
            )
            bin_root = _write_pipeline(
                Path(tmp_dir) / "bin",
                SDXL_INDEX,
                weight_name="pytorch_model.bin",
            )

            self.assertEqual(missing_diffusers_files(fp16_root), [])
            self.assertEqual(missing_diffusers_files(bin_root), [])

    def test_incomplete_sharded_weights_report_the_missing_shard(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(
                Path(tmp_dir) / "sdxl",
                SDXL_INDEX,
                with_weights=False,
            )
            shard_name = "diffusion_pytorch_model-00001-of-00002.safetensors"
            for component in ("text_encoder", "unet", "vae"):
                (root / component / shard_name).write_bytes(b"stub")

            missing = missing_diffusers_files(root)

        self.assertIn(
            "unet/diffusion_pytorch_model-00002-of-00002.safetensors",
            missing,
        )

    def test_complete_sharded_weights_are_ready_without_an_index(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(
                Path(tmp_dir) / "sdxl",
                SDXL_INDEX,
                with_weights=False,
            )
            for component in ("text_encoder", "unet", "vae"):
                for part in (1, 2):
                    name = (
                        f"diffusion_pytorch_model-{part:05d}-of-00002.safetensors"
                    )
                    (root / component / name).write_bytes(b"stub")

            self.assertEqual(missing_diffusers_files(root), [])

    def test_weight_index_requires_every_referenced_shard(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(
                Path(tmp_dir) / "sdxl",
                SDXL_INDEX,
                with_weights=False,
            )
            for component in ("text_encoder", "unet", "vae"):
                component_root = root / component
                first = "model-00001-of-00002.safetensors"
                second = "model-00002-of-00002.safetensors"
                (component_root / first).write_bytes(b"stub")
                (component_root / "model.safetensors.index.json").write_text(
                    json.dumps({"weight_map": {"a": first, "b": second}}),
                    encoding="utf-8",
                )

            missing = missing_diffusers_files(root)

        self.assertIn("unet/model-00002-of-00002.safetensors", missing)

    def test_clip_tokenizer_requires_vocabulary_and_merges(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(Path(tmp_dir) / "sdxl", SDXL_INDEX)
            (root / "tokenizer" / "merges.txt").unlink()

            missing = missing_diffusers_files(root)

        self.assertIn("tokenizer/merges.txt", missing)

    def test_t5_tokenizer_requires_sentencepiece_assets(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(Path(tmp_dir) / "cogvideox", COGVIDEOX_INDEX)
            (root / "tokenizer" / "spiece.model").unlink()

            missing = missing_diffusers_files(root)

        self.assertIn("tokenizer/spiece.model", missing)

    def test_null_component_slots_are_not_required(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(Path(tmp_dir) / "sdxl", SDXL_INDEX)

            missing = missing_diffusers_files(root)

        self.assertEqual(missing, [])
        self.assertFalse((root / "safety_checker").exists())

    def test_missing_model_index_reports_model_index(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "sdxl"
            root.mkdir()

            self.assertEqual(missing_diffusers_files(root), ["model_index.json"])

    def test_unreadable_model_index_is_not_ready(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "sdxl"
            root.mkdir()
            (root / "model_index.json").write_text("{not json", encoding="utf-8")

            self.assertEqual(missing_diffusers_files(root), ["model_index.json"])

    def test_missing_local_path_is_reported(self) -> None:
        readiness = evaluate_readiness(runtime="diffusers", local_path="./does-not-exist")

        self.assertEqual(readiness.status, STATUS_MISSING_FILES)
        self.assertIn("./does-not-exist", readiness.message)


class VoicevoxReadinessTests(unittest.TestCase):
    def test_loopback_endpoint_is_configured_without_local_files(self) -> None:
        readiness = evaluate_readiness(
            runtime="voicevox_http",
            local_path=None,
            remote_ref="http://127.0.0.1:50021/private/path",
        )

        self.assertEqual(readiness.status, STATUS_CONFIGURED)
        self.assertTrue(readiness.is_ready)
        self.assertIn("http://127.0.0.1:50021", readiness.message)
        self.assertNotIn("private/path", readiness.message)

    def test_endpoint_credentials_are_rejected_without_disclosure(self) -> None:
        readiness = evaluate_readiness(
            runtime="voicevox_http",
            local_path=None,
            remote_ref="http://user:secret@127.0.0.1:50021",
        )

        self.assertEqual(readiness.status, STATUS_INVALID_CONFIGURATION)
        self.assertFalse(readiness.is_ready)
        self.assertNotIn("secret", readiness.message)


class TransformersReadinessTests(unittest.TestCase):
    def test_config_without_weights_is_not_ready(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "musicgen"
            root.mkdir()
            (root / "config.json").write_text("{}", encoding="utf-8")

            readiness = evaluate_readiness(runtime="transformers", local_path=str(root))

        self.assertEqual(readiness.status, STATUS_MISSING_FILES)
        self.assertIn("*.safetensors", readiness.missing)

    def test_config_and_weights_are_ready(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "musicgen"
            root.mkdir()
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"stub")
            (root / "preprocessor_config.json").write_text("{}", encoding="utf-8")
            (root / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (root / "tokenizer.json").write_text("{}", encoding="utf-8")

            self.assertEqual(missing_transformers_files(root), [])
            readiness = evaluate_readiness(runtime="transformers", local_path=str(root))

        self.assertEqual(readiness.status, STATUS_READY)

    def test_musicgen_processor_assets_are_required(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "musicgen"
            root.mkdir()
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"stub")

            missing = missing_transformers_files(root)

        self.assertIn("preprocessor_config.json", missing)
        self.assertIn("tokenizer_config.json", missing)
        self.assertIn(
            "tokenizer.json|spiece.model|vocab.json+merges.txt|vocab.txt",
            missing,
        )

    def test_auxiliary_bin_file_is_not_treated_as_model_weights(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "musicgen"
            root.mkdir()
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "compression_state_dict.bin").write_bytes(b"stub")

            missing = missing_transformers_files(root)

        self.assertIn("*.safetensors", missing)


class LearnedReadinessTests(unittest.TestCase):
    def _adapter_root(self, tmp_dir: str) -> Path:
        adapter_root = Path(tmp_dir) / "learned-runtime"
        adapter_root.mkdir(parents=True)
        (adapter_root / "runtime.py").write_text("def load_runtime(m):\n    return {}\n", "utf-8")
        return adapter_root

    def test_scaffold_manifest_reports_scaffold(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            adapter_root = self._adapter_root(tmp_dir)

            readiness = evaluate_readiness(
                runtime="learned",
                local_path=str(adapter_root),
                default_params={"entrypoint": "runtime.py", "runtime_status": "scaffold"},
            )

        self.assertEqual(readiness.status, STATUS_SCAFFOLD)
        self.assertFalse(readiness.is_ready)

    def test_missing_entrypoint_is_reported(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            adapter_root = Path(tmp_dir) / "learned-runtime"
            adapter_root.mkdir()

            readiness = evaluate_readiness(
                runtime="learned",
                local_path=str(adapter_root),
                default_params={"entrypoint": "runtime.py"},
            )

        self.assertEqual(readiness.status, STATUS_MISSING_FILES)
        self.assertIn("runtime.py", readiness.message)

    def test_pipeline_weights_are_required(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            adapter_root = self._adapter_root(tmp_dir)
            pipeline_root = _write_pipeline(
                Path(tmp_dir) / "cogvideox-2b",
                COGVIDEOX_INDEX,
                with_weights=False,
            )
            params = {"entrypoint": "runtime.py", "pipeline_path": str(pipeline_root)}

            incomplete = evaluate_readiness(
                runtime="learned",
                local_path=str(adapter_root),
                default_params=params,
            )
            for component in ("text_encoder", "transformer", "vae"):
                (pipeline_root / component / "model.safetensors").write_bytes(b"stub")
            complete = evaluate_readiness(
                runtime="learned",
                local_path=str(adapter_root),
                default_params=params,
            )

        self.assertEqual(incomplete.status, STATUS_MISSING_FILES)
        self.assertIn("transformer/*.safetensors", incomplete.missing)
        self.assertEqual(complete.status, STATUS_READY)

    def test_unconfigured_pipeline_path_is_reported(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            adapter_root = self._adapter_root(tmp_dir)

            readiness = evaluate_readiness(
                runtime="learned",
                local_path=str(adapter_root),
                default_params={"entrypoint": "runtime.py"},
            )

        self.assertEqual(readiness.status, STATUS_MISSING_FILES)
        self.assertIn("pipeline_path", readiness.message)


class ManifestPayloadReadinessTests(unittest.TestCase):
    def test_relative_paths_resolve_from_repository_root(self) -> None:
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(resolve_repo_path("models"), (root / "models").resolve())

    def test_payload_matches_manifest_fields(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = _write_pipeline(Path(tmp_dir) / "sdxl", SDXL_INDEX)
            payload = {
                "id": "sdxl-local",
                "runtime": "diffusers",
                "local_path": str(root),
                "default_params": {"width": 1024},
            }

            readiness = evaluate_manifest_payload(payload)

        self.assertEqual(readiness.status, STATUS_READY)

    def test_procedural_runtime_only_needs_its_directory(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "procedural"
            root.mkdir()

            readiness = evaluate_manifest_payload(
                {"runtime": "procedural", "local_path": str(root)}
            )

        self.assertEqual(readiness.status, STATUS_READY)

    def test_manifest_without_local_path_is_not_ready(self) -> None:
        readiness = evaluate_manifest_payload({"runtime": "diffusers", "remote_ref": "org/model"})

        self.assertEqual(readiness.status, STATUS_MISSING_FILES)


class SetupCheckerDependencyTests(unittest.TestCase):
    def test_setup_checker_imports_without_site_packages(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "check_local_setup.py"
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                (
                    "import runpy; "
                    f"runpy.run_path({str(script)!r}, run_name='setup_probe')"
                ),
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
