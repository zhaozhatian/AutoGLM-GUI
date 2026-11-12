"""Unit tests for push scheduling windows."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from AutoGLM_GUI.push.schedule import (
    ScheduleError,
    plan_batch_schedule,
    spread_times,
)


@pytest.fixture(autouse=True)
def _timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOGLM_PUSH_TIMEZONE", "Asia/Shanghai")


def _settings(**overrides: object) -> dict:
    settings = {
        "default_window_start": "09:00",
        "default_window_end": "11:00",
        "min_gap_seconds": 90,
        "device_rules": {},
    }
    settings.update(overrides)
    return settings


def test_spread_times_respects_bounds_and_min_gap() -> None:
    start = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    times = spread_times(start, end, 5, 60, rng=random.Random(7))

    assert len(times) == 5
    assert times == sorted(times)
    assert times[0] >= start
    assert times[-1] <= end
    assert times[-1] < end  # the last item must not be pinned to the window end
    for earlier, later in zip(times, times[1:]):
        assert (later - earlier).total_seconds() >= 60 - 1e-6


def test_spread_times_falls_back_to_even_spacing_when_dense() -> None:
    start = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=100)
    times = spread_times(start, end, 5, 60, rng=random.Random(1))

    assert [round((t - start).total_seconds()) for t in times] == [0, 20, 40, 60, 80]


def test_plan_groups_by_device_and_keeps_gap() -> None:
    now = datetime(2026, 11, 12, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    window_start = now + timedelta(hours=1)
    window_end = now + timedelta(hours=3)
    items = [
        {"device_serial": "serial-1", "group_name": "g1"},
        {"device_serial": "serial-1", "group_name": "g2"},
        {"device_serial": "serial-1", "group_name": "g3"},
        {"device_serial": "serial-2", "group_name": "g4"},
        {"device_serial": "serial-2", "group_name": "g5"},
    ]

    planned = plan_batch_schedule(
        items,
        window_start=window_start,
        window_end=window_end,
        settings=_settings(min_gap_seconds=120),
        now=now,
        rng=random.Random(11),
    )

    assert len(planned) == len(items)
    for item in planned:
        assert window_start <= item["scheduled_at"] <= window_end

    for indices in ((0, 1, 2), (3, 4)):
        times = sorted(planned[index]["scheduled_at"] for index in indices)
        for earlier, later in zip(times, times[1:]):
            assert (later - earlier).total_seconds() >= 120 - 1e-6


def test_plan_uses_default_window_when_batch_window_missing() -> None:
    now = datetime(2026, 11, 12, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    planned = plan_batch_schedule(
        [{"device_serial": "serial-1", "group_name": "g1"}],
        window_start=None,
        window_end=None,
        settings=_settings(),
        now=now,
        rng=random.Random(3),
    )

    local = planned[0]["scheduled_at"].astimezone(timezone(timedelta(hours=8)))
    assert local.date() == now.date()
    assert (local.hour, local.minute) >= (9, 0)
    assert local.hour <= 11


def test_plan_respects_item_overrides() -> None:
    now = datetime(2026, 11, 12, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    window_start = now + timedelta(hours=1)
    window_end = now + timedelta(hours=5)
    send_after = now + timedelta(hours=3)
    send_before = now + timedelta(hours=4)

    planned = plan_batch_schedule(
        [
            {
                "device_serial": "serial-1",
                "group_name": "g1",
                "send_after": send_after,
                "send_before": send_before,
            }
        ],
        window_start=window_start,
        window_end=window_end,
        settings=_settings(),
        now=now,
        rng=random.Random(5),
    )

    assert send_after <= planned[0]["scheduled_at"] <= send_before


def test_plan_rejects_elapsed_window() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ScheduleError):
        plan_batch_schedule(
            [{"device_serial": "serial-1", "group_name": "g1"}],
            window_start=now - timedelta(hours=3),
            window_end=now - timedelta(hours=1),
            settings=_settings(),
            now=now,
        )
