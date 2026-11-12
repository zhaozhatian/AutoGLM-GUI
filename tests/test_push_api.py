"""Contract tests for push ingest and GUI endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import AutoGLM_GUI.api.push_gui as push_gui_api
import AutoGLM_GUI.api.push_ingest as push_ingest_api
import AutoGLM_GUI.push.ingest_adapter as adapter
from AutoGLM_GUI.push.settings import PushSettingsManager
from AutoGLM_GUI.push.store import ITEM_FAILED, PushStore


class FakeDevice:
    def __init__(self, serial: str) -> None:
        self.serial = serial

    def to_dict(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "id": f"id-{self.serial}",
            "state": "online",
            "connection_type": "wifi",
            "display_name": None,
        }


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    store = PushStore(tmp_path / "push.db")
    settings = PushSettingsManager(tmp_path / "push_settings.json")

    monkeypatch.setattr(push_ingest_api, "push_store", store)
    monkeypatch.setattr(push_gui_api, "push_store", store)
    monkeypatch.setattr(adapter, "push_store", store)
    monkeypatch.setattr(push_ingest_api, "push_settings", settings)
    monkeypatch.setattr(push_gui_api, "push_settings", settings)
    monkeypatch.setattr(adapter, "push_settings", settings)
    monkeypatch.setattr(
        adapter,
        "load_devices",
        lambda: {
            "serial-1": FakeDevice("serial-1"),
            "serial-2": FakeDevice("serial-2"),
        },
    )
    monkeypatch.setenv("AUTOGLM_INGEST_TOKEN", "secret")

    app = FastAPI()
    app.include_router(push_ingest_api.router)
    app.include_router(push_gui_api.router)
    return SimpleNamespace(
        client=TestClient(app), store=store, settings=settings, monkeypatch=monkeypatch
    )


def _payload(
    *,
    batch_id: str = "ext-1",
    serials: tuple[str, ...] = ("serial-1",),
    message: str = "第一行\n第二行",
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    items = []
    for index, serial in enumerate(serials):
        items.append(
            {
                "external_item_id": f"item-{index}",
                "device_serial": serial,
                "group_name": f"group-{index}",
                "message": message,
            }
        )
    return {
        "external_batch_id": batch_id,
        "window_start": (now + timedelta(hours=1)).isoformat(),
        "window_end": (now + timedelta(hours=2)).isoformat(),
        "items": items,
    }


HEADERS = {"X-API-Key": "secret"}


def test_ingest_disabled_without_token(env: SimpleNamespace) -> None:
    env.monkeypatch.delenv("AUTOGLM_INGEST_TOKEN")
    response = env.client.post(
        "/api/ingest/push-batches", json=_payload(), headers=HEADERS
    )
    assert response.status_code == 503


def test_ingest_requires_valid_token(env: SimpleNamespace) -> None:
    response = env.client.post(
        "/api/ingest/push-batches",
        json=_payload(),
        headers={"X-API-Key": "wrong"},
    )
    assert response.status_code == 401

    response = env.client.post("/api/ingest/push-batches", json=_payload())
    assert response.status_code == 401


def test_ingest_creates_batch_and_is_idempotent(env: SimpleNamespace) -> None:
    payload = _payload(serials=("serial-1", "serial-2"))
    first = env.client.post("/api/ingest/push-batches", json=payload, headers=HEADERS)
    assert first.status_code == 202
    body = first.json()
    assert body["created"] is True
    assert body["item_count"] == 2
    assert all(item["scheduled_at"] for item in body["items"])

    second = env.client.post("/api/ingest/push-batches", json=payload, headers=HEADERS)
    assert second.status_code == 200
    assert second.json()["batch_id"] == body["batch_id"]
    assert second.json()["created"] is False

    conflict = env.client.post(
        "/api/ingest/push-batches",
        json=_payload(serials=("serial-1", "serial-2"), message="changed"),
        headers=HEADERS,
    )
    assert conflict.status_code == 409


def test_ingest_rejects_unknown_device_and_whitelist(env: SimpleNamespace) -> None:
    unknown = env.client.post(
        "/api/ingest/push-batches",
        json=_payload(serials=("missing",)),
        headers=HEADERS,
    )
    assert unknown.status_code == 400

    env.settings.update_settings(
        {"device_rules": {"serial-1": {"allowed_groups": ["allowed-only"]}}}
    )
    blocked = env.client.post(
        "/api/ingest/push-batches", json=_payload(), headers=HEADERS
    )
    assert blocked.status_code == 400
    assert "not allowed" in blocked.json()["detail"]


def test_ingest_dry_run_does_not_persist(env: SimpleNamespace) -> None:
    response = env.client.post(
        "/api/ingest/push-batches/dry-run", json=_payload(), headers=HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dry_run"
    assert body["item_count"] == 1
    assert env.store.list_batches()[1] == 0


def test_ingest_query_by_external_id(env: SimpleNamespace) -> None:
    created = env.client.post(
        "/api/ingest/push-batches", json=_payload(), headers=HEADERS
    ).json()
    response = env.client.get("/api/ingest/push-batches/ext-1", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["batch_id"]
    assert len(body["items"]) == 1
    assert body["items"][0]["message"] == "第一行\n第二行"

    missing = env.client.get("/api/ingest/push-batches/nope", headers=HEADERS)
    assert missing.status_code == 404


def test_gui_list_detail_retrigger_and_cancel(env: SimpleNamespace) -> None:
    created = env.client.post(
        "/api/ingest/push-batches", json=_payload(), headers=HEADERS
    ).json()
    batch_id = created["batch_id"]
    item_id = created["items"][0]["item_id"]

    listing = env.client.get("/api/push-batches")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["batches"][0]["item_count"] == 1

    detail = env.client.get(f"/api/push-batches/{batch_id}")
    assert detail.status_code == 200
    assert detail.json()["items"][0]["status"] == "pending"

    # Mark the item failed so it becomes retriggerable.
    env.store.update_item(item_id, status=ITEM_FAILED, last_error="boom")
    retrigger = env.client.post(
        f"/api/push-batches/{batch_id}/items/{item_id}/retrigger"
    )
    assert retrigger.status_code == 200
    assert retrigger.json()["status"] == "pending"
    assert retrigger.json()["last_error"] is None

    cancel = env.client.post(f"/api/push-batches/{batch_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] == 1
    detail = env.client.get(f"/api/push-batches/{batch_id}").json()
    assert detail["status"] == "cancelled"
    assert detail["items"][0]["status"] == "cancelled"


def test_retrigger_rejects_active_item(env: SimpleNamespace) -> None:
    created = env.client.post(
        "/api/ingest/push-batches", json=_payload(), headers=HEADERS
    ).json()
    response = env.client.post(
        f"/api/push-batches/{created['batch_id']}/items/"
        f"{created['items'][0]['item_id']}/retrigger"
    )
    assert response.status_code == 409


def test_push_settings_roundtrip(env: SimpleNamespace) -> None:
    response = env.client.put(
        "/api/push-settings",
        json={
            "default_window_start": "10:00",
            "default_window_end": "12:00",
            "min_gap_seconds": 30,
            "device_rules": {"serial-1": {"enabled": True, "allowed_groups": ["g1"]}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["default_window_start"] == "10:00"
    assert body["device_rules"]["serial-1"]["allowed_groups"] == ["g1"]

    fetched = env.client.get("/api/push-settings")
    assert fetched.json()["min_gap_seconds"] == 30

    invalid = env.client.put(
        "/api/push-settings", json={"default_window_start": "25:00"}
    )
    assert invalid.status_code == 400


def test_ingest_devices(env: SimpleNamespace) -> None:
    response = env.client.get("/api/ingest/devices", headers=HEADERS)
    assert response.status_code == 200
    serials = {device["serial"] for device in response.json()}
    assert serials == {"serial-1", "serial-2"}
