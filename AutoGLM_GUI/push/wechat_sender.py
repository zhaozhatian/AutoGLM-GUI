"""Deterministic WeChat group-message sender.

The LLM agent is fine for open-ended navigation but too loose for exact,
repeated sends: it can skip the group search, type a literal "@所有人" instead
of selecting a mention, or report success without verifying the result.

This module drives WeChat with a fixed, verified sequence instead:

    launch -> search group -> open first result -> focus input -> "@"
    -> tap "所有人" -> inject exact text -> tap send -> verify input cleared

All text goes through ADB Keyboard's base64 broadcast, so newlines and
punctuation are preserved byte-for-byte and no quoting is involved.
"""

from __future__ import annotations

import base64
import io
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image

from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.platform_utils import resolve_adb_path

WECHAT_PACKAGE = "com.tencent.mm"
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"


class WeChatSendError(RuntimeError):
    """Raised when the deterministic send sequence cannot be completed."""


class WeChatSendCancelled(WeChatSendError):
    """Raised when the caller cancels between steps."""


Verifier = Callable[[bytes, str], bool]


def _default_verifier(png: bytes, question: str) -> bool:
    # Imported lazily: vision imports WeChatSendError from this module.
    from AutoGLM_GUI.push.vision import ask_yes_no

    return ask_yes_no(png, question)


@dataclass(frozen=True)
class WeChatProfile:
    """Normalized tap targets for a 1080x2400 WeChat layout."""

    search_icon: tuple[float, float] = (830 / 1080, 130 / 2400)
    search_input: tuple[float, float] = (450 / 1080, 150 / 2400)
    first_result: tuple[float, float] = (300 / 1080, 430 / 2400)
    chat_input: tuple[float, float] = (485 / 1080, 2275 / 2400)
    mention_all: tuple[float, float] = (300 / 1080, 940 / 2400)
    send_button: tuple[float, float] = (990 / 1080, 2270 / 2400)

    def point(
        self, ratio: tuple[float, float], size: tuple[int, int]
    ) -> tuple[int, int]:
        width, height = size
        return int(ratio[0] * width), int(ratio[1] * height)


DEFAULT_PROFILE = WeChatProfile()


