"""Runtime cache for loaded model instances."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

OnEvictCallback = Callable[[str, Any], None]

# Bucket key used for any entry that has no per-media budget configured for
# it -- either because the caller never passed `media_type` to `put()`, or
# because `media_limits` has no entry for that media type. Every such entry
# shares the single `max_entries` budget, which is exactly the pre-#182
# single-cache behavior (see "Missing per-media settings retain
# backward-compatible behavior" in issue #182's acceptance criteria).
_DEFAULT_BUCKET = "__default__"

# The media families the model system currently loads runtimes for (see
# `core.schemas.generation.MediaType`). Kept local to this module rather than
# importing `MediaType` so this cache stays a plain data structure that does
# not need to know about the generation-request schema.
DEFAULT_MEDIA_TYPES: tuple[str, ...] = ("image", "video", "audio", "text")


class ModelRuntimeCache:
    """Small in-memory cache for runtime reuse.

    Entries are grouped into "buckets": one per media type named in
    `media_limits`, plus a shared default bucket for everything else. Each
    bucket is evicted independently and deterministically -- oldest
    (least-recently-used) entry in that bucket first -- so, for example,
    configuring an image budget and a text budget lets one image runtime and
    one text runtime stay resident at the same time instead of the text load
    evicting the image runtime (or vice versa).
    """

    def __init__(
        self,
        max_entries: int = 1,
        *,
        media_limits: Mapping[str, int] | None = None,
        on_evict: OnEvictCallback | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        resolved_media_limits: dict[str, int] = {}
        for media_type, limit in (media_limits or {}).items():
            if limit < 1:
                raise ValueError(
                    f"media_limits[{media_type!r}] must be at least 1, got {limit!r}."
                )
            resolved_media_limits[media_type] = limit

        self.max_entries = max_entries
        self.media_limits = resolved_media_limits
        self._on_evict = on_evict
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._bucket_of: dict[str, str] = {}

    def has(self, model_id: str) -> bool:
        return model_id in self._cache

    def get(self, model_id: str) -> Any | None:
        if model_id not in self._cache:
            return None
        runtime = self._cache.pop(model_id)
        self._cache[model_id] = runtime
        return runtime

    def put(
        self,
        model_id: str,
        runtime_obj: Any,
        *,
        media_type: str | None = None,
    ) -> None:
        """Insert `runtime_obj`, then evict this entry's bucket down to budget.

        `media_type` selects which per-media budget (if any) governs this
        entry. Omitting it -- or passing a media type absent from
        `media_limits` -- puts the entry in the shared default bucket bounded
        by `max_entries`, unchanged from pre-#182 behavior.
        """

        if model_id in self._cache:
            # A replaced entry must run the same cleanup as unload()/
            # unload_all() -- on_evict is what actually returns GPU/MPS
            # memory (torch.mps.empty_cache() etc. in bootstrap/factories.py's
            # wiring), which plain Python GC does not reliably do on its own.
            previous_obj = self._cache.pop(model_id)
            self._evict(model_id, previous_obj)
        self._cache[model_id] = runtime_obj

        bucket = media_type if media_type in self.media_limits else _DEFAULT_BUCKET
        self._bucket_of[model_id] = bucket

        self._evict_bucket_overflow(bucket)

    def _evict_bucket_overflow(self, bucket: str) -> None:
        budget = self.media_limits.get(bucket, self.max_entries)
        # `self._cache` preserves LRU order across all buckets (an access
        # via `get()` moves an entry to the end); filtering it by bucket
        # keeps that relative order, so the earliest entry in this list is
        # deterministically the least-recently-used one for this bucket.
        bucket_ids = [
            entry_id
            for entry_id in self._cache
            if self._bucket_of.get(entry_id, _DEFAULT_BUCKET) == bucket
        ]
        while len(bucket_ids) > budget:
            evict_id = bucket_ids.pop(0)
            evicted_obj = self._cache.pop(evict_id)
            self._bucket_of.pop(evict_id, None)
            self._evict(evict_id, evicted_obj)

    def unload(self, model_id: str) -> None:
        if model_id not in self._cache:
            return
        runtime_obj = self._cache.pop(model_id)
        self._bucket_of.pop(model_id, None)
        self._evict(model_id, runtime_obj)

    def unload_all(self) -> None:
        while self._cache:
            model_id, runtime_obj = self._cache.popitem(last=False)
            self._bucket_of.pop(model_id, None)
            self._evict(model_id, runtime_obj)

    def loaded_ids(self) -> list[str]:
        return list(self._cache.keys())

    def _evict(self, model_id: str, runtime_obj: Any) -> None:
        if self._on_evict is None:
            return
        try:
            self._on_evict(model_id, runtime_obj)
        except Exception:  # noqa: BLE001 - a broken hook must not corrupt the cache
            logger.warning(
                "on_evict callback failed for model %r.", model_id, exc_info=True
            )


def resolve_media_cache_limits(
    env: Mapping[str, str] | None = None,
    *,
    media_types: Iterable[str] = DEFAULT_MEDIA_TYPES,
) -> dict[str, int]:
    """Read per-media runtime-cache budgets from `MAX_CACHED_MODELS_<MEDIA>`.

    For each `media_type` this checks `MAX_CACHED_MODELS_{MEDIA_TYPE.upper()}`
    (e.g. `MAX_CACHED_MODELS_TEXT`, `MAX_CACHED_MODELS_IMAGE`). A media type
    whose variable is absent, or whose value fails to parse as an integer
    >= 1, is left out of the returned mapping entirely rather than raising --
    that is what lets a caller pass the result straight to
    `ModelRuntimeCache(media_limits=...)` and get the pre-#182
    single-budget behavior for any media type nobody configured.
    """

    source = env if env is not None else os.environ
    limits: dict[str, int] = {}
    for media_type in media_types:
        env_var = f"MAX_CACHED_MODELS_{media_type.upper()}"
        raw_value = source.get(env_var)
        if raw_value is None or raw_value == "":
            continue
        try:
            parsed = int(raw_value)
        except ValueError:
            logger.warning(
                "Ignoring non-integer %s=%r; %r keeps the shared runtime-cache budget.",
                env_var,
                raw_value,
                media_type,
            )
            continue
        if parsed < 1:
            logger.warning(
                "Ignoring %s=%r (must be at least 1); %r keeps the shared "
                "runtime-cache budget.",
                env_var,
                raw_value,
                media_type,
            )
            continue
        limits[media_type] = parsed
    return limits


__all__ = [
    "DEFAULT_MEDIA_TYPES",
    "ModelRuntimeCache",
    "OnEvictCallback",
    "resolve_media_cache_limits",
]
