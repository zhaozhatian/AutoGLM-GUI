"""Minimal vision checks used by the deterministic WeChat sender."""

from __future__ import annotations

import base64
import re

from openai import OpenAI

from AutoGLM_GUI.config_manager import config_manager
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.push.wechat_sender import WeChatSendError

_YES_NO = re.compile(r"\b(YES|NO)\b")


def ask_yes_no(png: bytes, question: str) -> bool:
    """Ask the configured multimodal model a strict yes/no question."""
    config = config_manager.get_effective_config()
    if not config.base_url:
        raise WeChatSendError("model base_url is not configured; cannot verify screen")

    image_b64 = base64.b64encode(png).decode("ascii")
    prompt = (
        f"{question}\n"
        "Reply with YES or NO first, then a very short reason. "
        "If you cannot read the screen clearly, answer NO."
    )
    client = OpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=60,
    )
    response = client.chat.completions.create(
        model=config.model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        temperature=0,
        max_tokens=120,
    )
    content = (response.choices[0].message.content or "").strip()
    match = _YES_NO.search(content.upper())
    if match is None:
        logger.warning("Unparseable vision answer: %r", content[:200])
        raise WeChatSendError(
            f"vision check returned an unusable answer: {content[:120]}"
        )
    return match.group(1) == "YES"