@dataclass
class AdbRunner:
    adb_path: str
    device_id: str

    def run(
        self,
        *args: str,
        timeout: float = 20.0,
        binary: bool = False,
    ) -> subprocess.CompletedProcess[Any]:
        cmd = [self.adb_path, "-s", self.device_id, *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=not binary,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            raise WeChatSendError(
                f"adb {' '.join(args)} failed ({result.returncode}): {stderr.strip()}"
            )
        return result

    def shell(self, *args: str, timeout: float = 20.0) -> str:
        return self.run("shell", *args, timeout=timeout).stdout or ""

    def screenshot(self) -> bytes:
        return self.run("exec-out", "screencap", "-p", binary=True).stdout


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise WeChatSendCancelled("cancelled by user")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _screen_size(adb: AdbRunner) -> tuple[int, int]:
    output = adb.shell("wm", "size")
    matches = re.findall(r"(\d+)x(\d+)", output)
    if not matches:
        raise WeChatSendError(f"cannot determine screen size: {output.strip()!r}")
    width, height = matches[-1]
    return int(width), int(height)


def _foreground_package(adb: AdbRunner) -> str:
    output = adb.shell("dumpsys", "window")
    match = re.search(r"mCurrentFocus=Window\{[^}]*? ([^/\s]+)/", output)
    return match.group(1) if match else ""


def _wait_foreground(adb: AdbRunner, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _foreground_package(adb) == WECHAT_PACKAGE:
            return
        _sleep(0.4)
    raise WeChatSendError("WeChat did not come to the foreground")


def _wait_input_shown(adb: AdbRunner, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = adb.shell("dumpsys", "input_method")
        if "mInputShown=true" in output or "mIsInputViewShown=true" in output:
            return True
        _sleep(0.3)
    return False


def _current_ime(adb: AdbRunner) -> str:
    return adb.shell("settings", "get", "secure", "default_input_method").strip()


def _set_ime(adb: AdbRunner, ime: str) -> None:
    adb.shell("ime", "enable", ime)
    adb.shell("ime", "set", ime)


def _input_text(adb: AdbRunner, text: str) -> None:
    if text == "":
        return
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    adb.shell(
        "am",
        "broadcast",
        "-a",
        "ADB_INPUT_B64",
        "--es",
        "msg",
        encoded,
    )


def _clear_text(adb: AdbRunner) -> None:
    adb.shell("am", "broadcast", "-a", "ADB_CLEAR_TEXT")


def _tap(adb: AdbRunner, point: tuple[int, int]) -> None:
    adb.shell("input", "tap", str(point[0]), str(point[1]))


def _crop(
    png: bytes, size: tuple[int, int], box: tuple[float, float, float, float]
) -> Image.Image:
    width, height = size
    left, top, right, bottom = box
    image = Image.open(io.BytesIO(png)).convert("RGB")
    return image.crop(
        (
            int(left * width),
            int(top * height),
            int(right * width),
            int(bottom * height),
        )
    )


INPUT_BOX = (0.12, 0.918, 0.80, 0.955)
SEND_BOX = (0.80, 0.900, 1.0, 0.980)


def _input_has_text(png: bytes, size: tuple[int, int]) -> bool:
    image = _crop(png, size, INPUT_BOX)
    try:
        pixels = image.get_flattened_data()
    except AttributeError:  # pragma: no cover - older Pillow
        pixels = image.getdata()
    dark = sum(
        1 for red, green, blue in pixels if red < 110 and green < 110 and blue < 110
    )
    return dark > 300


def _has_send_button(png: bytes, size: tuple[int, int]) -> bool:
    image = _crop(png, size, SEND_BOX)
    try:
        pixels = image.get_flattened_data()
    except AttributeError:  # pragma: no cover - older Pillow
        pixels = image.getdata()
    green = sum(
        1
        for red, green_channel, blue in pixels
        if green_channel > 170 and red < 150 and blue < 170
    )
    return green > 1000


def _open_wechat(adb: AdbRunner) -> None:
    adb.shell("am", "force-stop", WECHAT_PACKAGE)
    _sleep(0.6)
    adb.shell(
        "monkey",
        "-p",
        WECHAT_PACKAGE,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )
    _wait_foreground(adb)
    _sleep(2.5)


def _ensure_chat_list(adb: AdbRunner, verify: Verifier) -> None:
    """Wait for WeChat's main chat list, pressing back only when needed."""
    for _ in range(5):
        _sleep(2.0)
        if _foreground_package(adb) != WECHAT_PACKAGE:
            _open_wechat(adb)
            continue
        if verify(
            adb.screenshot(),
            "Look at this screenshot. Are the four bottom tabs 微信, 通讯录, "
            "发现, 我 visible? That means this is the WeChat main chat list.",
        ):
            return
        adb.shell("input", "keyevent", "4")
        _sleep(1.5)
    raise WeChatSendError("无法回到微信主界面")


def _open_group(
    adb: AdbRunner,
    profile: WeChatProfile,
    size: tuple[int, int],
    group: str,
    verify: Verifier,
) -> None:
    _tap(adb, profile.point(profile.search_icon, size))
    _sleep(1.5)
    _tap(adb, profile.point(profile.search_input, size))
    _sleep(0.8)
    _input_text(adb, group)
    _sleep(2.2)
    _tap(adb, profile.point(profile.first_result, size))
    _sleep(2.2)
    _wait_foreground(adb)
    if not verify(
        adb.screenshot(),
        "Look at the top center title area of this WeChat chat page. Does it "
        f"contain the text: {group} ?",
    ):
        raise WeChatSendError(f"进入的群聊标题与目标不一致，期望「{group}」")


def send_group_message(
    *,
    device_id: str,
    group_name: str,
    message: str,
    mention_all: bool = True,
    adb_path: str | None = None,
    profile: WeChatProfile = DEFAULT_PROFILE,
    cancel_check: Callable[[], bool] | None = None,
    verifier: Verifier | None = None,
) -> dict[str, Any]:
    """Send ``message`` to the WeChat group ``group_name`` on one device.

    Raises WeChatSendError on any unverifiable step; never reports success
    unless the input box is empty after tapping send.
    """
    if not group_name.strip():
        raise WeChatSendError("group_name is empty")
    if not message.strip():
        raise WeChatSendError("message is empty")

    verify = verifier or _default_verifier
    adb = AdbRunner(adb_path or resolve_adb_path(), device_id)
    size = _screen_size(adb)
    original_ime = _current_ime(adb)
    steps: list[str] = []

    try:
        _check_cancel(cancel_check)
        adb.shell("wm", "dismiss-keyguard")
        _open_wechat(adb)
        _ensure_chat_list(adb, verify)
        steps.append("wechat_ready")

        _set_ime(adb, ADB_KEYBOARD_IME)
        _open_group(adb, profile, size, group_name, verify)
        steps.append("group_opened")

        _tap(adb, profile.point(profile.chat_input, size))
        if not _wait_input_shown(adb):
            raise WeChatSendError(f"无法进入群聊输入框，可能没有找到群「{group_name}」")
        # WeChat keeps per-chat drafts; an interrupted run may have left text.
        cleared = False
        for _ in range(2):
            _clear_text(adb)
            _sleep(0.6)
            if not _input_has_text(adb.screenshot(), size):
                cleared = True
                break
        if not cleared:
            raise WeChatSendError("无法清空输入框中的历史草稿")
        steps.append("input_focused")

        if mention_all:
            selected = False
            for attempt in range(2):
                _check_cancel(cancel_check)
                if attempt > 0:
                    adb.shell("input", "keyevent", "4")
                    _sleep(0.4)
                    _clear_text(adb)
                    _sleep(0.4)
                    _tap(adb, profile.point(profile.chat_input, size))
                    _sleep(0.4)
                _input_text(adb, "@")
                _sleep(1.6)
                _tap(adb, profile.point(profile.mention_all, size))
                _sleep(1.4)
                if verify(
                    adb.screenshot(),
                    "Look at the message input box at the bottom of this WeChat "
                    "chat page. Does it contain a selected mention of 所有人, "
                    "shown as @所有人 ?",
                ):
                    selected = True
                    break
                logger.warning(
                    "Mention chip not visible after selecting 所有人 (attempt %s)",
                    attempt + 1,
                )
            if not selected:
                raise WeChatSendError(
                    "无法选中『所有人』：可能没有群管理权限，或界面未出现提及列表"
                )
            steps.append("mention_selected")

        _check_cancel(cancel_check)
        _input_text(adb, message)
        _sleep(1.4)
        composed = adb.screenshot()
        if not _input_has_text(composed, size):
            raise WeChatSendError("消息正文未进入输入框")
        if not _has_send_button(composed, size):
            raise WeChatSendError("未找到发送按钮")
        steps.append("message_typed")

        sent = False
        for attempt in range(3):
            _check_cancel(cancel_check)
            _tap(adb, profile.point(profile.send_button, size))
            _sleep(1.2)
            after = adb.screenshot()
            if not _input_has_text(after, size):
                sent = True
                break
            logger.warning("Send tap did not clear input (attempt %s)", attempt + 1)
        if not sent:
            raise WeChatSendError("点击发送后输入框未清空，消息可能未发出")
        steps.append("sent")

        return {
            "device_id": device_id,
            "group_name": group_name,
            "mention_all": mention_all,
            "message_length": len(message),
            "steps": steps,
        }
    finally:
        try:
            if original_ime and original_ime != "null":
                adb.shell("ime", "set", original_ime)
        except Exception:
            logger.warning("Failed to restore IME %s on %s", original_ime, device_id)
