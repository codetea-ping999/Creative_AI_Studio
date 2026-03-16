"""Sample learned text-to-video runtime adapter.

Replace this file with a real local runtime loader. The current scaffold keeps the
manifest selectable while falling back to the procedural storyboard runtime.
"""

from __future__ import annotations

from typing import Any


def load_runtime(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime_adapter": "learned_text_to_video",
        "fallback_runtime": manifest.get("default_params", {}).get(
            "fallback_runtime",
            "procedural_storyboard",
        ),
        "load_error": (
            "No learned text-to-video runtime is connected yet. "
            "Replace models/video/learned-runtime/runtime.py with a local loader."
        ),
    }
