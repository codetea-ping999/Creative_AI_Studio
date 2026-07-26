"""Prompt composition: turn declarative intent into a reproducible prompt."""

from .composer import (
    TEMPLATE_NEGATIVE_TAILS,
    TEMPLATE_TAILS,
    ComposedPrompt,
    PromptComposer,
    PromptSpec,
)
from .patterns import get_axis_catalog, list_axis_catalogs

__all__ = [
    "ComposedPrompt",
    "PromptComposer",
    "PromptSpec",
    "TEMPLATE_NEGATIVE_TAILS",
    "TEMPLATE_TAILS",
    "get_axis_catalog",
    "list_axis_catalogs",
]
