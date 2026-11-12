"""Request/response schemas for the push pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_tz(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return value


class PushItemIn(BaseModel):
    """One device/group message supplied by the internal system."""

    external_item_id: str
    device_serial: str
    group_name: str
    message: str
    mention_all: bool = True
    send_after: datetime | None = None
    send_before: datetime | None = None

    @field_validator("external_item_id", "device_serial", "group_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message cannot be empty")
        if len(value) > 10_000:
            raise ValueError("message is longer than 10000 characters")
        return value

    @field_validator("send_after", "send_before")
    @classmethod
    def _aware(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return value
        return _require_tz(value, info.field_name)

    @model_validator(mode="after")
    def _valid_range(self) -> PushItemIn:
        if (
            self.send_after is not None
            and self.send_before is not None
            and self.send_before <= self.send_after
        ):
            raise ValueError(
                f"item {self.external_item_id}: send_before must be after send_after"
            )
        return self


class PushBatchIn(BaseModel):
    """A push batch from the internal system."""

    external_batch_id: str
    callback_url: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    items: list[PushItemIn] = Field(min_length=1)

    @field_validator("external_batch_id")
    @classmethod
    def _non_empty_batch(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("external_batch_id cannot be empty")
        return text

    @field_validator("window_start", "window_end")
    @classmethod
    def _aware_window(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return value
        return _require_tz(value, info.field_name)

    @model_validator(mode="after")
    def _valid_window(self) -> PushBatchIn:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start and window_end must be provided together")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end - self.window_start < timedelta(seconds=1)
        ):
            raise ValueError("window_end must be after window_start")

        seen: set[str] = set()
        for item in self.items:
            if item.external_item_id in seen:
                raise ValueError(f"duplicate external_item_id: {item.external_item_id}")
            seen.add(item.external_item_id)
        return self


class PushItemPlan(BaseModel):
    item_id: str
    external_item_id: str
    device_serial: str
    group_name: str
    scheduled_at: str
    window_end: str


class PushBatchAccepted(BaseModel):
    batch_id: str
    external_batch_id: str
    status: str
    item_count: int
    items: list[PushItemPlan]
    created: bool = True


class PushItemOut(BaseModel):
    id: str
    external_item_id: str
    device_serial: str
    group_name: str
    message: str
    mention_all: bool
    scheduled_at: str
    window_end: str
    status: str
    attempts: int
    task_id: str | None = None
    device_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None


class PushBatchOut(BaseModel):
    id: str
    external_batch_id: str
    callback_url: str | None = None
    window_start: str
    window_end: str
    status: str
    created_at: str
    updated_at: str
    finished_at: str | None = None
    item_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    items: list[PushItemOut] | None = None


class PushBatchListResponse(BaseModel):
    batches: list[PushBatchOut]
    total: int


class PushWebhookOut(BaseModel):
    id: str
    event: str
    url: str
    status: str
    attempts: int
    next_attempt_at: str
    last_error: str | None = None
    created_at: str
    updated_at: str


class DevicePushRule(BaseModel):
    enabled: bool = True
    allowed_groups: list[str] = Field(default_factory=list)
    window_start: str | None = None
    window_end: str | None = None
    min_gap_seconds: int | None = None


class PushSettingsModel(BaseModel):
    default_window_start: str = "09:00"
    default_window_end: str = "11:00"
    min_gap_seconds: int = 90
    offline_retry_seconds: int = 60
    max_items_per_batch: int = 500
    default_callback_url: str | None = None
    device_rules: dict[str, DevicePushRule] = Field(default_factory=dict)


class PushSettingsUpdate(BaseModel):
    default_window_start: str | None = None
    default_window_end: str | None = None
    min_gap_seconds: int | None = None
    offline_retry_seconds: int | None = None
    max_items_per_batch: int | None = None
    default_callback_url: str | None = None
    device_rules: dict[str, DevicePushRule] | None = None


class PushIngestDevice(BaseModel):
    serial: str
    name: str | None = None
    primary_device_id: str | None = None
    online: bool
    connection_type: str | None = None
