"""GUI-facing push batch endpoints (behind the reverse proxy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from AutoGLM_GUI.push.schemas import (
    PushBatchListResponse,
    PushBatchOut,
    PushItemOut,
    PushSettingsModel,
    PushSettingsUpdate,
    PushWebhookOut,
)
from AutoGLM_GUI.push.settings import push_settings
from AutoGLM_GUI.push.store import (
    ACTIVE_ITEM_STATUSES,
    BATCH_CANCELLED,
    ITEM_CANCELLED,
    ITEM_DISPATCHING,
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_QUEUED,
    ITEM_RUNNING,
    ITEM_SUCCEEDED,
    push_store,
)

router = APIRouter()


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


def _batch_out(
    batch: dict[str, Any],
    *,
    items: list[dict[str, Any]] | None = None,
    counts: dict[str, int] | None = None,
) -> PushBatchOut:
    counts = counts or {}
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
        item_count=(
            len(items)
            if items is not None
            else int(batch.get("item_count") or sum(counts.values()))
        ),
        success_count=int(batch.get("success_count") or counts.get(ITEM_SUCCEEDED, 0)),
        failure_count=int(batch.get("failure_count") or counts.get(ITEM_FAILED, 0)),
        items=[_item_out(item) for item in items] if items is not None else None,
    )


@router.get("/api/push-batches", response_model=PushBatchListResponse)
def list_push_batches(
    status: str | None = None,
    device_serial: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> PushBatchListResponse:
    rows, total = push_store.list_batches(
        status=status,
        device_serial=device_serial,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )
    return PushBatchListResponse(batches=[_batch_out(row) for row in rows], total=total)


@router.get("/api/push-batches/{batch_id}", response_model=PushBatchOut)
def get_push_batch(batch_id: str) -> PushBatchOut:
    batch = push_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _batch_out(batch, items=push_store.list_items(batch_id))


@router.get(
    "/api/push-batches/{batch_id}/webhooks", response_model=list[PushWebhookOut]
)
def list_push_webhooks(batch_id: str) -> list[PushWebhookOut]:
    batch = push_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return [
        PushWebhookOut(
            id=str(row["id"]),
            event=str(row["event"]),
            url=str(row["url"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            next_attempt_at=str(row["next_attempt_at"]),
            last_error=row.get("last_error"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
        for row in push_store.list_webhooks(batch_id)
    ]


@router.post(
    "/api/push-batches/{batch_id}/items/{item_id}/retrigger",
    response_model=PushItemOut,
)
def retrigger_push_item(batch_id: str, item_id: str) -> PushItemOut:
    batch = push_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    item = push_store.get_item(item_id)
    if item is None or str(item["batch_id"]) != batch_id:
        raise HTTPException(status_code=404, detail="Item not found")
    if str(item["status"]) in ACTIVE_ITEM_STATUSES:
        raise HTTPException(status_code=409, detail="Item is still active")

    settings = push_settings.get_settings()
    now = datetime.now(timezone.utc)
    grace = max(300, int(settings.get("offline_retry_seconds", 60)) * 5)
    updated = push_store.reset_item_for_retrigger(
        item_id,
        now.isoformat(),
        (now + timedelta(seconds=grace)).isoformat(),
    )
    push_store.reopen_batch(batch_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return _item_out(updated)


@router.post("/api/push-batches/{batch_id}/cancel")
async def cancel_push_batch(batch_id: str) -> dict[str, Any]:
    batch = push_store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    from AutoGLM_GUI.task_manager import task_manager

    now_iso = datetime.now(timezone.utc).isoformat()
    cancelled = 0
    for item in push_store.list_items(batch_id):
        status = str(item["status"])
        if status in (ITEM_PENDING, ITEM_DISPATCHING):
            push_store.update_item(
                str(item["id"]),
                status=ITEM_CANCELLED,
                finished_at=now_iso,
                last_error="cancelled by operator",
            )
            cancelled += 1
        elif status in (ITEM_QUEUED, ITEM_RUNNING) and item.get("task_id"):
            await task_manager.cancel_task(str(item["task_id"]))
            cancelled += 1

    counts = push_store.batch_counts(batch_id)
    if not any(counts.get(status, 0) for status in ACTIVE_ITEM_STATUSES):
        push_store.update_batch_status(batch_id, BATCH_CANCELLED, finished_at=now_iso)

    return {
        "success": True,
        "cancelled": cancelled,
        "message": f"Cancellation requested for {cancelled} item(s)",
    }


@router.get("/api/push-settings", response_model=PushSettingsModel)
def get_push_settings() -> PushSettingsModel:
    return PushSettingsModel(**push_settings.get_settings())


@router.put("/api/push-settings", response_model=PushSettingsModel)
def update_push_settings(request: PushSettingsUpdate) -> PushSettingsModel:
    try:
        merged = push_settings.update_settings(request.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PushSettingsModel(**merged)
