"""Diffusers image pipeline families declared by image manifests (#140).

An image manifest names its pipeline explicitly through ``pipeline_class``; the loader
and generator look the family up here instead of guessing from a model id. A manifest
without ``pipeline_class`` keeps the original SDXL behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ImagePipelineClass = Literal["StableDiffusionXLPipeline", "FluxPipeline"]

DEFAULT_IMAGE_PIPELINE_CLASS: ImagePipelineClass = "StableDiffusionXLPipeline"


@dataclass(frozen=True)
class ImagePipelineFamily:
    pipeline_class: ImagePipelineClass
    # Diffusers class that wraps the same components for reference-image (img2img)
    # calls, or None when the family has no supported reference path.
    img2img_pipeline_class: str | None
    # FluxPipeline's __call__ has no negative_prompt argument; passing one raises.
    supports_negative_prompt: bool


IMAGE_PIPELINE_FAMILIES: dict[str, ImagePipelineFamily] = {
    "StableDiffusionXLPipeline": ImagePipelineFamily(
        pipeline_class="StableDiffusionXLPipeline",
        img2img_pipeline_class="StableDiffusionXLImg2ImgPipeline",
        supports_negative_prompt=True,
    ),
    "FluxPipeline": ImagePipelineFamily(
        pipeline_class="FluxPipeline",
        img2img_pipeline_class=None,
        supports_negative_prompt=False,
    ),
}


def resolve_image_pipeline_family(pipeline_class: str | None) -> ImagePipelineFamily:
    """Return the family for a manifest's ``pipeline_class`` (None means SDXL)."""

    name = pipeline_class or DEFAULT_IMAGE_PIPELINE_CLASS
    family = IMAGE_PIPELINE_FAMILIES.get(name)
    if family is None:
        supported = ", ".join(sorted(IMAGE_PIPELINE_FAMILIES))
        raise ValueError(
            f"Unsupported image pipeline_class {name!r}. Use one of: {supported}."
        )
    return family


__all__ = [
    "DEFAULT_IMAGE_PIPELINE_CLASS",
    "IMAGE_PIPELINE_FAMILIES",
    "ImagePipelineClass",
    "ImagePipelineFamily",
    "resolve_image_pipeline_family",
]
