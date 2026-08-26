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
- `ensure_image_provider_opt_in` rejects a remote provider before any
  transport call unless explicit opt-in (`ALLOW_CLOUD_PROVIDERS`, or a
  provider-specific `ALLOW_CLOUD_PROVIDER_<ID>`) is present; local providers
  are always allowed, so provider-unspecified/default generation stays local
  with zero remote calls (issue #256).
- `build_image_provider_preflight` / `run_image_provider_request` build the
  "what would leave the machine" disclosure (destination, prompt summary,
  reference asset ids/roles, estimated request count) before a real
  transport call, and `run_image_provider_request` guarantees the opt-in
  guard runs ahead of that call.

Secret handling, retries, cost estimation, and the actual outbound transport
call are out of scope here (see the sibling micro-issues under #66); this
module only fixes the shape the local path and a future cloud path both
implement, plus the opt-in/disclosure gate every remote call must pass
through before reaching that (not-yet-built) transport.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import os
from typing import Any, Protocol, TypeVar, runtime_checkable

__all__ = [
    "ImageGenerationSpec",
    "ImageProvider",
    "ImageProviderCapabilities",
    "ImageProviderIdentity",
    "ImageProviderPreflightDisclosure",
    "ImageProviderReferenceAsset",
    "ImageProviderResult",
    "LocalDiffusersImageProvider",
    "RemoteImageProviderOptInRequiredError",
    "UnsupportedImageParameterError",
    "build_image_provider_preflight",
    "cloud_image_provider_opt_in_granted",
    "ensure_image_provider_opt_in",
    "is_local_image_provider",
    "run_image_provider_request",
    "summarize_prompt_for_preflight",
    "validate_capabilities",
]

_T = TypeVar("_T")


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


class RemoteImageProviderOptInRequiredError(PermissionError):
    """A remote provider was invoked without explicit outbound opt-in.

    Distinct from :class:`UnsupportedImageParameterError`: this is not about
    a malformed request, it is about authorization to send anything at all
    to a given remote provider. Always raised before a transport call is
    reachable -- see :func:`ensure_image_provider_opt_in`.
    """


_GLOBAL_CLOUD_OPT_IN_ENV_VAR = "ALLOW_CLOUD_PROVIDERS"
_LOCAL_PROVIDER_ID_PREFIX = "local-"
_PROMPT_SUMMARY_MAX_CHARS = 160


@dataclass(frozen=True)
class ImageProviderReferenceAsset:
    """One reference asset that would be attached to an outbound request.

    `role` names *why* the asset is in scope (e.g. "style-reference",
    "character-reference", "init-image") so a preflight reviewer sees not
    only that an asset would leave the machine but what it is for.
    """

    asset_id: str
    role: str


@dataclass(frozen=True)
class ImageProviderPreflightDisclosure:
    """Everything that would leave the machine if a request were sent now.

    Built without making any transport call, so it can be shown to a caller
    (API layer, UI, or a human operator) *before* a remote request is
    submitted -- this is what issue #256 calls "preflight disclosure".
    `destination` is `"local"` or `"remote"` rather than a raw provider id,
    so a reviewer does not have to know provider-id naming conventions to
    tell whether anything would leave the machine at all.
    """

    provider_id: str
    model_id: str
    destination: str
    prompt_summary: str
    reference_assets: tuple[ImageProviderReferenceAsset, ...] = ()
    estimated_request_count: int = 1


def is_local_image_provider(provider_id: str) -> bool:
    """Whether `provider_id` names a local (on-machine) provider.

    Anything else is treated as remote and subject to
    :func:`ensure_image_provider_opt_in`. This is what keeps
    provider-unspecified/default generation local with zero remote calls
    (issue #256 acceptance criteria): `LocalDiffusersImageProvider` is the
    only provider constructed when a request does not name one, and its
    `provider_id` (`"local-diffusers"`) satisfies this check.
    """

    return provider_id.startswith(_LOCAL_PROVIDER_ID_PREFIX)


def _provider_specific_opt_in_env_var(provider_id: str) -> str:
    """Env var name that opts in exactly one provider, e.g. `ALLOW_CLOUD_PROVIDER_ACME`."""

    suffix = "".join(character if character.isalnum() else "_" for character in provider_id.upper())
    return f"ALLOW_CLOUD_PROVIDER_{suffix}"


def cloud_image_provider_opt_in_granted(
    provider_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether remote provider `provider_id` has explicit opt-in right now.

    Two independent switches, either sufficient on its own:
    - `ALLOW_CLOUD_PROVIDERS=true` opts every remote provider in at once.
    - `ALLOW_CLOUD_PROVIDER_<PROVIDER_ID>=true` opts in only that one
      provider, so a deployment can enable a single vetted vendor without a
      blanket switch that would also opt in providers it never vetted.

    Both are default-closed: unset, empty, or any value other than the
    literal string `"true"` means "not granted" -- matching
    `ALLOW_REMOTE_TEXT_ENDPOINTS` (`core/models/text_runtimes.py`) and
    `ALLOW_REMOTE_AUDIO_ENDPOINTS` (`core/models/audio_runtimes.py`).
    """

    source = env if env is not None else os.environ
    if str(source.get(_GLOBAL_CLOUD_OPT_IN_ENV_VAR, "")).strip().lower() == "true":
        return True
    provider_env_var = _provider_specific_opt_in_env_var(provider_id)
    return str(source.get(provider_env_var, "")).strip().lower() == "true"


def ensure_image_provider_opt_in(
    provider_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise before any transport call if `provider_id` lacks opt-in.

    Local providers (see :func:`is_local_image_provider`) are always
    allowed -- this only gates remote ones. Reading `env` only touches
    process state already present, so this check itself never performs
    network I/O; it is safe to call unconditionally ahead of a transport
    invocation.
    """

    if is_local_image_provider(provider_id):
        return
    if cloud_image_provider_opt_in_granted(provider_id, env=env):
        return
    raise RemoteImageProviderOptInRequiredError(
        f"Remote image provider {provider_id!r} requires explicit opt-in. "
        f"Set {_GLOBAL_CLOUD_OPT_IN_ENV_VAR}=true to allow all remote image "
        f"providers, or {_provider_specific_opt_in_env_var(provider_id)}=true "
        "to allow only this one."
    )


def summarize_prompt_for_preflight(
    prompt: str, *, max_chars: int = _PROMPT_SUMMARY_MAX_CHARS
) -> str:
    """Collapse whitespace and truncate `prompt` for preflight display.

    Never includes reference image bytes, LoRA weights, or any secret -- a
    prompt is ordinary request text the caller already supplied, only
    shortened so a preflight summary stays reviewable rather than
    reproducing an arbitrarily long prompt verbatim.
    """

    collapsed = " ".join(prompt.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1].rstrip() + "…"


def build_image_provider_preflight(
    spec: ImageGenerationSpec,
    *,
    provider_id: str,
    model_id: str,
    reference_assets: Sequence[ImageProviderReferenceAsset] = (),
) -> ImageProviderPreflightDisclosure:
    """Build the issue #256 preflight disclosure for `spec`.

    Pure and side-effect free: no transport call, no opt-in check. A caller
    building a UI/API preview can call this for a remote provider even when
    opt-in has not been granted yet, so a human can see what *would* leave
    the machine before deciding to opt in. Enforcement is a separate call,
    :func:`ensure_image_provider_opt_in` -- see :func:`run_image_provider_request`
    for the combination a caller should use ahead of an actual transport
    invocation.
    """

    return ImageProviderPreflightDisclosure(
        provider_id=provider_id,
        model_id=model_id,
        destination="local" if is_local_image_provider(provider_id) else "remote",
        prompt_summary=summarize_prompt_for_preflight(spec.prompt),
        reference_assets=tuple(reference_assets),
        estimated_request_count=max(1, spec.batch_size),
    )


def run_image_provider_request(
    spec: ImageGenerationSpec,
    *,
    provider_id: str,
    model_id: str,
    transport: Callable[[], _T],
    reference_assets: Sequence[ImageProviderReferenceAsset] = (),
    env: Mapping[str, str] | None = None,
) -> tuple[ImageProviderPreflightDisclosure, _T]:
    """Disclose, then authorize, then invoke `transport` -- in that order.

    This is the one call sequence a caller should use ahead of any real
    transport invocation (the actual network call is out of scope here; see
    #257+): it guarantees `transport` is unreachable for a remote provider
    lacking opt-in, so the guard demonstrably runs before transport rather
    than relying on each call site to order the two checks itself.
    """

    disclosure = build_image_provider_preflight(
        spec,
        provider_id=provider_id,
        model_id=model_id,
        reference_assets=reference_assets,
    )
    ensure_image_provider_opt_in(provider_id, env=env)
    return disclosure, transport()


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
        if pipeline_kwargs is None:
            # This fallback cannot honor spec.seed/lora_path/lora_scale: a
            # diffusers pipeline expects seed as a `torch.Generator`, not a
            # raw int, and LoRA is applied via a pre-call
            # `pipeline.load_lora_weights(...)`, not a kwarg -- neither
            # translation belongs in this generic contract. Silently dropping
            # a requested seed or LoRA would be a correctness bug the caller
            # would never see, so fail clearly instead of guessing.
            if spec.seed is not None or spec.lora_path is not None:
                raise UnsupportedImageParameterError(
                    "LocalDiffusersImageProvider cannot honor spec.seed or "
                    "spec.lora_path without explicit pipeline_kwargs (seed needs "
                    "a torch.Generator, LoRA needs a pre-call load_lora_weights); "
                    "pass pipeline_kwargs built the same way ImageGenerator does."
                )
            call_kwargs: dict[str, Any] = {
                "prompt": spec.prompt,
                "negative_prompt": spec.negative_prompt,
                "width": spec.width,
                "height": spec.height,
                **spec.extra_params,
            }
        else:
            call_kwargs = dict(pipeline_kwargs)
        pipeline_output = self._pipeline(**call_kwargs)
        image = pipeline_output.images[0]
        identity = ImageProviderIdentity(
            provider_id=self.provider_id,
            model_id=self._model_id,
            request_id=request_id,
        )
        return ImageProviderResult(identity=identity, image=image, metadata={})
