"""Best-effort accelerator memory release for evicted/unloaded runtimes."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def release_runtime(model_id: str, runtime_obj: Any) -> None:
    """Release LoRA adapters, move weights off-device, and clear accelerator caches.

    Called whenever a runtime leaves :class:`ModelRuntimeCache` (eviction,
    ``unload``, or ``unload_all``). Runtimes that hold no torch state (procedural
    or learned-video runtimes, plain fixtures) are left untouched. Any failure
    is logged and swallowed so a broken cleanup step never blocks the cache
    operation that triggered it.
    """

    if not isinstance(runtime_obj, dict):
        return

    _safe(model_id, "reset LoRA adapters", lambda: _reset_lora(runtime_obj))

    pipeline = runtime_obj.get("pipeline")
    if pipeline is not None:
        _safe(model_id, "move pipeline to cpu", lambda: pipeline.to("cpu"))

    # img2img_pipeline (#201) wraps the *same* unet/vae/text-encoder objects
    # as "pipeline" via StableDiffusionXLImg2ImgPipeline(**pipeline
    # .components) rather than owning separate weights, but it is still a
    # distinct Python object holding its own references to them. Leaving it
    # in runtime_obj after this function returns would keep every shared
    # component reachable even once "pipeline" itself is gone, defeating the
    # point of dropping these keys below.
    img2img_pipeline = runtime_obj.get("img2img_pipeline")
    if img2img_pipeline is not None:
        _safe(
            model_id,
            "move img2img pipeline to cpu",
            lambda: img2img_pipeline.to("cpu"),
        )

    model = runtime_obj.get("model")
    if model is not None:
        _safe(model_id, "move model to cpu", lambda: model.to("cpu"))

    for key in ("pipeline", "img2img_pipeline", "model", "processor"):
        runtime_obj.pop(key, None)

    _safe(model_id, "empty accelerator cache", _empty_accelerator_cache)


def _reset_lora(runtime_obj: dict[str, Any]) -> None:
    pipeline = runtime_obj.get("pipeline")
    active_adapter = runtime_obj.get("active_lora_adapter")
    if pipeline is not None and active_adapter is not None:
        unload_lora_weights = getattr(pipeline, "unload_lora_weights", None)
        if callable(unload_lora_weights):
            unload_lora_weights()
    for key in ("active_lora_path", "active_lora_adapter", "active_lora_scale"):
        runtime_obj.pop(key, None)


def _empty_accelerator_cache() -> None:
    try:
        import torch
    except ModuleNotFoundError:
        return

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if hasattr(torch, "mps") and torch.backends.mps.is_available():
        torch.mps.empty_cache()


def _safe(model_id: str, description: str, action: Any) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 - cleanup must never break the cache
        logger.warning(
            "Runtime cleanup step failed for model %r: %s",
            model_id,
            description,
            exc_info=True,
        )


__all__ = ["release_runtime"]
