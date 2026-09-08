"""Batch endpoints for fan-out generation and comparison."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_services
from bootstrap import ApplicationServices
from core.batches import (
    BatchRecord,
    BatchSpec,
    BatchStageMaterializationError,
    build_batch_template,
    list_batch_templates,
)
from core.reference_capabilities import MissingReferenceAssetError, UnsupportedReferenceError

router = APIRouter(prefix="/batches", tags=["batches"])


def _detail_with_committed_batch_id(exc: Exception) -> str:
    """Append an already-committed batch's id to a permanent-failure message.

    `UnsupportedReferenceError` / `MissingReferenceAssetError` / a plain
    `ValueError` raised from inside `create_batch()`'s stage-0
    materialization retry loop can fire *after* the Batch record was
    already durably committed (Codex exact-HEAD audit, eleventh round,
    adversarial follow-up to finding 1: these three widely-reused
    exception types otherwise carry no batch identity at all, unlike
    `BatchStageMaterializationError`, which finding 1 itself already
    fixed). Without this, the client sees a bare client error with no way
    to tell the batch already exists -- and, if it blindly retries the
    exact same create request assuming nothing happened, mints a second,
    separate batch under a new id for the identical permanent failure.

    `committed_batch_id` is only ever present when `create_batch()`'s own
    retry loop stamped it, right before re-raising, onto an exception
    instance it knows fired after that exact commit -- absent for every
    other raise site of these same shared exception types (the pre-commit
    preflight check above, `advance_batch()`, template building, ...),
    which have no batch to report and must not be reworded.
    """

    committed_batch_id = getattr(exc, "committed_batch_id", None)
    detail = str(exc)
    if committed_batch_id is not None:
        detail = (
            f"{detail} (batch {committed_batch_id} was already created -- "
            f"do not resubmit; use GET /batches/{committed_batch_id} instead)"
        )
    return detail


class BatchItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    index: int
    label: str
    stage_name: str
    stage_index: int
    axis_values: dict[str, str]
    job_id: str | None
    status: str
    score: float | None
    output_path: str | None
    preview_path: str | None
    error_message: str | None
    promoted: bool


class BatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    media_type: str
    project_id: str | None
    status: str
    stage_index: int
    stage_names: list[str]
    aggregate: dict[str, Any]
    items: list[BatchItemResponse]
    error_message: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: BatchRecord) -> "BatchResponse":
        return cls(
            id=record.id,
            name=record.spec.name,
            media_type=record.spec.media_type,
            project_id=record.spec.project_id,
            status=record.status,
            stage_index=record.stage_index,
            stage_names=[stage.name for stage in record.spec.resolved_stages()],
            aggregate=record.aggregate.model_dump(mode="json"),
            items=[
                BatchItemResponse(**item.model_dump(mode="json", exclude={"request"}))
                for item in record.items
            ],
            error_message=record.advance_error,
            created_at=record.created_at.isoformat(),
            updated_at=record.updated_at.isoformat(),
        )


class BatchListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BatchResponse]


class CreateBatchRequest(BaseModel):
    """Create a batch from an explicit spec or from a named template."""

    model_config = ConfigDict(extra="forbid")

    spec: BatchSpec | None = None
    template: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=BatchListResponse)
def list_batches(
    project_id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
    services: ApplicationServices = Depends(get_services),
) -> BatchListResponse:
    records = services.batch_service.list_batches(project_id=project_id, limit=limit)
    return BatchListResponse(
        items=[BatchResponse.from_record(record) for record in records]
    )


@router.post(
    "",
    response_model=BatchResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        # PR3 exact-HEAD audit, eleventh round, finding 8: round 10 added
        # a genuine runtime 202 response (materialization exhausted, but
        # the Batch is durably committed) without declaring it here, so
        # the generated OpenAPI contract told clients a successful create
        # could only ever be 201 -- documenting the runtime behavior this
        # route actually has, not adding a new one.
        status.HTTP_202_ACCEPTED: {
            "model": BatchResponse,
            "description": (
                "The batch was created and durably committed, but its "
                "stage-0 materialization could not be confirmed complete "
                "right now (a transient storage failure). Continue using "
                "the returned batch id -- poll GET /batches/{id}, or wait "
                "for the next process restart's startup resume -- rather "
                "than submitting a new create request."
            ),
        },
    },
)
def create_batch(
    request: CreateBatchRequest,
    response: Response,
    services: ApplicationServices = Depends(get_services),
) -> BatchResponse:
    if (request.spec is None) == (request.template is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of spec or template.",
        )

    if request.template is not None:
        try:
            spec = build_batch_template(request.template, **request.overrides)
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
    else:
        spec = request.spec
        if request.overrides:
            spec = spec.model_copy(update=request.overrides)

    if spec.project_id is not None:
        if services.project_repository.get(spec.project_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
            )

    try:
        record = services.batch_service.create_batch(spec)
    except (UnsupportedReferenceError, MissingReferenceAssetError) as exc:
        # Both subclass ValueError, so this must come before the plain
        # `except ValueError` below -- Python matches except clauses in
        # order, and the broader clause would otherwise catch these first,
        # making this one unreachable and reporting 400 instead of 422.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_detail_with_committed_batch_id(exc),
        ) from exc
    except BatchStageMaterializationError as exc:
        # `POST /batches` is non-idempotent: unlike `advance_batch()`'s
        # identical exception (safe to retry -- the same batch_id is
        # already in the URL), the Batch this raised for was already
        # durably committed to disk *before* this exception was ever
        # raised (PR3 exact-HEAD audit, tenth round, finding 1). Reporting
        # a bare 503 here would invite a client to retry the *create*
        # request, minting a second, entirely separate Batch under a new
        # id -- both eventually running -- while also skipping the
        # project-binding loop below for whatever children already got a
        # stable id.
        #
        # Uses `exc.record` directly -- never a separate `get_batch()`
        # call of its own (PR3 exact-HEAD audit, eleventh round, finding
        # 1): a second read here would itself be fallible, and if the
        # exact same storage failure that caused the exhaustion also
        # broke *this* read, the route would fall back to exactly the
        # bare 503 this fix exists to eliminate -- the committed Batch's
        # identity would survive on disk but be unreachable from this
        # response. `create_batch()`'s own raise site guarantees `record`
        # is never `None` (see `BatchStageMaterializationError`'s own
        # docstring for how). `record` being present is not a claim that
        # materialization is complete -- only that this exact committed
        # Batch's identity/state, as best known at the moment of raising,
        # is returned: 202 Accepted, not 201 -- the request was accepted
        # and the batch does exist, but materialization is not yet
        # confirmed complete. The client continues using this same batch
        # id (poll `GET /batches/{id}`, or wait for the next restart's
        # startup resume) instead of submitting a new create request.
        committed_record = exc.record
        if committed_record is None:
            # Defensive only -- unreachable given `create_batch()`'s own
            # guarantee, but never silently fabricate a response for a
            # batch this exception cannot actually identify.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if spec.project_id is not None:
            for item in committed_record.items:
                if item.job_id:
                    services.project_repository.add_job(spec.project_id, item.job_id)
        response.status_code = status.HTTP_202_ACCEPTED
        return BatchResponse.from_record(committed_record)
    except ValueError as exc:
        # Expansion refusing an oversized sweep is a client error, and the message
        # already names the count and the cap.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_detail_with_committed_batch_id(exc),
        ) from exc

    if spec.project_id is not None:
        for item in record.items:
            if item.job_id:
                services.project_repository.add_job(spec.project_id, item.job_id)

    return BatchResponse.from_record(record)


@router.get("/templates", response_model=list[dict[str, Any]])
def get_batch_templates() -> list[dict[str, Any]]:
    return list_batch_templates()


@router.get("/{batch_id}", response_model=BatchResponse)
def get_batch(
    batch_id: str,
    services: ApplicationServices = Depends(get_services),
) -> BatchResponse:
    record = services.batch_service.get_batch(batch_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )
    return BatchResponse.from_record(record)


@router.post("/{batch_id}/advance", response_model=BatchResponse)
def advance_batch(
    batch_id: str,
    services: ApplicationServices = Depends(get_services),
) -> BatchResponse:
    try:
        record = services.batch_service.advance(batch_id)
    except (UnsupportedReferenceError, MissingReferenceAssetError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except BatchStageMaterializationError as exc:
        # The stage advance itself persisted, but materializing its Job
        # rows could not be confirmed right now (a transient storage
        # failure, not a permanent reference problem) -- distinct from a
        # 404 (the batch is confirmed gone) and a 422 (a permanent
        # preflight failure): retrying the same request once storage
        # recovers is the correct next step.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )
    return BatchResponse.from_record(record)


@router.post("/{batch_id}/cancel", response_model=BatchResponse)
def cancel_batch(
    batch_id: str,
    services: ApplicationServices = Depends(get_services),
) -> BatchResponse:
    record = services.batch_service.cancel(batch_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )
    return BatchResponse.from_record(record)


@router.post("/{batch_id}/items/{item_id}/promote", response_model=BatchResponse)
def promote_batch_item(
    batch_id: str,
    item_id: str,
    services: ApplicationServices = Depends(get_services),
) -> BatchResponse:
    try:
        record = services.batch_service.promote(batch_id, item_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found"
        )
    return BatchResponse.from_record(record)


__all__ = ["router"]
