from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image

from AutoGLM_GUI.actions.async_handler import AsyncActionHandler
from AutoGLM_GUI.adb_plus.keyboard_installer import _run_coro_sync
from AutoGLM_GUI.agents.glm.parser import GLMParser
from AutoGLM_GUI.model.grid_overlay import add_grid_overlay
from AutoGLM_GUI.model.message_builder import MessageBuilder


class TestAnswerWrapperParsing:
    def test_swipe_with_answer_wrapper(self) -> None:
        action = GLMParser().parse(
            'do(action="Swipe", start=[200, 500], end=[800, 500])</answer>'
        )
        assert action == {
            "_metadata": "do",
            "action": "Swipe",
            "start": [200, 500],
            "end": [800, 500],
        }

    def test_tap_with_answer_wrapper(self) -> None:
        action = GLMParser().parse(
            'do(action="Tap", element=[377, 340])</answer>'
        )
        assert action["element"] == [377, 340]

    def test_finish_with_answer_wrapper(self) -> None:
        action = GLMParser().parse('finish(message="done")</answer>')
        assert action == {"_metadata": "finish", "message": "done"}


class FakeDevice:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, ...], dict[str, int]]] = []

    async def swipe(self, *args: int, **kwargs: int) -> None:
        self.calls.append((args, kwargs))


class TestCoordinateConversion:
    def test_max_coordinate_maps_inside_screen(self) -> None:
        handler = AsyncActionHandler(FakeDevice())
        assert handler._convert_relative_to_absolute([999, 999], 1080, 2400) == (
            1078,
            2397,
        )

    def test_invalid_coordinates_raise_clear_error(self) -> None:
        handler = AsyncActionHandler(FakeDevice())
        with pytest.raises(ValueError, match="Invalid coordinates"):
            handler._convert_relative_to_absolute(
                "[800, 500])</answer", 1080, 2400  # type: ignore[arg-type]
            )

    def test_short_horizontal_swipe_is_expanded(self) -> None:
        sx, sy, ex, ey = AsyncActionHandler._normalize_swipe(
            500, 1200, 540, 1200, 1080, 2400
        )
        assert abs(ex - sx) >= int(1080 * 0.28)
        assert (sy, ey) == (1200, 1200)

    def test_swipe_executes_with_duration(self) -> None:
        device = FakeDevice()
        handler = AsyncActionHandler(device)
        asyncio.run(
            handler.execute(
                {
                    "_metadata": "do",
                    "action": "Swipe",
                    "start": [800, 500],
                    "end": [200, 500],
                },
                1080,
                2400,
            )
        )
        assert device.calls
        _, kwargs = device.calls[0]
        assert 250 <= kwargs["duration_ms"] <= 600


class TestScreenInfo:
    def test_includes_screen_size(self) -> None:
        text = MessageBuilder.build_screen_info("com.tencent.mm", 1080, 2400)
        assert "Screen Size: 1080x2400" in text
        assert "0-999" in text

    def test_optional_size(self) -> None:
        text = MessageBuilder.build_screen_info("com.tencent.mm")
        assert "Screen Size" not in text


class TestGridOverlay:
    def test_returns_valid_png(self) -> None:
        image = Image.new("RGB", (108, 240), (255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        result = add_grid_overlay(
            base64.b64encode(buffer.getvalue()).decode("ascii"), 108, 240
        )
        decoded = Image.open(io.BytesIO(base64.b64decode(result)))
        assert decoded.size == (108, 240)
        assert decoded.format == "PNG"


class TestRunCoroSync:
    def test_outside_running_loop(self) -> None:
        async def work() -> int:
            return 7

        assert _run_coro_sync(work()) == 7

    def test_inside_running_loop(self) -> None:
        async def main() -> int:
            async def work() -> int:
                return 42

            return _run_coro_sync(work())

        assert asyncio.run(main()) == 42