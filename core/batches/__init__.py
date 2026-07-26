"""Batch fan-out: expand one creative intent into many comparable generations."""

from .expansion import expand_items
from .repository import BatchRepository
from .schemas import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_PARTIAL,
    BATCH_STATUS_QUEUED,
    BATCH_STATUS_RUNNING,
    BATCH_STATUS_SUCCEEDED,
    BATCH_STRATEGIES,
    DEFAULT_ITEM_LIMIT,
    ITEM_STATUS_PENDING,
    SEED_POLICIES,
    Axis,
    AxisValue,
    BatchAggregate,
    BatchItem,
    BatchRecord,
    BatchSpec,
    Stage,
)
from .service import BatchService, resolve_max_items_limit
from .templates import (
    PROBE_STAGE_PARAMS,
    REFINE_STAGE_PARAMS,
    build_batch_template,
    list_batch_templates,
)

__all__ = [
    "BATCH_STATUS_CANCELLED",
    "BATCH_STATUS_FAILED",
    "BATCH_STATUS_PARTIAL",
    "BATCH_STATUS_QUEUED",
    "BATCH_STATUS_RUNNING",
    "BATCH_STATUS_SUCCEEDED",
    "BATCH_STRATEGIES",
    "DEFAULT_ITEM_LIMIT",
    "ITEM_STATUS_PENDING",
    "PROBE_STAGE_PARAMS",
    "REFINE_STAGE_PARAMS",
    "SEED_POLICIES",
    "Axis",
    "AxisValue",
    "BatchAggregate",
    "BatchItem",
    "BatchRecord",
    "BatchRepository",
    "BatchService",
    "BatchSpec",
    "Stage",
    "build_batch_template",
    "expand_items",
    "list_batch_templates",
    "resolve_max_items_limit",
]
