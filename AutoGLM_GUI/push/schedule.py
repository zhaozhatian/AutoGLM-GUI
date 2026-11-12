"""Time-window resolution and discrete scheduling for push items."""

from __future__ import annotations

import os
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo


class ScheduleError(ValueError):
    """Raised when a push item has no usable send window."""


def get_push_timezone() -> tzinfo:
    """Return the configured push timezone (defaults to the host timezone)."""
    name = os.environ.get("AUTOGLM_PUSH_TIMEZONE") or os.environ.get(
        "AUTOGLM_SCHEDULER_TZ"
    )
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # pragma: no cover - invalid env value
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def ensure_aware(value: datetime, tz: tzinfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def _day_window(
    settings: dict[str, Any], rule: dict[str, Any], now: datetime
) -> tuple[datetime, datetime]:
    tz = get_push_timezone()
    local_now = now.astimezone(tz)
    start_text = rule.get("window_start") or settings["default_window_start"]
    end_text = rule.get("window_end") or settings["default_window_end"]
    start_time = time.fromisoformat(start_text)
    end_time = time.fromisoformat(end_text)

    start = datetime.combine(local_now.date(), start_time, tzinfo=tz)
    end = datetime.combine(local_now.date(), end_time, tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    if end <= now:
        start += timedelta(days=1)
        end += timedelta(days=1)
    return start, end


def spread_times(
    start: datetime,
    end: datetime,
    count: int,
    min_gap_seconds: float,
    rng: random.Random | None = None,
) -> list[datetime]:
    """Spread ``count`` timestamps across [start, end] with a minimum gap.

    When the window is too small for the requested gap the times are laid out
    evenly instead, so a batch never fails just because it is dense.
    """
    if count <= 0:
        return []
    total = (end - start).total_seconds()
    if total <= 0:
        return [start for _ in range(count)]

    gap = max(0.0, float(min_gap_seconds))
    if count > 1 and (count - 1) * gap >= total:
        step = total / count
        return [start + timedelta(seconds=index * step) for index in range(count)]

    generator = rng or random.Random()
    slack = total - (count - 1) * gap
    # Allocate the free time to the gaps before, between and after items so the
    # last item is not pinned to the window end.
    segments = [generator.random() for _ in range(count + 1)]
    segment_sum = sum(segments) or 1.0

    times: list[datetime] = []
    cursor = 0.0
    for index in range(count):
        cursor += slack * segments[index] / segment_sum
        times.append(start + timedelta(seconds=cursor))
        cursor += gap
    return times


def plan_batch_schedule(
    items: list[dict[str, Any]],
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    settings: dict[str, Any],
    now: datetime,
    rng: random.Random | None = None,
) -> list[dict[str, datetime]]:
    """Assign a scheduled time and hard deadline to every item.

    Items are grouped by device; each device's items are spread inside the
    effective window (batch window > device rule > global default), with the
    device's minimum gap applied.
    """
    tz = get_push_timezone()
    now_utc = ensure_aware(now, tz).astimezone(timezone.utc)
    rules = settings.get("device_rules", {})
    grouped: dict[str, list[tuple[int, datetime, datetime]]] = defaultdict(list)
    deadlines: dict[int, datetime] = {}

    for index, item in enumerate(items):
        serial = str(item["device_serial"])
        rule = rules.get(serial, {})
        if window_start is not None and window_end is not None:
            base_start = ensure_aware(window_start, tz)
            base_end = ensure_aware(window_end, tz)
        else:
            base_start, base_end = _day_window(settings, rule, now_utc)

        start = max(base_start, now_utc)
        end = base_end
        send_after = item.get("send_after")
        send_before = item.get("send_before")
        if isinstance(send_after, datetime):
            start = max(start, ensure_aware(send_after, tz))
        if isinstance(send_before, datetime):
            end = min(end, ensure_aware(send_before, tz))

        if end <= start:
            raise ScheduleError(
                f"item {index} ({serial} / {item.get('group_name')}) has no "
                "remaining send window"
            )
        grouped[serial].append((index, start, end))
        deadlines[index] = end

    planned: list[dict[str, datetime]] = [{} for _ in items]
    for serial, entries in grouped.items():
        rule = rules.get(serial, {})
        gap = rule.get("min_gap_seconds")
        if gap is None:
            gap = settings["min_gap_seconds"]

        starts = {start for _, start, _ in entries}
        ends = {end for _, _, end in entries}
        if len(starts) == 1 and len(ends) == 1:
            times = spread_times(
                next(iter(starts)), next(iter(ends)), len(entries), gap, rng
            )
            for (index, _, _), when in zip(entries, times):
                planned[index] = {
                    "scheduled_at": when.astimezone(timezone.utc),
                    "window_end": deadlines[index].astimezone(timezone.utc),
                }
            continue

        generator = rng or random.Random()
        sampled = [
            start
            + timedelta(seconds=generator.random() * (end - start).total_seconds())
            for _, start, end in entries
        ]
        order = sorted(range(len(entries)), key=lambda pos: sampled[pos])
        previous: datetime | None = None
        for pos in order:
            index, _, end = entries[pos]
            when = sampled[pos]
            if previous is not None and when < previous + timedelta(seconds=gap):
                when = min(previous + timedelta(seconds=gap), end)
            planned[index] = {
                "scheduled_at": when.astimezone(timezone.utc),
                "window_end": deadlines[index].astimezone(timezone.utc),
            }
            previous = when

    return planned


def local_day_bounds(day: date, settings: dict[str, Any]) -> tuple[datetime, datetime]:
    """Convenience helper used by the API for default-window reporting."""
    tz = get_push_timezone()
    start = datetime.combine(
        day, time.fromisoformat(settings["default_window_start"]), tzinfo=tz
    )
    end = datetime.combine(
        day, time.fromisoformat(settings["default_window_end"]), tzinfo=tz
    )
    if end <= start:
        end += timedelta(days=1)
    return start, end
