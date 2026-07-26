"""Runtime cache for loaded model instances."""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

OnEvictCallback = Callable[[str, Any], None]


class ModelRuntimeCache:
    """Small in-memory cache for runtime reuse."""

    def __init__(
        self,
        max_entries: int = 1,
        *,
        on_evict: OnEvictCallback | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        self.max_entries = max_entries
        self._on_evict = on_evict
        self._cache: OrderedDict[str, Any] = OrderedDict()

    def has(self, model_id: str) -> bool:
        return model_id in self._cache

    def get(self, model_id: str) -> Any | None:
        if model_id not in self._cache:
            return None
        runtime = self._cache.pop(model_id)
        self._cache[model_id] = runtime
        return runtime

    def put(self, model_id: str, runtime_obj: Any) -> None:
        if model_id in self._cache:
            self._cache.pop(model_id)
        self._cache[model_id] = runtime_obj

        while len(self._cache) > self.max_entries:
            evicted_id, evicted_obj = self._cache.popitem(last=False)
            self._evict(evicted_id, evicted_obj)

    def unload(self, model_id: str) -> None:
        if model_id not in self._cache:
            return
        runtime_obj = self._cache.pop(model_id)
        self._evict(model_id, runtime_obj)

    def unload_all(self) -> None:
        while self._cache:
            model_id, runtime_obj = self._cache.popitem(last=False)
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


__all__ = ["ModelRuntimeCache", "OnEvictCallback"]
