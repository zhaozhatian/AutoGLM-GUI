"""Tests for the deterministic push executor and pixel helpers."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

import AutoGLM_GUI.push.wechat_sender as sender_module
from AutoGLM_GUI.push.wechat_sender import (
    DEFAULT_PROFILE,
    WeChatSendError,
    _has_send_button,
    _input_has_text,
)
from AutoGLM_GUI.task_manager import TaskManager
from AutoGLM_GUI.task_store import TaskStatus, TaskStore


def _png(draw_callback) -> bytes:
    image = Image.new("RGB", (1080, 2400), (245, 245, 245))
    draw_callback(ImageDraw.Draw(image))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_profile_scales_to_screen() -> None:
    assert DEFAULT_PROFILE.point(DEFAULT_PROFILE.search_icon, (1080, 2400)) == (
        830,
        130,
    )
    assert DEFAULT_PROFILE.point(DEFAULT_PROFILE.send_button, (2160, 4800)) == (
        1980,
        4540,
    )


def test_input_text_detection() -> None:
    empty = _png(lambda draw: None)
    with_text = _png(
        lambda draw: draw.rectangle((180, 2210, 700, 2260), fill=(15, 23, 10))
    )
    assert _input_has_text(empty, (1080, 2400)) is False
    assert _input_has_text(with_text, (1080, 2400)) is True


def test_send_button_detection() -> None:
    without = _png(lambda draw: None)
    with_button = _png(
        lambda draw: draw.rounded_rectangle(
            (900, 2200, 1060, 2330), radius=20, fill=(7, 193, 96)
        )
    )
    assert _has_send_button(without, (1080, 2400)) is False
    assert _has_send_button(with_button, (1080, 2400)) is True


def _create_push_task(store: TaskStore) -> dict:
    return store.create_task_run(
        source="push",
        executor_key="push_deterministic",
        device_id="192.168.2.98:42851",
        device_serial="56fe42860624",
        input_text="prompt",
        push_payload={
            "group_name": "26.11.12小快灵测试一群",
            "message": "第一行\n第二行",
            "mention_all": True,
        },
    )


def test_push_executor_marks_success(tmp_path: Path, monkeypatch) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    manager = TaskManager(store)
    calls: dict[str, object] = {}

    def fake_send(**kwargs):
        calls.update(kwargs)
        return {"steps": ["wechat_ready", "sent"]}

    monkeypatch.setattr(sender_module, "send_group_message", fake_send)
    task = _create_push_task(store)

    asyncio.run(manager._execute_push_deterministic(task))

    updated = store.get_task(task["id"])
    assert updated is not None
    assert updated["status"] == TaskStatus.SUCCEEDED.value
    assert updated["step_count"] == 2
    assert calls["group_name"] == "26.11.12小快灵测试一群"
    assert calls["message"] == "第一行\n第二行"
    assert calls["mention_all"] is True


def test_push_executor_marks_failure(tmp_path: Path, monkeypatch) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    manager = TaskManager(store)

    def fake_send(**kwargs):
        raise WeChatSendError("无法选中『所有人』")

    monkeypatch.setattr(sender_module, "send_group_message", fake_send)
    task = _create_push_task(store)

    asyncio.run(manager._execute_push_deterministic(task))

    updated = store.get_task(task["id"])
    assert updated is not None
    assert updated["status"] == TaskStatus.FAILED.value
    assert "所有人" in (updated["error_message"] or "")
