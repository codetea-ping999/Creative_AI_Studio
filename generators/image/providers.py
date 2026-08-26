"""Provider-neutral contract for image generation backends.

Today `ImageGenerator` (`generators/image/generator.py`) talks directly to a
local diffusers pipeline. This module defines the shape that both that local
runtime and a future cloud API would speak, so a caller does not need to
branch on provider/model name to know what a request may safely ask for:

- `ImageProviderCapabilities` is declared explicitly per provider/model
  combination rather than inferred from a name string.
- `ImageGenerationSpec` normalizes a single-image request across providers.
- `validate_capabilities` rejects a spec that needs an undeclared capability
  *before* any model invocation happens.
- `ImageProviderIdentity` / `ImageProviderResult` carry stable provider/model
  identity on every result for provenance.
- `LocalDiffusersImageProvider` adapts the existing local diffusers pipeline
  call onto this contract without changing what gets passed to the pipeline.

Outbound cloud calls, secret handling, retries, and cost estimation are
out of scope here (see the sibling micro-issues under #66); this module only
fixes the shape the local path and a future cloud path both implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ImageGenerationSpec",
    "ImageProvider",
    "ImageProviderCapabilities",
    "ImageProviderIdentity",
    "ImageProviderResult",
    "LocalDiffusersImageProvider",
    "UnsupportedImageParameterError",
    "validate_capabilities",
]


@dataclass(frozen=True)
class ImageProviderCapabilities:
    """What one provider/model combination can actually do.

    A provider declares this once; nothing about it is guessed from a
    provider or model name, so `validate_capabilities` can reject a request
    parameter the provider does not support before it is ever invoked.
    """

    supports_text_to_image: bool = True
    supports_reference_image: bool = False
    supports_lora: bool = False
    supports_seed: bool = False
    min_size: int = 64
    max_size: int = 2048
    size_step: int = 8
    max_batch: int = 1


@dataclass(frozen=True)
class ImageGenerationSpec:
    """Normalized single-image request handed to `ImageProvider.generate_image`.

    A caller producing several variations builds one spec per variation
    (same prompt/size, distinct `seed`) rather than encoding a batch shape
    the contract does not describe.
    """

    prompt: str
    negative_prompt: str | None
    width: int
    height: int
    seed: int | None
    batch_size: int = 1
    lora_path: str | None = None
    lora_scale: float = 1.0
    reference_image_path: str | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageProviderIdentity:
    """Stable provider/model identity attached to a result for provenance."""

    provider_id: str
    model_id: str
    request_id: str


@dataclass(frozen=True)
class ImageProviderResult:
    """Normalized output of one `ImageProvider.generate_image` call."""

    identity: ImageProviderIdentity
    image: Any
    metadata: dict[str, Any] = field(default_factory=dict)


class UnsupportedImageParameterError(ValueError):
    """A spec needs a capability the target provider does not declare."""


def validate_capabilities(
    capabilities: ImageProviderCapabilities,
    spec: ImageGenerationSpec,
) -> None:
    """Reject `spec` if it needs a capability `capabilities` does not declare.

    Raised before any provider is invoked, naming the offending parameter so
    the caller (or the API layer above it) can surface an actionable error
    instead of the provider silently ignoring an unsupported option.
    """

    if not capabilities.supports_text_to_image:
        raise UnsupportedImageParameterError(
            "This provider does not support text-to-image generation."
        )
    if spec.reference_image_path is not None and not capabilities.supports_reference_image:
        raise UnsupportedImageParameterError(
            "reference_image_path is not supported by this provider."
        )
    if spec.lora_path is not None and not capabilities.supports_lora:
        raise UnsupportedImageParameterError(
            "lora_path is not supported by this provider."
        )
    if spec.seed is not None and not capabilities.supports_seed:
        raise UnsupportedImageParameterError(
            "seed is not supported by this provider."
        )
    if spec.batch_size > capabilities.max_batch:
        raise UnsupportedImageParameterError(
            f"batch_size={spec.batch_size} exceeds this provider's max_batch "
            f"of {capabilities.max_batch}."
        )
    for dimension_name, dimension_value in (
        ("width", spec.width),
        ("height", spec.height),
    ):
        if not (capabilities.min_size <= dimension_value <= capabilities.max_size):
            raise UnsupportedImageParameterError(
                f"{dimension_name}={dimension_value} is outside this provider's "
                f"supported range [{capabilities.min_size}, {capabilities.max_size}]."
            )
        if dimension_value % capabilities.size_step != 0:
            raise UnsupportedImageParameterError(
                f"{dimension_name}={dimension_value} must be a multiple of "
                f"{capabilities.size_step} for this provider."
            )


@runtime_checkable
class ImageProvider(Protocol):
    """Common contract every image generation backend implements.

    `generate_image` must call `validate_capabilities(self.capabilities, spec)`
    (or an equivalent check) before invoking the underlying model, so an
    unsupported parameter is rejected rather than silently dropped.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def capabilities(self) -> ImageProviderCapabilities: ...

    def generate_image(
        self,
        spec: ImageGenerationSpec,
        *,
        request_id: str,
    ) -> ImageProviderResult: ...


_LOCAL_DIFFUSERS_CAPABILITIES = ImageProviderCapabilities(
    supports_text_to_image=True,
    supports_reference_image=False,
    supports_lora=True,
    supports_seed=True,
    min_size=64,
    max_size=2048,
    size_step=8,
    max_batch=4,
)


class LocalDiffusersImageProvider:
    """Adapts an already-loaded local diffusers pipeline onto the contract.

    This wraps the exact pipeline call `ImageGenerator` performed inline
    before this contract existed: `generate_image` invokes
    `pipeline(**pipeline_kwargs)` with the caller-supplied kwargs unchanged
    and returns the first output image, so routing through this provider
    changes no observable output.
    """

    def __init__(
        self,
        *,
        model_id: str,
        pipeline: Any,
        capabilities: ImageProviderCapabilities | None = None,
    ) -> None:
        self._model_id = model_id
        self._pipeline = pipeline
        self._capabilities = capabilities or _LOCAL_DIFFUSERS_CAPABILITIES

    @property
    def provider_id(self) -> str:
        return "local-diffusers"

    @property
    def capabilities(self) -> ImageProviderCapabilities:
        return self._capabilities

    def generate_image(
        self,
        spec: ImageGenerationSpec,
        *,
        request_id: str,
        pipeline_kwargs: dict[str, Any] | None = None,
    ) -> ImageProviderResult:
        validate_capabilities(self._capabilities, spec)
        call_kwargs = dict(pipeline_kwargs) if pipeline_kwargs is not None else {
            "prompt": spec.prompt,
            "negative_prompt": spec.negative_prompt,
            "width": spec.width,
            "height": spec.height,
            **spec.extra_params,
        }
        pipeline_output = self._pipeline(**call_kwargs)
        image = pipeline_output.images[0]
        identity = ImageProviderIdentity(
            provider_id=self.provider_id,
            model_id=self._model_id,
            request_id=request_id,
        )
        return ImageProviderResult(identity=identity, image=image, metadata={})
