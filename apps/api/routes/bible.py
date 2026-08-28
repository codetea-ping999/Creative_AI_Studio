"""Creative Bible endpoints for reusable character and style settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.assets import Asset
from core.bible import BIBLE_KINDS, BibleEntry
from core.prompting import PromptSpec, get_axis_catalog, list_axis_catalogs
from core.reference_capabilities import MissingReferenceAssetError, ReferenceImageInput

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
    # #199: same asset ids as above, but with the character/location role and
    # strength a generation request would carry -- lets the preview surface
    # what reference conditioning would actually be requested.
    resolved_references: list[ReferenceImageInput] = Field(default_factory=list)
    palette: list[str]
    attributes: dict[str, str]
    applied: list[dict[str, Any]]
    conflicts: list[str]


class AxisCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    values: list[dict[str, Any]]


class PromoteWinnerRequest(BaseModel):
    """Which generated asset should become this entry's new baseline (#195)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)


class PromoteWinnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: BibleEntryResponse
    # The `promotion_history` entry this call just appended -- returned inline
    # so a caller does not need a second request to see what was applied, even
    # though `BibleEntryResponse` itself omits `metadata` (see `from_entry`).
    promotion: dict[str, Any]


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
    try:
        composed = services.prompt_composer.compose(PromptSpec(**request.model_dump()))
    except MissingReferenceAssetError as exc:
        # #199: a broken Bible reference (deleted/incompatible asset) must
        # surface here, in the dry-run the UI calls before generating,
        # exactly the way an unsupported reference already fails at job
        # creation (see UnsupportedReferenceError in generate.py/jobs.py).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
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


@router.post("/{entry_id}/promote", response_model=PromoteWinnerResponse)
def promote_bible_winner(
    entry_id: str,
    request: PromoteWinnerRequest,
    services: ApplicationServices = Depends(get_services),
) -> PromoteWinnerResponse:
    """Promote one completed asset into ``entry_id``'s baseline (#195, #49).

    The winner is chosen by a human (see the parent epic's non-goal: no
    automatic winner selection) and identified only by ``asset_id`` -- every
    other value is read back from that asset's own recorded metadata, never
    from the request body, so promoting cannot smuggle in whatever the caller's
    UI happens to be showing right now.
    """

    asset = services.asset_repository.get(request.asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found"
        )

    model_id, seed, params, attributes = _effective_promotion_settings(asset)
    if seed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Asset {asset.id!r} has no effective seed to lock; "
                "promotion requires a completed generation with a recorded seed."
            ),
        )
    if not model_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Asset {asset.id!r} has no effective model_id to promote.",
        )

    try:
        entry = services.bible_repository.promote_winner(
            entry_id,
            asset_id=asset.id,
            job_id=asset.job_id,
            model_id=model_id,
            seed=seed,
            params=params,
            attributes=attributes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bible entry not found"
        )

    history = entry.metadata.get("promotion_history")
    latest = history[-1] if isinstance(history, list) and history else {}
    return PromoteWinnerResponse(entry=BibleEntryResponse.from_entry(entry), promotion=latest)


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


def _effective_promotion_settings(
    asset: Asset,
) -> tuple[str | None, int | None, dict[str, Any], dict[str, str]]:
    """Read back what a generation actually used, not what a form might show.

    Mirrors ``apps/api/routes/gallery.py``'s ``_request_from_asset``: the
    model/seed/params an asset was *actually* produced with live in
    ``asset.metadata['request_snapshot']`` (built once, at sync time, by
    ``core.assets._effective_request_snapshot``), not on ``asset`` itself or on
    the originating job's stored request -- a multi-variation job's assets each
    resolve to a different effective seed from the same request, for instance.
    Character attributes a Bible reference contributed to the prompt live
    separately, in ``asset.metadata['prompt_composition']['attributes']``.
    """

    model_id: str | None = asset.model_id or None
    seed: int | None = None
    params: dict[str, Any] = {}

    snapshot = asset.metadata.get("request_snapshot")
    if isinstance(snapshot, dict):
        snapshot_model_id = snapshot.get("model_id")
        if isinstance(snapshot_model_id, str) and snapshot_model_id:
            model_id = snapshot_model_id
        snapshot_seed = snapshot.get("seed")
        if isinstance(snapshot_seed, int) and not isinstance(snapshot_seed, bool):
            seed = snapshot_seed
        snapshot_params = snapshot.get("params")
        if isinstance(snapshot_params, dict):
            params = dict(snapshot_params)

    attributes: dict[str, str] = {}
    composition = asset.metadata.get("prompt_composition")
    if isinstance(composition, dict):
        raw_attributes = composition.get("attributes")
        if isinstance(raw_attributes, dict):
            attributes = {str(key): str(value) for key, value in raw_attributes.items()}

    return model_id, seed, params, attributes


__all__ = ["router"]
