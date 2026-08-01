"""Creative Bible endpoints for reusable character and style settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.bible import BIBLE_KINDS, BibleEntry
from core.prompting import PromptSpec, get_axis_catalog, list_axis_catalogs

router = APIRouter(prefix="/bible", tags=["bible"])


class BibleEntryResponse(BaseModel):
    """A bible entry as returned to the UI."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    name: str
    project_id: str | None
    summary: str
    prompt_fragment: str
    negative_fragment: str
    tokens: list[str]
    attributes: dict[str, str]
    lora: dict[str, Any] | None
    reference_asset_ids: list[str]
    seed_policy: dict[str, Any]
    palette: list[str]
    tone_and_manner: dict[str, Any]
    locked_fields: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_entry(cls, entry: BibleEntry) -> "BibleEntryResponse":
        payload = entry.to_dict()
        payload.pop("metadata", None)
        return cls(**payload)


class BibleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BibleEntryResponse]
    kinds: list[str] = Field(default_factory=lambda: list(BIBLE_KINDS))


class CreateBibleEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    name: str = Field(min_length=1)
    project_id: str | None = None
    summary: str = ""
    prompt_fragment: str = ""
    negative_fragment: str = ""
    tokens: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    lora: dict[str, Any] | None = None
    reference_asset_ids: list[str] = Field(default_factory=list)
    seed_policy: dict[str, Any] = Field(default_factory=dict)
    palette: list[str] = Field(default_factory=list)
    tone_and_manner: dict[str, Any] = Field(default_factory=dict)
    locked_fields: list[str] = Field(default_factory=list)


class UpdateBibleEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    project_id: str | None = None
    summary: str | None = None
    prompt_fragment: str | None = None
    negative_fragment: str | None = None
    tokens: list[str] | None = None
    attributes: dict[str, str] | None = None
    lora: dict[str, Any] | None = None
    reference_asset_ids: list[str] | None = None
    seed_policy: dict[str, Any] | None = None
    palette: list[str] | None = None
    tone_and_manner: dict[str, Any] | None = None
    locked_fields: list[str] | None = None


class PromptPreviewRequest(BaseModel):
    """Dry-run composition so a prompt can be inspected before generating."""

    model_config = ConfigDict(extra="forbid")

    base_prompt: str = ""
    negative_prompt: str | None = None
    bible_refs: list[str] = Field(default_factory=list)
    axis_values: dict[str, Any] = Field(default_factory=dict)
    template: str = "image"
    extra_fragments: list[str] = Field(default_factory=list)
    seed: int | None = None


class PromptPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    negative_prompt: str | None
    seed: int | None
    lora: dict[str, Any] | None
    reference_asset_ids: list[str]
    palette: list[str]
    attributes: dict[str, str]
    applied: list[dict[str, Any]]
    conflicts: list[str]


class AxisCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    values: list[dict[str, Any]]


@router.get("", response_model=BibleListResponse)
def list_bible_entries(
    kind: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    query: str | None = Query(default=None),
    services: ApplicationServices = Depends(get_services),
) -> BibleListResponse:
    if kind is not None and kind not in BIBLE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown kind {kind!r}; expected one of {', '.join(BIBLE_KINDS)}",
        )
    entries = services.bible_repository.list_all(
        kind=kind,
        project_id=project_id,
        query_text=query,
    )
    return BibleListResponse(
        items=[BibleEntryResponse.from_entry(entry) for entry in entries]
    )


@router.post("", response_model=BibleEntryResponse, status_code=status.HTTP_201_CREATED)
def create_bible_entry(
    request: CreateBibleEntryRequest,
    services: ApplicationServices = Depends(get_services),
) -> BibleEntryResponse:
    try:
        entry = services.bible_repository.create(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return BibleEntryResponse.from_entry(entry)


@router.get("/catalogs", response_model=list[str])
def list_catalogs() -> list[str]:
    return list_axis_catalogs()


@router.get("/catalogs/{name}", response_model=AxisCatalogResponse)
def get_catalog(name: str) -> AxisCatalogResponse:
    try:
        values = get_axis_catalog(name)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return AxisCatalogResponse(name=name, values=values)


@router.post("/preview", response_model=PromptPreviewResponse)
def preview_prompt(
    request: PromptPreviewRequest,
    services: ApplicationServices = Depends(get_services),
) -> PromptPreviewResponse:
    composed = services.prompt_composer.compose(PromptSpec(**request.model_dump()))
    return PromptPreviewResponse(**composed.model_dump(mode="json"))


@router.get("/{entry_id}", response_model=BibleEntryResponse)
def get_bible_entry(
    entry_id: str,
    services: ApplicationServices = Depends(get_services),
) -> BibleEntryResponse:
    entry = services.bible_repository.get(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bible entry not found"
        )
    return BibleEntryResponse.from_entry(entry)


@router.patch("/{entry_id}", response_model=BibleEntryResponse)
def update_bible_entry(
    entry_id: str,
    request: UpdateBibleEntryRequest,
    services: ApplicationServices = Depends(get_services),
) -> BibleEntryResponse:
    fields = request.model_dump(exclude_unset=True)
    try:
        entry = services.bible_repository.update(entry_id, **fields)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bible entry not found"
        )
    return BibleEntryResponse.from_entry(entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bible_entry(
    entry_id: str,
    services: ApplicationServices = Depends(get_services),
) -> None:
    if not services.bible_repository.delete(entry_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bible entry not found"
        )


__all__ = ["router"]
