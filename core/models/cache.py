"""Runtime cache for loaded model instances."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class ModelRuntimeCache:
    """Small in-memory cache for runtime reuse."""

    def __init__(self, max_entries: int = 1) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        self.max_entries = max_entries
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
            self._cache.popitem(last=False)

    def unload(self, model_id: str) -> None:
        self._cache.pop(model_id, None)

    def unload_all(self) -> None:
        self._cache.clear()

    def loaded_ids(self) -> list[str]:
        return list(self._cache.keys())


__all__ = ["ModelRuntimeCache"]
