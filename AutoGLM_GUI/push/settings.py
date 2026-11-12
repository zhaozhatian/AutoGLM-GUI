"""Persistent push rules and defaults."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_window_start": "09:00",
    "default_window_end": "11:00",
    "min_gap_seconds": 90,
    "offline_retry_seconds": 60,
    "max_items_per_batch": 500,
    "default_callback_url": None,
    # serial -> {"enabled": bool, "allowed_groups": [str], "window_start": "HH:MM"|None,
    #            "window_end": "HH:MM"|None, "min_gap_seconds": int|None}
    "device_rules": {},
}


def _validate_hhmm(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HHMM.match(value.strip()):
        raise ValueError(f"{field} must be HH:MM (24h)")
    return value.strip()


def _validate_int(value: Any, field: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


class PushSettingsManager:
    """JSON-backed settings with atomic writes and mtime refresh."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = (
            Path(path)
            if path is not None
            else Path.home() / ".config" / "autoglm" / "push_settings.json"
        )
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return copy.deepcopy(self._cache or DEFAULT_SETTINGS)

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get_settings()
            merged = _merge_and_validate(current, patch)
            self._write(merged)
            self._cache = merged
            self._mtime = self._path.stat().st_mtime
            return copy.deepcopy(merged)

    def get_device_rule(self, serial: str) -> dict[str, Any]:
        settings = self.get_settings()
        rule = settings.get("device_rules", {}).get(serial, {})
        return {
            "enabled": bool(rule.get("enabled", True)),
            "allowed_groups": list(rule.get("allowed_groups", [])),
            "window_start": rule.get("window_start"),
            "window_end": rule.get("window_end"),
            "min_gap_seconds": rule.get("min_gap_seconds"),
        }

    def is_device_enabled(self, serial: str) -> bool:
        return self.get_device_rule(serial)["enabled"]

    def is_group_allowed(self, serial: str, group_name: str) -> bool:
        allowed = self.get_device_rule(serial)["allowed_groups"]
        if not allowed:
            return True
        return group_name.strip() in allowed

    def _refresh(self) -> None:
        if not self._path.exists():
            if self._cache is None:
                self._cache = copy.deepcopy(DEFAULT_SETTINGS)
                self._mtime = None
            return
        mtime = self._path.stat().st_mtime
        if self._cache is not None and self._mtime == mtime:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self._cache = _merge_and_validate(copy.deepcopy(DEFAULT_SETTINGS), raw)
        self._mtime = mtime

    def _write(self, settings: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp_path, self._path)


def _merge_and_validate(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)

    for key in ("default_window_start", "default_window_end"):
        if key in patch and patch[key] is not None:
            merged[key] = _validate_hhmm(patch[key], key)

    if "min_gap_seconds" in patch and patch["min_gap_seconds"] is not None:
        merged["min_gap_seconds"] = _validate_int(
            patch["min_gap_seconds"], "min_gap_seconds", minimum=0
        )
    if "offline_retry_seconds" in patch and patch["offline_retry_seconds"] is not None:
        merged["offline_retry_seconds"] = _validate_int(
            patch["offline_retry_seconds"], "offline_retry_seconds", minimum=5
        )
    if "max_items_per_batch" in patch and patch["max_items_per_batch"] is not None:
        merged["max_items_per_batch"] = _validate_int(
            patch["max_items_per_batch"], "max_items_per_batch", minimum=1
        )
    if "default_callback_url" in patch:
        value = patch["default_callback_url"]
        if value in (None, ""):
            merged["default_callback_url"] = None
        elif isinstance(value, str):
            merged["default_callback_url"] = value.strip()
        else:
            raise ValueError("default_callback_url must be a string or null")

    if "device_rules" in patch:
        if not isinstance(patch["device_rules"], dict):
            raise ValueError("device_rules must be an object keyed by device serial")
        rules: dict[str, Any] = {}
        for serial, rule in patch["device_rules"].items():
            if not isinstance(serial, str) or not serial.strip():
                raise ValueError("device_rules keys must be non-empty serials")
            if not isinstance(rule, dict):
                raise ValueError(f"device rule for {serial} must be an object")
            normalized: dict[str, Any] = {
                "enabled": bool(rule.get("enabled", True)),
                "allowed_groups": _validate_groups(rule.get("allowed_groups", [])),
            }
            if rule.get("window_start") is not None:
                normalized["window_start"] = _validate_hhmm(
                    rule["window_start"], f"device_rules.{serial}.window_start"
                )
            if rule.get("window_end") is not None:
                normalized["window_end"] = _validate_hhmm(
                    rule["window_end"], f"device_rules.{serial}.window_end"
                )
            if rule.get("min_gap_seconds") is not None:
                normalized["min_gap_seconds"] = _validate_int(
                    rule["min_gap_seconds"],
                    f"device_rules.{serial}.min_gap_seconds",
                    minimum=0,
                )
            rules[serial.strip()] = normalized
        merged["device_rules"] = rules

    return merged


def _validate_groups(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("allowed_groups must be a list of group names")
    groups: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("allowed_groups entries must be strings")
        name = raw.strip()
        if not name or name in seen:
            continue
        groups.append(name)
        seen.add(name)
    return groups


push_settings = PushSettingsManager()
