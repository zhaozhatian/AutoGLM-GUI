"""Push ingest endpoints used by the internal system."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from AutoGLM_GUI.push import ingest_adapter
from AutoGLM_GUI.push.schemas import (
    PushBatchAccepted,
    PushBatchIn,
    PushBatchOut,
    PushIngestDevice,
    PushItemOut,
    PushItemPlan,
)
from AutoGLM_GUI.push.settings import push_settings
from AutoGLM_GUI.push.store import (
    ITEM_FAILED,
    ITEM_SUCCEEDED,
    push_store,
)


def require_ingest_token(
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.environ.get("AUTOGLM_INGEST_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Ingest API disabled: set AUTOGLM_INGEST_TOKEN on the server",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid X-API-Key")


router = APIRouter(dependencies=[Depends(require_ingest_token)])


def _item_plan(item: dict[str, Any], index: int) -> PushItemPlan:
    return PushItemPlan(
        item_id=str(item.get("id") or f"dry-run-{index}"),
        external_item_id=str(item["external_item_id"]),
        device_serial=str(item["device_serial"]),
        group_name=str(item["group_name"]),
        scheduled_at=str(item["scheduled_at"]),
        window_end=str(item["window_end"]),
    )


def _accepted(
    batch: dict[str, Any], items: list[dict[str, Any]], *, created: bool
) -> PushBatchAccepted:
    return PushBatchAccepted(
        batch_id=str(batch["id"]),
        external_batch_id=str(batch["external_batch_id"]),
        status=str(batch["status"]),
        item_count=len(items),
        items=[_item_plan(item, index) for index, item in enumerate(items)],
        created=created,
    )


def _item_out(item: dict[str, Any]) -> PushItemOut:
    return PushItemOut(
        id=str(item["id"]),
        external_item_id=str(item["external_item_id"]),
        device_serial=str(item["device_serial"]),
        group_name=str(item["group_name"]),
        message=str(item["message"]),
        mention_all=bool(item["mention_all"]),
        scheduled_at=str(item["scheduled_at"]),
        window_end=str(item["window_end"]),
        status=str(item["status"]),
        attempts=int(item["attempts"]),
        task_id=item.get("task_id"),
        device_id=item.get("device_id"),
        last_error=item.get("last_error"),
        created_at=str(item["created_at"]),
        updated_at=str(item["updated_at"]),
        finished_at=item.get("finished_at"),
    )


def _batch_out(batch: dict[str, Any], items: list[dict[str, Any]]) -> PushBatchOut:
    counts = push_store.batch_counts(str(batch["id"]))
    return PushBatchOut(
        id=str(batch["id"]),
        external_batch_id=str(batch["external_batch_id"]),
        callback_url=batch.get("callback_url"),
        window_start=str(batch["window_start"]),
        window_end=str(batch["window_end"]),
        status=str(batch["status"]),
        created_at=str(batch["created_at"]),
        updated_at=str(batch["updated_at"]),
        finished_at=batch.get("finished_at"),
        item_count=len(items),
        success_count=int(counts.get(ITEM_SUCCEEDED, 0)),
        failure_count=int(counts.get(ITEM_FAILED, 0)),
        items=[_item_out(item) for item in items],
    )


@router.post("/api/ingest/push-batches", response_model=PushBatchAccepted)
def ingest_push_batch(request: PushBatchIn) -> Any:
    try:
        batch, items, created = ingest_adapter.create_batch(request)
    except ingest_adapter.IngestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    payload = _accepted(batch, items, created=created)
    return JSONResponse(
        status_code=202 if created else 200, content=payload.model_dump()
    )


@router.post("/api/ingest/push-batches/dry-run", response_model=PushBatchAccepted)
def dry_run_push_batch(request: PushBatchIn) -> Any:
    settings = push_settings.get_settings()
    now = datetime.now(timezone.utc)
    try:
        ingest_adapter.validate_payload(
            request, settings=settings, devices=ingest_adapter.load_devices()
        )
        items, _start, _end = ingest_adapter.build_plan(
            request, settings=settings, now=now
        )
    except ingest_adapter.IngestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return PushBatchAccepted(
        batch_id="dry-run",
        external_batch_id=request.external_batch_id,
        status="dry_run",
        item_count=len(items),
        items=[_item_plan(item, index) for index, item in enumerate(items)],
        created=False,
    )


@router.get("/api/ingest/push-batches/{external_batch_id}", response_model=PushBatchOut)
def get_push_batch(external_batch_id: str) -> PushBatchOut:
    batch = push_store.get_batch_by_external_id(external_batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _batch_out(batch, push_store.list_items(str(batch["id"])))


@router.get("/api/ingest/devices", response_model=list[PushIngestDevice])
def list_ingest_devices() -> list[PushIngestDevice]:
    devices = ingest_adapter.load_devices()
    result: list[PushIngestDevice] = []
    for serial in sorted(devices):
        device = devices[serial]
        payload = device.to_dict() if hasattr(device, "to_dict") else {}
        result.append(
            PushIngestDevice(
                serial=str(payload.get("serial") or serial),
                name=payload.get("display_name"),
                primary_device_id=payload.get("id"),
                online=str(payload.get("state")) == "online",
                connection_type=payload.get("connection_type"),
            )
        )
    return result
