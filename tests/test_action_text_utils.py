"""Tests for normalization of model-produced action text."""

import asyncio
from typing import Any

import pytest

import AutoGLM_GUI.actions.async_handler as async_handler_module
import AutoGLM_GUI.actions.handler as handler_module
from AutoGLM_GUI.actions import ActionHandler, AsyncActionHandler
from AutoGLM_GUI.actions.text_utils import strip_wrapping_quotes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"hello"', "hello"),
        ("'hello'", "hello"),
        ("hello", "hello"),
        ('"hello', '"hello'),
        ('hello"', 'hello"'),
        ('"say "hi""', 'say "hi"'),
        ('""', ""),
        ('"', '"'),
        ("", ""),
    ],
)
def test_strip_wrapping_quotes(raw: str, expected: str) -> None:
    assert strip_wrapping_quotes(raw) == expected


class _RecordingDevice:
    """Minimal sync device recording type_text calls."""

    def __init__(self) -> None:
        self.typed: list[str] = []
        self.default_ime = "com.example.ime/.ImeService"

    def detect_and_set_adb_keyboard(self) -> str:
        return self.default_ime

    def clear_text(self) -> None:
        pass

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def restore_keyboard(self, ime: str) -> None:
        pass


def test_type_action_strips_wrapping_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "trace_sleep", lambda *a, **k: None)
    device = _RecordingDevice()
    handler = ActionHandler(device)  # type: ignore[arg-type]

    handler._handle_type(  # type: ignore[attr-defined]
        {"_metadata": "do", "action": "Type", "text": '"你好"'},
        0,
        0,
    )

    assert device.typed == ["你好"]


class _AsyncRecordingDevice:
    """Minimal async device recording type_text calls."""

    def __init__(self) -> None:
        self.typed: list[str] = []
        self.default_ime = "com.example.ime/.ImeService"

    async def detect_and_set_adb_keyboard(self) -> str:
        return self.default_ime

    async def clear_text(self) -> None:
        pass

    async def type_text(self, text: str) -> None:
        self.typed.append(text)

    async def restore_keyboard(self, ime: str) -> None:
        pass


def test_async_type_action_strips_wrapping_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(async_handler_module, "trace_sleep_async", _noop)
    device = _AsyncRecordingDevice()
    handler = AsyncActionHandler(device)  # type: ignore[arg-type]

    asyncio.run(
        handler._handle_type(  # type: ignore[attr-defined]
            {"_metadata": "do", "action": "Type", "text": '"你好"'},
            0,
            0,
        )
    )

    assert device.typed == ["你好"]
