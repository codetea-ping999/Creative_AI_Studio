"""Catalog endpoints for local reusable assets such as LoRAs."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/catalog", tags=["catalog"])
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LORA_EXTENSIONS = {".safetensors", ".pt", ".bin", ".ckpt"}


class LoraCatalogItem(BaseModel):
    """Single local LoRA entry exposed to the UI."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)


class LoraCatalogResponse(BaseModel):
    """Response wrapper for local LoRA assets."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1)
    items: list[LoraCatalogItem] = Field(default_factory=list)


def _resolve_lora_root() -> Path:
    lora_root_env = os.getenv("LORA_ROOT")
    if lora_root_env:
        return Path(lora_root_env).expanduser().resolve()
    return (_REPO_ROOT / "models" / "loras").resolve()


@router.get("/loras", response_model=LoraCatalogResponse)
def list_loras() -> LoraCatalogResponse:
    root = _resolve_lora_root()
    if not root.exists():
        return LoraCatalogResponse(root=str(root), items=[])

    items: list[LoraCatalogItem] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _LORA_EXTENSIONS:
            continue
        relative_path = path.relative_to(_REPO_ROOT).as_posix() if path.is_relative_to(_REPO_ROOT) else path.name
        items.append(
            LoraCatalogItem(
                id=relative_path,
                display_name=path.stem.replace("_", " ").replace("-", " ").title(),
                path=str(path),
                relative_path=relative_path,
            )
        )
    return LoraCatalogResponse(root=str(root), items=items)


__all__ = ["LoraCatalogItem", "LoraCatalogResponse", "router"]
