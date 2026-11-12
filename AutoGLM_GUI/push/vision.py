"""Minimal vision text extraction used by the deterministic WeChat sender."""

from __future__ import annotations

import base64

from openai import OpenAI

from AutoGLM_GUI.config_manager import config_manager
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.push.wechat_sender import WeChatSendError


def ask_text(png: bytes, question: str) -> str:
    """Ask the configured multimodal model to read text from a cropped image."""
    config = config_manager.get_effective_config()
    if not config.base_url:
        raise WeChatSendError("model base_url is not configured; cannot verify screen")

    image_b64 = base64.b64encode(png).decode("ascii")
    prompt = (
        f"{question}\n"
        "Return only the text, exactly as shown. Do not explain, translate or "
        "add quotes around the whole answer."
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
        max_tokens=300,
    )
    content = (response.choices[0].message.content or "").strip()
    if content == "":
        logger.warning("Vision text extraction returned an empty answer")
        raise WeChatSendError("vision check returned an empty answer")
    return content
