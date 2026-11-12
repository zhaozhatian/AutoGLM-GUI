"""Unit tests for push persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from AutoGLM_GUI.push.store import (
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_QUEUED,
    ITEM_SUCCEEDED,
    PushStore,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item(serial: str = "serial-1", group: str = "group-1") -> dict[str, object]:
    return {
        "external_item_id": f"{serial}-{group}",
        "device_serial": serial,
        "group_name": group,
        "message": "hello\nworld",
        "mention_all": True,
        "scheduled_at": (_now() - timedelta(seconds=5)).isoformat(),
        "window_end": (_now() + timedelta(hours=1)).isoformat(),
    }


def _create_batch(store: PushStore, *, callback_url: str | None = None) -> dict:
    now = _now()
    return store.create_batch(
        external_batch_id="ext-1",
        payload_hash="hash-1",
        callback_url=callback_url,
        window_start=(now - timedelta(minutes=1)).isoformat(),
        window_end=(now + timedelta(hours=1)).isoformat(),
        raw_payload={"external_batch_id": "ext-1"},
    )


def test_batch_item_lifecycle_and_claim(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.db")
    batch = _create_batch(store)
    items = store.create_items(batch["id"], [_item(), _item(group="group-2")])

    assert len(items) == 2
    assert store.batch_counts(batch["id"]) == {ITEM_PENDING: 2}

    claimed = store.claim_due_items(_now().isoformat())
    assert {row["id"] for row in claimed} == {item["id"] for item in items}

    # Claimed rows are now dispatching and cannot be claimed twice.
    assert store.claim_due_items(_now().isoformat()) == []

    store.update_item(items[0]["id"], status=ITEM_QUEUED, task_id="task-1")
    updated = store.get_item(items[0]["id"])
    assert updated is not None
    assert updated["status"] == ITEM_QUEUED
    assert updated["task_id"] == "task-1"

    store.update_item(
        items[0]["id"], status=ITEM_SUCCEEDED, finished_at=_now().isoformat()
    )
    store.update_item(items[1]["id"], status=ITEM_FAILED, last_error="boom")
    counts = store.batch_counts(batch["id"])
    assert counts[ITEM_SUCCEEDED] == 1
    assert counts[ITEM_FAILED] == 1


def test_external_batch_id_is_unique(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.db")
    _create_batch(store)
    with pytest.raises(Exception):
        _create_batch(store)

    assert store.get_batch_by_external_id("ext-1") is not None


def test_list_batches_filters_and_counts(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.db")
    batch = _create_batch(store)
    store.create_items(
        batch["id"], [_item(serial="serial-1"), _item(serial="serial-2")]
    )
    store.update_item(store.list_items(batch["id"])[0]["id"], status=ITEM_SUCCEEDED)

    rows, total = store.list_batches()
    assert total == 1
    assert rows[0]["item_count"] == 2
    assert rows[0]["success_count"] == 1

    rows, total = store.list_batches(device_serial="serial-2")
    assert total == 1
    rows, total = store.list_batches(device_serial="missing")
    assert total == 0


def test_reset_in_flight_items_and_webhooks(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.db")
    batch = _create_batch(store)
    item = store.create_items(batch["id"], [_item()])[0]
    store.claim_due_items(_now().isoformat())
    assert store.reset_in_flight_items() == 1
    assert store.get_item(item["id"])["status"] == ITEM_PENDING  # type: ignore[index]

    delivery = store.enqueue_webhook(
        batch_id=batch["id"],
        item_id=item["id"],
        event="push.item.completed",
        url="https://example.com/hook",
        payload={"ok": True},
    )
    claimed = store.claim_due_webhooks((_now() + timedelta(seconds=1)).isoformat())
    assert [row["id"] for row in claimed] == [delivery["id"]]
    store.mark_webhook_sent(delivery["id"])
    assert store.list_webhooks(batch["id"])[0]["status"] == "sent"


def test_reset_item_for_retrigger_clears_state(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.db")
    batch = _create_batch(store)
    item = store.create_items(batch["id"], [_item()])[0]
    store.update_item(
        item["id"], status=ITEM_FAILED, last_error="boom", task_id="task-1", attempts=1
    )

    now = _now()
    updated = store.reset_item_for_retrigger(
        item["id"], now.isoformat(), (now + timedelta(minutes=30)).isoformat()
    )
    assert updated is not None
    assert updated["status"] == ITEM_PENDING
    assert updated["attempts"] == 0
    assert updated["task_id"] is None
    assert updated["last_error"] is None
    assert updated["finished_at"] is None
