"""Unit tests for the push dispatcher state machine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import AutoGLM_GUI.push.dispatcher as dispatcher_module
from AutoGLM_GUI.push.dispatcher import PushDispatcher
from AutoGLM_GUI.push.store import (
    BATCH_COMPLETED,
    BATCH_RUNNING,
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_QUEUED,
    ITEM_SUCCEEDED,
    PushStore,
)


class FakeDeviceManager:
    def __init__(self, devices: list[SimpleNamespace]) -> None:
        self.devices = devices

    def get_devices(self) -> list[SimpleNamespace]:
        return self.devices


class FakeTaskManager:
    def __init__(self, task_store: FakeTaskStore) -> None:
        self.calls: list[dict[str, object]] = []
        self._task_store = task_store

    async def enqueue_push_task(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        task_id = f"task-{len(self.calls)}"
        self._task_store.tasks[task_id] = {"status": "QUEUED"}
        return {"id": task_id}


class FakeTaskStore:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, object]] = {}

    def get_task(self, task_id: str) -> dict[str, object] | None:
        return self.tasks.get(task_id)


class FakeSettings:
    def get_settings(self) -> dict[str, object]:
        return {"offline_retry_seconds": 60}


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    store = PushStore(tmp_path / "push.db")
    task_store = FakeTaskStore()
    task_manager = FakeTaskManager(task_store)
    devices: list[SimpleNamespace] = []

    monkeypatch.setattr(
        dispatcher_module, "_get_device_manager", lambda: FakeDeviceManager(devices)
    )
    monkeypatch.setattr(dispatcher_module, "_get_task_manager", lambda: task_manager)
    monkeypatch.setattr(dispatcher_module, "_get_task_store", lambda: task_store)
    monkeypatch.setattr(dispatcher_module, "push_settings", FakeSettings())

    async def _accept_delivery(_delivery: dict) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(dispatcher_module, "deliver_webhook", _accept_delivery)

    dispatcher = PushDispatcher(store=store, interval_seconds=0)
    return SimpleNamespace(
        store=store,
        task_manager=task_manager,
        task_store=task_store,
        devices=devices,
        dispatcher=dispatcher,
    )


def _online(serial: str = "serial-1") -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial,
        primary_device_id=f"id-{serial}",
        state=SimpleNamespace(value="online"),
    )


def _offline(serial: str = "serial-1") -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial,
        primary_device_id=f"id-{serial}",
        state=SimpleNamespace(value="offline"),
    )


def _seed(
    store: PushStore,
    *,
    callback_url: str | None = None,
    window_end: datetime | None = None,
) -> tuple[dict, dict]:
    now = datetime.now(timezone.utc)
    batch = store.create_batch(
        external_batch_id="ext-1",
        payload_hash="hash",
        callback_url=callback_url,
        window_start=(now - timedelta(minutes=1)).isoformat(),
        window_end=(window_end or now + timedelta(hours=1)).isoformat(),
        raw_payload={},
    )
    item = store.create_items(
        batch["id"],
        [
            {
                "external_item_id": "item-1",
                "device_serial": "serial-1",
                "group_name": "group-1",
                "message": "第一行\n第二行",
                "mention_all": True,
                "scheduled_at": (now - timedelta(seconds=1)).isoformat(),
                "window_end": (window_end or now + timedelta(hours=1)).isoformat(),
            }
        ],
    )[0]
    return batch, item


def test_offline_device_is_rescheduled_inside_window(env: SimpleNamespace) -> None:
    _batch, item = _seed(env.store)
    env.devices.append(_offline())

    now = datetime.now(timezone.utc)
    asyncio.run(env.dispatcher.tick(now=now))

    updated = env.store.get_item(item["id"])
    assert updated is not None
    assert updated["status"] == ITEM_PENDING
    assert datetime.fromisoformat(updated["scheduled_at"]) > now
    assert env.task_manager.calls == []


def test_offline_device_fails_after_window_and_emits_webhook(
    env: SimpleNamespace,
) -> None:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=5)
    batch, item = _seed(
        env.store, callback_url="https://example.com/hook", window_end=window_end
    )
    env.devices.append(_offline())

    asyncio.run(env.dispatcher.tick(now=now))
    asyncio.run(env.dispatcher.tick(now=window_end + timedelta(seconds=1)))

    updated = env.store.get_item(item["id"])
    assert updated is not None
    assert updated["status"] == ITEM_FAILED
    assert updated["last_error"] == "device offline"

    webhooks = env.store.list_webhooks(batch["id"])
    item_events = [row for row in webhooks if row["event"] == "push.item.completed"]
    assert len(item_events) == 1
    assert '"status": "failed"' in item_events[0]["payload_json"]


def test_online_device_is_queued_and_completed(env: SimpleNamespace) -> None:
    batch, item = _seed(env.store, callback_url="https://example.com/hook")
    env.devices.append(_online())

    now = datetime.now(timezone.utc)
    asyncio.run(env.dispatcher.tick(now=now))

    assert len(env.task_manager.calls) == 1
    call = env.task_manager.calls[0]
    assert call["device_id"] == "id-serial-1"
    assert call["device_serial"] == "serial-1"
    assert call["group_name"] == "group-1"
    assert call["message"] == "第一行\n第二行"
    assert call["mention_all"] is True
    assert "group-1" in str(call["input_text"])
    assert "第一行\n第二行" in str(call["input_text"])

    queued = env.store.get_item(item["id"])
    assert queued is not None
    assert queued["status"] == ITEM_QUEUED
    assert queued["task_id"] == "task-1"
    assert env.store.get_batch(batch["id"])["status"] == BATCH_RUNNING  # type: ignore[index]

    env.task_store.tasks["task-1"] = {"status": "SUCCEEDED", "final_message": "done"}
    asyncio.run(env.dispatcher.tick(now=now + timedelta(seconds=10)))

    completed = env.store.get_item(item["id"])
    assert completed is not None
    assert completed["status"] == ITEM_SUCCEEDED
    assert env.store.get_batch(batch["id"])["status"] == BATCH_COMPLETED  # type: ignore[index]

    events = [row["event"] for row in env.store.list_webhooks(batch["id"])]
    assert events == ["push.item.completed", "push.batch.completed"]


def test_failed_task_maps_error_to_item(env: SimpleNamespace) -> None:
    _batch, item = _seed(env.store)
    env.devices.append(_online())

    now = datetime.now(timezone.utc)
    asyncio.run(env.dispatcher.tick(now=now))
    env.task_store.tasks["task-1"] = {
        "status": "FAILED",
        "error_message": "search failed",
    }
    asyncio.run(env.dispatcher.tick(now=now + timedelta(seconds=10)))

    updated = env.store.get_item(item["id"])
    assert updated is not None
    assert updated["status"] == ITEM_FAILED
    assert updated["last_error"] == "search failed"


def test_webhook_delivery_retries_then_succeeds(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = env.store.create_batch(
        external_batch_id="ext-2",
        payload_hash="hash",
        callback_url="https://example.com/hook",
        window_start=datetime.now(timezone.utc).isoformat(),
        window_end=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        raw_payload={},
    )
    delivery = env.store.enqueue_webhook(
        batch_id=batch["id"],
        item_id=None,
        event="push.batch.completed",
        url="https://example.com/hook",
        payload={"ok": False},
        next_attempt_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    async def fail_delivery(_delivery: dict) -> tuple[bool, str | None]:
        return False, "HTTP 500"

    monkeypatch.setattr(dispatcher_module, "deliver_webhook", fail_delivery)
    asyncio.run(env.dispatcher.tick(now=datetime.now(timezone.utc)))
    pending = env.store.list_webhooks(batch["id"])[0]
    assert pending["status"] == "pending"
    assert pending["attempts"] == 1

    async def ok_delivery(_delivery: dict) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(dispatcher_module, "deliver_webhook", ok_delivery)
    asyncio.run(
        env.dispatcher.tick(now=datetime.now(timezone.utc) + timedelta(minutes=10))
    )
    assert env.store.list_webhooks(batch["id"])[0]["status"] == "sent"
    assert env.store.list_webhooks(batch["id"])[0]["id"] == delivery["id"]
