"""Background dispatcher for push items and webhook deliveries."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.push.prompts import build_group_message_prompt
from AutoGLM_GUI.push.schedule import to_utc_iso
from AutoGLM_GUI.push.settings import push_settings
from AutoGLM_GUI.push.store import (
    ACTIVE_ITEM_STATUSES,
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_FAILED,
    BATCH_PARTIAL,
    BATCH_PENDING,
    BATCH_RUNNING,
    ITEM_CANCELLED,
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_QUEUED,
    ITEM_RUNNING,
    ITEM_SUCCEEDED,
    PushStore,
    push_store,
)
from AutoGLM_GUI.push.webhook import deliver_webhook, next_retry_at

_TERMINAL_TASK_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


def _get_device_manager() -> Any:
    from AutoGLM_GUI.device_manager import DeviceManager

    return DeviceManager.get_instance()


def _get_task_manager() -> Any:
    from AutoGLM_GUI.task_manager import task_manager

    return task_manager


def _get_task_store() -> Any:
    from AutoGLM_GUI.task_store import task_store

    return task_store


class PushDispatcher:
    """Periodically dispatches due items and delivers webhooks."""

    def __init__(self, store: PushStore | None = None, interval_seconds: float = 2.0):
        self._store = store or push_store
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        try:
            self._store.reset_in_flight_items()
            self._store.reset_sending_webhooks()
            self._store.prune(older_than_days=30)
        except Exception:
            logger.exception("Failed to reset push state on start")
        self._task = asyncio.create_task(self._run(), name="PushDispatcher")
        logger.info("Push dispatcher started")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Push dispatcher stopped with error")
        logger.info("Push dispatcher stopped")

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Push dispatcher tick failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise

    async def tick(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        await self._dispatch_due(current)
        await self._reconcile_items()
        await self._finalize_batches()
        await self._deliver_webhooks(current)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def _dispatch_due(self, now: datetime) -> None:
        due = await asyncio.to_thread(self._store.claim_due_items, to_utc_iso(now))
        for item in due:
            try:
                await self._dispatch_item(item, now)
            except Exception:
                logger.exception("Failed to dispatch push item %s", item.get("id"))
                await asyncio.to_thread(
                    self._mark_item_terminal,
                    item,
                    ITEM_FAILED,
                    "dispatcher error",
                )

    async def _dispatch_item(self, item: dict[str, Any], now: datetime) -> None:
        batch = await asyncio.to_thread(self._store.get_batch, str(item["batch_id"]))
        if batch is None:
            await asyncio.to_thread(
                self._store.update_item, item["id"], status=ITEM_FAILED
            )
            return
        if batch["status"] == BATCH_CANCELLED:
            await asyncio.to_thread(
                self._store.update_item,
                item["id"],
                status=ITEM_CANCELLED,
                finished_at=to_utc_iso(now),
            )
            return

        device = await asyncio.to_thread(self._find_device, str(item["device_serial"]))
        if device is None or not self._is_online(device):
            window_end = datetime.fromisoformat(str(item["window_end"]))
            settings = push_settings.get_settings()
            retry_seconds = int(settings.get("offline_retry_seconds", 60))
            if now < window_end:
                next_at = min(now + timedelta(seconds=retry_seconds), window_end)
                await asyncio.to_thread(
                    self._store.update_item,
                    item["id"],
                    status=ITEM_PENDING,
                    scheduled_at=to_utc_iso(next_at),
                    last_error="device offline; sent window still open, rescheduled",
                )
                logger.info(
                    "Push item %s waiting for offline device %s until %s",
                    item["id"],
                    item["device_serial"],
                    next_at.isoformat(),
                )
                return
            await asyncio.to_thread(
                self._mark_item_terminal, item, ITEM_FAILED, "device offline"
            )
            return

        prompt = build_group_message_prompt(
            group_name=str(item["group_name"]),
            message=str(item["message"]),
            mention_all=bool(item["mention_all"]),
        )
        # Mark the item queued *before* creating the task. If the process dies
        # between the two steps the item is reconciled as failed instead of
        # being dispatched again, which keeps group messages at-most-once.
        await asyncio.to_thread(
            self._store.update_item,
            item["id"],
            status=ITEM_QUEUED,
            device_id=str(device.primary_device_id),
            attempts=int(item["attempts"]) + 1,
            last_error=None,
        )
        task_manager = _get_task_manager()
        task = await task_manager.enqueue_push_task(
            device_id=str(device.primary_device_id),
            device_serial=str(device.serial),
            input_text=prompt,
            group_name=str(item["group_name"]),
            message=str(item["message"]),
            mention_all=bool(item["mention_all"]),
        )
        await asyncio.to_thread(
            self._store.update_item, item["id"], task_id=str(task["id"])
        )
        await asyncio.to_thread(
            self._store.update_batch_status, item["batch_id"], BATCH_RUNNING
        )
        logger.info(
            "Push item %s queued as task %s on %s",
            item["id"],
            task["id"],
            device.serial,
        )

    @staticmethod
    def _find_device(serial: str) -> Any:
        for device in _get_device_manager().get_devices():
            if device.serial == serial:
                return device
        return None

    @staticmethod
    def _is_online(device: Any) -> bool:
        state = getattr(device, "state", None)
        value = getattr(state, "value", state)
        return value == "online"

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    async def _reconcile_items(self) -> None:
        items = await asyncio.to_thread(
            self._store.list_items_by_status, (ITEM_QUEUED, ITEM_RUNNING)
        )
        if not items:
            return
        task_store = _get_task_store()
        for item in items:
            task_id = item.get("task_id")
            if not task_id:
                await asyncio.to_thread(
                    self._mark_item_terminal, item, ITEM_FAILED, "task id missing"
                )
                continue
            task = await asyncio.to_thread(task_store.get_task, str(task_id))
            if task is None:
                await asyncio.to_thread(
                    self._mark_item_terminal,
                    item,
                    ITEM_FAILED,
                    "task record missing",
                )
                continue
            status = str(task["status"])
            if status == "RUNNING" and item["status"] != ITEM_RUNNING:
                await asyncio.to_thread(
                    self._store.update_item, item["id"], status=ITEM_RUNNING
                )
                continue
            if status not in _TERMINAL_TASK_STATUSES:
                continue
            if status == "SUCCEEDED":
                await asyncio.to_thread(
                    self._mark_item_terminal, item, ITEM_SUCCEEDED, None
                )
            elif status == "CANCELLED":
                await asyncio.to_thread(
                    self._mark_item_terminal, item, ITEM_CANCELLED, "task cancelled"
                )
            else:
                error = (
                    task.get("error_message")
                    or task.get("final_message")
                    or f"task {status.lower()}"
                )
                await asyncio.to_thread(
                    self._mark_item_terminal, item, ITEM_FAILED, str(error)
                )

    # ------------------------------------------------------------------
    # Batch finalisation
    # ------------------------------------------------------------------
    async def _finalize_batches(self) -> None:
        batches = await asyncio.to_thread(
            self._store.list_batches_by_status, (BATCH_PENDING, BATCH_RUNNING)
        )
        for batch in batches:
            batch_id = str(batch["id"])
            counts = await asyncio.to_thread(self._store.batch_counts, batch_id)
            if not counts:
                continue
            if any(counts.get(status, 0) for status in ACTIVE_ITEM_STATUSES):
                continue

            succeeded = counts.get(ITEM_SUCCEEDED, 0)
            failed = counts.get(ITEM_FAILED, 0)
            cancelled = counts.get(ITEM_CANCELLED, 0)
            if failed and succeeded:
                final_status = BATCH_PARTIAL
            elif failed:
                final_status = BATCH_FAILED
            elif cancelled and not succeeded:
                final_status = BATCH_CANCELLED
            else:
                final_status = BATCH_COMPLETED

            now_iso = to_utc_iso(datetime.now(timezone.utc))
            updated = await asyncio.to_thread(
                self._store.update_batch_status,
                batch_id,
                final_status,
                finished_at=now_iso,
            )
            if updated is None:
                continue
            await self._enqueue_batch_webhook(
                updated,
                {
                    "success_count": succeeded,
                    "failure_count": failed,
                    "cancelled_count": cancelled,
                    "total_count": sum(counts.values()),
                },
            )

    # ------------------------------------------------------------------
    # Terminal bookkeeping + webhooks
    # ------------------------------------------------------------------
    def _mark_item_terminal(
        self, item: dict[str, Any], status: str, error: str | None
    ) -> None:
        now_iso = to_utc_iso(datetime.now(timezone.utc))
        updated = self._store.update_item(
            item["id"],
            status=status,
            last_error=error,
            finished_at=now_iso,
        )
        if updated is None:
            return
        batch = self._store.get_batch(str(item["batch_id"]))
        if batch is None or not batch.get("callback_url"):
            return
        payload = {
            "event": "push.item.completed",
            "batch_id": str(item["batch_id"]),
            "external_batch_id": str(batch.get("external_batch_id", "")),
            "item_id": str(item["id"]),
            "external_item_id": str(item["external_item_id"]),
            "device_serial": str(item["device_serial"]),
            "group_name": str(item["group_name"]),
            "status": status,
            "error": error,
            "task_id": item.get("task_id"),
            "finished_at": now_iso,
        }
        self._store.enqueue_webhook(
            batch_id=str(item["batch_id"]),
            item_id=str(item["id"]),
            event="push.item.completed",
            url=str(batch["callback_url"]),
            payload=payload,
        )

    async def _enqueue_batch_webhook(
        self, batch: dict[str, Any], counts: dict[str, int]
    ) -> None:
        url = batch.get("callback_url")
        if not url:
            return
        payload = {
            "event": "push.batch.completed",
            "batch_id": str(batch["id"]),
            "external_batch_id": str(batch.get("external_batch_id", "")),
            "status": str(batch.get("status")),
            "success_count": counts["success_count"],
            "failure_count": counts["failure_count"],
            "cancelled_count": counts["cancelled_count"],
            "total_count": counts["total_count"],
            "finished_at": batch.get("finished_at"),
        }
        await asyncio.to_thread(
            self._store.enqueue_webhook,
            batch_id=str(batch["id"]),
            item_id=None,
            event="push.batch.completed",
            url=str(url),
            payload=payload,
        )

    async def _deliver_webhooks(self, now: datetime) -> None:
        deliveries = await asyncio.to_thread(
            self._store.claim_due_webhooks, to_utc_iso(now)
        )
        for delivery in deliveries:
            ok, error = await deliver_webhook(delivery)
            if ok:
                await asyncio.to_thread(self._store.mark_webhook_sent, delivery["id"])
                continue
            attempts = int(delivery["attempts"]) + 1
            retry_at = next_retry_at(int(delivery["attempts"]), now=now)
            if retry_at is None:
                await asyncio.to_thread(
                    self._store.mark_webhook_failed,
                    delivery["id"],
                    error=error or "delivery failed",
                    attempts=attempts,
                )
                continue
            await asyncio.to_thread(
                self._store.reschedule_webhook,
                delivery["id"],
                next_attempt_at=to_utc_iso(retry_at),
                error=error or "delivery failed",
                attempts=attempts,
            )


push_dispatcher = PushDispatcher()
