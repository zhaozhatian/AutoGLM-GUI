"""Validation, idempotency and planning for inbound push batches.

This module is the only place that knows the external system's payload shape.
If the integration contract changes, adapt here; the dispatcher and store stay
unchanged.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from datetime import datetime, timezone
from typing import Any

from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.push.schedule import (
    ScheduleError,
    plan_batch_schedule,
    to_utc_iso,
)
from AutoGLM_GUI.push.schemas import PushBatchIn
from AutoGLM_GUI.push.settings import push_settings
from AutoGLM_GUI.push.store import PushStore, push_store
from AutoGLM_GUI.push.webhook import validate_callback_url


class IngestError(ValueError):
    """Validation failure that maps directly to an HTTP error."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def payload_hash(payload: PushBatchIn) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _payload_matches_existing(
    store: PushStore,
    batch: dict[str, Any],
    payload: PushBatchIn,
    settings: dict[str, Any],
) -> bool:
    """Compare the actual content, ignoring the send window.

    Replaying the same items with a freshly computed window must be treated as
    an idempotent retry; only changed items/callback are a real conflict.
    """
    existing_items = store.list_items(str(batch["id"]))
    if len(existing_items) != len(payload.items):
        return False

    expected_callback = payload.callback_url or settings.get("default_callback_url")
    if (batch.get("callback_url") or None) != (expected_callback or None):
        return False

    by_id = {str(item["external_item_id"]): item for item in existing_items}
    for item in payload.items:
        existing = by_id.get(item.external_item_id)
        if existing is None:
            return False
        if str(existing["device_serial"]) != item.device_serial:
            return False
        if str(existing["group_name"]) != item.group_name:
            return False
        if str(existing["message"]) != item.message:
            return False
        if bool(existing["mention_all"]) != item.mention_all:
            return False
    return True


def load_devices() -> dict[str, Any]:
    """Return serial -> ManagedDevice for currently known devices."""
    from AutoGLM_GUI.device_manager import DeviceManager

    try:
        manager = DeviceManager.get_instance()
    except Exception:
        return {}
    return {device.serial: device for device in manager.get_devices()}


def _validate_callback(payload: PushBatchIn, settings: dict[str, Any]) -> str | None:
    url = payload.callback_url or settings.get("default_callback_url")
    allowed, reason = validate_callback_url(url)
    if not allowed:
        raise IngestError(reason)
    return url


def validate_payload(
    payload: PushBatchIn,
    *,
    settings: dict[str, Any],
    devices: dict[str, Any] | None,
) -> None:
    if len(payload.items) > int(settings.get("max_items_per_batch", 500)):
        raise IngestError(
            f"batch has {len(payload.items)} items, more than the configured "
            f"limit of {settings.get('max_items_per_batch', 500)}"
        )

    rules = settings.get("device_rules", {})
    if devices is not None:
        for item in payload.items:
            if item.device_serial not in devices:
                raise IngestError(
                    f"unknown device_serial: {item.device_serial}", status_code=400
                )
            rule = rules.get(item.device_serial, {})
            if not rule.get("enabled", True):
                raise IngestError(
                    f"device {item.device_serial} is disabled in push rules"
                )
            allowed = rule.get("allowed_groups", [])
            if allowed and item.group_name.strip() not in allowed:
                raise IngestError(
                    f"group '{item.group_name}' is not allowed for device "
                    f"{item.device_serial}"
                )

    _validate_callback(payload, settings)


def resolve_window(
    payload: PushBatchIn, now: datetime
) -> tuple[datetime | None, datetime | None]:
    if payload.window_start is None or payload.window_end is None:
        return None, None
    if payload.window_end <= now:
        raise IngestError("window_end has already passed")
    return payload.window_start, payload.window_end


def build_plan(
    payload: PushBatchIn,
    *,
    settings: dict[str, Any],
    now: datetime,
    rng: random.Random | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    window_start, window_end = resolve_window(payload, now)
    raw_items = [item.model_dump() for item in payload.items]
    try:
        planned = plan_batch_schedule(
            raw_items,
            window_start=window_start,
            window_end=window_end,
            settings=settings,
            now=now,
            rng=rng,
        )
    except ScheduleError as exc:
        raise IngestError(str(exc)) from exc

    items: list[dict[str, Any]] = []
    for raw, slot in zip(raw_items, planned):
        items.append(
            {
                "external_item_id": raw["external_item_id"],
                "device_serial": raw["device_serial"],
                "group_name": raw["group_name"],
                "message": raw["message"],
                "mention_all": raw["mention_all"],
                "scheduled_at": to_utc_iso(slot["scheduled_at"]),
                "window_end": to_utc_iso(slot["window_end"]),
            }
        )

    batch_start = (
        to_utc_iso(window_start)
        if window_start is not None
        else min(item["scheduled_at"] for item in items)
    )
    batch_end = (
        to_utc_iso(window_end)
        if window_end is not None
        else max(item["window_end"] for item in items)
    )
    return items, batch_start, batch_end


def create_batch(
    payload: PushBatchIn,
    *,
    settings: dict[str, Any] | None = None,
    devices: dict[str, Any] | None = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
    store: PushStore | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Create a batch idempotently.

    Returns (batch, items, created). Existing identical batches return the
    stored rows with ``created=False``; conflicting content raises 409.
    """
    store = store or push_store
    current = now or datetime.now(timezone.utc)
    settings = settings or push_settings.get_settings()
    devices = load_devices() if devices is None else devices

    fingerprint = payload_hash(payload)
    existing = store.get_batch_by_external_id(payload.external_batch_id)
    if existing is not None:
        if _payload_matches_existing(store, existing, payload, settings):
            return existing, store.list_items(existing["id"]), False
        raise IngestError(
            "external_batch_id already exists with different content",
            status_code=409,
        )

    validate_payload(payload, settings=settings, devices=devices)

    planned_items, batch_start, batch_end = build_plan(
        payload, settings=settings, now=current, rng=rng
    )
    callback_url = _validate_callback(payload, settings)

    try:
        batch = store.create_batch(
            external_batch_id=payload.external_batch_id,
            payload_hash=fingerprint,
            callback_url=callback_url,
            window_start=batch_start,
            window_end=batch_end,
            raw_payload=payload.model_dump(mode="json"),
        )
    except sqlite3.IntegrityError:
        # Concurrent replay of the same batch.
        existing = store.get_batch_by_external_id(payload.external_batch_id)
        if existing is not None and _payload_matches_existing(
            store, existing, payload, settings
        ):
            return existing, store.list_items(existing["id"]), False
        raise IngestError(
            "external_batch_id already exists with different content",
            status_code=409,
        ) from None

    items = store.create_items(batch["id"], planned_items)
    logger.info(
        "Accepted push batch %s (%s) with %s item(s)",
        batch["external_batch_id"],
        batch["id"],
        len(items),
    )
    return batch, items, True
